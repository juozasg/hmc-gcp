"""Build an ODM project folder (images + gcp_list.txt) and the command to process it."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# shared by all variants
COMMON = [
    "--camera-lens brown",
    "--matcher-neighbors 16",
    "--dsm",
    "--dem-resolution 4",
    "--orthophoto-resolution 1",
    "--skip-3dmodel",
    "--texturing-skip-global-seam-leveling",
    "--build-overviews",
    "--cog",
]

# (title, explanation, variant options)
VARIANTS = [
    ("Recommended", "half-resolution features, matching limited to the 16 nearest photos",
     ["--feature-quality high", "--min-num-features 10000", "--pc-quality high"]),
    ("Higher quality, slower", "full-resolution features, twice the features per photo",
     ["--feature-quality ultra", "--min-num-features 20000", "--pc-quality high"]),
    ("Much faster, quick check", "bag-of-words matching, which may miss some matches; "
     "medium point cloud",
     ["--feature-quality high", "--min-num-features 10000", "--matcher-type bow",
      "--pc-quality medium"]),
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


def default_threads() -> int:
    """All logical processors except 2, so the computer stays responsive."""
    return max(1, (os.cpu_count() or 4) - 2)


def build_project(odm_dir: Path, photos: list[Path], gcp_list: Path,
                  cameras: Path | None = None, threads: int | None = None) -> list:
    """Fill odm_dir/images with the photos, put gcp_list.txt next to it, and write
    odm_command.txt. Photos no longer in the list are removed from odm_dir/images.
    Returns [(title, explanation, ODM Console command)], recommended first."""
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

    tail = [f"--max-concurrency {threads or default_threads()}"]
    if cameras:
        tail += [f'--cameras "{Path(cameras).resolve()}"', "--use-fixed-camera-params"]
    cmds = []
    for title, why, opts in VARIANTS:
        cmd = f'run "{odm_dir.resolve()}" ' + " ".join(opts + COMMON + tail)
        cmds.append((title, why, cmd))
    text = "Run in the ODM Console:\n\n"
    for title, why, cmd in cmds:
        text += f"{title}: {why}\n{cmd}\n\n"
    text += ("To redo only part of a run, add e.g. --rerun-from opensfm (after a new\n"
             "gcp_list.txt or other matching options) or --rerun-from odm_dem (after changing\n"
             "DSM options).\n")
    (odm_dir / "odm_command.txt").write_text(text, encoding="utf-8")
    print(f"ODM project {odm_dir}: {len(photos)} photos "
          f"({counts['linked']} linked, {counts['copied']} copied, {counts['kept']} unchanged, "
          f"{counts['removed']} removed), gcp_list.txt")
    return cmds


def default_dir(images: Path, survey_folder: Path | None) -> Path:
    if survey_folder is not None:
        return Path(survey_folder) / "odm"
    images = Path(images).resolve()
    return images.parent / f"{images.name}_odm"
