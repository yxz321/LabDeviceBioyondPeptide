from __future__ import annotations

import errno
import json
import time
from typing import Any
from pathlib import Path
from urllib import request
from urllib.error import URLError

import pytest

from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPHandler, WorkstationHTTPService


REPO_ROOT = Path(__file__).resolve().parents[1]
MATERIAL_CHANGE_LOG = REPO_ROOT / "temp_benyao/peptide/_input/http_1780642977908.log"


class _FakeWorkstation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def process_material_change_report(self, report_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(report_data)
        return {
            "processed": True,
            "material_id": report_data["id"],
            "type_name": report_data["typeName"],
        }


def _handler_for(workstation: Any) -> WorkstationHTTPHandler:
    handler = object.__new__(WorkstationHTTPHandler)
    handler.workstation = workstation
    return handler


def test_bioyond_material_change_delegates_text_payload_to_station() -> None:
    workstation = _FakeWorkstation()
    handler = _handler_for(workstation)
    material_text = {"id": "mat-1", "typeName": "96孔收集板"}

    response = handler._handle_material_change_report({"brand": "bioyond", "text": material_text})

    assert response.success is True
    assert workstation.calls == [material_text]
    assert response.data == {
        "processed": True,
        "material_id": "mat-1",
        "type_name": "96孔收集板",
    }


def _load_first_material_change_report() -> tuple[str, dict[str, Any], dict[str, Any]]:
    for line in MATERIAL_CHANGE_LOG.read_text(encoding="utf-8").splitlines():
        captured = json.loads(line)
        if captured.get("endpoint") != "/report/material_change":
            continue
        body = dict(captured["body"])
        body.pop("method", None)
        return captured["endpoint"], body, body["text"]
    raise AssertionError("material_change report not found in log fixture")


def _start_service(workstation: Any) -> tuple[WorkstationHTTPService, int]:
    for port in (8080, 8081):
        service = WorkstationHTTPService(workstation, host="127.0.0.1", port=port)
        try:
            service.start()
        except OSError as exc:
            service.stop()
            if exc.errno == errno.EADDRINUSE:
                continue
            raise
        return service, port
    pytest.skip("ports 8080 and 8081 are both occupied")


def _wait_for_health(url: str) -> None:
    deadline = time.time() + 3
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with request.urlopen(f"{url}/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"HTTP service did not become healthy: {last_error}")


def test_real_http_service_replays_bioyond_material_change_log() -> None:
    endpoint, body, material_row = _load_first_material_change_report()
    workstation = _FakeWorkstation()
    service, port = _start_service(workstation)

    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(base_url)
        post_url = f"{base_url}{endpoint}"
        request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            post_url,
            data=request_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        with request.urlopen(http_request, timeout=3) as response:
            response_data = json.loads(response.read().decode("utf-8"))

        assert response_data["success"] is True
        assert workstation.calls == [material_row]
        assert response_data["data"] == {
            "processed": True,
            "material_id": material_row["id"],
            "type_name": material_row["typeName"],
        }
    finally:
        service.stop()
