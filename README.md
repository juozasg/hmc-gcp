# hmc-gcp

Tag ground control points (GCPs) in drone photos for OpenDroneMap, then review the tags.

- `hmc-gcp tag` predicts where each surveyed GCP should appear in each photo, finds the
  target (a light square with a dark centre dot), and shows you one crop per GCP-photo pair
  to accept, correct or reject. It writes `gcp_list.txt` for ODM and a GeoPackage/CSV of
  the GCPs seen in too few photos, for use as checkpoints.
- `hmc-gcp view` pages through the photos and shows what was tagged in each.

## Install

Needs [uv](https://docs.astral.sh/uv/). In this folder:

```
uv sync
uv run hmc-gcp --help
```

## Quick start

Survey-folder mode (photos, flight plan and GCP layer are found from the folder layout):

```
uv run hmc-gcp tag E:\hmc-data\neringa\surveys\2026-11-06-L1-smiltyne
uv run hmc-gcp view E:\hmc-data\neringa\surveys\2026-11-06-L1-smiltyne
```

The expected layout:

```
<project>\gcp.gpkg                               layer "<survey> gcp"
<project>\surveys\<survey>\uav\ortho\            photos
<project>\surveys\<survey>\uav\mission\waypoints.csv
```

`gcp.gpkg` is looked for in the folders above the survey (up to 4 levels). The layer is
`<survey> gcp`, or the only layer starting with the survey name and containing "gcp". The
survey folder, its `uav` folder or its `uav\ortho` folder can all be given. Anything given
explicitly (GeoPackage, layer, `--plan`) wins; `--no-plan` skips the plan.

Explicit paths work for any other layout:

```
uv run hmc-gcp tag D:\flights\nida\images D:\survey\gcps.gpkg gcp_points --takeoff-alt 2.35
uv run hmc-gcp view D:\flights\nida\images
```

Outputs go to a folder next to the photos (`ortho_gcp`), or to `-o <folder>`.

## How it works

1. **Predict.** For every photo, the GPS position, height above take-off and gimbal
   heading/pitch from the DJI metadata give a camera model. Every GCP is projected into
   every photo. Pairs whose prediction falls inside the photo (plus a search margin) are
   queued for review.
2. **Detect.** In a window around the prediction, the detector removes the local
   background (wet/dry sand, shading, vegetation), then scores bright, colourless blobs on
   size (from GSD and `--target-size`), squareness, corners, flatness, contrast and a dark
   centre dot. The dot centre becomes the GCP pixel (sub-pixel). If no dot is found, the
   square's centre is used.
3. **Learn the GPS error.** Each confirmed tag tells how far the photo's GPS position was
   off. The median error is applied to all photos (and a photo's own error, once it has a
   tag), so the search window shrinks from `--search-radius` (10 m) to `--refine-radius`
   (2.5 m). Pairs that then fall outside their photo are parked as `outside`, and so are
   pairs predicted just past the photo edge where detection found no target.
4. **Learn each flight's take-off height.** Photos are split into flights wherever the
   capture times have a pause longer than `--flight-gap` (120 s, e.g. a battery swap).
   For each flight, the confirmed tags are fitted for the flight's horizontal GPS offset
   and its take-off height error (tags away from the photo centre show the scale error).
   `--takeoff-alt` is only the starting value. The window shows each flight's learned
   take-off height with its uncertainty; `tag_report.txt` lists them all.
5. **Review.** You accept, click to correct, or mark "not visible". Everything is saved to
   `tags.json` after every action, so you can quit and resume.
6. **Export.** GCPs tagged in at least `--min-images` photos (default 2) go to
   `gcp_list.txt`. GCPs tagged in fewer photos, or named with `--checkpoint`, go to the
   checkpoint outputs.

Before the window opens, the tool runs detection on all unreviewed pairs and accepts those
scoring at least `--auto-accept` (default 0.75) **and** having a visible centre dot. It
repeats while new tags sharpen the predictions, and sends auto tags more than 2 m off their
flight's fit back for review. Auto-accepted tags are marked `auto`; check them in the
viewer. Pairs you have accepted, rejected or reset by hand are never auto-accepted. Use
`--no-auto` to review everything by hand.

## Output files

| File | Contents |
| --- | --- |
| `gcp_list.txt` | ODM GCP file. Header is the GeoPackage CRS (e.g. `EPSG:3346`). Copy it to the ODM project folder |
| `gcp_points.gpkg` | Layers `control` (in gcp_list.txt), `checkpoints`, `untagged`, with photo counts and pixel positions. Opens in QGIS |
| `checkpoints.csv` | The checkpoint layer as CSV |
| `tag_report.txt` | Counts per GCP and the estimated GPS offset |
| `tags.json` | Full review state (used to resume and by the viewer) |

## tag: options

| Option | Meaning |
| --- | --- |
| `images [gpkg layer]` | Survey folder, or photo folder + GeoPackage + point layer name |
| `--no-plan` | Do not use the flight plan found in survey-folder mode |
| `--name-field` | Attribute holding the GCP name (default: feature id). Spaces become `_` |
| `--z-field` | Attribute holding the height, if the points are 2D |
| `--takeoff-alt` | Starting take-off height **in the same height system as the GCPs**, refined per flight from the tags. Without it, the median GCP height is used |
| `--flight-gap` | Pause in seconds that starts a new flight (default 120) |
| `--plan` | Flight plan CSV (Litchi style). Heading and gimbal pitch are taken from the nearest waypoint (within 15 m) |
| `--target-size` | Target side in metres (default 0.40) |
| `--search-radius` / `--refine-radius` | Search radius before / after 2 tags are confirmed (10 m / 2.5 m) |
| `--focal35` | 35 mm equivalent focal length if EXIF lacks it (DJI Mini 3: 24) |
| `--min-images` | Photos a GCP needs to be used as control (default 2; ODM recommends 3+) |
| `--checkpoint NAME` | Keep this GCP out of gcp_list.txt and export it as a checkpoint (repeatable) |
| `--auto-accept SCORE` | Score needed for automatic acceptance (default 0.75) |
| `--no-auto` | Skip automatic acceptance |
| `--export-only` | Write the outputs from saved tags without opening the window |
| `--window 1500x950` | Window size |

## tag: keys

| Key | Action |
| --- | --- |
| Enter / A | Accept point, go to next unreviewed |
| X / Delete | Target not visible, go to next unreviewed |
| Left click | Place the point by hand |
| Shift + click | Place by hand and accept |
| I J K L | Nudge point 1 px (Shift: 5 px) |
| Right / Left | Next / previous pair |
| N | Next unreviewed pair |
| PgDn / PgUp | Next / previous GCP |
| U | Reset pair to unreviewed |
| R | Run detection again |
| O / Tab | Detail / overview of the search area |
| + / - / wheel | Zoom |
| S | Save and export |
| H / F1 | Help |
| Q / Esc | Save, export and quit |

Colours: yellow ring = search area around the prediction, green box = detected target,
orange box = weak candidate without a centre dot (the point then starts at the prediction),
white cross = point to accept, green = accepted, cyan = placed by hand, orange = other GCPs
predicted in the same photo.

## view: keys

| Key | Action |
| --- | --- |
| Right / Left | Next / previous photo |
| Home / End, PgDn / PgUp | First / last, 10 photos forward / back |
| + / - / wheel | Zoom (wheel zooms at the cursor) |
| Drag | Pan |
| F / 0 | Fit whole photo |
| Tab | Zoom to the next GCP in the photo |
| T | Filter: photos with GCPs / with tags / all |
| 1 / 2 / 3 | Show/hide accepted / unreviewed / not visible |
| L | Labels on/off |
| R | Reload tags.json (see changes made in the tag window) |
| H / F1 | Help |
| Q / Esc | Quit |

## Assumptions and limits

- **Gimbal angles:** some DJI drones (the Mini 3 flown with Litchi) write all-zero gimbal
  angles. These are treated as missing: heading comes from the aircraft yaw, and the camera
  is assumed to point straight down.
- **Photos:** JPG with DJI XMP metadata (GPS, `RelativeAltitude`, gimbal yaw/pitch). Other
  drones work if EXIF has GPS; heading then defaults to north-up unless `--plan` is given.
- **Lens:** a plain pinhole model from the 35 mm focal length, referred to a 4:3 sensor as
  wide as the photo, so 16:9 photos (a crop of the same sensor) get the right scale. DJI JPGs are distortion-
  corrected in the camera, so this is fine for prediction. The tagged pixels themselves are
  exact, because they come from the image, not the model.
- **Gimbal roll** is assumed 0.
- **Heights:** `--takeoff-alt` must be in the GCPs' height system (for example LAS07 if the
  GCPs are LAS07). Take-off heights only affect where the tool searches; the exported pixel
  positions come from the photos. Each flight's height is learned once it has a few tags,
  best when they lie away from the photo centre. Photos without a capture time form one
  group ("flight 0").
- **GeoPackage:** the GCP layer is read from a temporary copy (with its `-wal` file), so it
  works while the file is open in QGIS and the original is never modified.
- **Target spacing:** identity comes from the prediction, so targets should be further apart
  than the GPS error (15 m or more is safe with a Mini 3).
- **Other target designs:** the detector looks for a light, colourless square with a dark
  centre dot. A different design needs changes in `detect.py`.

## Tested

- Synthetic flight (40 photos, 8 GCPs, 2.6 m GPS bias): 66 of 67 visible sightings found
  automatically, no false positives, median error 0.1 px. The one missed target was cut by
  the photo edge.
- Synthetic flight split into 3 flights with take-off heights 1.2, 4.5 and 0.5 m (starting
  value 1.2 m): learned 1.31 +/- 0.19, 4.55 +/- 0.20 and 0.61 +/- 0.49 m; 72 of 72 visible
  sightings found.
- Three real beach photos (dry sand, wet sand, dune vegetation): the target was found in
  21 of 21 searches with the prediction up to 400 px off.
