from __future__ import annotations

from pathlib import Path

import pytest

from mushroom_alerts import locations as L

SHIPPED = Path(__file__).resolve().parent.parent / "locations.yaml"
SHIPPED_SLUGS = [
    "valmez",
    "jablunka",
    "valasska-bystrice",
    "maruska",
    "benesky",
    "kudlacena",
    "kohutka",
    "lidecko-lacnov",
    "bumbalka",
]
SHIPPED_SHORTS = [
    "Valmez",
    "Jablůnka",
    "Bystřice",
    "Maruška",
    "Benešky",
    "Kudlačena",
    "Kohútka",
    "Lidečko",
    "Bumbálka",
]


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Valašská Bystřice", "valasska-bystrice"),
        ("Valašské Meziříčí", "valasske-mezirici"),
        ("Rožnov pod Radhoštěm", "roznov-pod-radhostem"),
        ("  Kelč  ", "kelc"),
        ("Ústí nad Labem", "usti-nad-labem"),
        ("!!!", "location"),
    ],
)
def test_slugify(name, slug):
    assert L.slugify(name) == slug


def test_shipped_file_has_the_watched_locations_in_report_order():
    locs = L.load_locations(SHIPPED)
    assert [l.slug for l in locs] == SHIPPED_SLUGS
    assert (locs[0].lat, locs[0].lon) == (49.4718, 17.9711)
    assert (locs[2].lat, locs[2].lon) == (49.416, 18.106)
    assert [l.short_name for l in locs] == SHIPPED_SHORTS


def test_a_short_name_is_explicit_and_never_guessed(tmp_path):
    """Truncating to the last word would print "Hostýnem" (PLAN §9f)."""
    path = tmp_path / "l.yaml"
    path.write_text(
        "- {name: Bystřice pod Hostýnem, lat: 49.3994, lon: 17.6742}\n"
        "- {name: Valašské Meziříčí, short: Valmez, lat: 49.4718, lon: 17.9711}\n",
        encoding="utf-8",
    )
    locs = L.load_locations(path)
    assert locs[0].short == "" and locs[0].short_name == "Bystřice pod Hostýnem"
    assert locs[1].short == "Valmez" and locs[1].short_name == "Valmez"


def test_a_short_name_survives_a_rewrite(tmp_path):
    path = tmp_path / "l.yaml"
    L.save_locations(L.load_locations(SHIPPED), path)
    assert "short: Valmez" in path.read_text(encoding="utf-8")
    assert [l.short for l in L.load_locations(path)] == SHIPPED_SHORTS


def test_load_missing_file(tmp_path):
    assert L.load_locations(tmp_path / "nope.yaml") == []


def test_roundtrip_preserves_order_and_explicit_slug(tmp_path):
    path = tmp_path / "locations.yaml"
    L.save_locations(L.load_locations(SHIPPED), path)
    again = L.load_locations(path)
    assert [x.slug for x in again] == SHIPPED_SLUGS
    assert "slug: valmez" in path.read_text(encoding="utf-8")


def test_add_and_remove(tmp_path):
    path = tmp_path / "locations.yaml"
    L.save_locations(L.load_locations(SHIPPED), path)
    new = L.add_location("Rožnov pod Radhoštěm", 49.4586, 18.1436, path)
    assert new.slug == "roznov-pod-radhostem"
    assert [x.slug for x in L.load_locations(path)][-1] == new.slug

    with pytest.raises(ValueError):
        L.add_location("Rožnov pod Radhoštěm", 49.0, 18.0, path)

    L.remove_location("roznov-pod-radhostem", path)
    assert [x.slug for x in L.load_locations(path)] == SHIPPED_SLUGS

    with pytest.raises(ValueError):
        L.remove_location("nowhere", path)


def test_find_location_by_name_slug_or_accents():
    locs = L.load_locations(SHIPPED)
    assert L.find_location(locs, "valmez").slug == "valmez"
    assert L.find_location(locs, "Valašská Bystřice").slug == "valasska-bystrice"
    assert L.find_location(locs, "valasska bystrice").slug == "valasska-bystrice"
    assert L.find_location(locs, "VALMEZ").slug == "valmez"
    assert L.find_location(locs, "Brno") is None


def test_duplicate_slugs_are_disambiguated(tmp_path):
    path = tmp_path / "l.yaml"
    path.write_text(
        "- {name: Kelč, lat: 49.5, lon: 17.8}\n- {name: Kelc, lat: 49.6, lon: 17.9}\n",
        encoding="utf-8",
    )
    assert [x.slug for x in L.load_locations(path)] == ["kelc", "kelc-2"]


@pytest.mark.parametrize(
    "body",
    [
        "- {lat: 49.5, lon: 17.8}\n",
        "- {name: X, lat: nonsense, lon: 17.8}\n",
        "- {name: X, lat: 149.5, lon: 17.8}\n",
    ],
)
def test_bad_entries_raise(tmp_path, body):
    path = tmp_path / "l.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        L.load_locations(path)


def test_locations_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(tmp_path / "custom.yaml"))
    assert L.locations_path() == tmp_path / "custom.yaml"
