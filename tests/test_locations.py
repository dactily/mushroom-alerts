from __future__ import annotations

from pathlib import Path

import pytest

from mushroom_alerts import locations as L

SHIPPED = Path(__file__).resolve().parent.parent / "locations.yaml"


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


def test_shipped_file_has_the_two_plan_locations():
    locs = L.load_locations(SHIPPED)
    assert [l.slug for l in locs] == ["valmez", "valasska-bystrice"]
    assert (locs[0].lat, locs[0].lon) == (49.4718, 17.9711)
    assert (locs[1].lat, locs[1].lon) == (49.416, 18.106)


def test_load_missing_file(tmp_path):
    assert L.load_locations(tmp_path / "nope.yaml") == []


def test_roundtrip_preserves_order_and_explicit_slug(tmp_path):
    path = tmp_path / "locations.yaml"
    L.save_locations(L.load_locations(SHIPPED), path)
    again = L.load_locations(path)
    assert [x.slug for x in again] == ["valmez", "valasska-bystrice"]
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
    assert [x.slug for x in L.load_locations(path)] == ["valmez", "valasska-bystrice"]

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
