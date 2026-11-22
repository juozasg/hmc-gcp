"""Remove cast sun shadows from an orthophoto, using the DSM and the sun position.

1. Shadows are traced through the DSM toward the sun: where an obstacle rises more than
   `tolerance` above the line to the sun. These are only candidates: the DSM is coarser than
   the photo and is unreliable over water.
2. Calibration from the image itself: sand just inside the predicted shadows is compared
   with sand just outside (bands a few decimetres wide, away from the uncertain edge). Per
   channel, the brightness distribution inside is mapped onto the one outside (quantile
   mapping), so no camera tone curve has to be assumed. Sky-lit shadow is also bluer.
3. Within a margin around the predicted shadows, each pixel is compared with the sunlit sand
   nearby: it counts as shadow to the degree that it is both darker and bluer in the way the
   calibration says shadow is. Dark minerals and wet sand are darker but not bluer, so they
   are left alone.
4. Shadow pixels are mapped to their sunlit brightness, blended by that degree.

Slope shading (sunny vs shaded sides of dunes) is not corrected.
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from pyproj import Transformer

from . import geotiff
from .geotiff import TileReader

LOW, HIGH = 3, 250


# ------------------------------------------------------------------ sun position
def sun_position(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Sun azimuth (degrees clockwise from north) and elevation (degrees, with standard
    refraction) at a UTC time, by the NOAA solar position equations (~0.1 deg)."""
    when = when.astimezone(timezone.utc)
    jd = when.timestamp() / 86400.0 + 2440587.5
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + 0.0003032 * T)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * T) + math.sin(3 * Mr) * 0.000289)
    true_long = L0 + C
    omega = 125.04 - 1934.136 * T
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam)))
    y = math.tan(math.radians(eps) / 2) ** 2
    L0r = math.radians(L0)
    eqt = 4 * math.degrees(y * math.sin(2 * L0r) - 2 * e * math.sin(Mr)
                           + 4 * e * y * math.sin(Mr) * math.cos(2 * L0r)
                           - 0.5 * y * y * math.sin(4 * L0r) - 1.25 * e * e * math.sin(2 * Mr))
    minutes = when.hour * 60 + when.minute + when.second / 60 + when.microsecond / 6e7
    tst = (minutes + eqt + 4 * lon) % 1440
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1.0, min(1.0, cz)))
    el = 90 - math.degrees(zen)
    # atmospheric refraction (NOAA)
    if el > 85:
        ref = 0.0
    elif el > 5:
        te = math.tan(math.radians(el))
        ref = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
    elif el > -0.575:
        ref = 1735 + el * (-518.2 + el * (103.4 + el * (-12.79 + el * 0.711)))
    else:
        ref = -20.774 / math.tan(math.radians(el))
    el += ref / 3600
    az = math.degrees(math.atan2(math.sin(ha),
                                 math.cos(ha) * math.sin(la) - math.tan(decl) * math.cos(la)))
    return (az + 180) % 360, el


