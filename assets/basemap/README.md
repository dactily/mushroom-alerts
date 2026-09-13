# The basemap

`vsetinsko.png` is the map the daily forecast image is drawn on: a
simplified, deliberately quiet picture of the area the 13 watched
locations sit in -- forests, water, the four road classes worth showing,
and three towns for orientation. `vsetinsko.json` says what it shows and
where, so a marker can be put on it to the pixel.

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
.venv/bin/python tools/build_basemap.py             # refetch if the cache is cold, redraw, rewrite both files
.venv/bin/python tools/build_basemap.py --refresh   # force fresh Overpass answers
.venv/bin/python tools/build_basemap.py --streams   # also draw waterway=stream (noisy at this scale)
```

Overpass answers are cached in `~/.cache/mushroom-alerts/basemap`
(`--cache-dir` to move it, `--no-cache` to skip it). With a warm cache the
build needs no network at all and takes a second or two. It is
deterministic: the same cached answers produce a **byte-identical** PNG,
so a rerun that changes nothing leaves an empty diff.

A cold build fetches about 45 MB across the four queries (the forest one
alone is 41 MB and 13 s) and takes roughly a minute, much of it waiting --
the public Overpass instance grants two slots per IP and answers `429`
when they are busy, so the tool pauses between queries and retries once.

The tool is a **one-off**. Nothing on the daily path may import it: the
daily path only ever calls `mushroom_alerts.mapping.basemap.load_basemap()`,
which reads these two files and touches nothing else.

## What is drawn, in this order

| layer   | tags                                                            | how                                                 |
| ------- | --------------------------------------------------------------- | --------------------------------------------------- |
| forest  | `landuse=forest`, `natural=wood` (ways **and** relations)         | muted green `#C9DABB` fill, inner rings punched out  |
| water   | `natural=water` (ways and relations), `waterway=river`            | soft blue `#B1D0E3`, rivers as 1.5 px lines          |
| roads   | `highway=motorway\|trunk\|primary\|secondary`                     | grey lines, motorway/trunk thicker                   |
| places  | `place=town\|city`                                                | a dot plus the name, dark grey with a light halo     |

Multipolygon **relations are included**: each relation's member ways are
stitched into rings by matching endpoints (a big forest's outer ring is
usually split across several ways), outer rings are filled and inner rings
become holes -- which is why the forests are freckled with clearings and
villages rather than being one flat blanket.

Place labels are for orientation only. Valašské Meziříčí, Vsetín and
Rožnov pod Radhoštěm are always labelled; any other town inside the box
gets a label only if it neither collides with one already placed nor runs
off the edge (Zubří and Vizovice make it; Frenštát pod Radhoštěm sits on
the top border and is dropped).

Everything is drawn at 2x and downsampled with Lanczos -- Pillow does not
anti-alias -- and the result is quantised to a 128-colour palette, which
is visually indistinguishable from the RGB original (a flat-colour map)
and less than half the size.

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
  way["natural"="water"](49.16,17.74,49.56,18.46);
  relation["natural"="water"](49.16,17.74,49.56,18.46);
  way["waterway"="river"](49.16,17.74,49.56,18.46);
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
node["place"~"^(city|town)$"](49.16,17.74,49.56,18.46);
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
