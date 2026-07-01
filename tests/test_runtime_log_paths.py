"""Runtime log directory resolution contracts."""

from __future__ import annotations

from pathlib import Path

import pytest


def _basic_config() -> object:
    config_module = pytest.importorskip("unilabos.config.config")
    return config_module.BasicConfig


def test_http_reports_dir_uses_unilabos_working_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    http_module = pytest.importorskip("bioyond_peptide_station._vendored.workstation_http_service")
    basic_config = _basic_config()
    working_dir = tmp_path / "unilabos_data"
    monkeypatch.setattr(basic_config, "working_dir", str(working_dir), raising=False)

    assert http_module._http_reports_dir() == working_dir / "http_reports"


def test_http_reports_dir_falls_back_to_cwd_unilabos_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    http_module = pytest.importorskip("bioyond_peptide_station._vendored.workstation_http_service")
    basic_config = _basic_config()
    monkeypatch.setattr(basic_config, "working_dir", "", raising=False)
    monkeypatch.chdir(tmp_path)

    assert http_module._http_reports_dir() == tmp_path / "unilabos_data" / "http_reports"


def test_debug_log_default_uses_unilabos_working_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    station_module = pytest.importorskip("bioyond_peptide_station._vendored.station")
    basic_config = _basic_config()
    working_dir = tmp_path / "unilabos_data"
    monkeypatch.setattr(basic_config, "working_dir", str(working_dir), raising=False)
    workstation = object.__new__(station_module.BioyondWorkstation)
    workstation.bioyond_config = {}

    assert workstation._debug_log_resolved_dir() == working_dir / "api_logs"


def test_debug_log_legacy_unilabos_data_prefix_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    station_module = pytest.importorskip("bioyond_peptide_station._vendored.station")
    basic_config = _basic_config()
    working_dir = tmp_path / "unilabos_data"
    monkeypatch.setattr(basic_config, "working_dir", str(working_dir), raising=False)
    workstation = object.__new__(station_module.BioyondWorkstation)
    workstation.bioyond_config = {"debug_log_dir": "unilabos_data/api_logs"}

    assert workstation._debug_log_resolved_dir() == working_dir / "api_logs"
