"""Generates from the real Hue spec and checks protoc accepts the result."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from protogen.__main__ import main
from protogen.convert import convert_document
from protogen.spec import load_spec

REPO_ROOT = Path(__file__).parents[2]
SPEC = REPO_ROOT / "openapi.yaml"

pytestmark = pytest.mark.skipif(not SPEC.exists(), reason="openapi.yaml not present")


def test_every_light_put_field_is_optional() -> None:
    """LightPut declares no `required` array, so nothing may lose presence.

    This is the property the whole generator exists for: an omitted field
    must not be indistinguishable from an explicit zero or false.
    """
    proto = convert_document(load_spec(SPEC), ["LightPut"], package="hue.v1")

    light_put = next(m for m in proto.messages if m.name == "LightPut")
    non_optional = [
        f.name for f in light_put.fields if not f.optional and not f.repeated
    ]

    assert non_optional == []


def test_required_fields_of_resource_identifier_are_not_optional() -> None:
    """The counterpart: `required` must actually suppress `optional`."""
    proto = convert_document(load_spec(SPEC), ["ResourceIdentifier"], package="hue.v1")

    message = next(m for m in proto.messages if m.name == "ResourceIdentifier")
    by_name = {f.name: f for f in message.fields}

    assert by_name["rid"].optional is False
    assert by_name["rtype"].optional is False


def test_cli_writes_a_file(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "lighting.proto"

    assert main(["--spec", str(SPEC), "--out", str(out), "--root", "LightPut"]) == 0
    assert "message LightPut {" in out.read_text()


@pytest.mark.skipif(shutil.which("protoc") is None, reason="protoc not available")
def test_protoc_accepts_the_generated_file(tmp_path: Path) -> None:
    """The acceptance criterion for the generator: valid protobuf."""
    out = tmp_path / "lighting.proto"
    main(
        [
            "--spec",
            str(SPEC),
            "--out",
            str(out),
            "--root",
            "LightGet",
            "--root",
            "LightPut",
            "--root",
            "ResourceIdentifier",
        ]
    )

    result = subprocess.run(
        [
            "protoc",
            f"--proto_path={tmp_path}",
            f"--descriptor_set_out={tmp_path / 'd.bin'}",
            str(out),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
