"""``python -m fake_hue`` argument handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_hue.__main__ import main


def test_mint_certs_writes_a_ca_and_prints_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["mint-certs", "--bridge-id", "ECB5FAFFFE334703", "--out", str(tmp_path)]
    )

    assert exit_code == 0
    assert (tmp_path / "ca.pem").is_file()
    assert capsys.readouterr().out.strip() == str(tmp_path / "ca.pem")


def test_serving_without_a_bridge_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="bridge-id"):
        main(["--application-key", "k", "--cert-dir", str(tmp_path)])


def test_serving_without_an_application_key_is_refused() -> None:
    with pytest.raises(SystemExit, match="Application Key"):
        main(["--bridge-id", "ECB5FAFFFE334703"])
