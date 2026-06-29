from __future__ import annotations

import importlib
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, get_args, get_type_hints

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = "bioyond_peptide_station.peptide_station"
CLASS_NAME = "BioyondPeptideStation"
DEVICE_ID = "bioyond_peptide_station"
PEPTIDE_STATION_PATH = REPO_ROOT / "bioyond_peptide_station/peptide_station.py"

BASE_ERROR: Dict[str, Any] = {
    "task": "task-1",
    "ijk": "0_0_2",
    "token": "token-1",
    "code": 4005,
    "errMessage": "step failed",
    "errInnerMessage": "device failed",
    "errInnerMessage2": "command failed",
    "errInnerMessage3": "executeDeviceCommand failed",
    "optionMessage": "Please choose option: 1:RetryCmd, 2:SkipCmd, 5:StopCurrent.",
}


def _import_module() -> Any:
    return importlib.import_module(MODULE_PATH)


class _FakeRpc:
    def __init__(self, result_code: int = 1) -> None:
        self.result_code = result_code
        self.scheduler_reply_calls: list[Dict[str, Any]] = []

    def scheduler_reply_error_handling(self, data: Dict[str, Any]) -> int:
        self.scheduler_reply_calls.append(dict(data))
        return self.result_code


def _fresh_station(rpc: _FakeRpc | None = None) -> tuple[Any, _FakeRpc]:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    station = object.__new__(cls)
    fake_rpc = rpc or _FakeRpc()
    station.hardware_interface = fake_rpc
    station.error_handling_event = threading.Event()
    station.error_handling_lock = threading.Lock()
    station.error_queue = []
    station.error_in_flight = {}
    station._debug_call_session = lambda _name: nullcontext()
    return station, fake_rpc


def _patch_reply_builder(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[Dict[str, Any], int]]:
    calls: list[tuple[Dict[str, Any], int]] = []

    def fake_build(error_report: Dict[str, Any], reply_option: int, **_kwargs: Any) -> Dict[str, Any]:
        calls.append((dict(error_report), reply_option))
        return {
            "ijk": error_report["ijk"],
            "token": error_report["token"],
            "errorHandlingOption": reply_option,
            "creationTime": "2026-06-09T00:00:00.000Z",
        }

    monkeypatch.setattr(module, "build_scheduler_error_handling_reply_data", fake_build)
    return calls


def _error(**overrides: Any) -> Dict[str, Any]:
    payload = dict(BASE_ERROR)
    payload.update(overrides)
    return payload


