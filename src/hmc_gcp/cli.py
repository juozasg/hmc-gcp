"""Command line: hmc-gcp tag ... / hmc-gcp view ..."""

from __future__ import annotations

import argparse
from pathlib import Path


def _size(s: str) -> tuple[int, int]:
    w, h = s.lower().split("x")
    return int(w), int(h)


def _default_out(images: Path) -> Path:
    images = images.resolve()
    return images.parent / f"{images.name}_gcp"


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hmc-gcp",
        description="Tag ground control points in drone photos for ODM, and review the tags.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tag", help="find and confirm GCP targets in the photos")
    t.add_argument("images", type=Path, help="folder with the drone photos (JPG)")
    t.add_argument("gpkg", type=Path, help="GeoPackage with the surveyed GCP points")
    t.add_argument("layer", help="point layer name in the GeoPackage")
    t.add_argument("-o", "--out", type=Path,
                   help="output folder (default: <images folder>_gcp next to it)")
    t.add_argument("--name-field", default="name", help="attribute with the GCP name (default: feature id)")
    t.add_argument("--z-field", help="attribute with the GCP height, if the points are 2D")
    t.add_argument("--takeoff-alt", type=float,
                   help="starting take-off height, in the same height system as the GCPs "
                        "(default: median GCP height); refined per flight from the tags")
    t.add_argument("--flight-gap", type=float, default=120.0, metavar="SECONDS",
                   help="a pause between photos longer than this starts a new flight with its "
                        "own learned take-off height (default 120)")
    t.add_argument("--plan", type=Path,
                   help="flight plan CSV (Litchi style); heading and gimbal pitch are taken from it")
    t.add_argument("--target-size", type=float, default=0.40,
                   help="target side length in metres (default 0.40)")
    t.add_argument("--search-radius", type=float, default=10.0,
                   help="search radius in metres before any tag is confirmed (default 10)")
    t.add_argument("--refine-radius", type=float, default=2.5,
                   help="search radius in metres once 2 tags are confirmed (default 2.5)")
    t.add_argument("--focal35", type=float,
                   help="35 mm equivalent focal length, if missing from EXIF (Mini 3: 24)")
    t.add_argument("--min-images", type=int, default=2,
                   help="photos a GCP needs to go into gcp_list.txt (default 2)")
    t.add_argument("--checkpoint", action="append", default=[], metavar="NAME",
                   help="keep this GCP out of gcp_list.txt and export it as a checkpoint "
                        "(repeatable)")
    t.add_argument("--auto-accept", type=float, metavar="SCORE",
                   help="detect all pairs first and accept those scoring at least SCORE (0-1)")
    t.add_argument("--export-only", action="store_true",
                   help="write the output files from saved tags without opening the window")
    t.add_argument("--window", type=_size, default=(1500, 950), help="window size, e.g. 1500x950")

    v = sub.add_parser("view", help="page through the photos and see the tags")
    v.add_argument("images", type=Path, help="folder with the drone photos (JPG)")
    v.add_argument("-o", "--out", type=Path,
                   help="output folder used by 'tag' (default: <images folder>_gcp)")
    v.add_argument("--window", type=_size, default=(1500, 950), help="window size, e.g. 1500x950")

    a = p.parse_args(argv)
    out = a.out or _default_out(a.images)

    if a.cmd == "tag":
        from .session import Session
        s = Session.create(
            a.images, a.gpkg, a.layer, out, name_field=a.name_field, z_field=a.z_field,
            takeoff_alt=a.takeoff_alt, plan=a.plan, target_size=a.target_size,
            search_radius=a.search_radius, refine_radius=a.refine_radius, focal35=a.focal35,
            min_images=a.min_images, checkpoints=a.checkpoint, flight_gap=a.flight_gap)
        if a.auto_accept is not None:
            s.auto_pass(a.auto_accept)
        if a.export_only:
            s.save()
            r = s.export()
            print(f"Exported {r['control']} control GCPs, {r['checkpoints']} checkpoints, "
                  f"{r['untagged']} untagged to {out}")
            return
        from .tag_ui import TagApp
        TagApp(s, a.window).run()
    else:
        from .view_ui import ViewApp
        ViewApp(a.images, out, a.window).run()


if __name__ == "__main__":
    main()
