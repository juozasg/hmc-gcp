"""Three-endmember linear unmixing of an RGB orthophoto: quartz, heavy minerals, shadow.

Each pixel's colour (in linear light, from 8-bit values through the sRGB curve) is taken as
a mix  P = q Q + h H + s S  of the three endmember colours, with q + h + s = 1 and all
fractions >= 0 (fully constrained least squares). With three endmembers in RGB this is
exact geometry: the answer is the point of the triangle Q-H-S closest to P. The
endmember colours are averaged in a small circle around a map coordinate each.

Outputs:
  <stem>_unmix.tif     bands: quartz, minerals, shadow (fractions x 255), residual
                       (distance to the triangle, linear units x 1000), valid (255 = data)
  <stem>_minerals.tif  shadow-free mineral share h / (q + h) as 1-255 (1 = none,
                       255 = all minerals); 0 = no data or deep shadow (s > max_shadow)
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer

from . import geotiff
from .geotiff import HalfCollector, TileReader

NAMES = ("quartz", "minerals", "shadow")


def _srgb_to_lin(v):
    v = np.asarray(v, np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def endmember(rd: TileReader, x: float, y: float, radius: float) -> tuple[np.ndarray, int]:
    """Mean linear RGB of the valid pixels within radius (map units) of (x, y)."""
    g = rd.grid
    c, r = (x - g.x0) / g.res, (g.y0 - y) / g.ry
    if not (0 <= c < g.width and 0 <= r < g.height):
        raise ValueError(f"point {x:.2f} {y:.2f} is outside the raster")
    k = max(1, int(np.ceil(radius / g.res)))
    c0, r0 = int(c) - k, int(r) - k
    win = rd.read_window(c0, r0, 2 * k + 1, 2 * k + 1)
    yy, xx = np.mgrid[0:2 * k + 1, 0:2 * k + 1]
    d = np.hypot((xx + c0 + 0.5 - c) * g.res, (yy + r0 + 0.5 - r) * g.ry)
    ok = d <= max(radius, g.res)
    if win.shape[2] > 3:
        ok &= win[..., 3] > 0
    ok &= np.all(win[..., :3] < 255, axis=2)
    if ok.sum() == 0:
        raise ValueError(f"no valid pixels around {x:.2f} {y:.2f}")
    lut = _srgb_to_lin(np.arange(256))
    return lut[win[..., :3][ok]].mean(0), int(ok.sum())


def fcls_triangle(P: np.ndarray, E: np.ndarray):
    """Fully constrained unmixing with 3 endmembers (rows of E, 3 x 3): fractions (N x 3)
    and residual distance (N) of the closest point of triangle E to each point in P."""
    A, B, C = E
    v0, v1 = B - A, C - A
    d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
    den = d00 * d11 - d01 * d01
    w = P - A
    d20, d21 = w @ v0, w @ v1
    fb = (d11 * d20 - d01 * d21) / den
    fc = (d00 * d21 - d01 * d20) / den
    F = np.stack([1 - fb - fc, fb, fc], axis=1)
    out = F.copy()
    bad = np.any(F < 0, axis=1)
    if bad.any():
        Pb = P[bad]
        best_d = np.full(len(Pb), np.inf)
        best_f = np.zeros((len(Pb), 3))
        for i, j in ((0, 1), (1, 2), (0, 2)):
            e = E[j] - E[i]
            t = np.clip(((Pb - E[i]) @ e) / max(e @ e, 1e-12), 0, 1)
            q = E[i] + t[:, None] * e
            dd = np.linalg.norm(Pb - q, axis=1)
            take = dd < best_d
            best_d[take] = dd[take]
            f = np.zeros((take.sum(), 3))
            f[:, i], f[:, j] = 1 - t[take], t[take]
            best_f[take] = f
        out[bad] = best_f
    resid = np.linalg.norm(P - out @ E, axis=1)
    return out, resid


def run(raster: Path, points: dict[str, tuple[float, float]], crs: str, out_dir: Path | None,
        radius: float = 0.05, max_shadow: float = 0.7):
    t0 = time.time()
    rd = TileReader(raster)
    g = rd.grid
    if rd.bands < 3:
        raise ValueError("the raster must be RGB(A)")
    tr = Transformer.from_crs(CRS.from_user_input(crs), CRS.from_epsg(g.epsg or 32634),
                              always_xy=True)
    E = np.zeros((3, 3))
    print(f"Endmembers (mean of a {radius * 100:.0f} cm circle):")
    for i, name in enumerate(NAMES):
        x, y = tr.transform(*points[name])
        E[i], n = endmember(rd, x, y, radius)
        srgb = np.clip(255 * np.where(E[i] <= 0.0031308, 12.92 * E[i],
                                      1.055 * E[i] ** (1 / 2.4) - 0.055), 0, 255)
        print(f"  {name:9s} at {points[name][0]:.2f} {points[name][1]:.2f}: RGB "
              f"{srgb[0]:.0f} {srgb[1]:.0f} {srgb[2]:.0f} ({n} px)")
    # how well separated are the endmembers? (triangle area relative to its size)
    area = 0.5 * np.linalg.norm(np.cross(E[1] - E[0], E[2] - E[0]))
    scale = max(np.linalg.norm(E[1] - E[0]), np.linalg.norm(E[2] - E[0]),
                np.linalg.norm(E[2] - E[1])) ** 2
    if area / max(scale, 1e-12) < 0.02:
        print("Warning: the three colours lie almost on a line: minerals and shadow (or "
              "quartz) can hardly be told apart, and the fractions will be noisy")
    lut = _srgb_to_lin(np.arange(256)).astype(np.float32)
    out_dir = out_dir or raster.parent
    stem = raster.stem
    p_unmix, p_min = out_dir / f"{stem}_unmix.tif", out_dir / f"{stem}_minerals.tif"
    tile = rd.tw
    halves = [HalfCollector(g.height, g.width, 5, tile), HalfCollector(g.height, g.width, 1, tile)]
    stats = {"n": 0, "share": 0.0, "shadow": 0, "resid": 0.0}
    cache = {}

    def compute(r0, c0, t):
        valid = t[..., 3] > 0 if t.shape[2] > 3 else np.ones(t.shape[:2], bool)
        P = lut[t[..., :3]].reshape(-1, 3).astype(np.float64)
        F, res = fcls_triangle(P, E)
        F = F.reshape(t.shape[0], t.shape[1], 3)
        res = res.reshape(t.shape[:2])
        u = np.zeros(t.shape[:2] + (5,), np.uint8)
        u[..., :3] = np.clip(F * 255 + 0.5, 0, 255).astype(np.uint8)
        u[..., 3] = np.clip(res * 1000 + 0.5, 0, 255).astype(np.uint8)
        u[..., 4] = np.where(valid, 255, 0)
        u[~valid, :4] = 0
        qh = F[..., 0] + F[..., 1]
        share = F[..., 1] / np.maximum(qh, 1e-6)
        m = np.clip(1 + share * 254 + 0.5, 1, 255).astype(np.uint8)
        deep = F[..., 2] > max_shadow
        m[deep | ~valid] = 0
        stats["n"] += int(valid.sum())
        stats["share"] += float(share[valid & ~deep].sum())
        stats["shadow"] += int((deep & valid).sum())
        stats["resid"] += float(res[valid].sum())
        return u, m

    def tiles_unmix():
        for r0, c0, t in rd.tiles():
            u, m = compute(r0, c0, np.ascontiguousarray(t))
            cache[(r0, c0)] = m
            halves[0].add(r0, c0, u)
            yield u

    geotiff.write_tiled_bands(p_unmix, g, tiles_unmix(), tile, 5,
                              names=["quartz", "minerals", "shadow", "residual x1000", "valid"],
                              overviews=lambda: halves[0].levels())

    def tiles_min():
        for r0, c0, _ in ((r, c, None) for r in range(0, rd.nty * rd.th, rd.th)
                          for c in range(0, rd.ntx * rd.tw, rd.tw)):
            m = cache.pop((r0, c0))
            halves[1].add(r0, c0, m, nearest=True)
            yield m

    geotiff.write_tiled_bands(p_min, g, tiles_min(), tile, 1, names=["mineral share"],
                              nodata=0, overviews=lambda: halves[1].levels(nearest=True))
    rd.close()
    n = max(stats["n"], 1)
    print(f"Mean shadow-free mineral share {stats['share'] / max(n - stats['shadow'], 1):.1%}; "
          f"deep shadow (masked) {stats['shadow'] / n:.1%}; mean residual "
          f"{stats['resid'] / n * 1000:.1f} (x1000, linear)")
    print(f"Wrote {p_unmix}\n      {p_min}  in {time.time() - t0:.0f} s")
