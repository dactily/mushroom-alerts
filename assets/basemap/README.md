# The basemap

`vsetinsko.png` is the map the daily forecast image is drawn on: a
simplified picture of the area the 13 watched locations sit in -- forest,
settlements, water, the four road classes worth showing, and sixteen place
names for orientation. `vsetinsko.json` says what it shows and where, so a
marker can be put on it to the pixel.

It is a *background*, not a wallpaper. The first version pushed "quiet" to
the point of saying nothing: forest `#C9DABB` on `#F6F4EE`, two tones 30
luminance apart, rivers as 1.5 px hairlines and roads as flat grey. At 1:1
it was pale; at the 390 px width Telegram previews a photo at it was one
green smear you could not find Vsetín in. The palette below is the answer
to that -- see [Reading it at 390 px](#reading-it-at-390-px).

**Both files are derived artefacts, and both are committed on purpose.**
The daily render must work on the server with no tile server, no network
and no browser: it opens this PNG, draws markers on a copy of it, and
sends the result. Rebuilding is a manual, occasional act -- OSM does not
move Radhošť overnight.

|              |                                                              |
| ------------ | ------------------------------------------------------------ |
| size         | 1200 x 1000 px (the map viewport; the Telegram image is taller) |
| projection   | Web Mercator, EPSG:3857 -- what every slippy map uses         |
| longitude    | 17.749 .. 18.449 (centre 18.0990, span 0.70°)                 |
| latitude     | 49.16906695848169 .. 49.54899925170674 (centre 49.3594)       |
| ground       | ~50.8 x 42.3 km, ~0.0423 km per pixel                         |
| data         | OpenStreetMap via the public Overpass API                     |
| licence      | ODbL -- see below                                             |

The latitudes are not a guess: the longitudes and the centre are fixed by
hand and the latitudes are solved for, so that

    (mercator_y(max_lat) - mercator_y(min_lat)) / (mercator_x(max_lon) - mercator_x(min_lon)) == 1000 / 1200

The picture is therefore geometrically undistorted -- a kilometre is the
same number of pixels whichever way it runs -- and every watched location
lands at least 2 km inside the frame (the tightest are Bumbálka in the
east and Rajnochovice in the west, both 2.8 km). `tests/test_basemap.py`
asserts all of that against the committed files.

## Rebuilding

```sh
.venv/bin/python tools/build_basemap.py               # refetch if the cache is cold, redraw, rewrite both files
.venv/bin/python tools/build_basemap.py --refresh     # force fresh Overpass answers
.venv/bin/python tools/build_basemap.py --no-streams  # rivers only, no waterway=stream at all
.venv/bin/python tools/build_basemap.py --colors 0    # keep the PNG in RGB (1.6 MB; too big to commit)
```

Overpass answers are cached in `~/.cache/mushroom-alerts/basemap`
(`--cache-dir` to move it, `--no-cache` to skip it). The cache key is the
query text, so editing one query refetches that layer and leaves the others
alone. With a warm cache the build needs no network at all and takes a
second or two. It is deterministic: the same cached answers produce a
**byte-identical** PNG, so a rerun that changes nothing leaves an empty
diff.

A cold build fetches about 70 MB across the five queries (the forest one
alone is 41 MB) and takes a couple of minutes, much of it waiting -- the
public Overpass instance grants two slots per IP and answers `429` when they
are busy, so the tool pauses 5 s between queries and retries once after
30 s.

The tool is a **one-off**. Nothing on the daily path may import it: the
daily path only ever calls `mushroom_alerts.mapping.basemap.load_basemap()`,
which reads these two files and touches nothing else.

## What is drawn, in this order

| layer       | tags                                                              | how                                                                     |
| ----------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------- |
| forest      | `landuse=forest`, `natural=wood` (ways **and** relations)          | green `#96B484` fill, inner rings punched out                            |
| settlements | `landuse=residential\|industrial` (ways and relations)             | warm grey-tan `#D5C3A5`, industrial a shade greyer `#CBC3B6`             |
| water       | `natural=water` (ways and relations), `waterway=river\|stream`     | lakes `#92C1DC` rimmed `#488AB6`; rivers 3 px `#488AB6`, streams 1.5 px `#85B4D4` |
| roads       | `highway=motorway\|trunk\|primary\|secondary`                      | casing under fill; trunk orange, primary sand, secondary white           |
| places      | `place=city\|town\|village`                                        | a dot plus the name, dark grey `#37342F` in a white halo, 16 at most     |

The counts of what each layer actually drew are in `vsetinsko.json` under
`layers` -- 8 695 forest rings and holes, 3 576 settlement ones, 44 rivers
and 1 958 streams, 1 658 roads, 16 labels.

Multipolygon **relations are included**: each relation's member ways are
stitched into rings by matching endpoints (a big forest's outer ring is
usually split across several ways), outer rings are filled and inner rings
become holes -- which is why the forests are freckled with clearings and
villages rather than being one flat blanket.

**Streams are back, filtered.** `waterway=stream` was off in the first
version because all 10 856 of them were lint at 40 m per pixel. They are
also the region's main orientation cue: every valley here is a brook with a
road and a string of houses along it. So the builder keeps the ones that
are at least `STREAM_MIN_KM` (1.5 km) long and throws the rest away --
8 898 dropped, 1 958 kept. Length is measured **over the whole watercourse
of one name, not per way**: OSM splits one brook into a dozen ways wherever
a bridge or a landuse boundary crosses it, and a per-way filter would draw
the long ones full of holes. An unnamed way answers for itself.

**Roads are drawn casing-then-fill** -- a dark thin outline under a lighter
core -- which is what makes a 3 px road legible over forest. Every casing
goes down before the first fill rather than each road being finished in
turn: otherwise a trunk's dark casing would be stamped across the pale core
of every secondary it crosses. Widths in finished pixels: trunk and
motorway 6.5 with a 4 px core, primary 4.5/2.5, secondary 2.5/1.5.

Place labels are for orientation only, and there is a ceiling of 16 --
past that the names start hiding the valleys they were meant to help find.
Valašské Meziříčí, Vsetín and Rožnov pod Radhoštěm are always labelled;
after them it is city before town before village and, within a rank, the
bigger population, so the cap drops hamlets and keeps places a reader has
heard of. Any candidate is skipped if it collides with a label already
placed, runs off the edge, or lands on the attribution box. Towns get 15 px
type and a 6 px dot, villages 11.5 px and a 4 px dot.

Everything is drawn at 2x and downsampled with Lanczos -- Pillow does not
anti-alias -- and the result is quantised to a **256**-colour palette. The
flat first map could afford 128 (pixel-identical to the RGB original); this
one cannot. Measured against `--colors 0`: at 128 colours 2.7 % of the map
moves by more than 16 levels and the darkest label ink by 109, visibly
thinning the type; at 256 that tail is 0.7 %, the worst pixel moves by 77
and the type is indistinguishable from the RGB original. The extra palette
costs 84 KB (653 against 569); full RGB would be 1 688 KB.

## Reading it at 390 px

Telegram shows a photo in the timeline about 390 px wide, a third of the
map's own width, and that is where most people will ever see it. Two
consequences run through the palette above:

* **anything that must survive has to be saturated or cased.** A 1 px line
  at 390 px is a tint, not a line, so the rivers are a real blue rather
  than a pale one and every road carries a dark casing. Flat mid-greys of
  the kind the first version used vanish completely.
* **the map may be much louder than it looks like it should be.** The
  markers on top are 34 px dots with 52 px bold text in a white halo; they
  win regardless. The forest therefore sits at luminance 166 instead of
  209 -- dark enough that the open valleys, which stay near-white, read as
  the shapes they are. `tests/test_basemap.py` pins the limit from the
  other side: the map's mean tone must stay above the *lightest* colour a
  marker can be, and next to none of it may be as dark as the darkest.

## The queries

Fetched for `bbox = (49.16, 17.74, 49.56, 18.46)`, slightly wider than the
drawn box so nothing is cut mid-polygon. `out geom;` makes every way and
every relation member carry its own coordinates, so one request per layer
is enough. The exact strings are also stored in `vsetinsko.json` under
`overpass_queries`.

```
[out:json][timeout:180];
(
  way["landuse"="forest"](49.16,17.74,49.56,18.46);
  way["natural"="wood"](49.16,17.74,49.56,18.46);
  relation["landuse"="forest"](49.16,17.74,49.56,18.46);
  relation["natural"="wood"](49.16,17.74,49.56,18.46);
);
out geom;
```

```
[out:json][timeout:180];
(
  way["landuse"~"^(residential|industrial)$"](49.16,17.74,49.56,18.46);
  relation["landuse"~"^(residential|industrial)$"](49.16,17.74,49.56,18.46);
);
out geom;
```

```
[out:json][timeout:180];
(
  way["natural"="water"](49.16,17.74,49.56,18.46);
  relation["natural"="water"](49.16,17.74,49.56,18.46);
  way["waterway"="river"](49.16,17.74,49.56,18.46);
  way["waterway"="stream"](49.16,17.74,49.56,18.46);
);
out geom;
```

```
[out:json][timeout:180];
way["highway"~"^(motorway|trunk|primary|secondary)$"](49.16,17.74,49.56,18.46);
out geom;
```

```
[out:json][timeout:180];
node["place"~"^(city|town|village)$"](49.16,17.74,49.56,18.46);
out geom;
```

## Licence and attribution

The data is © OpenStreetMap contributors and licensed under the **Open
Database Licence (ODbL)**: <https://www.openstreetmap.org/copyright>.

This PNG is a *produced work* under the ODbL -- a picture made from the
database, not a copy of it -- so it may be published freely as long as the
source is credited. The credit is therefore **baked into the image**, in
small grey type in the bottom-right corner:

> © OpenStreetMap contributors

Whoever composes the final Telegram image does not need to add it again
(and should not): it is already there, it cannot be cropped off by
accident, and `Basemap.attribution` carries the same string for anyone who
wants to print it elsewhere. Keep it legible; do not cover that corner
with markers or a legend.

## The sidecar

`vsetinsko.json` is what `load_basemap()` reads:

* `width`/`height`, `projection`, `bbox` -- the projection contract;
* `image` -- the PNG next to it;
* `attribution`, `source`, `built_at` -- provenance for the render and for
  whoever asks where the map came from;
* `layers`, `labels`, `overpass_queries`, `fetch_bbox`, `supersample`,
  `palette_colors` -- how this particular build was made, so a future one
  can be compared with it.

Editing it by hand is a mistake: the numbers in it and the pixels in the
PNG have to agree, and only the tool makes both.

## Fonts

`assets/fonts/DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` (with
`LICENSE-DejaVu.txt`) are vendored next to the map and exposed as
`FONT_PATH`/`FONT_BOLD_PATH`. They cover Czech and Cyrillic, which the
labels here and the Russian text of the report both need, and vendoring
them means the render looks the same on the laptop as on the server
whatever fonts the host happens to have installed.
