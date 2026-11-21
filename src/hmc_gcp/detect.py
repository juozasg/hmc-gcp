"""Find a light, flat, square target with a dark centre dot near a predicted position."""

from __future__ import annotations

import math

import cv2
import numpy as np


def detect_target(crop: np.ndarray, pred_xy, expected_px: float, search_r_px: float,
                  valid: np.ndarray | None = None):
    """Search a BGR crop. pred_xy and the result are in crop pixel coordinates.

    Returns a dict (score 0..1, center [x, y], box 4x[x, y], has_dot, side_px)
    for the best candidate, or None.
    """
    h, w = crop.shape[:2]
    if h < 8 or w < 8:
        return None
    if valid is None:
        valid = np.ones((h, w), dtype=bool)
    if valid.sum() < 64:
        return None
    expected_px = max(4.0, float(expected_px))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    S = hsv[..., 1].astype(np.float32)
    V = hsv[..., 2].astype(np.float32)
    white = V - S  # bright and colourless: high; yellow sand: lower
    # remove the local background (wet/dry sand, shading, vegetation) with a median
    # filter much larger than the target, so one threshold works across the crop
    w8 = np.clip(white * 0.5 + 128, 0, 255).astype(np.uint8)
    w8[~valid] = int(np.median(w8[valid]))
    ksz = int(min(255, max(15, 3 * expected_px))) | 1
    scale = 1.0
    if ksz > 101:  # large kernels: filter a downscaled copy for speed
        scale = 101.0 / ksz
        small = cv2.resize(w8, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        bgs = cv2.medianBlur(small, 101)
        bgw = cv2.resize(bgs, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        bgw = cv2.medianBlur(w8, ksz)
    white = (w8.astype(np.float32) - bgw.astype(np.float32)) * 2.0

    wv = white[valid]
    med = float(np.median(wv))
    mad = float(np.median(np.abs(wv - med))) * 1.4826 + 1.0
    # cap the threshold: vegetation and shadows inflate the spread, but a target is
    # always well above the local background
    thr = med + float(np.clip(3.0 * mad, 20.0, 45.0))
    mask = ((white > thr) & valid).astype(np.uint8)
    k = max(1, int(round(expected_px * 0.06)))
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, ker)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ker)

    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    exp_area = expected_px ** 2
    bg = V[(mask == 0) & valid]
    bg_std = float(np.std(bg)) + 1e-3 if bg.size else 1.0

    cands = []
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < 0.12 * exp_area or area > 3.5 * exp_area:
            continue
        comp = (lab[y : y + bh, x : x + bw] == i).astype(np.uint8)
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        cnt = max(cnts, key=cv2.contourArea)
        (rcx, rcy), (rw, rh), ang = cv2.minAreaRect(cnt)
        if rw < 3 or rh < 3:
            continue
        side = math.sqrt(rw * rh)
        size_s = math.exp(-(math.log(side / expected_px)) ** 2 / (2 * 0.45 ** 2))
        square_s = min(rw, rh) / max(rw, rh)

        hull = cv2.convexHull(cnt)
        hull_mask = np.zeros_like(comp)
        cv2.fillConvexPoly(hull_mask, hull, 1)
        hull_area = float(hull_mask.sum()) or 1.0
        fill_s = min(1.0, hull_area / (rw * rh))
        solid_s = min(1.0, area / hull_area)
        approx = cv2.approxPolyDP(hull, 0.06 * cv2.arcLength(hull, True), True)
        corner_s = {4: 1.0, 5: 0.5}.get(len(approx), 0.0)

        Vb = V[y : y + bh, x : x + bw]
        tv = float(np.median(Vb[comp > 0]))
        # sun glint on water is clipped white; a matte target in normal exposure is not
        if float(np.mean(Vb[comp > 0] >= 252)) > 0.4:
            continue
        e = max(1, int(0.12 * side))
        inner = cv2.erode(hull_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * e + 1, 2 * e + 1)))
        vals = Vb[(comp > 0) & (inner > 0)]
        flat_s = 1.0 if vals.size < 5 else float(np.clip(1.2 - np.std(vals) / bg_std, 0, 1))

        # dark centre dot: dark pixels inside the target, away from its edges
        dot = (inner > 0) & (Vb < tv - max(30.0, 0.3 * tv))
        dot_n = int(dot.sum())
        center = (x + rcx, y + rcy)
        has_dot, dot_s = False, 0.0
        if dot_n > 0:
            ys, xs = np.nonzero(dot)
            wts = (tv - Vb[ys, xs]).astype(np.float64)
            dcx = float((xs * wts).sum() / wts.sum())
            dcy = float((ys * wts).sum() / wts.sum())
            off = math.hypot(dcx - rcx, dcy - rcy) / side
            if 0.001 * side * side <= dot_n <= 0.10 * side * side and off < 0.3:
                has_dot, dot_s = True, 1.0
                center = (x + dcx, y + dcy)
            else:
                dot_s = 0.3

        wcomp = white[y : y + bh, x : x + bw][comp > 0]
        contrast_s = float(np.clip((float(np.median(wcomp)) - med) / (6 * mad + 40), 0, 1))
        dist = math.hypot(center[0] - pred_xy[0], center[1] - pred_xy[1])
        dist_s = math.exp(-0.5 * (dist / max(1.0, 0.7 * search_r_px)) ** 2)

        score = (0.20 * size_s + 0.08 * square_s + 0.06 * fill_s + 0.06 * solid_s
                 + 0.10 * corner_s + 0.28 * dot_s + 0.07 * contrast_s + 0.05 * flat_s
                 + 0.10 * dist_s)
        box = cv2.boxPoints(((x + rcx, y + rcy), (rw, rh), ang))
        cands.append({
            "score": round(float(score), 3),
            "center": [float(center[0]), float(center[1])],
            "box": [[float(a), float(b)] for a, b in box],
            "has_dot": has_dot,
            "side_px": round(float(side), 1),
        })
    if not cands:
        return None
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[0]
