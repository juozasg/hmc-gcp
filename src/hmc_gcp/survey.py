"""Survey-folder mode: find photos, flight plan and GCP layer from the folder layout

    <project>/gcp.gpkg                      layer "<survey> gcp"
    <project>/surveys/<survey>/uav/ortho/   photos
    <project>/surveys/<survey>/uav/mission/waypoints.csv
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gpkg

GPKG_NAME = "gcp.gpkg"


@dataclass
class Survey:
    name: str
    folder: Path
    images: Path
    plan: Path | None
    gpkg: Path | None
    layer: str | None


def _find_plan(uav: Path) -> Path | None:
    mission = uav / "mission"
    if not mission.is_dir():
        return None
    wp = mission / "waypoints.csv"
    if wp.is_file():
        return wp
    csvs = sorted(mission.glob("*.csv"))
    return csvs[0] if len(csvs) == 1 else None


def _find_gpkg(start: Path, levels: int = 4) -> Path | None:
    p = start
    for _ in range(levels + 1):
        cand = p / GPKG_NAME
        if cand.is_file():
            return cand
        if p.parent == p:
            break
        p = p.parent
    return None


def _find_layer(path: Path, survey: str) -> str | None:
    try:
        layers = gpkg.list_layers(path)
    except Exception:
        return None
    exact = f"{survey} gcp"
    if exact in layers:
        return exact
    cands = [l for l in layers if l.startswith(survey) and "gcp" in l.lower()]
    if len(cands) == 1:
        return cands[0]
    cands = [l for l in layers if l == survey]
    return cands[0] if cands else None


def resolve(path: Path) -> Survey | None:
    """Recognise a survey folder, its uav folder or its uav/ortho folder."""
    p = Path(path).resolve()
    if (p / "uav" / "ortho").is_dir():
        survey, uav = p, p / "uav"
    elif p.name.lower() == "uav" and (p / "ortho").is_dir():
        survey, uav = p.parent, p
    elif p.name.lower() == "ortho" and p.parent.name.lower() == "uav":
        survey, uav = p.parent.parent, p.parent
    else:
        return None
    g = _find_gpkg(survey.parent)
    return Survey(
        name=survey.name, folder=survey, images=uav / "ortho", plan=_find_plan(uav),
        gpkg=g, layer=_find_layer(g, survey.name) if g else None,
    )
