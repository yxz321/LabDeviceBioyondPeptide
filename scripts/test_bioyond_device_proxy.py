"""Bioyond peptide device proxy offline tests."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

MODULE_PATH = Path("unilabos/devices/workstation/bioyond_studio/peptide_station/bioyond_device_proxy.py")
FIXTURE_PATH = Path("temp_benyao/peptide/_input/api_lims_device_list_operations_2026-06-05_172_20_23_145_44388.json")
CONFIG_PATH = Path("temp_benyao/peptide/peptide_station_config.json")
GRAPH_PATH = Path("temp_benyao/peptide/peptide_station_graph.with_bioyond_devices.json")
ORIGINAL_GRAPH_PATH = Path("temp_benyao/peptide/peptide_station_graph.json")


def _import_module() -> Any:
    import importlib

    return importlib.import_module("bioyond_peptide_station.bioyond_device_proxy")


def test_operation_fixture_contract() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    devices = data.get("data") or []
    assert data.get("code") == 1
    assert len(devices) == 31
    assert sum(len(device.get("operations") or []) for device in devices) == 115
    assert sum(len(operation.get("parameters") or []) for device in devices for operation in device.get("operations") or []) == 71
    fridge = next(device for device in devices if device.get("deviceName") == "冰箱")
    assert [operation.get("description") for operation in fridge.get("operations") or []][:2] == ["开门", "关门"]


def test_proxy_module_declares_expected_device_ids_and_actions() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    device_ids = set()
    action_methods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "device":
                    for keyword in decorator.keywords:
                        if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                            device_ids.add(keyword.value.value)
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    if any(isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "action" for decorator in item.decorator_list):
                        action_methods.add((node.name, item.name))
    assert "bioyond_proxy_peptide_fridge" in device_ids
    assert "bioyond_proxy_peptide_turntable" in device_ids
    assert "bioyond_proxy_peptide_nitrogen_blow" in device_ids
    assert len(device_ids) == 18
    assert ("BioyondFridgeProxy", "open_door") in action_methods
    assert ("BioyondFridgeProxy", "close_door") in action_methods
    assert ("BioyondTurntableProxy", "rotate_to_absolute_angle") in action_methods


def _make_proxy(proxy_cls: Any, device_name: str) -> Any:
    return proxy_cls(
        config_path=str(CONFIG_PATH),
        operation_snapshot_path=str(FIXTURE_PATH),
        bioyond_device_name=device_name,
    )


def test_proxy_init_tolerates_operation_snapshot_generation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    config_path = tmp_path / "bioyond_config.json"
    config_path.write_text(json.dumps({"api_host": "http://bioyond.example", "api_key": "test-key"}), encoding="utf-8")
    missing_snapshot_path = tmp_path / "missing_device_list_snapshot.json"
    post_mock = MagicMock(side_effect=RuntimeError("device-list unavailable"))
    monkeypatch.setattr(module.requests, "post", post_mock)

    proxy = module.BioyondFridgeProxy(
        config_path=str(config_path),
        operation_snapshot_path=str(missing_snapshot_path),
        bioyond_device_name="冰箱",
    )

    assert proxy.operation_snapshot is None
    assert proxy._fixture_device is None
    assert not missing_snapshot_path.exists()
    assert post_mock.call_count == 1


def test_missing_snapshot_is_generated_during_init_and_reused_for_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    config_path = tmp_path / "bioyond_config.json"
    config_path.write_text(json.dumps({"api_host": "http://bioyond.example", "api_key": "test-key"}), encoding="utf-8")
    missing_snapshot_path = tmp_path / "missing_device_list_snapshot.json"
    captured: Dict[str, Any] = {"urls": []}

    def fake_post(url: str, data: str, headers: Dict[str, str], timeout: int) -> Any:
        captured["urls"].append(url)
        request_body = json.loads(data)
        assert request_body["apiKey"] == "test-key"
        if url.endswith("/api/lims/device/device-list"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "code": 1,
                    "data": [
                        {
                            "deviceName": "冰箱",
                            "operations": [{"index": 2, "description": "关门", "cmd": "CloseDoor", "parameters": []}],
                        }
                    ],
                },
            )
        captured["submitted_operation"] = request_body["data"]
        return SimpleNamespace(status_code=200, json=lambda: {"code": 1, "message": "ok"})

    monkeypatch.setattr(module.requests, "post", fake_post)
    proxy = module.BioyondFridgeProxy(
        config_path=str(config_path),
        operation_snapshot_path=str(missing_snapshot_path),
        bioyond_device_name="冰箱",
    )

    result = proxy.close_door()

    assert result["success"] is True
    assert missing_snapshot_path.exists()
    assert json.loads(missing_snapshot_path.read_text(encoding="utf-8"))["data"][0]["deviceName"] == "冰箱"
    assert captured["urls"] == [
        "http://bioyond.example/api/lims/device/device-list",
        "http://bioyond.example/api/lims/device/execute-operation",
    ]
    assert captured["submitted_operation"]["description"] == "关门"


def test_fridge_close_builds_execute_operation_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    proxy = _make_proxy(module.BioyondFridgeProxy, "冰箱")
    captured: Dict[str, Any] = {}

    def fake_post(url: str, data: str, headers: Dict[str, str], timeout: int) -> Any:
        captured.update({"url": url, "data": json.loads(data), "headers": headers, "timeout": timeout})
        return SimpleNamespace(status_code=200, json=lambda: {"code": 1, "message": "ok", "data": {"done": True}})

    monkeypatch.setattr(module.requests, "post", fake_post)
    result = proxy.close_door()

    assert result["success"] is True
    assert captured["url"].endswith("/api/lims/device/execute-operation")
    assert captured["data"]["apiKey"] == proxy.api_key
    operation = captured["data"]["data"]
    assert operation["index"] == 2
    assert operation["description"] == "关门"
    assert operation["parameters"] == []
    assert result["submitted_operation"]["description"] == "关门"


def test_parameterized_operation_sets_value_without_mutating_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    proxy = _make_proxy(module.BioyondPlateReaderProxy, "酶标仪")
    before = proxy._resolve_operation_template("运行协议")
    captured: Dict[str, Any] = {}

    def fake_post(url: str, data: str, headers: Dict[str, str], timeout: int) -> Any:
        captured["operation"] = json.loads(data)["data"]
        return SimpleNamespace(status_code=200, json=lambda: {"code": 1})

    monkeypatch.setattr(module.requests, "post", fake_post)
    proxy.run_protocol(protocol="read.prt", timeout=300)
    after = proxy._resolve_operation_template("运行协议")

    values = {parameter["name"]: parameter.get("value") for parameter in captured["operation"]["parameters"]}
    assert values == {"protocol": "read.prt", "timeout": 300}
    assert before == after
    assert all(parameter.get("value") is None for parameter in after["parameters"])


def test_enum_mapping_and_range_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    nitrogen = _make_proxy(module.BioyondNitrogenBlowProxy, "双位氮吹仪1")
    turntable = _make_proxy(module.BioyondTurntableProxy, "转台1")
    captured: Dict[str, Any] = {}

    def fake_post(url: str, data: str, headers: Dict[str, str], timeout: int) -> Any:
        captured["operation"] = json.loads(data)["data"]
        return SimpleNamespace(status_code=200, json=lambda: {"code": 1})

    monkeypatch.setattr(module.requests, "post", fake_post)
    nitrogen.stop_nitrogen_blow(channelNo="全部通道")
    assert captured["operation"]["parameters"][0]["value"] == 3
    turntable.rotate_to_absolute_angle(angle=-360)
    turntable.rotate_to_absolute_angle(angle=360)
    with pytest.raises(ValueError, match="angle"):
        turntable.rotate_to_absolute_angle(angle=361)


def test_envelope_code_zero_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    proxy = _make_proxy(module.BioyondFridgeProxy, "冰箱")
    monkeypatch.setattr(
        module.requests,
        "post",
        MagicMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"code": 0, "message": "RPC failed"})),
    )
    result = proxy.open_door()
    assert result["success"] is False
    assert result["code"] == 0
    assert result["message"] == "RPC failed"


@pytest.mark.skipif(not GRAPH_PATH.exists(), reason="fridge-only proxy graph copy not created in this sandbox")
def test_fridge_only_graph_copy_shape() -> None:
    original = json.loads(ORIGINAL_GRAPH_PATH.read_text(encoding="utf-8"))
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    assert len(graph["nodes"]) == len(original["nodes"]) + 1
    assert graph.get("edges", []) == original.get("edges", [])
    node = next(item for item in graph["nodes"] if item["id"] == "bioyond_proxy_peptide_fridge")
    assert node["class"] == "bioyond_proxy_peptide_fridge"
    assert node["parent"] == "bioyond_peptide_station"
    assert node["config"]["config_path"] == "temp_benyao/peptide/peptide_station_config.json"
    assert node["config"]["bioyond_device_name"] == "冰箱"
    station = next(item for item in graph["nodes"] if item["id"] == "bioyond_peptide_station")
    assert "bioyond_proxy_peptide_fridge" in station["children"]
