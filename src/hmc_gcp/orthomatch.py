"""Give an orthophoto (e.g. ODM's) the large-scale brightness of a gain-balanced mosaic
(orthorect + orthogain), keeping its own fine detail and seam blending.

Both rasters are averaged on a coarse grid; the per-channel log ratio reference/target is
smoothed over `window` metres (masked, so gaps and edges do not bias it) and multiplied
into the target. Brightness differences finer than the window, such as ODM's own patch
steps, stay. Works tile by tile, so large orthophotos need little memory.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np

from . import geotiff
from .geotiff import TileReader

LOW, HIGH = 6, 249  # pixels with a channel outside this range are not used for the ratio


def _accumulate(rd: TileReader, X0: float, Y0: float, cell: float, gw: int, gh: int,
                lut: np.ndarray | None = None):
    """Per-cell sums of R, G, B (through lut, if given) and pixel counts of the valid
    pixels of a raster."""
    g = rd.grid
    sums = np.zeros((3, gh * gw))
    cnt = np.zeros(gh * gw)
    for r0, c0, t in rd.tiles():
        h = min(rd.th, g.height - r0)
        w = min(rd.tw, g.width - c0)
        t = t[:h, :w]
        rgb = t[..., :3]
        ok = np.all((rgb >= LOW) & (rgb <= HIGH), axis=2)
        if t.shape[2] > 3:
            ok &= t[..., 3] > 0
        if not ok.any():
            continue
        xs = g.x0 + (c0 + np.arange(w) + 0.5) * g.res
        ys = g.y0 - (r0 + np.arange(h) + 0.5) * g.ry
        cx = np.floor((xs - X0) / cell).astype(np.int64)
        cy = np.floor((Y0 - ys) / cell).astype(np.int64)
        ok &= ((cx >= 0) & (cx < gw))[None, :] & ((cy >= 0) & (cy < gh))[:, None]
        idx = (cy[:, None] * gw + cx[None, :])[ok]
        cnt += np.bincount(idx, minlength=gh * gw)
        for k in range(3):
            v = rgb[..., k][ok]
            v = lut[v] if lut is not None else v.astype(np.float64)
            sums[k] += np.bincount(idx, weights=v, minlength=gh * gw)
    return sums.reshape(3, gh, gw), cnt.reshape(gh, gw)


def _smooth(L: np.ndarray, w: np.ndarray, sigma: float) -> np.ndarray:
    """Masked Gaussian smoothing; cells far from any data get a wider average."""
    out = np.zeros_like(L)
    done = np.zeros(L.shape, bool)
    for s in (sigma, sigma * 4, sigma * 16):
        num = cv2.GaussianBlur(L * w, (0, 0), s)
        den = cv2.GaussianBlur(w, (0, 0), s)
        fill = (den > 0.05) & ~done
        out[fill] = num[fill] / den[fill]
        done |= fill
    out[~done] = np.median(L[w > 0]) if (w > 0).any() else 0.0
    return out


def run(target: Path, reference: Path, out: Path, window: float = 3.0):
    t0 = time.time()
    tg, rf = TileReader(target), TileReader(reference)
    g = tg.grid
    if tg.bands < 3 or rf.bands < 3:
        raise ValueError("both rasters must be RGB(A)")
    if g.epsg and rf.grid.epsg and g.epsg != rf.grid.epsg:
        raise ValueError(f"different CRS: EPSG:{g.epsg} vs EPSG:{rf.grid.epsg}")
    cell = window / 6.0
    X0, Y0 = g.x0, g.y0
    gw = int(math.ceil(g.width * g.res / cell))
    gh = int(math.ceil(g.height * g.ry / cell))
    print(f"Target {g.width} x {g.height} px at {g.res * 100:.1f} cm; reference at "
          f"{rf.grid.res * 100:.1f} cm; window {window:g} m (cells {cell:.2f} m)")
    st, nt = _accumulate(tg, X0, Y0, cell, gw, gh)
    sr, nr = _accumulate(rf, X0, Y0, cell, gw, gh)
    rf.close()
    need_t = 0.25 * cell * cell / (g.res * g.ry)
    need_r = 0.25 * cell * cell / (rf.grid.res * rf.grid.ry)
    valid = (nt >= need_t) & (nr >= need_r)
    if valid.sum() < 10:
        raise ValueError("the rasters hardly overlap")
    with np.errstate(divide="ignore", invalid="ignore"):
        L = np.log((sr / np.maximum(nr, 1)) / (st / np.maximum(nt, 1)))
    L = np.where(valid[None], np.clip(L, -0.7, 0.7), 0.0).astype(np.float32)
    wgt = valid.astype(np.float32)
    sigma = window / 2.0 / cell
    S = np.stack([_smooth(L[k], wgt, sigma) for k in range(3)], axis=-1).astype(np.float32)
    raw = np.exp(np.median(np.abs(L[:, valid]), axis=1)) - 1
    print(f"Brightness difference target vs reference (median, R/G/B): "
          f"{raw[0]:.1%} / {raw[1]:.1%} / {raw[2]:.1%} over {valid.sum()} cells")
    gains = np.exp(S[valid])
    print("Applied gains range R/G/B: " + ", ".join(
        f"{gains[:, k].min():.3f}-{gains[:, k].max():.3f}" for k in range(3)))

    tile = tg.tw

    def apply(t, r0, c0):
        xs = (c0 + np.arange(tile) + 0.5) * g.res / cell - 0.5
        ys = (r0 + np.arange(tile) + 0.5) * g.ry / cell - 0.5
        mx, my = np.meshgrid(xs.astype(np.float32), ys.astype(np.float32))
        gain = np.exp(cv2.remap(S, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE))
        o = t.copy()
        o[..., :3] = np.clip(t[..., :3] * gain + 0.5, 0, 255).astype(np.uint8)
        return o

    geotiff.write_mapped(tg, out, apply)
    tg.close()
    print(f"Wrote {out} in {time.time() - t0:.0f} s")
