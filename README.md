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

## Checkpoints

Checkpoints are GCPs left out of `gcp_list.txt`, so ODM does not use them; their error in the
finished orthophoto and DSM is an independent accuracy measure. Pick them interactively:

```
uv run hmc-gcp pick-checkpoints "E:/hmc-data/neringa/surveys/2026-11-06-L1-smiltyne"
```

It suggests about a quarter of the control GCPs (at least 2, `--count N` to change), never
the GCPs at both ends of the survey and never leaving fewer than 5 control GCPs
(`--min-control`). The most redundant GCPs (closest to another control GCP) are suggested
first, spread over the survey. Answer y/n for each (q stops). The choice is saved in
`checkpoints.txt` in the output folder and used by every later `tag` run; delete a line to
use that GCP as control again, or run with `--reset` to start over. The command then
re-exports and rebuilds the ODM folder (`--no-odm` skips that); rerun ODM with
`--rerun-from opensfm` for the new GCP list to take effect.

`--checkpoint NAME` on `tag` still adds a checkpoint for that run only.

## ODM project folder

When `tag` finishes (window closed, or `--export-only`), it builds the ODM project folder
and prints the command to paste into the ODM Console:

```
<survey>\odm\images\        the photos (hard links: no extra disk space on the same drive)
<survey>\odm\gcp_list.txt    control GCPs (checkpoints left out)
<survey>\odm\geo.txt         corrected photo positions and heights
<survey>\odm\odm_command.txt the ODM command
```

`geo.txt` replaces the photos' own GPS for ODM (the photos are not edited). Positions are
corrected by the GPS offsets learned from the tags; heights are each flight's learned take-off
height plus the height above take-off, in the GCPs' height system (DJI's own GPS altitude is
often wrong, e.g. negative). This gives ODM better photo-pair selection and starting
positions. It does not constrain the result unless `--force-gps` is added; the GCPs set the
georeferencing. `--no-geo` leaves it out.

Without a survey folder it is `<images folder>_odm` next to the photos; `--odm-dir` sets
it, `--no-odm` skips it. Re-running updates it: changed photos are replaced, and photos no
longer in the photo folder (e.g. moved out as unusable) are removed from `odm\images`.
`--odm-cameras <cameras.json>` adds `--cameras ... --use-fixed-camera-params` to the
command, to reuse a lens calibration from an earlier run.

Three commands are printed (and saved in `odm_command.txt`). All share `--camera-lens brown
--matcher-neighbors 16 --dsm --dem-resolution 4 --orthophoto-resolution 1 --skip-3dmodel
--texturing-skip-global-seam-leveling --build-overviews --cog --max-concurrency N`, with N =
logical processors minus 2 (`--odm-threads` overrides it):

| Variant | Extra options |
| --- | --- |
| Recommended | `--feature-quality high --min-num-features 10000 --pc-quality high` |
| Higher quality, slower | `--feature-quality ultra --min-num-features 20000 --pc-quality high` |
| Much faster, quick check | `--feature-quality high --min-num-features 10000 --matcher-type bow --pc-quality medium` |

Photo GPS is not forced (`--force-gps` is left out), so the GCPs alone set the
georeferencing.

## Nadir mosaic: orthorect and orthogain

`orthorect` rebuilds the orthomosaic from the original photos with no blending. ODM's
camera poses and calibration (`opensfm/reconstruction.json`) and its DSM
(`odm_dem/dsm.tif`) are used to project every ground cell into the photos, and each cell
takes its colour from the photo whose camera is horizontally closest (the most nadir view).
Pixel values are the camera's own, so the mosaic is suited to radiometric work.

    uv run hmc-gcp orthorect E:/hmc-data/neringa/surveys/2026-11-06-L1-smiltyne

