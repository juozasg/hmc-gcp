"""Suggest checkpoints among the control GCPs and remember the choice per survey."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

FILE = "checkpoints.txt"


def load(out_dir: Path) -> list[str]:
    f = Path(out_dir) / FILE
    if not f.exists():
        return []
    names = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def save(out_dir: Path, names: list[str]):
    text = ("# GCPs used as checkpoints: left out of gcp_list.txt, exported to checkpoints.csv\n"
            "# and the 'checkpoints' layer. Remove a line to use that GCP as control again.\n")
    text += "".join(f"{n}\n" for n in names)
    (Path(out_dir) / FILE).write_text(text, encoding="utf-8")


def suggest(control: list[dict], count: int, min_control: int = 5) -> list[dict]:
    """Pick checkpoints from the control GCPs (dicts with name, x, y, n_photos).

    Keeps the two GCPs at the ends of the survey (they control extrapolation) and at
    least min_control GCPs as control. Picks the most redundant GCPs first: those
    closest to another control GCP, avoiding GCPs right next to an already picked
    checkpoint, so the checkpoints spread over the survey.
    """
    if len(control) <= min_control:
        return []
    xy = np.array([[g["x"], g["y"]] for g in control])
    centre = xy.mean(axis=0)
    # main axis of the survey (beach strip) by PCA
    _, _, vt = np.linalg.svd(xy - centre, full_matrices=False)
    along = (xy - centre) @ vt[0]
    for g, t in zip(control, along):
        g["along_m"] = float(t - along.min())
    ends = {control[int(np.argmin(along))]["name"], control[int(np.argmax(along))]["name"]}

    remaining = list(control)
    picked: list[dict] = []
    count = min(count, len(control) - min_control)
    while len(picked) < count:
        best, best_score = None, math.inf
        for g in remaining:
            if g["name"] in ends:
                continue
            others = [o for o in remaining if o is not g]
            near = min(math.dist((g["x"], g["y"]), (o["x"], o["y"])) for o in others)
            # spread: penalise candidates close to an already picked checkpoint
            if picked:
                d_cp = min(math.dist((g["x"], g["y"]), (c["x"], c["y"])) for c in picked)
                if d_cp < 20:
                    near += 1000
            if near < best_score:
                best, best_score = g, near
        if best is None:
            break
        best["nearest_control_m"] = min(
            math.dist((best["x"], best["y"]), (o["x"], o["y"])) for o in remaining if o is not best)
        picked.append(best)
        remaining.remove(best)
    return picked
