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


def _build_odm(s, a, out: Path, sv, r: dict):
    from .odm import build_project, default_dir
    odm_dir = a.odm_dir or default_dir(a.images, sv.folder if sv else None)
    photos = [Path(sh.path) for sh in s.shots.values()]
    cmds = build_project(odm_dir, photos, out / "gcp_list.txt", a.odm_cameras, a.odm_threads)
    geo = Path(odm_dir) / "geo.txt"
    if a.no_geo:
        if geo.exists():
            geo.unlink()
    else:
        n = s.write_geo(geo)
        print(f"geo.txt: {n} photo positions with corrected heights")
    if r.get("control", 0) < 3:
        print("WARNING: fewer than 3 control GCPs in gcp_list.txt; ODM needs at least 3.")
    print("\nRun in the ODM Console:")
    for title, why, cmd in cmds:
        print(f"\n{title}: {why}\n{cmd}")
    print(f"\n(also saved in {Path(odm_dir) / 'odm_command.txt'})")


def _ask(prompt: str) -> str:
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("y", "yes", "n", "no", "q", "quit"):
            return ans[0]
        print("  please answer y (yes), n (no) or q (stop asking)")


def _pick_checkpoints(s, a, out: Path):
    """Suggest checkpoints among the control GCPs, ask, save and re-export."""
    from .checkpoints import save, suggest
    from .session import ACCEPTED
    if a.reset:
        s.settings["checkpoints"] = list(a.checkpoint)
    current = list(s.settings["checkpoints"])
    n_photos = {g["name"]: 0 for g in s.gcps}
    for it in s.items:
        if it.status in ACCEPTED and it.px:
            n_photos[it.gcp] += 1
    control = [dict(g, n_photos=n_photos[g["name"]]) for g in s.gcps
               if n_photos[g["name"]] >= s.settings["min_images"] and g["name"] not in current]
    print(f"\nControl GCPs: {len(control)}. Checkpoints already chosen: "
          f"{', '.join(current) if current else 'none'}")
    count = a.count if a.count is not None else max(2, round(len(control) / 4))
    picks = suggest(control, count, a.min_control)
    if not picks:
        print(f"No suggestions: keeping at least {a.min_control} control GCPs "
              "(change with --min-control).")
        return None
    print("Suggested checkpoints (most redundant control GCPs first; the GCPs at both ends of "
          "the survey are never suggested):\n")
    chosen = []
    for g in picks:
        line = (f"  {g['name']}: {g['n_photos']} photos, {g['along_m']:.0f} m along the survey, "
                f"nearest control GCP {g['nearest_control_m']:.0f} m away")
        if a.yes:
            print(line + "  -> checkpoint")
            chosen.append(g["name"])
            continue
        print(line)
        ans = _ask("  Use as checkpoint? [y/n/q] ")
        if ans == "q":
            break
        if ans == "y":
            chosen.append(g["name"])
    if not chosen:
        print("Nothing changed.")
        return None
    s.settings["checkpoints"] = sorted(set(current) | set(chosen))
    save(out, s.settings["checkpoints"])
    s.save()
    r = s.export()
    print(f"\nCheckpoints now: {', '.join(s.settings['checkpoints'])} "
          f"(saved in {out / 'checkpoints.txt'})")
    print(f"Exported {r['control']} control GCPs, {r['checkpoints']} checkpoints, "
          f"{r['untagged']} untagged to {out}")
    return r


def _odm_folder(p, folder: Path) -> Path:
    """Survey folder -> <survey>/odm; ODM folder as is."""
    folder = Path(folder)
    for cand in (folder / "odm", folder):
        if (cand / "opensfm").is_dir() or (cand / "odm_dem").is_dir():
            return cand
    p.error(f"no ODM project found in {folder}")


def _ortho_folder(p, folder: Path) -> Path:
    folder = Path(folder)
    for cand in (folder, folder / "orthorect", folder / "odm" / "orthorect"):
        if (cand / "mosaic.tif").exists() and (cand / "source.tif").exists():
            return cand
    p.error(f"no orthorect output (mosaic.tif, source.tif) found in {folder}; run orthorect first")


def _mosaic_path(p, ref: Path) -> Path:
    ref = Path(ref)
    if ref.is_file():
        return ref
    d = _ortho_folder(p, ref)
    return d / "mosaic_gain.tif" if (d / "mosaic_gain.tif").exists() else d / "mosaic.tif"


