"""Build an ODM project folder (images + gcp_list.txt) and the command to process it."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ODM_OPTIONS = [
    "--camera-lens brown",
    "--feature-quality ultra",
    "--min-num-features 20000",
    "--pc-quality high",
    "--dsm",
    "--dem-resolution 4",
    "--orthophoto-resolution 1",
    "--skip-3dmodel",
    "--texturing-skip-global-seam-leveling",
    "--build-overviews",
    "--cog",
]


def _same(a: Path, b: Path) -> bool:
    if not b.exists():
        return False
    sa, sb = a.stat(), b.stat()
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def _place(src: Path, dst: Path) -> str:
    """Hard-link src to dst (no extra disk space, same drive), else copy."""
    if _same(src, dst):
        return "kept"
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copy2(src, dst)
        return "copied"


def build_project(odm_dir: Path, photos: list[Path], gcp_list: Path,
                  cameras: Path | None = None) -> str:
    """Fill odm_dir/images with the photos, put gcp_list.txt next to it, and write
    odm_command.txt. Photos no longer in the list are removed from odm_dir/images.
    Returns the ODM Console command."""
    odm_dir = Path(odm_dir)
    img_dir = odm_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    counts = {"kept": 0, "linked": 0, "copied": 0, "removed": 0}
    names = set()
    for p in photos:
        counts[_place(p, img_dir / p.name)] += 1
        names.add(p.name)
    for old in img_dir.iterdir():
        if old.is_file() and old.suffix.lower() in (".jpg", ".jpeg") and old.name not in names:
            old.unlink()
            counts["removed"] += 1
    shutil.copy2(gcp_list, odm_dir / "gcp_list.txt")

    opts = list(ODM_OPTIONS)
    if cameras:
        opts += [f'--cameras "{Path(cameras).resolve()}"', "--use-fixed-camera-params"]
    cmd = f'run "{odm_dir.resolve()}" ' + " ".join(opts)
    (odm_dir / "odm_command.txt").write_text(
        "Run in the ODM Console:\n\n" + cmd + "\n\n"
        "To redo only part of a run, add e.g. --rerun-from opensfm (after a new\n"
        "gcp_list.txt) or --rerun-from odm_dem (after changing DSM options).\n",
        encoding="utf-8")
    print(f"ODM project {odm_dir}: {len(photos)} photos "
          f"({counts['linked']} linked, {counts['copied']} copied, {counts['kept']} unchanged, "
          f"{counts['removed']} removed), gcp_list.txt")
    return cmd


def default_dir(images: Path, survey_folder: Path | None) -> Path:
    if survey_folder is not None:
        return Path(survey_folder) / "odm"
    images = Path(images).resolve()
    return images.parent / f"{images.name}_odm"
