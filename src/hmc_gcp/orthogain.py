"""Balance the brightness of the photos in an orthorect mosaic.

One gain per photo and colour channel is solved so that overlapping photos agree on the
ground they share (log-ratio least squares; the mean gain is 1, so overall brightness is
kept). Optionally, a reference mosaic (e.g. the first day of a repeat survey) sets one
overall gain per channel, so the two mosaics are on the same brightness scale.
"""

from __future__ import annotations

import csv
from pathlib import Path

import cv2
import numpy as np

from . import geotiff

SIG_PRIOR = 0.5  # weak pull of each log gain towards 0, for photos with few overlaps


def _log(msg):
    print(msg, flush=True)


def solve_gains(n: int, pairs: list[tuple[int, int, int, np.ndarray]]):
    """pairs: (a, b, samples, log ratio a/b per channel [r, g, b]); indices 0-based.
    Returns n x 3 log gains with mean 0."""
    rows, rhs, w = [], [], []
    for a, b, cnt, lr in pairs:
        row = np.zeros(n)
        row[a], row[b] = 1.0, -1.0
        rows.append(row)
        rhs.append(-lr)  # g_a - g_b = -(log v_a - log v_b)
        w.append(np.sqrt(cnt))
    A = np.array(rows) if rows else np.zeros((0, n))
    Bm = np.array(rhs) if rhs else np.zeros((0, 3))
    W = np.array(w)[:, None] if w else np.zeros((0, 1))
    A = np.vstack([A * W, np.ones((1, n)) * 1e3, np.eye(n) / SIG_PRIOR])
    Bm = np.vstack([Bm * W, np.zeros((1, 3)), np.zeros((n, 3))])
    g, *_ = np.linalg.lstsq(A, Bm, rcond=None)
    return g - g.mean(axis=0)


def _apply(mosaic, source, gains_rgb):
    """mosaic H x W x 4 (RGBA), source H x W (1-based photo index), gains n x 3 (linear)."""
    lut = np.vstack([np.ones((1, 3)), gains_rgb]).astype(np.float32)
    out = mosaic.copy()
    for r0 in range(0, mosaic.shape[0], 1024):
        sl = slice(r0, r0 + 1024)
        g = lut[source[sl]]
        rgb = mosaic[sl, :, :3].astype(np.float32) * g
        out[sl, :, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return out


def _reference_gain(mosaic, grid, ref_path: Path, stride: int = 8):
    """One log gain per channel that brings mosaic to the reference mosaic's brightness."""
    ref, rgrid, _ = geotiff.read(ref_path)
    if ref.ndim != 3 or ref.shape[2] < 3:
        raise ValueError(f"{ref_path}: not an RGB mosaic")
    if rgrid.epsg != grid.epsg:
        raise ValueError(f"reference is EPSG:{rgrid.epsg}, mosaic is EPSG:{grid.epsg}")
    rows = np.arange(0, grid.height, stride)
    cols = np.arange(0, grid.width, stride)
    C, R = np.meshgrid(cols, rows)
    X, Y = grid.xy(C, R)
    rc, rr = rgrid.col_row(X, Y)
    rc = np.rint(rc).astype(int)
    rr = np.rint(rr).astype(int)
    inside = (rc >= 0) & (rr >= 0) & (rc < rgrid.width) & (rr < rgrid.height)
    a = mosaic[R[inside], C[inside]]
    b = ref[rr[inside], rc[inside]]
    ok = (a[:, 3] > 0) if a.shape[1] == 4 else np.ones(len(a), bool)
    if b.shape[1] == 4:
        ok &= b[:, 3] > 0
    a = a[ok, :3].astype(np.float32)
    b = b[ok, :3].astype(np.float32)
    good = (a.min(1) > 10) & (b.min(1) > 10) & (a.max(1) < 245) & (b.max(1) < 245)
    if good.sum() < 1000:
        raise ValueError(f"too little overlap with the reference ({int(good.sum())} samples)")
    lr = np.log(b[good]) - np.log(a[good])
    k = np.median(lr, axis=0)
    spread = np.median(np.abs(lr - k), axis=0)
    return k, int(good.sum()), spread


def run(ortho_dir: Path, reference: Path | None = None, out: Path | None = None) -> dict:
    ortho_dir = Path(ortho_dir)
    mosaic, grid, _ = geotiff.read(ortho_dir / "mosaic.tif")
    source, sgrid, _ = geotiff.read(ortho_dir / "source.tif")
    if source.shape != mosaic.shape[:2]:
        raise ValueError("mosaic.tif and source.tif differ in size")
    with open(ortho_dir / "sources.csv", newline="", encoding="utf-8") as fh:
        photos = [row["photo"] for row in csv.DictReader(fh)]
    n = len(photos)
    pairs = []
    with open(ortho_dir / "overlaps.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lr = np.array([float(row["logratio_r"]), float(row["logratio_g"]),
                           float(row["logratio_b"])])
            pairs.append((int(row["index_a"]) - 1, int(row["index_b"]) - 1, int(row["samples"]), lr))
    _log(f"{n} photos, {len(pairs)} overlapping pairs")
    g = solve_gains(n, pairs)

    def mismatch(gl):
        if not pairs:
            return np.zeros(3)
        res = np.array([lr + gl[a] - gl[b] for a, b, _, lr in pairs])
        return np.median(np.abs(res), axis=0)

    before = mismatch(np.zeros((n, 3)))
    after = mismatch(g)
    _log("Brightness mismatch between overlapping photos (median, R/G/B): "
         + " / ".join(f"{b * 100:.1f}%" for b in np.expm1(before))
         + "  ->  " + " / ".join(f"{a * 100:.1f}%" for a in np.expm1(after)))

    ref_info = None
    if reference:
        balanced = _apply(mosaic, source, np.exp(g))
        k, cnt, spread = _reference_gain(balanced, grid, Path(reference))
        g = g + k
        ref_info = {"gain": np.exp(k), "samples": cnt, "spread": np.expm1(spread)}
        _log(f"Reference {reference}: overall gain R/G/B "
             + " / ".join(f"{v:.3f}" for v in np.exp(k))
             + f" from {cnt} cells; pixel-level spread "
             + " / ".join(f"{s * 100:.0f}%" for s in np.expm1(spread))
             + " (includes real changes between the surveys)")

    gains = np.exp(g)
    result = _apply(mosaic, source, gains)
    out = Path(out) if out else ortho_dir / "mosaic_gain.tif"
    geotiff.write(out, result, grid, alpha=True)
    with open(out.with_name(out.stem + "_gains.csv"), "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["index", "photo", "gain_r", "gain_g", "gain_b"])
        for i, (p, gg) in enumerate(zip(photos, gains), 1):
            wr.writerow([i, p, f"{gg[0]:.4f}", f"{gg[1]:.4f}", f"{gg[2]:.4f}"])
    lo, hi = gains.min(axis=0), gains.max(axis=0)
    _log("Gains range R/G/B: " + ", ".join(f"{a:.3f}-{b:.3f}" for a, b in zip(lo, hi)))
    _log(f"Wrote {out}")
    return {"gains": gains, "before": before, "after": after, "reference": ref_info}