def test_http_error_handling_route_unwraps_bioyond_text_payload() -> None:
    from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPHandler

    calls: list[Dict[str, Any]] = []

    class _FakeWorkstation:
        def handle_external_error(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            calls.append(payload)
            return {"queued": True}

    handler = object.__new__(WorkstationHTTPHandler)
    handler.workstation = _FakeWorkstation()

    response = handler._handle_error_handling_report({"brand": "bioyond", "text": BASE_ERROR})

    assert response.success is True
    assert calls == [BASE_ERROR]
    assert response.data == {"queued": True}


def test_handle_external_error_queues_scheduler_errors_and_sets_event() -> None:
    station, _rpc = _fresh_station()

    out = station.handle_external_error(_error())

    assert out["reply_status"] == "pending_manual_confirm"
    assert out["token"] == "token-1"
    assert out["queued_error_count"] == 1
    assert station.error_handling_event.is_set()
    assert len(station.error_queue) == 1
    assert station.error_queue[0]["token"] == "token-1"
    assert station.error_queue[0]["error_report"]["token"] == "token-1"
    assert station.error_queue[0]["status"] == "pending"


def test_handle_external_error_does_not_queue_incomplete_scheduler_payloads() -> None:
    station, _rpc = _fresh_station()

    out = station.handle_external_error({"task": "task-1", "code": 4005, "ijk": "0_0_2"})

    assert "reply_status" not in out
    assert station.error_queue == []
    assert not station.error_handling_event.is_set()


def test_wait_for_error_handling_times_out_when_queue_empty() -> None:
    station, _rpc = _fresh_station()

    out = station.wait_for_error_handling(timeout_seconds=0.01, poll_interval_seconds=0.001)

    assert out["success"] is False
    assert out["error_handling_status"] == "timeout"
    assert out["requires_manual_reply"] is False


def test_wait_for_error_handling_claims_fifo_and_moves_to_in_flight() -> None:
    station, _rpc = _fresh_station()
    first = station.handle_external_error(_error(task="task-1", token="token-1"))["token"]
    second = station.handle_external_error(_error(task="task-2", token="token-2"))["token"]

    out = station.wait_for_error_handling(timeout_seconds=1, poll_interval_seconds=0.001, ignore_errors_with=[])

    assert out["success"] is True
    assert out["token"] == first
    assert out["task"] == "task-1"
    assert list(station.error_in_flight) == [first]
    assert station.error_queue[0]["token"] == second
    assert station.error_handling_event.is_set()


def test_reply_success_keeps_remaining_queue_available(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, _rpc = _fresh_station()
    first = station.handle_external_error(_error(task="task-1", token="token-1"))["token"]
    second = station.handle_external_error(_error(task="task-2", token="token-2"))["token"]
    station.wait_for_error_handling(timeout_seconds=1, poll_interval_seconds=0.001, ignore_errors_with=[])

    reply = station.reply_error_handling(token=first, reply_choice="skip")
    next_wait = station.wait_for_error_handling(timeout_seconds=1, poll_interval_seconds=0.001, ignore_errors_with=[])

    assert reply["success"] is True
    assert reply["reply_status"] == "sent"
    assert first not in station.error_in_flight
    assert next_wait["token"] == second


def test_wait_auto_skips_default_ignored_error_and_returns_next_manual_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _import_module()
    builder_calls = _patch_reply_builder(monkeypatch, module)
    station, rpc = _fresh_station()
    ignored = station.handle_external_error(
        _error(
            task="ignored",
            token="token-ignored",
            errInnerMessage="Executor LabelPrinterA failed while running BY_Print.",
        )
    )["token"]
    manual = station.handle_external_error(_error(task="manual", token="token-manual"))["token"]

    out = station.wait_for_error_handling(timeout_seconds=1, poll_interval_seconds=0.001)

    assert out["token"] == manual
    assert out["auto_handled_errors"][0]["token"] == ignored
    assert out["auto_handled_errors"][0]["reply_result"] == 1
    assert builder_calls[0][1] == 2
    assert rpc.scheduler_reply_calls[0]["errorHandlingOption"] == 2
    assert ignored not in station.error_in_flight
    assert manual in station.error_in_flight


def test_wait_auto_skip_failure_preserves_item_for_manual_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, _rpc = _fresh_station(_FakeRpc(result_code=0))
    ignored = station.handle_external_error(
        _error(
            task="ignored",
            token="token-ignored",
            errInnerMessage="Executor LabelPrinterA failed while running BY_Print.",
        )
    )["token"]

    out = station.wait_for_error_handling(timeout_seconds=1, poll_interval_seconds=0.001)

    assert out["success"] is True
    assert out["token"] == ignored
    assert out["auto_handled_errors"][0]["reply_result"] == 0
    assert station.error_in_flight[ignored]["status"] == "auto_skip_failed"


def test_wait_custom_empty_ignore_list_disables_default_auto_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, rpc = _fresh_station()
    error_id = station.handle_external_error(
        _error(errInnerMessage="Executor LabelPrinterA failed while running BY_Print.")
    )["token"]

    out = station.wait_for_error_handling(
        timeout_seconds=1,
        poll_interval_seconds=0.001,
        ignore_errors_with=[],
    )

    assert out["token"] == error_id
    assert out["auto_handled_errors"] == []
    assert rpc.scheduler_reply_calls == []
    assert station.error_in_flight[error_id]["status"] == "in_flight"


@pytest.mark.parametrize(
    ("choice", "expected_option"),
    [("retry", 1), ("skip", 2), ("end_experiment", 5)],
)
def test_reply_error_handling_maps_choice_and_removes_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    expected_option: int,
) -> None:
    module = _import_module()
    builder_calls = _patch_reply_builder(monkeypatch, module)
    station, rpc = _fresh_station()
    report = _error(token=f"token-{choice}")
    station.error_in_flight[report["token"]] = {
        "token": report["token"],
        "status": "in_flight",
        "error_report": report,
    }

    out = station.reply_error_handling(token=report["token"], reply_choice=choice)

    assert out["success"] is True
    assert out["reply_status"] == "sent"
    assert out["bioyond_option"] == expected_option
    assert builder_calls == [(report, expected_option)]
    assert rpc.scheduler_reply_calls[0]["errorHandlingOption"] == expected_option
    assert report["token"] not in station.error_in_flight


def test_reply_error_handling_failure_keeps_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, _rpc = _fresh_station(_FakeRpc(result_code=0))
    station.error_in_flight["token-1"] = {
        "token": "token-1",
        "status": "in_flight",
        "error_report": _error(),
    }

    out = station.reply_error_handling(token="token-1", reply_choice="retry")

    assert out["success"] is False
    assert out["reply_status"] == "send_failed"
    assert station.error_in_flight["token-1"]["status"] == "send_failed"


def test_reply_error_handling_requires_in_flight_context(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, rpc = _fresh_station()

    out = station.reply_error_handling(token="missing", reply_choice="retry")

    assert out["success"] is False
    assert out["reply_status"] == "missing_context"
    assert rpc.scheduler_reply_calls == []


def test_reply_error_handling_without_token_uses_error_report(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_module()
    builder_calls = _patch_reply_builder(monkeypatch, module)
    station, rpc = _fresh_station()
    report = _error(errMessage="E1", errInnerMessage="E2")

    out = station.reply_error_handling(error_report=report, reply_choice="end_experiment")

    assert out["success"] is True
    assert out["token"] == "token-1"
    assert out["bioyond_option"] == 5
    assert "E1\nE2" in out["confirmation_message"]
    assert builder_calls == [(report, 5)]
    assert rpc.scheduler_reply_calls[0]["errorHandlingOption"] == 5


def test_reply_error_handling_error_report_claims_matching_queued_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _import_module()
    _patch_reply_builder(monkeypatch, module)
    station, _rpc = _fresh_station()
    report = _error(token="token-direct")
    station.handle_external_error(report)

    out = station.reply_error_handling(error_report=report, reply_choice="skip")

    assert out["success"] is True
    assert out["token"] == "token-direct"
    assert station.error_queue == []
    assert station.error_in_flight == {}


def test_error_handling_action_metadata_and_literal_choice() -> None:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)

    wait_meta = getattr(cls.wait_for_error_handling, "_action_registry_meta", {})
    assert wait_meta.get("always_free") is True
    assert wait_meta.get("node_type") != module.NodeType.MANUAL_CONFIRM
    assert wait_meta.get("description") == "等待奔曜调度异常；可自动跳过已知可忽略错误，其余异常交给人工确认。"
    assert wait_meta["goal_default"]["timeout_seconds"] == 36000
    assert wait_meta["goal_default"]["ignore_errors_with"] == list(module.DEFAULT_ERROR_HANDLING_IGNORE_TEXTS)

    reply_meta = getattr(cls.reply_error_handling, "_action_registry_meta", {})
    assert reply_meta.get("always_free") is True
    assert reply_meta.get("node_type") == module.NodeType.MANUAL_CONFIRM
    assert reply_meta.get("description") == "现场确认调度异常的处理方式：重试当前步骤、跳过当前步骤或结束实验。"
    assert reply_meta.get("placeholder_keys") == {"assignee_user_ids": "unilabos_manual_confirm"}
    assert reply_meta["goal_default"] == {
        "reply_choice": "retry",
        "error_message": "",
        "timeout_seconds": 3600,
        "assignee_user_ids": [],
    }

    hints = get_type_hints(cls.reply_error_handling, globalns=vars(module))
    assert set(get_args(hints["reply_choice"])) == {"retry", "skip", "end_experiment"}
    assert [item["label"] for item in module.ERROR_HANDLING_AVAILABLE_OPTIONS] == [
        "重试当前步骤",
        "跳过当前步骤",
        "结束实验",
    ]


def test_error_handling_nodes_are_ast_visible() -> None:
    scanner = pytest.importorskip("unilabos.registry.ast_registry_scanner")
    devices, _ = scanner._parse_file(PEPTIDE_STATION_PATH, REPO_ROOT)
    device = next((item for item in devices if item.get("device_id") == DEVICE_ID), None)
    if device is None:
        pytest.skip("peptide station AST metadata not parsed")

    actions = device["actions"]
    wait_args = actions["wait_for_error_handling"]["action_args"]
    assert wait_args["always_free"] is True
    assert not wait_args.get("node_type")
    assert wait_args["goal_default"]["ignore_errors_with"] == [
        "Executor LabelPrinterA failed while running BY_Print."
    ]
    wait_handles = {handle["key"] for handle in wait_args["handles"]}
    assert wait_handles == {"available_options", "token", "error_report", "error_message"}
    wait_labels = {handle["key"]: handle["label"] for handle in wait_args["handles"]}
    assert wait_labels["token"] == "错误标识"
    assert wait_labels["error_report"] == "错误详情"
    assert wait_labels["error_message"] == "错误说明"

    reply_args = actions["reply_error_handling"]["action_args"]
    assert reply_args["always_free"] is True
    assert reply_args["node_type"] == "MANUAL_CONFIRM"
    assert reply_args["placeholder_keys"] == {"assignee_user_ids": "unilabos_manual_confirm"}
    assert reply_args["goal_default"]["reply_choice"] == "retry"
    assert reply_args["goal_default"]["error_message"] == ""
    reply_handles = {handle["key"] for handle in reply_args["handles"]}
    assert reply_handles == {"token", "error_report", "error_message"}
    reply_labels = {handle["key"]: handle["label"] for handle in reply_args["handles"]}
    assert reply_labels == {
        "token": "错误标识",
        "error_report": "错误详情",
        "error_message": "错误说明",
    }
    error_message_handle = next(handle for handle in reply_args["handles"] if handle["key"] == "error_message")
    assert error_message_handle["data_type"] == "text"
    reply_params = {
        param["name"]: param
        for param in actions["reply_error_handling"].get("params", [])
    }
    assert reply_params["reply_choice"]["type"] == "Literal[retry, skip, end_experiment]"
