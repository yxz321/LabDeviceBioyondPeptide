from __future__ import annotations

import importlib
import threading
from typing import Any


MODULE_PATH = "bioyond_peptide_station.peptide_station"
CLASS_NAME = "BioyondPeptideStation"


class _ReportRequest:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class _FakeSynchronizer:
    def __init__(self) -> None:
        self.sync_count = 0

    def sync_from_external(self) -> bool:
        self.sync_count += 1
        return True


def _fresh_station() -> Any:
    module = importlib.import_module(MODULE_PATH)
    cls = getattr(module, CLASS_NAME)
    station = object.__new__(cls)
    station.resource_synchronizer = _FakeSynchronizer()
    station.published_statuses = []
    station._publish_task_status = lambda **kwargs: station.published_statuses.append(kwargs)
    station.order_finish_event = threading.Event()
    station.last_order_code = "ORDER-1"
    station.last_order_report = None
    station.last_used_materials = []
    return station


def test_process_step_finish_report_publishes_without_full_sync() -> None:
    station = _fresh_station()
    request = _ReportRequest({
        "orderCode": "ORDER-1",
        "stepName": "coupling",
        "stepId": "STEP-1",
        "sampleId": "SAMPLE-1",
    })

    result = station.process_step_finish_report(request)

    assert result["processed"] is True
    assert station.resource_synchronizer.sync_count == 0
    assert station.published_statuses == [{
        "task_id": "ORDER-1",
        "task_code": "ORDER-1",
        "task_type": "bioyond_step",
        "status": "running",
        "progress": 0.5,
        "result": {"step_name": "coupling", "step_id": "STEP-1"},
    }]


def test_process_order_finish_report_completion_publishes_sets_event_without_full_sync() -> None:
    station = _fresh_station()
    request = _ReportRequest({
        "orderCode": "ORDER-1",
        "orderName": "Peptide order",
        "status": "30",
    })

    result = station.process_order_finish_report(request, used_materials=[])

    assert result["processed"] is True
    assert station.resource_synchronizer.sync_count == 0
    assert station.order_finish_event.is_set()
    assert station.last_order_report == request.data
    assert station.last_used_materials == []
    assert station.published_statuses == [{
        "task_id": "ORDER-1",
        "task_code": "ORDER-1",
        "task_type": "bioyond_order",
        "status": "completed",
        "progress": 1.0,
        "result": {"order_name": "Peptide order", "status": "完成", "materials_count": 0},
    }]
