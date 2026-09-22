from __future__ import annotations

import io
from datetime import date

from PIL import Image

from mushroom_alerts.base import SourceArtifact
from mushroom_alerts.fetch_chmi_map import BBOX, PALETTE
from mushroom_alerts.mapping import heatmap
from mushroom_alerts.store import Store


def _png(levels: list[int | None], *, indices: list[int]) -> bytes:
    """Indexed PNG with arbitrary palette indices, including transparency."""
    image = Image.new("P", (len(levels), 1), 0)
    palette = [0] * 768
    palette[0:3] = [255, 255, 255]
    level_to_index = dict(zip(range(1, 6), indices))
    for rgb, level in PALETTE.items():
        index = level_to_index[level]
        palette[index * 3 : index * 3 + 3] = list(rgb)
    image.putpalette(palette)
    image.putdata([0 if level is None else level_to_index[level] for level in levels])
    image.info["transparency"] = 0
    out = io.BytesIO()
    image.save(out, format="PNG", transparency=0)
    return out.getvalue()


def _artifact(day: date, data: bytes) -> SourceArtifact:
    return SourceArtifact(
        "chmi_map",
        "growth_raster",
        day,
        "image/png",
        data,
        {"bbox": list(BBOX), "projection": "EPSG:3857", "size": [5, 1]},
    )


def test_masks_classify_by_rgb_not_palette_index():
    data = _png([1, 3, 4, 5, None], indices=[11, 7, 23, 3, 19])
    favourable, valid = heatmap._indexed_masks(data)
    assert list(favourable.get_flattened_data()) == [0, 0, 1, 1, 0]
    assert list(valid.get_flattened_data()) == [255, 255, 255, 255, 0]


def test_panel_uses_latest_21_calendar_days_and_reports_coverage(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_artifacts(
            [
                _artifact(date(2026, 9, 1), _png([5], indices=[1, 2, 3, 4, 5])),
                _artifact(date(2026, 9, 2), _png([4], indices=[5, 4, 3, 2, 1])),
                _artifact(date(2026, 9, 22), _png([3], indices=[8, 9, 10, 11, 12])),
            ]
        )
        panel = heatmap.build_heatmap_panel(store)

    assert panel is not None
    assert panel.start_date == date(2026, 9, 2)
    assert panel.latest_date == date(2026, 9, 22)
    assert panel.available_days == 2
    assert panel.window_days == 21
    assert panel.image.size == (1200, 750)


def test_panel_returns_none_for_an_empty_archive(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        assert heatmap.build_heatmap_panel(store) is None