def _resolve_inputs(p, a, sv):
    """Fill GeoPackage, layer and plan from the survey folder; error if missing."""
    if sv:
        a.gpkg = a.gpkg or sv.gpkg
        a.layer = a.layer or sv.layer
        if not a.plan and not a.no_plan:
            a.plan = sv.plan
        print(f"  GCPs: {a.gpkg} / {a.layer}")
        print(f"  plan: {a.plan or 'none'}")
    if a.no_plan:
        a.plan = None
    if not a.gpkg or not a.layer:
        msg = "GeoPackage and layer were not given"
        if sv:
            msg += f" and not found for survey {sv.name}"
        if a.gpkg:
            from .gpkg import list_layers
            msg += f". Layers in {a.gpkg}: {', '.join(list_layers(a.gpkg))}"
        p.error(msg)


def _add_session_args(t):
    """Arguments shared by tag and pick-checkpoints."""
    t.add_argument("images", type=Path,
                   help="survey folder (finds uav/ortho, uav/mission/waypoints.csv, gcp.gpkg and "
                        "the '<survey> gcp' layer), or a folder with the drone photos (JPG)")
    t.add_argument("gpkg", type=Path, nargs="?",
                   help="GeoPackage with the surveyed GCP points (found automatically in "
                        "survey-folder mode)")
    t.add_argument("layer", nargs="?",
                   help="point layer in the GeoPackage (default in survey-folder mode: "
                        "'<survey> gcp')")
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
                   help="flight plan CSV (Litchi style); heading and gimbal pitch are taken from it "
                        "(default in survey-folder mode: uav/mission/waypoints.csv)")
    t.add_argument("--no-plan", action="store_true", help="do not use a flight plan")
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
    t.add_argument("--auto-accept", type=float, default=0.75, metavar="SCORE",
                   help="detect all unreviewed pairs first and accept those scoring at least "
                        "SCORE (0-1) with a visible centre dot (default 0.75)")
    t.add_argument("--no-auto", action="store_true",
                   help="do not auto-accept; review every pair by hand")
    t.add_argument("--odm-dir", type=Path,
                   help="ODM project folder to build on exit (default: <survey>/odm, or "
                        "<images folder>_odm next to it)")
    t.add_argument("--no-odm", action="store_true", help="do not build the ODM project folder")
    t.add_argument("--odm-cameras", type=Path, metavar="CAMERAS_JSON",
                   help="cameras.json from an earlier ODM run; adds --cameras and "
                        "--use-fixed-camera-params to the printed ODM command")
    t.add_argument("--no-geo", action="store_true",
                   help="do not write geo.txt (corrected photo positions) to the ODM folder")
    t.add_argument("--odm-threads", type=int, metavar="N",
                   help="--max-concurrency for ODM (default: logical processors minus 2)")
    t.add_argument("--show-rejected", action="store_true",
                   help="also stop at pairs marked not visible when stepping (toggle with V)")
    t.add_argument("--export-only", action="store_true",
                   help="write the output files from saved tags without opening the window")
    t.add_argument("--window", type=_size, default=(1500, 950), help="window size, e.g. 1500x950")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hmc-gcp",
        description="Tag ground control points in drone photos for ODM, and review the tags.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tag", help="find and confirm GCP targets in the photos")
    _add_session_args(t)

    c = sub.add_parser("pick-checkpoints",
                       help="suggest GCPs to hold out as checkpoints and ask yes/no for each")
    _add_session_args(c)
    c.add_argument("--count", type=int, metavar="N",
                   help="number of checkpoints to suggest (default: about a quarter of the "
                        "control GCPs, at least 2)")
    c.add_argument("--min-control", type=int, default=5,
                   help="never leave fewer control GCPs than this (default 5)")
    c.add_argument("--reset", action="store_true",
                   help="forget the earlier checkpoint choice and start over")
    c.add_argument("--yes", action="store_true", help="accept all suggestions without asking")

    v = sub.add_parser("view", help="page through the photos and see the tags")
    v.add_argument("images", type=Path, help="survey folder, or folder with the drone photos (JPG)")
    v.add_argument("-o", "--out", type=Path,
                   help="output folder used by 'tag' (default: <images folder>_gcp)")
    v.add_argument("--window", type=_size, default=(1500, 950), help="window size, e.g. 1500x950")

    o = sub.add_parser("orthorect",
                       help="mosaic the original photos on ODM's DSM (most-nadir pixel, no blending)")
    o.add_argument("folder", type=Path, help="survey folder or ODM project folder")
    o.add_argument("-o", "--out", type=Path, help="output folder (default: <ODM folder>/orthorect)")
    o.add_argument("--images", type=Path,
                   help="photos to mosaic (default: <ODM folder>/images). File names must match; "
                        "e.g. TIFFs developed from RAW with the same names")
    o.add_argument("--res", type=float, metavar="M",
                   help="pixel size in metres (default: the photos' ground pixel size, ~0.01)")
    o.add_argument("--margin", type=int, default=40,
                   help="photo border in pixels never used (default 40)")
    o.add_argument("--interp", choices=["nearest", "bilinear"], default="nearest",
                   help="resampling (default nearest: keeps the original pixel values)")

    g = sub.add_parser("orthogain",
                       help="balance photo brightness in an orthorect mosaic, optionally to a "
                            "reference mosaic")
    g.add_argument("folder", type=Path, help="orthorect output folder, ODM folder or survey folder")
    g.add_argument("--reference", type=Path,
                   help="mosaic to match overall brightness to (a .tif, or a survey / ODM / "
                        "orthorect folder: its mosaic_gain.tif, else mosaic.tif)")
    g.add_argument("-o", "--out", type=Path, help="output file (default: mosaic_gain.tif)")

    w = sub.add_parser("worldfiles",
                       help="roughly georeference photos from their metadata (one .vrt per "
                            "photo, for previews in QGIS)")
    w.add_argument("images", type=Path, help="survey folder, or folder with the drone photos (JPG)")
    w.add_argument("-o", "--out", type=Path,
                   help="output folder (default: <images folder>_vrt next to it)")
    w.add_argument("--crs", default="EPSG:3346", help="output CRS (default EPSG:3346, LKS94)")
    w.add_argument("--plan", type=Path,
                   help="Litchi waypoint CSV for headings (found automatically in survey-folder mode)")
    w.add_argument("--no-plan", action="store_true", help="ignore the flight plan")
    w.add_argument("--height-offset", type=float, default=0.0, metavar="M",
                   help="add to the height above take-off, e.g. if you took off from a dune "
                        "(negative) or below the beach (positive); default 0")
    w.add_argument("--focal35", type=float, help="35 mm equivalent focal length (default: EXIF)")

    a = p.parse_args(argv)
    if a.cmd == "orthorect":
        odm = _odm_folder(p, a.folder)
        from . import orthorect
        try:
            orthorect.run(odm, a.out or odm / "orthorect", a.images, a.res, a.margin, a.interp)
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            p.error(str(e))
        return
    if a.cmd == "orthogain":
        from . import orthogain
        ref = _mosaic_path(p, a.reference) if a.reference else None
        try:
            orthogain.run(_ortho_folder(p, a.folder), ref, a.out)
        except (ValueError, FileNotFoundError) as e:
            p.error(str(e))
        return
    if not a.images.is_dir():
        p.error(f"folder not found: {a.images}"
                + ("  (in bash, quote Windows paths or use forward slashes)"
                   if "\\" not in str(a.images) and ":" in str(a.images) else ""))
    from .survey import resolve
    sv = resolve(a.images)
    if sv:
        a.images = sv.images
        print(f"Survey {sv.name}: photos {sv.images}")
    if a.cmd == "worldfiles":
        from . import worldfiles
        plan = None if a.no_plan else (a.plan or (sv.plan if sv else None))
        out = a.out or a.images.parent / f"{a.images.name}_vrt"
        print(f"  plan: {plan or 'none'}")
        n = worldfiles.run(a.images, out, a.crs, plan, a.height_offset, a.focal35)
        print(f"Wrote {n} .vrt files to {out}. In QGIS: drag them into a group "
              f"(Layer > Add Layer > Add Raster Layer, select all).")
        return
    out = a.out or _default_out(a.images)

    if a.cmd in ("tag", "pick-checkpoints"):
        _resolve_inputs(p, a, sv)
        from .session import Session
        try:
            s = Session.create(
                a.images, a.gpkg, a.layer, out, name_field=a.name_field, z_field=a.z_field,
                takeoff_alt=a.takeoff_alt, plan=a.plan, target_size=a.target_size,
                search_radius=a.search_radius, refine_radius=a.refine_radius,
                focal35=a.focal35, min_images=a.min_images, checkpoints=a.checkpoint,
                flight_gap=a.flight_gap)
        except (ValueError, FileNotFoundError) as e:
            p.error(str(e))
        if a.cmd == "pick-checkpoints":
            r = _pick_checkpoints(s, a, out)
            if r is not None and not a.no_odm:
                _build_odm(s, a, out, sv, r)
            return
        if not a.no_auto:
            s.auto_pass(a.auto_accept)
        if a.export_only:
            s.save()
            r = s.export()
            print(f"Exported {r['control']} control GCPs, {r['checkpoints']} checkpoints, "
                  f"{r['untagged']} untagged to {out}")
        else:
            from .tag_ui import TagApp
            r = TagApp(s, a.window, show_rejected=a.show_rejected).run()
        if not a.no_odm:
            _build_odm(s, a, out, sv, r)
    else:
        from .view_ui import ViewApp
        ViewApp(a.images, out, a.window).run()


if __name__ == "__main__":
    main()