The folder can be a survey folder (uses `<survey>/odm`) or an ODM project folder. Options:
`-o` output folder (default `<odm>/orthorect`), `--images` photo folder (default
`<odm>/images`), `--res` cell size in metres (default: the photos' median GSD),
`--margin` pixels ignored at photo edges (40), `--interp nearest|bilinear` (nearest).
The DSM must exist, so run ODM with `--dsm`.

Outputs, in the same CRS as the ODM products (UTM 34N):

| File | Contents |
| --- | --- |
| `mosaic.tif` | RGBA mosaic, alpha 0 where no photo covers the cell |
| `source.tif` | Photo index per cell (1-based, 0 = none); load in QGIS to see seamlines |
| `sources.csv` | Photo index, file name and camera position |
| `overlaps.csv` | Median brightness log-ratio of each overlapping photo pair, used by orthogain |

`orthogain` fixes per-photo brightness differences (exposure, sun, haze). It solves one
gain per photo and channel so that overlapping photos agree, with the mean gain fixed at 1,
and writes `mosaic_gain.tif` and `mosaic_gain_gains.csv`.

    uv run hmc-gcp orthogain E:/hmc-data/neringa/surveys/2026-11-07-L1-smiltyne
    uv run hmc-gcp orthogain E:/hmc-data/neringa/surveys/2026-11-07-L1-smiltyne --reference E:/hmc-data/neringa/surveys/2026-11-06-L1-smiltyne

With `--reference` (a mosaic .tif, or a survey/ODM/orthorect folder, which uses its
`mosaic_gain.tif` or else `mosaic.tif`), one extra overall gain per channel is applied so
that the median of the overlapping area matches the reference. Gain the first day first,
then use it as the reference for the second day. The reference must be in the same CRS.
Only a global gain is matched, so real changes between the surveys (new heavy-mineral
streaks, wet sand) do not leak into the correction, but the two mosaics must be roughly
co-registered (a 1–2 m shift is fine for a global median).

Tested on synthetic data: targets in the mosaic were within 1 px (≤2 cm) of their true
positions; random per-photo gains of ±14% dropped to 2–3% after orthogain, and a gained
mosaic matched its reference to within 0.1%.

### Gains on the ODM orthophoto: orthomatch

The orthorect mosaic switches photo along hard seams, which can cut thin features and
show up as edges to a neural network. `orthomatch` keeps the ODM orthophoto (its own
texture blending and detail) but gives it the large-scale brightness of the gain-balanced
mosaic of the same survey:

    uv run hmc-gcp orthomatch "<day>/odm/odm_orthophoto/odm_orthophoto.tif" --reference "<day>/odm/orthorect/mosaic_gain.tif"

Both are averaged on a coarse grid, and the per-channel ratio is smoothed over `--window`
metres (default 3) and multiplied into the ODM orthophoto. Brightness steps finer than the
window, such as ODM's own patch edges, stay; a smaller window follows the reference more
closely but also carries more of its seams. Output: `<target>_gain.tif` (or `-o`), tiled,
with overviews, in the target's grid. It works tile by tile, so it needs little memory.
Use the day's own mosaic as reference; if that mosaic was matched to the other day with
`orthogain --reference`, the ODM orthophoto gets that match too.

### Matching two days: colormatch

`colormatch` changes the colours of one orthophoto to match another of the same place,
e.g. a sunny day 1 to an overcast day 2:

    uv run hmc-gcp colormatch "day1 odm orthophoto.tif" --reference "day2 odm orthophoto.tif"

It fits one transform for the whole scene (the light is the same everywhere within a
flight; a local correction would also wipe out real changes between the days). Both are
averaged in 0.5 m cells (`--cell`), so small misalignments between the days do not matter,
and the fit uses robust weights, so areas that really changed (moved or wet sand, water)
are left out of it; the output says how much was used. Methods:

- `gain` (default): gain and offset per channel in linear light, i.e. brightness, colour
  balance and haze/contrast. A full colour matrix is not used: sand colours are so alike
  that its channel mixing is poorly determined and would distort other colours.
- `curves`: gain, then a tone curve per channel from the unchanged areas, to match
  contrast more closely.

Output: `<target>_match.tif` (or `-o`), tiled with overviews, in the target's grid. Shadows
from the sun cannot be removed by a colour transform.

### Sun shadows: shadows

`shadows` brightens cast sun shadows in an orthophoto (e.g. a sunny day with a low sun):

    uv run hmc-gcp shadows "<day>/odm/odm_orthophoto/odm_orthophoto.tif" --dsm "<day>/odm/odm_dem/dsm.tif" --photos "<day>/uav/ortho"

The sun position is computed for the photos' median capture time (camera clock in
Lithuanian winter time, `--utc-offset 2`; or give `--time` or `--sun AZ EL`). Shadows are
traced through the DSM toward the sun, which only marks candidates. The correction is
calibrated from the image itself: sand just inside the predicted shadows is mapped onto
sand just outside (per channel, so no camera tone curve is assumed). A pixel near a
predicted shadow is corrected to the degree that it is darker and bluer than nearby
sunlit sand in the way shadow is, so dark mineral laminae, wet sand and water are left
alone. Use it before `colormatch`, which should be fitted on the shadow-free image.

It works for shadows of objects the DSM resolves (logs, debris, scarps). Shadows of
vegetation and of the dune itself are only partly found, because the DSM captures them
poorly; `--search` (default 0.3 m) widens the area checked around predicted shadows.
Slope shading is not corrected. `--mask` also writes the predicted shadow mask.

