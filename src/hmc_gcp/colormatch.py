"""Match the colours of one orthophoto (e.g. day 1) to another of the same place (day 2).

One transform for the whole scene: the light is the same everywhere within a flight, and a
spatially varying correction would also remove real changes between the days. Both rasters
are averaged on a coarse grid (cells of `cell` metres, so small misalignments between the
days do not matter), and the transform is fitted on the cells both cover, with robust
weights so that areas that really changed (wet sand, moved sand, water) do not pull it.

Methods:
  gain    gain and offset per channel in linear light: brightness and colour balance
          (white balance, haze). Default. (A full 3 x 3 colour matrix is not used: sand
          colours are so alike that its channel mixing is poorly determined and would
          distort other colours.)
  curves  gain, then a tone curve per channel from the quantiles of the unchanged cells,
          which also matches contrast (e.g. a harsher sunny day).
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from . import geotiff
from .geotiff import TileReader
from .orthomatch import _accumulate



def _srgb_to_lin(v):
    v = np.asarray(v, np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return 255.0 * np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _robust_gain(X: np.ndarray, Y: np.ndarray, iters: int = 15):
    """Fit Y_k ~ a_k X_k + b_k per channel, with shared Tukey biweights on the residual
    size (in log units, so dark and bright cells count alike). Returns (a, b, weights)."""
    w = np.ones(len(X))
    a, b = np.ones(3), np.zeros(3)
    for _ in range(iters):
        sw = np.sqrt(w)
        for k in range(3):
            A = np.c_[X[:, k], np.ones(len(X))] * sw[:, None]
            (a[k], b[k]), *_ = np.linalg.lstsq(A, Y[:, k] * sw, rcond=None)
        P = X * a + b
        r = np.linalg.norm(np.log(np.maximum(P, 1e-4)) - np.log(np.maximum(Y, 1e-4)), axis=1)
        s = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-6
        u = r / (4.685 * max(s, 0.01))
        w = np.where(u < 1, (1 - u * u) ** 2, 0.0)
    return a, b, w


def _mismatch(a_srgb: np.ndarray, b_srgb: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted median absolute log difference per channel."""
    d = np.abs(np.log(np.maximum(a_srgb, 1)) - np.log(np.maximum(b_srgb, 1)))
    out = []
    for k in range(3):
        o = np.argsort(d[:, k])
        cw = np.cumsum(w[o])
        out.append(d[o, k][np.searchsorted(cw, cw[-1] / 2)])
    return np.exp(np.array(out)) - 1


def run(target: Path, reference: Path, out: Path, method: str = "gain", cell: float = 0.5):
    t0 = time.time()
    tg, rf = TileReader(target), TileReader(reference)
    g, gr = tg.grid, rf.grid
    if tg.bands < 3 or rf.bands < 3:
        raise ValueError("both rasters must be RGB(A)")
    if g.epsg and gr.epsg and g.epsg != gr.epsg:
        raise ValueError(f"different CRS: EPSG:{g.epsg} vs EPSG:{gr.epsg}")
    # common coarse grid over the overlap
    X0 = max(g.x0, gr.x0)
    X1 = min(g.x0 + g.width * g.res, gr.x0 + gr.width * gr.res)
    Y0 = min(g.y0, gr.y0)
    Y1 = max(g.y0 - g.height * g.ry, gr.y0 - gr.height * gr.ry)
    if X1 <= X0 or Y0 <= Y1:
        raise ValueError("the rasters do not overlap")
    gw, gh = int(math.ceil((X1 - X0) / cell)), int(math.ceil((Y0 - Y1) / cell))
    print(f"Target {g.width} x {g.height} px at {g.res * 100:.1f} cm, reference "
          f"{gr.width} x {gr.height} px at {gr.res * 100:.1f} cm; overlap "
          f"{X1 - X0:.0f} x {Y0 - Y1:.0f} m in {cell:g} m cells")
    lut = _srgb_to_lin(np.arange(256))
    st, nt = _accumulate(tg, X0, Y0, cell, gw, gh, lut)
    sr, nr = _accumulate(rf, X0, Y0, cell, gw, gh, lut)
    rf.close()
    ok = (nt >= 0.5 * cell * cell / (g.res * g.ry)) & (nr >= 0.5 * cell * cell / (gr.res * gr.ry))
    if ok.sum() < 50:
        raise ValueError("too little overlap with valid pixels")
    X = (st[:, ok] / nt[ok]).T  # linear light, n x 3
    Y = (sr[:, ok] / nr[ok]).T
    gain, off, w = _robust_gain(X, Y)
    stable = w > 0.5
    print(f"{ok.sum()} cells in both; {stable.mean():.0%} used as unchanged "
          f"(the rest differ too much: changed sand, water, wet/dry)")
    print("Gain / offset per channel (linear light): " + ", ".join(
        f"{n} {gain[k]:.3f} / {off[k]:+.4f}" for k, n in enumerate("RGB")))

    Xs, Ys = _lin_to_srgb(X), _lin_to_srgb(Y)
    Pm = _lin_to_srgb(X * gain + off)
    curves = None
    if method == "curves":
        q = np.linspace(0, 1, 201)
        curves = []
        for k in range(3):
            src = np.quantile(Pm[stable, k], q)
            dst = np.quantile(Ys[stable, k], q)
            src, keep = np.unique(src, return_index=True)
            dst = dst[keep]
            x = np.arange(256, dtype=np.float64)
            c = np.interp(x, src, dst)
            # beyond the data, continue with slope 1 so highlights and shadows keep detail
            c = np.where(x < src[0], dst[0] + (x - src[0]), c)
            c = np.where(x > src[-1], dst[-1] + (x - src[-1]), c)
            curves.append(np.clip(np.maximum.accumulate(c), 0, 255))
        curves = np.stack(curves)
        P = np.stack([np.interp(Pm[:, k], np.arange(256), curves[k]) for k in range(3)], 1)
    else:
        P = Pm
    before = _mismatch(Xs, Ys, stable.astype(float))
    after = _mismatch(P, Ys, stable.astype(float))
    print(f"Colour difference on unchanged cells (median, R/G/B): "
          f"{before[0]:.1%} / {before[1]:.1%} / {before[2]:.1%}  ->  "
          f"{after[0]:.1%} / {after[1]:.1%} / {after[2]:.1%}")

    gt, offt = gain.astype(np.float32), off.astype(np.float32)
    lut32 = lut.astype(np.float32)
    # linear -> 8-bit sRGB through a fine table (fast and exact enough)
    grid_lin = np.linspace(0, 1, 4097)
    to8 = np.clip(_lin_to_srgb(grid_lin) + 0.5, 0, 255).astype(np.uint8)
    if curves is not None:
        cl = np.clip(np.round(curves), 0, 255).astype(np.uint8)

    def apply(t, r0, c0):
        lin = lut32[t[..., :3]]
        o = lin * gt + offt
        idx = np.clip(np.rint(o * 4096), 0, 4096).astype(np.int32)
        rgb = to8[idx]
        if curves is not None:
            for k in range(3):
                rgb[..., k] = cl[k][rgb[..., k]]
        res = t.copy()
        res[..., :3] = rgb
        return res

    geotiff.write_mapped(tg, out, apply)
    tg.close()
    print(f"Wrote {out} in {time.time() - t0:.0f} s")
