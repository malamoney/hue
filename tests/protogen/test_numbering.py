"""Tests for stable protobuf field numbering across spec revisions."""

from __future__ import annotations

import json
from pathlib import Path

from protogen.numbering import FieldNumbers


def test_new_fields_are_numbered_from_one() -> None:
    numbers = FieldNumbers()

    assert numbers.assign("LightPut", "on") == 1
    assert numbers.assign("LightPut", "dimming") == 2


def test_numbering_restarts_for_each_message() -> None:
    numbers = FieldNumbers()
    numbers.assign("LightPut", "on")

    assert numbers.assign("LightGet", "id") == 1


def test_an_existing_assignment_is_reused() -> None:
    numbers = FieldNumbers({"LightPut.mode": 11})

    assert numbers.assign("LightPut", "mode") == 11


def test_inserting_a_field_does_not_renumber_existing_ones() -> None:
    """The reason this module exists.

    A property added upstream must not shift the numbers of fields already
    deployed, or old clients decode new bytes as an unrelated field.
    """
    numbers = FieldNumbers({"LightPut.on": 1, "LightPut.mode": 2})

    inserted = numbers.assign("LightPut", "brightness")

    assert inserted == 3
    assert numbers.assign("LightPut", "mode") == 2


def test_a_removed_field_number_is_never_reused() -> None:
    """Reusing a retired number would make old and new clients disagree."""
    numbers = FieldNumbers({"LightPut.removed": 1, "LightPut.kept": 2})

    # `removed` is never assigned again, so it is absent from this generation.
    assert numbers.assign("LightPut", "kept") == 2
    assert numbers.assign("LightPut", "added") == 3


def test_nested_messages_are_scoped_separately() -> None:
    numbers = FieldNumbers()
    numbers.assign("LightGet.Powerup", "preset")

    assert numbers.assign("LightGet", "preset") == 1


def test_round_trips_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "field-numbers.json"
    numbers = FieldNumbers()
    numbers.assign("LightPut", "on")
    numbers.save(path)

    reloaded = FieldNumbers.load(path)

    assert reloaded.assign("LightPut", "on") == 1
    assert json.loads(path.read_text()) == {"LightPut.on": 1}


def test_load_of_a_missing_file_starts_empty(tmp_path: Path) -> None:
    numbers = FieldNumbers.load(tmp_path / "absent.json")

    assert numbers.assign("M", "a") == 1