## Quick preview in QGIS: worldfiles

`worldfiles` places the raw photos on the map from their metadata alone, for a rough
look before (or without) ODM:

    uv run hmc-gcp worldfiles E:/hmc-data/neringa/surveys/2026-11-06-L1-smiltyne/uav/ortho

It takes a folder of photos and writes one small `.vrt` file per photo to a `vrt`
subfolder of it.
Each points to the original JPG (which is not changed) and holds its position, scale,
rotation and CRS. In QGIS, create a group, then Layer > Add Layer > Add Raster Layer and
select all the `.vrt` files (or drag them in from the file browser).

The footprint comes from the GPS position, the height above take-off (`RelativeAltitude`),
the heading (EXIF, or a Litchi plan given with `--plan`), gimbal pitch and focal length, with the
ground flat at take-off level. Expect a few metres of error (DJI GPS and heading), so use it
for orientation only. Options: `-o` output folder, `--crs` (default EPSG:3346),
`--plan`, `--height-offset M` (add to the height above take-off, e.g.
negative if you took off from a dune), `--focal35`.

Phone photos work too, for example photos of the beach surface taken pointing down. They
have GPS but no height above ground, and usually no heading or tilt, so give the height
of the phone above the ground and, if you know it, the direction the top of the photo
points (`--heading`, degrees from north; default north, or the phone's compass direction
if it saved one). They are taken as looking straight down. Without a 35 mm focal length
in EXIF, 26 mm (a typical main phone camera) is assumed:

    uv run hmc-gcp worldfiles C:/photos/beach --height 1.5

Phone GPS is good to a few metres, more than the footprint of a photo taken from 1.5 m
(about 2 x 1.5 m), so treat the position as approximate.

`--no-overlap` moves photos north (map y) just enough that no two footprints overlap,
so photos taken close together (or with GPS scatter) can all be seen side by side. Photos
are placed from south to north; each moves only if it overlaps one already placed. This is
for small sets like phone photos: on an overlapping drone survey it would stack the photos
far to the north. Existing `.vrt` files are always overwritten.

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
5. **Review.** You accept, right-click to correct, or mark "not visible". Everything is saved to
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
| `--odm-dir` / `--no-odm` | ODM project folder to build on exit / do not build it |
| `--odm-cameras` | `cameras.json` to reuse in the printed ODM command |
| `--no-geo` | Do not write `geo.txt` to the ODM folder |
| `--odm-threads` | ODM `--max-concurrency` (default: logical processors minus 2) |
| `--show-rejected` | Start with rejected pairs shown when stepping (V toggles) |
| `--export-only` | Write the outputs from saved tags without opening the window |
| `--window 1500x950` | Starting window size; the window can be resized by dragging its edge, and the image area stretches with it |

## tag: keys

| Key | Action |
| --- | --- |
| Enter / A | Accept point, go to next unreviewed |
| X / Delete | Target not visible, go to next unreviewed |
| Right click | Place the point by hand |
| Shift + right click | Place by hand and accept |
| Left drag | Pan the view (resets when you move to another pair or switch detail/overview) |
| I J K L | Nudge point 1 px (Shift: 5 px) |
| Right / Left | Next / previous pair |
| N | Next unreviewed pair |
| Pair list click | Open that pair (list under the buttons; wheel or the bar on its right scrolls) |
| PgDn / PgUp | Next / previous GCP |
| U | Reset pair to unreviewed |
| R | Run detection again |
| O / Tab | Detail / overview of the search area |
| F | Fit the whole photo in the window (F again: back) |
| V | Show / hide rejected (not visible) pairs when stepping with Right/Left and PgDn/PgUp |
| + / - / wheel | Zoom (the wheel zooms at the mouse position). The title shows the magnification (screen px per photo px), limited to 40x on every photo |
| S | Save and export |
| H / F1 | Help |
| Q / Esc | Save, export and quit |

The pair list shows every pair not hidden (by number, GCP and photo, shortened to the part
after the last `-` or `_`, e.g. `L1G1  0273`; the title shows `L1G1 in 0273.JPG`), coloured by status, with the current pair highlighted. The GCP
name is also drawn next to the point, underlined, in the point's colour (green accepted or
detected, orange weak detection, cyan by hand); the title in the top left uses the same colour.

Colours: yellow ring = search area around the prediction, green box = detected target,
orange box = weak candidate without a centre dot (the point then starts at the prediction),
white cross = point to accept, green = accepted, cyan = placed by hand, purple = the other
GCPs from the GeoPackage projected into this photo (tagged ones in their status colour). The
purple marks show every GCP, also those with no pair in the queue, which helps find untagged
GCPs.

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
