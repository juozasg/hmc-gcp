"""Most-nadir mosaic of the original photos on ODM's DSM, without blending.

For every output pixel (ground cell): take its height from the DSM, project it into every
photo that sees it, and copy the colour from the photo whose camera is horizontally closest
(the most-nadir view). Pixel values are only resampled, never adjusted. Also records which
photo each pixel came from, and brightness ratios between overlapping photos for orthogain.
"""

from __future__ import annotations

import csv
import math
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from pyproj import CRS

from . import geotiff
from .imgio import ImageCache
from .odmrecon import Reconstruction

PAIR_SAMPLES = 4000  # overlap samples kept per photo pair and tile


def _log(msg):
    print(msg, flush=True)


def run(odm_dir: Path, out_dir: Path, images: Path | None = None, res: float | None = None,
        margin: int = 40, interp: str = "nearest", tile: int = 1024) -> dict:
    odm_dir, out_dir = Path(odm_dir), Path(out_dir)
    images = Path(images) if images else odm_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    rec = Reconstruction(odm_dir)
    dsm, dgrid, nodata = geotiff.read(odm_dir / "odm_dem" / "dsm.tif")
    dsm = dsm.astype(np.float32)
    if dsm.ndim == 3:
        dsm = dsm[..., 0]
    valid_dsm = np.isfinite(dsm) & (dsm > -1000)
    if nodata is not None:
        valid_dsm &= dsm != nodata
    if not dgrid.epsg:
        raise ValueError("DSM has no EPSG code")
    crs = CRS.from_epsg(dgrid.epsg)
    zvals = dsm[valid_dsm]
    zmin, zmax = float(zvals.min()), float(zvals.max())
    dsm_filled = np.where(valid_dsm, dsm, np.float32(np.median(zvals)))

    shots = [s for s in rec.shots.values() if (images / s.name).exists()]
    missing = len(rec.shots) - len(shots)
    if not shots:
        raise FileNotFoundError(f"none of the reconstructed photos found in {images}")
    if missing:
        _log(f"WARNING: {missing} reconstructed photos not found in {images}")

    err = rec.fit_map(crs, dgrid.bounds, (zmin - 5, zmax + 60))
    z_ground = float(np.median(zvals))
    gsd = np.median([(s.centre_map[2] - z_ground) / s.camera.focal_px for s in shots])
    res = res or round(float(gsd), 3)
    _log(f"{len(shots)} photos, DSM {dgrid.width}x{dgrid.height} at {dgrid.res:.3f} m, "
         f"photo GSD {gsd * 100:.2f} cm, output {res * 100:.2f} cm/px "
         f"(transform fit {err * 1000:.2f} mm)")

    # output grid: DSM extent, snapped to res
    xmin, ymin, xmax, ymax = dgrid.bounds
    x0 = math.floor(xmin / res) * res
    y0 = math.ceil(ymax / res) * res
    W = int(math.ceil((xmax - x0) / res))
    H = int(math.ceil((y0 - ymin) / res))
    grid = geotiff.Grid(x0, y0, res, W, H, dgrid.epsg)
    _log(f"Mosaic {W} x {H} px ({W * res:.0f} x {H * res:.0f} m)")

    mosaic = np.zeros((H, W, 4), np.uint8)
    source = np.zeros((H, W), np.uint16)
    cache = ImageCache(32)

    # rough ground footprint radius of each photo, for picking candidates per tile
    cams = np.array([s.centre_map for s in shots])
    radius = np.array([(s.centre_map[2] - zmin) * math.hypot(s.camera.width, s.camera.height)
                       / 2 / s.camera.focal_px * 1.15 for s in shots])
    pair_logs: dict[tuple, list] = defaultdict(list)
    rng = np.random.default_rng(0)

    tiles = [(r, c) for r in range(0, H, tile) for c in range(0, W, tile)]
    # visit tiles along the survey's long axis so photos stay in the cache
    if W >= H:
        tiles.sort(key=lambda rc: (rc[1], rc[0]))
    n_done = 0
    for r0, c0 in tiles:
        th, tw = min(tile, H - r0), min(tile, W - c0)
        cols, rows = np.meshgrid(np.arange(c0, c0 + tw), np.arange(r0, r0 + th))
        X, Y = grid.xy(cols, rows)
        tx, ty = X.mean(), Y.mean()
        half = math.hypot(tw, th) * res / 2
        cand = np.nonzero(np.hypot(cams[:, 0] - tx, cams[:, 1] - ty) < radius + half)[0]
        if cand.size == 0:
            continue
        dc, dr = dgrid.col_row(X, Y)
        Z = cv2.remap(dsm_filled, dc.astype(np.float32), dr.astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_REPLICATE)
        ok_dsm = cv2.remap(valid_dsm.astype(np.uint8), dc.astype(np.float32),
                           dr.astype(np.float32), cv2.INTER_NEAREST, borderValue=0) > 0
        if not ok_dsm.any():
            continue
        P = rec.map_to_topo(X.ravel(), Y.ravel(), Z.ravel())
        best = np.full(th * tw, np.inf)
        choice = np.full(th * tw, -1, np.int32)
        maps = {}
        for k in cand:
            s = shots[k]
            u, v, depth = s.project(P)
            ok = (depth > 0) & (u >= margin) & (v >= margin) & \
                 (u <= s.camera.width - 1 - margin) & (v <= s.camera.height - 1 - margin) & \
                 ok_dsm.ravel()
            if not ok.any():
                continue
            d2 = (X.ravel() - s.centre_map[0]) ** 2 + (Y.ravel() - s.centre_map[1]) ** 2
            d2 = np.where(ok, d2, np.inf)
            better = d2 < best
            best[better] = d2[better]
            choice[better] = k
            maps[k] = (u.reshape(th, tw).astype(np.float32), v.reshape(th, tw).astype(np.float32),
                       ok.reshape(th, tw))
        if not maps:
            continue
        flag = cv2.INTER_NEAREST if interp == "nearest" else cv2.INTER_LINEAR
        choice = choice.reshape(th, tw)
        out = np.zeros((th, tw, 3), np.uint8)
        samples = {}
        for k, (u, v, ok) in maps.items():
            img = cache.get(str(images / shots[k].name))
            px = cv2.remap(img, u, v, flag, borderMode=cv2.BORDER_CONSTANT)
            sel = choice == k
            out[sel] = px[sel]
            samples[k] = (px, ok)
        # overlap statistics for orthogain: log ratios of photo pairs on shared cells
        keys = sorted(samples)
        sub = (slice(None, None, 4), slice(None, None, 4))
        for i, a in enumerate(keys):
            pa, oka = samples[a]
            for b in keys[i + 1:]:
                pb, okb = samples[b]
                both = (oka & okb)[sub]
                if both.sum() < 200:
                    continue
                va = pa[sub][both].astype(np.float32)
                vb = pb[sub][both].astype(np.float32)
                good = (va.min(1) > 10) & (vb.min(1) > 10) & (va.max(1) < 245) & (vb.max(1) < 245)
                if good.sum() < 200:
                    continue
                lr = np.log(va[good]) - np.log(vb[good])
                if len(lr) > PAIR_SAMPLES:
                    lr = lr[rng.choice(len(lr), PAIR_SAMPLES, replace=False)]
                pair_logs[(a, b)].append(lr)
        sel = choice >= 0
        tile_mosaic = mosaic[r0:r0 + th, c0:c0 + tw]
        tile_mosaic[..., :3][sel] = out[..., ::-1][sel]  # BGR -> RGB
        tile_mosaic[..., 3][sel] = 255
        source[r0:r0 + th, c0:c0 + tw][sel] = (choice[sel] + 1).astype(np.uint16)
        n_done += 1
        if n_done % 20 == 0:
            _log(f"  {n_done}/{len(tiles)} tiles")

    _log("Writing mosaic...")
    geotiff.write(out_dir / "mosaic.tif", mosaic, grid, alpha=True)
    geotiff.write(out_dir / "source.tif", source, grid, nodata=0)
    with open(out_dir / "sources.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["index", "photo", "camera_x", "camera_y", "camera_z"])
        for k, s in enumerate(shots):
            wr.writerow([k + 1, s.name, f"{s.centre_map[0]:.3f}", f"{s.centre_map[1]:.3f}",
                         f"{s.centre_map[2]:.3f}"])
    with open(out_dir / "overlaps.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["index_a", "index_b", "samples", "logratio_r", "logratio_g", "logratio_b"])
        for (a, b), parts in sorted(pair_logs.items()):
            lr = np.concatenate(parts)
            med = np.median(lr, axis=0)  # BGR order
            wr.writerow([a + 1, b + 1, len(lr), f"{med[2]:.5f}", f"{med[1]:.5f}", f"{med[0]:.5f}"])
    used = np.unique(source[source > 0]).size
    _log(f"Done in {time.time() - t0:.0f} s: {used} photos used, {len(pair_logs)} overlapping "
         f"photo pairs measured. Outputs in {out_dir}")
    return {"photos": used, "pairs": len(pair_logs), "grid": grid}