def photo_time(folder: Path, utc_offset: float) -> tuple[datetime, datetime, datetime]:
    """Median, first and last capture time (UTC) of the JPG photos in a folder; the camera
    clock is taken to be local time utc_offset hours ahead of UTC."""
    from .camera import read_shot
    ts = []
    for p in sorted(Path(folder).iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg"):
            t = read_shot(p).time
            if t:
                ts.append(t)
    if not ts:
        raise ValueError(f"no capture times in the photos in {folder}")
    tz = timezone(timedelta(hours=utc_offset))

    def conv(t):  # read_shot gives the naive camera time as a local timestamp
        return datetime.fromtimestamp(t).replace(tzinfo=tz).astimezone(timezone.utc)
    return conv(statistics.median(ts)), conv(min(ts)), conv(max(ts))


# ------------------------------------------------------------------ DSM lighting
def _fill(z: np.ndarray, valid: np.ndarray, sigma: float) -> np.ndarray:
    num = cv2.GaussianBlur(np.where(valid, z, 0).astype(np.float32), (0, 0), sigma)
    den = cv2.GaussianBlur(valid.astype(np.float32), (0, 0), sigma)
    out = z.copy()
    gap = ~valid
    out[gap] = num[gap] / np.maximum(den[gap], 1e-6)
    out[gap & (den < 1e-3)] = np.nanmedian(z[valid])
    return out


def cast_shadow(z: np.ndarray, valid: np.ndarray, res: float, az: float, el: float,
                tol: float) -> np.ndarray:
    """1 where the sun is hidden by the DSM: some cell toward the sun rises more than
    tol above the line from this cell to the sun."""
    h, w = z.shape
    dc, dr = math.sin(math.radians(az)), -math.cos(math.radians(az))  # toward the sun
    # rotation that maps the sun direction (dc, dr) to +x
    R = np.array([[dc, dr], [-dr, dc]])
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], float) @ R.T
    lo = corners.min(0)
    W, H = (np.ceil(corners.max(0) - lo) + 2).astype(int)
    A = np.c_[R, -lo + 1]
    zz = np.where(valid, z, -1e4).astype(np.float32)
    rz = cv2.warpAffine(zz, A, (int(W), int(H)), flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=-1e4)
    g = rz - np.arange(W, dtype=np.float32)[None, :] * np.float32(res * math.tan(math.radians(el)))
    rev = np.maximum.accumulate(g[:, ::-1], axis=1)[:, ::-1]
    m = np.full_like(g, -np.inf)
    m[:, :-1] = rev[:, 1:]
    sh = ((m - g) > tol).astype(np.float32)
    sh[rz < -1e3] = 0
    back = cv2.warpAffine(sh, A, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return np.clip(back, 0, 1)


# ------------------------------------------------------------------ run
def _step(x, lo, hi):
    """Smooth 0 -> 1 between lo and hi."""
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def _srgb_to_lin(v):
    v = np.asarray(v, np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return 255.0 * np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def run(ortho: Path, dsm: Path, out: Path, when: datetime | None = None,
        sun: tuple[float, float] | None = None, tol: float = 0.05, search: float = 0.3,
        band: float = 0.3, mask_out: Path | None = None):
    t0 = time.time()
    tg = TileReader(ortho)
    g = tg.grid
    zarr, dg, nodata = geotiff.read(dsm)
    z = zarr.astype(np.float32)
    if z.ndim == 3:
        z = z[..., 0]
    valid = np.isfinite(z) & (z > -1000)
    if nodata is not None:
        valid &= z != nodata
    if g.epsg and dg.epsg and g.epsg != dg.epsg:
        raise ValueError(f"orthophoto EPSG:{g.epsg} and DSM EPSG:{dg.epsg} differ")
    if sun is None:
        cx, cy = g.x0 + g.width * g.res / 2, g.y0 - g.height * g.ry / 2
        lon, lat = Transformer.from_crs(g.epsg or 32634, 4326, always_xy=True).transform(cx, cy)
        az, el = sun_position(when, lat, lon)
        print(f"Sun at {when:%Y-%m-%d %H:%M} UTC, {lat:.4f} N {lon:.4f} E: azimuth {az:.1f} deg, "
              f"elevation {el:.1f} deg (shadows {1 / math.tan(math.radians(el)):.2f} x height, "
              f"pointing to {(az + 180) % 360:.0f} deg)")
    else:
        az, el = sun
        print(f"Sun azimuth {az:.1f} deg, elevation {el:.1f} deg (given)")
    if el <= 1:
        raise ValueError("sun too low (or below the horizon) for this correction")

    # 1. predicted shadows and the zones around them, on the DSM grid
    zf = _fill(z, valid, 3.0)
    hard = (cast_shadow(zf, valid, dg.res, az, el, tol) > 0.5) & valid
    din = cv2.distanceTransform(hard.astype(np.uint8), cv2.DIST_L2, 5) * dg.res
    dout = cv2.distanceTransform((~hard).astype(np.uint8), cv2.DIST_L2, 5) * dg.res
    skip = max(0.08, 2 * dg.res)  # stay clear of the uncertain edge
    label = np.zeros(z.shape, np.uint8)
    label[valid & (dout < search)] |= 1                      # candidate zone (incl. shadow)
    label[hard & (din > skip) & (din < skip + band)] |= 2    # inside band
    label[valid & ~hard & (dout > skip) & (dout < skip + band)] |= 4  # outside band
    print(f"DSM {dg.width} x {dg.height} at {dg.res * 100:.0f} cm: "
          f"{hard[valid].mean():.1%} predicted in cast shadow")

    def sample(r0, c0, th, tw):
        """DSM-grid label at the centres of a tile's pixels (nearest)."""
        cols = np.floor((g.x0 + (c0 + np.arange(tw) + 0.5) * g.res - dg.x0) / dg.res).astype(int)
        rows = np.floor((dg.y0 - (g.y0 - (r0 + np.arange(th) + 0.5) * g.ry)) / dg.ry).astype(int)
        okc = (cols >= 0) & (cols < dg.width)
        okr = (rows >= 0) & (rows < dg.height)
        lab = label[np.clip(rows, 0, dg.height - 1)][:, np.clip(cols, 0, dg.width - 1)]
        lab[~okr, :] = 0
        lab[:, ~okc] = 0
        return lab

    # 2. pass 1: inside/outside histograms, and mean sunlit colour per 0.5 m cell
    lut = _srgb_to_lin(np.arange(256)).astype(np.float32)
    hin = np.zeros((3, 256), np.int64)
    hout = np.zeros((3, 256), np.int64)
    cell = 0.5
    gw = int(math.ceil(g.width * g.res / cell))
    gh = int(math.ceil(g.height * g.ry / cell))
    lsum = np.zeros((3, gh * gw))
    lcnt = np.zeros(gh * gw)
    brbins = np.linspace(-3, 3, 601)
    hbr = np.zeros(600)
    for r0, c0, t in tg.tiles():
        th, tw = t.shape[:2]
        sub = t[::2, ::2]
        rgb = sub[..., :3]
        ok = np.all((rgb >= LOW) & (rgb <= HIGH), axis=2)
        if t.shape[2] > 3:
            ok &= sub[..., 3] > 0
        if not ok.any():
            continue
        lab = sample(r0, c0, th, tw)[::2, ::2]
        for bit, hist in ((2, hin), (4, hout)):
            m = ok & ((lab & bit) > 0)
            if m.any():
                for k in range(3):
                    hist[k] += np.bincount(rgb[..., k][m], minlength=256)
        m = ok & ((lab & 4) > 0)
        if m.any():
            br = np.log(lut[rgb[..., 2][m]] / np.maximum(lut[rgb[..., 0][m]], 1e-4))
            hbr += np.histogram(br, bins=brbins)[0]
    n_in, n_out = hin[0].sum(), hout[0].sum()
    if n_in < 5000 or n_out < 5000:
        raise ValueError(f"too few pixels along predicted shadow edges ({n_in} inside, "
                         f"{n_out} outside): check the sun position and --tolerance")
    # quantile mapping inside -> outside, per channel (8-bit, monotone)
    q = np.linspace(0.005, 0.995, 199)
    maps = np.zeros((3, 256), np.float32)
    kk = np.zeros(3)
    for k in range(3):
        ci, co = np.cumsum(hin[k]) / n_in, np.cumsum(hout[k]) / n_out
        qi = np.searchsorted(ci, q).astype(float)
        qo = np.searchsorted(co, q).astype(float)
        qi, keep = np.unique(qi, return_index=True)
        qo = qo[keep]
        x = np.arange(256, dtype=float)
        mp = np.interp(x, qi, qo)
        slope_hi = (qo[-1] - qo[-5]) / max(qi[-1] - qi[-5], 1)
        mp = np.where(x > qi[-1], qo[-1] + (x - qi[-1]) * slope_hi, mp)
        mp = np.where(x < qi[0], qo[0] * x / max(qi[0], 1), mp)
        maps[k] = np.clip(np.maximum.accumulate(mp), 0, 255)
        mi = lut[int(np.searchsorted(ci, 0.5))]
        mo = lut[int(np.searchsorted(co, 0.5))]
        kk[k] = mi / max(mo, 1e-6)
    print("Calibration from shadow edges: shadow light " + ", ".join(
        f"{n} {kk[i]:.0%}" for i, n in enumerate("RGB")) + " of sunlit (linear, median)")
    logk = np.log(np.clip(kk, 1e-3, 0.999)).astype(np.float32)
    dchroma = float(logk[2] - logk[0])  # shadow is bluer: blue drops less than red
    if dchroma < 0.05:
        print("Warning: shadows are hardly bluer than sunlit sand here; telling shadow from "
              "dark sand by colour will be unreliable")
    # sunlit reference: pixels outside the candidate zone that are not shadow-coloured
    # (real shadows the DSM missed must not count as sunlit)
    cb = np.cumsum(hbr) / max(hbr.sum(), 1)
    br_sun = float(brbins[np.searchsorted(cb, 0.5)])
    br_cut = br_sun + 0.5 * dchroma if dchroma >= 0.05 else np.inf
    for r0, c0, t in tg.tiles():
        th, tw = t.shape[:2]
        sub = t[::2, ::2]
        rgb = sub[..., :3]
        ok = np.all((rgb >= LOW) & (rgb <= HIGH), axis=2)
        if t.shape[2] > 3:
            ok &= sub[..., 3] > 0
        lab = sample(r0, c0, th, tw)[::2, ::2]
        br = np.log(np.maximum(lut[rgb[..., 2]], 1e-4) / np.maximum(lut[rgb[..., 0]], 1e-4))
        lit = ok & ((lab & 1) == 0) & (br < br_cut)
        if lit.any():
            ys = (r0 + 2 * np.arange(sub.shape[0]) + 0.5) * g.ry
            xs = (c0 + 2 * np.arange(sub.shape[1]) + 0.5) * g.res
            cy = np.minimum((ys / cell).astype(np.int64), gh - 1)
            cx = np.minimum((xs / cell).astype(np.int64), gw - 1)
            idx = (cy[:, None] * gw + cx[None, :])[lit]
            lcnt += np.bincount(idx, minlength=gh * gw)
            for k in range(3):
                lsum[k] += np.bincount(idx, weights=lut[rgb[..., k][lit]], minlength=gh * gw)
    # sunlit reference colour: masked smoothing of the cell means (about 1 m)
    lsum = lsum.reshape(3, gh, gw)
    lcnt = lcnt.reshape(gh, gw)
    w = (lcnt > 20).astype(np.float32)
    ref = np.zeros((gh, gw, 3), np.float32)
    for k in range(3):
        mean = np.where(w > 0, lsum[k] / np.maximum(lcnt, 1), 0).astype(np.float32)
        num = cv2.GaussianBlur(mean * w, (0, 0), 2.0)
        den = cv2.GaussianBlur(w, (0, 0), 2.0)
        big_n = cv2.GaussianBlur(mean * w, (0, 0), 12.0)
        big_d = cv2.GaussianBlur(w, (0, 0), 12.0)
        ref[..., k] = np.where(den > 0.05, num / np.maximum(den, 1e-6),
                               big_n / np.maximum(big_d, 1e-6))

    maps8 = np.clip(np.round(maps), 0, 255).astype(np.uint8)
    nlogk2 = float((logk * logk).sum())
    stats = {"cand": 0, "shadow": 0.0}

    # 3-4. pass 2: classify within the candidate zone and correct
    def apply(t, r0, c0):
        th, tw = t.shape[:2]
        lab = sample(r0, c0, th, tw)
        cand = (lab & 1) > 0
        if not cand.any():
            return t.copy()
        xs = ((c0 + np.arange(tw) + 0.5) * g.res / cell - 0.5).astype(np.float32)
        ys = ((r0 + np.arange(th) + 0.5) * g.ry / cell - 0.5).astype(np.float32)
        mx, my = np.meshgrid(xs, ys)
        rf = cv2.remap(ref, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        lin = lut[t[..., :3]]
        lr = np.log(np.maximum(lin, 1e-4) / np.maximum(rf, 1e-4))
        dark = (lr * logk).sum(2) / nlogk2                 # 0 = like sunlit, 1 = like shadow
        a = _step(dark, 0.3, 0.7)
        if dchroma >= 0.05:
            blue = (lr[..., 2] - lr[..., 0]) / dchroma      # 0 = sunlit colour, 1 = shadow colour
            a = a * _step(blue, 0.2, 0.55)
        a = cv2.GaussianBlur(a.astype(np.float32), (0, 0), 1.5)
        a[~cand] = 0
        a[t[..., 3] == 0] = 0
        stats["cand"] += int(cand.sum())
        stats["shadow"] += float(a.sum())
        o = t.copy()
        corr = np.stack([maps8[k][t[..., k]] for k in range(3)], -1).astype(np.float32)
        o[..., :3] = np.clip(t[..., :3] * (1 - a[..., None]) + corr * a[..., None] + 0.5,
                             0, 255).astype(np.uint8)
        return o

    geotiff.write_mapped(tg, out, apply)
    tg.close()
    tot = g.width * g.height
    print(f"Corrected as shadow: {stats['shadow'] / tot:.1%} of the image "
          f"(candidate zone {stats['cand'] / tot:.1%})")
    if mask_out is not None:
        geotiff.write(mask_out, (hard * 255).astype(np.uint8), dg)
        print(f"Predicted shadow mask (DSM grid): {mask_out}")
    print(f"Wrote {out} in {time.time() - t0:.0f} s")
