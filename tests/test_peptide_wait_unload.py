"""单元测试：多肽工作站新增的「等待订单完成」+「人工下料」两个节点（deep_clone sirna v2）。

覆盖 plan ``plan/2026-05-29_add_two_node_new.md`` §十一 的测试目标：

- ``BioyondV1RPC.materials_by_order_id``：endpoint = ``/api/lims/order/materials-by-order-id``、
  ``data`` 传 orderId GUID 字符串、缺 orderId / 非法 JSON / code != 1 / data 为 null 均返回 ``[]``。
- ``process_order_finish_report`` override：先 super() 再保存 state，orderCode 匹配触发 event；
  super() 抛错时仍要触发 event（防御性）。
- ``_build_unload_rows_from_materials_by_order_id`` + ``_build_unload_table``：4 列 ``{"name", "key"}``
  结构、同名物料多库位拆多行、空 location 占位、location.quantity=0 回退物料级、float 去尾。
- ``wait_for_order_finish``：超时、立即唤醒、status 映射、用 ``order_id`` 调 ``materials_by_order_id``、
  ``order_code`` 兜底反查、缺 order_id 报错、多 order_ids 歧义报错。
- ``unload_materials``：``materials_unloaded=False`` raise；调用 ``rpc.take_out(order_id, [], [])``；
  code==1 → success，code==99 → success=False，非 dict 响应 → success=False。
- AST 可见性：``wait_for_order_finish`` / ``unload_materials`` / ``start_experiment`` 的 metadata。
- 常量：``ORDER_FINISH_STATUS_MAP``；``UNLOAD_TABLE_COLUMNS_MULTI_ORDER`` 已删除。

测试设计原则：

- 使用 ``object.__new__(BioyondPeptideStation)`` 跳过 ``__init__``，避免 ROS / HTTP / deck 启动；
  仅注入测试所需的字段。``peptide_station.py`` 在缺 ``pylabrobot`` / ``rclpy`` 时通过
  ``_BIOYOND_IMPORT_ERROR`` fallback 路径正常加载（``BioyondWorkstation == object``）。
  override 里的 ``super().process_order_finish_report(...)`` 会触发 AttributeError 走 except 分支。
- RPC 运行时测试用 ``pytest.importorskip("rclpy")`` gating，缺 rclpy 时只跑 AST 静态检查。
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = "bioyond_peptide_station.peptide_station"
CLASS_NAME = "BioyondPeptideStation"
DEVICE_ID = "bioyond_peptide_station"
PEPTIDE_STATION_PATH = (
    REPO_ROOT / "bioyond_peptide_station/peptide_station.py"
)
RPC_PATH = REPO_ROOT / "bioyond_peptide_station/_vendored/bioyond_rpc.py"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _import_module() -> Any:
    return importlib.import_module(MODULE_PATH)


def _fresh_station() -> Any:
    """Construct a bare ``BioyondPeptideStation`` for behavior tests (skips __init__)."""
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    station = object.__new__(cls)
    station.bioyond_config = {
        "api_host": "http://bioyond.invalid",
        "api_key": "offline-key",
    }
    station.order_finish_event = threading.Event()
    station.last_order_code = None
    station.last_order_report = None
    station.last_used_materials = []
    # Bypass debug-call-session context manager (would otherwise reach base impl).
    station._debug_call_session = lambda _name: nullcontext()
    return station


class _ReportRequest:
    """Mimic the ``WorkstationReportRequest`` object the base callback expects."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data


class _FakeUsedMaterial:
    def __init__(self, material_id: str, used_quantity: float) -> None:
        self.materialId = material_id
        self.usedQuantity = used_quantity


# ---------------------------------------------------------------------------
# 0. BioyondV1RPC.materials_by_order_id —— AST-only (run everywhere)
# ---------------------------------------------------------------------------


def _rpc_function(name: str) -> ast.FunctionDef:
    source = RPC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RPC_PATH))
    for module_node in ast.walk(tree):
        if isinstance(module_node, ast.ClassDef) and module_node.name == "BioyondV1RPC":
            for item in module_node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"BioyondV1RPC.{name} AST node not found")


def _materials_by_order_id_function() -> ast.FunctionDef:
    return _rpc_function("materials_by_order_id")


def test_materials_by_order_id_method_exists_in_class() -> None:
    func = _materials_by_order_id_function()
    assert [arg.arg for arg in func.args.args] == ["self", "json_str"]


def test_materials_by_order_id_source_targets_correct_endpoint_and_payload() -> None:
    func = _materials_by_order_id_function()
    source = ast.unparse(func) if hasattr(ast, "unparse") else ""
    if not source:
        full_source = RPC_PATH.read_text(encoding="utf-8")
        source = ast.get_source_segment(full_source, func) or ""
    assert "materials-by-order-id" in source, (
        "materials_by_order_id 必须 POST /api/lims/order/materials-by-order-id"
    )
    assert "all-stock-material" not in source, (
        "旧 endpoint /api/lims/storage/all-stock-material 仿真器 404，不应继续出现"
    )
    assert "apiKey" in source and "requestTime" in source and "data" in source
    assert "orderId" in source, "必须按 orderId 校验 —— 不是 orderCode"


# ---------------------------------------------------------------------------
# 0b. BioyondV1RPC.materials_by_order_id —— runtime (requires rclpy import chain)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rpc_class() -> Any:
    pytest.importorskip(
        "rclpy",
        reason="BioyondV1RPC depends on rclpy via unilabos.device_comms.rpc",
    )
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC
    return BioyondV1RPC


def _make_rpc(rpc_class: Any) -> Any:
    instance = object.__new__(rpc_class)
    instance.config = {"api_key": "test-key", "api_host": "http://invalid.local"}
    instance.api_key = "test-key"
    instance.host = "http://invalid.local"

    class _SilentLogger:
        def __init__(self) -> None:
            self.errors: List[str] = []

        def info(self, msg: Any) -> None:
            pass

        def debug(self, msg: Any) -> None:
            pass

        def warning(self, msg: Any) -> None:
            pass

        def critical(self, msg: Any) -> None:
            pass

        def error(self, msg: Any) -> None:
            self.errors.append(str(msg))

    instance._logger = _SilentLogger()
    return instance


def test_materials_by_order_id_runtime_posts_to_correct_url_with_order_id(rpc_class: Any) -> None:
    rpc = _make_rpc(rpc_class)
    posted: Dict[str, Any] = {}

    def fake_post(*, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        posted["url"] = url
        posted["params"] = params
        return {"code": 1, "data": [{"id": "m1", "name": "X"}]}

    rpc.post = fake_post  # type: ignore[method-assign]

    result = rpc.materials_by_order_id(json.dumps({"orderId": "OID-xyz", "typeMode": 0}))

    assert result == [{"id": "m1", "name": "X"}]
    assert posted["url"] == "http://invalid.local/api/lims/order/materials-by-order-id"
    assert posted["params"]["apiKey"] == "test-key"
    assert "requestTime" in posted["params"]
    assert posted["params"]["data"] == "OID-xyz", (
        "materials-by-order-id 的 data 字段必须是 orderId GUID 字符串，不能传对象"
    )


def test_materials_by_order_id_runtime_returns_empty_when_order_id_missing(rpc_class: Any) -> None:
    rpc = _make_rpc(rpc_class)
    rpc.post = lambda **_kw: pytest.fail("缺 orderId 时不应发起 HTTP 请求")  # type: ignore[method-assign]
    assert rpc.materials_by_order_id(json.dumps({"typeMode": 0})) == []
    assert any("orderId" in msg for msg in rpc._logger.errors)


def test_materials_by_order_id_runtime_returns_empty_on_invalid_json(rpc_class: Any) -> None:
    rpc = _make_rpc(rpc_class)
    rpc.post = lambda **_kw: pytest.fail("json 解析失败时不应发起 HTTP 请求")  # type: ignore[method-assign]
    assert rpc.materials_by_order_id("not-json") == []


def test_materials_by_order_id_runtime_returns_empty_when_code_not_one(rpc_class: Any) -> None:
    rpc = _make_rpc(rpc_class)
    rpc.post = lambda **_kw: {"code": 99, "message": "boom", "data": [{"id": "x"}]}  # type: ignore[method-assign]
    assert rpc.materials_by_order_id(json.dumps({"orderId": "OID-1"})) == []


def test_materials_by_order_id_runtime_returns_empty_when_data_is_null(rpc_class: Any) -> None:
    rpc = _make_rpc(rpc_class)
    rpc.post = lambda **_kw: {"code": 1, "data": None}  # type: ignore[method-assign]
    assert rpc.materials_by_order_id(json.dumps({"orderId": "OID-1"})) == []


# ---------------------------------------------------------------------------
# 1. process_order_finish_report override
# ---------------------------------------------------------------------------


def test_process_order_finish_report_records_state_and_sets_event_on_match() -> None:
    station = _fresh_station()
    station.last_order_code = "EXP-0001"

    request = _ReportRequest({"orderCode": "EXP-0001", "status": "30"})
    used = [_FakeUsedMaterial("mat-1", 1.5)]

    result = station.process_order_finish_report(request, used)

    assert station.order_finish_event.is_set(), "matching orderCode must set the event"
    assert station.last_order_report == {"orderCode": "EXP-0001", "status": "30"}
    assert station.last_used_materials == used
    assert isinstance(result, dict)
    assert result.get("processed") is True
    assert result.get("materials_count") == 1


def test_process_order_finish_report_uses_waiting_order_id_for_material_sync() -> None:
    station = _fresh_station()
    station.last_order_code = "EXP-0001"
    station.last_order_id = "OID-0001"
    material_sync = {
        "requested": True,
        "status": "synced",
        "order_id": "OID-0001",
        "materials": [],
        "material_count": 0,
    }
    station._sync_order_materials_from_bioyond = MagicMock(return_value=material_sync)

    result = station.process_order_finish_report(
        _ReportRequest({"orderCode": "EXP-0001", "status": "30"}),
        [],
    )

    station._sync_order_materials_from_bioyond.assert_called_once_with(
        "OID-0001",
        reason="order_finish_report",
    )
    assert result["material_sync"] == material_sync
    assert station.last_order_material_sync == material_sync


def test_process_order_finish_report_does_not_set_event_on_mismatch() -> None:
    station = _fresh_station()
    station.last_order_code = "EXP-EXPECTED"

    request = _ReportRequest({"orderCode": "EXP-OTHER", "status": "30"})
    station.process_order_finish_report(request, [])

    assert not station.order_finish_event.is_set(), "mismatched orderCode must NOT trigger event"
    assert station.last_order_report == {"orderCode": "EXP-OTHER", "status": "30"}
    assert station.last_used_materials == []


def test_process_order_finish_report_no_event_when_no_expected_code_pending() -> None:
    station = _fresh_station()
    station.last_order_code = None
    station.process_order_finish_report(_ReportRequest({"orderCode": "EXP-X", "status": "30"}), [])
    assert not station.order_finish_event.is_set()


# ---------------------------------------------------------------------------
# 2. _build_unload_rows_from_materials_by_order_id + _build_unload_table
# ---------------------------------------------------------------------------


def test_build_unload_rows_uses_four_columns_from_locations() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {
            "id": "m1", "code": "0017-00733", "name": "G3-50ul枪头盒",
            "typeMode": "Sample", "unit": "个", "quantity": 1.0, "isUse": False,
            "locations": [
                {"id": "L1", "code": "10-2", "whName": "自动化堆栈",
                 "quantity": 1, "x": 2, "y": 10, "z": 1},
            ],
        }
    ])
    assert rows == [
        {"whName": "自动化堆栈", "locationCode": "10-2",
         "materialName": "G3-50ul枪头盒", "quantity": "1 个"}
    ]
    for row in rows:
        assert set(row.keys()) == {"whName", "locationCode", "materialName", "quantity"}


def test_build_unload_rows_splits_multi_locations() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {
            "id": "m2", "name": "细胞培养板", "quantity": 2.0,
            "locations": [
                {"code": "10-3", "whName": "自动化堆栈", "quantity": 1},
                {"code": "10-4", "whName": "自动化堆栈", "quantity": 1},
            ],
        }
    ])
    assert len(rows) == 2
    assert [row["locationCode"] for row in rows] == ["10-3", "10-4"]
    assert all(row["materialName"] == "细胞培养板" for row in rows)
    assert [row["quantity"] for row in rows] == ["1", "1"]


def test_build_unload_rows_keeps_empty_location_placeholder() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {"id": "m3", "name": "裂解液", "quantity": 5, "unit": "mL", "locations": []},
    ])
    assert rows == [
        {"whName": "", "locationCode": "", "materialName": "裂解液", "quantity": "5 mL"},
    ]


def test_build_unload_rows_preserves_quantity_only_when_unit_empty() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {"id": "m3", "name": "裂解液", "quantity": 5, "unit": "", "locations": []},
    ])
    assert rows[0]["quantity"] == "5"


def test_build_unload_rows_falls_back_when_location_quantity_is_zero() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {
            "id": "m_real", "name": "G3-50ul枪头盒", "quantity": 1.0,
            "locations": [{"code": "10-2", "whName": "自动化堆栈", "quantity": 0}],
        }
    ])
    assert rows[0]["quantity"] == "1", "location.quantity=0 必须回退到 material.quantity"


def test_build_unload_rows_formats_integer_float_as_int_string() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {"id": "m1", "name": "A", "quantity": 4.0,
         "locations": [{"code": "3-2", "whName": "WH", "quantity": 4.0}]},
        {"id": "m2", "name": "B", "quantity": 1.0, "locations": []},
    ])
    assert rows[0]["quantity"] == "4"
    assert rows[1]["quantity"] == "1"


def test_build_unload_rows_keeps_non_integer_float_as_is() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    rows = cls._build_unload_rows_from_materials_by_order_id([
        {"id": "m", "name": "X", "quantity": 1.5,
         "locations": [{"code": "1-1", "whName": "WH", "quantity": 1.5}]}
    ])
    assert rows[0]["quantity"] == "1.5"


def test_build_unload_rows_handles_empty_input_and_non_dict_items() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    assert cls._build_unload_rows_from_materials_by_order_id([]) == []
    assert cls._build_unload_rows_from_materials_by_order_id(None) == []  # type: ignore[arg-type]
    assert cls._build_unload_rows_from_materials_by_order_id(["not-a-dict"]) == []  # type: ignore[list-item]


def test_build_unload_table_uses_four_columns_in_name_key_format() -> None:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    columns_const = getattr(module, "UNLOAD_TABLE_COLUMNS")
    assert columns_const == [
        {"name": "设备", "key": "whName"},
        {"name": "位置", "key": "locationCode"},
        {"name": "物料名称", "key": "materialName"},
        {"name": "数量", "key": "quantity"},
    ]
    table = cls._build_unload_table([{"whName": "WH", "locationCode": "1-1",
                                      "materialName": "X", "quantity": "1"}])
    assert table["columns"] == columns_const
    assert table["tableName"] == "下料指引"
    assert table["data"] == [
        {"whName": "WH", "locationCode": "1-1", "materialName": "X", "quantity": "1"}
    ]


def test_build_unload_table_sorts_rows_by_warehouse_location_then_material() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    table = cls._build_unload_table([
        {"whName": "WH-B", "locationCode": "1-01", "materialName": "WH-B 样品", "quantity": "1"},
        {"whName": "WH-A", "locationCode": "10-2", "materialName": "样品C", "quantity": "1"},
        {"whName": "WH-A", "locationCode": "2-02", "materialName": "样品B", "quantity": "1"},
        {"whName": "WH-A", "locationCode": "2-01", "materialName": "", "quantity": "1"},
        {"whName": "WH-A", "locationCode": "2-01", "materialName": "样品A", "quantity": "1"},
    ])
    assert [
        (row["whName"], row["locationCode"], row["materialName"])
        for row in table["data"]
    ] == [
        ("WH-A", "2-01", "样品A"),
        ("WH-A", "2-01", ""),
        ("WH-A", "2-02", "样品B"),
        ("WH-A", "10-2", "样品C"),
        ("WH-B", "1-01", "WH-B 样品"),
    ]


# ---------------------------------------------------------------------------
# 3. wait_for_order_finish
# ---------------------------------------------------------------------------


class _FakeRPCForWait:
    def __init__(
        self,
        *,
        order_report_response: Optional[Dict[str, Any]] = None,
        all_stock_response: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.order_report_calls: List[str] = []
        self.materials_by_order_id_calls: List[str] = []
        self.materials_by_order_id_result_calls: List[str] = []
        self._order_report_response = order_report_response or {}
        self._all_stock_response = list(all_stock_response or [])

    def order_report(self, order_id: str) -> Dict[str, Any]:
        self.order_report_calls.append(order_id)
        return dict(self._order_report_response)

    def materials_by_order_id(self, json_str: str) -> List[Dict[str, Any]]:
        self.materials_by_order_id_calls.append(json_str)
        return list(self._all_stock_response)

    def materials_by_order_id_result(self, json_str: str) -> Dict[str, Any]:
        # 同步路径已切到 result-aware 取数（非折叠 dict 形状）。
        self.materials_by_order_id_result_calls.append(json_str)
        return {"ok": True, "data": list(self._all_stock_response), "message": "", "code": 1}


def test_wait_for_order_finish_returns_timeout_when_event_never_fires() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForWait()

    result = station.wait_for_order_finish(
        order_id="OID-1",
        order_code="EXP-001",
        timeout_seconds=1,
        poll_mode=True,
        poll_interval_seconds=0.05,
    )
    assert result["order_finish_status"] == "timeout"
    assert result["success"] is False
    assert result["order_id"] == "OID-1"
    assert result["order_code"] == "EXP-001"
    assert result["materials_by_order_id"] == []
    assert result["resultTable"]["data"] == []
    assert station.hardware_interface.materials_by_order_id_result_calls == []


def test_wait_for_order_finish_uses_order_id_when_calling_materials_by_order_id() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForWait(
        all_stock_response=[
            {"id": "m1", "name": "样品A", "quantity": 1,
             "locations": [{"code": "1-1", "whName": "自动化堆栈"}]}
        ],
    )
    station.hardware_interface = rpc

    def trigger_later() -> None:
        time.sleep(0.05)
        station.last_order_report = {"orderCode": "EXP-001", "status": "30"}
        station.last_used_materials = [_FakeUsedMaterial("mat-1", 1)]
        station.order_finish_event.set()

    threading.Thread(target=trigger_later, daemon=True).start()

    result = station.wait_for_order_finish(
        order_id="OID-1",
        order_code="EXP-001",
        timeout_seconds=2,
        poll_mode=True,
        poll_interval_seconds=0.01,
    )

    assert result["order_finish_status"] == "success"
    assert result["success"] is True
    assert len(rpc.materials_by_order_id_result_calls) == 1
    payload = json.loads(rpc.materials_by_order_id_result_calls[0])
    assert payload == {"orderId": "OID-1"}, "materials_by_order_id 必须接收 orderId 作为输入"
    assert result["resultTable"]["columns"][0] == {"name": "设备", "key": "whName"}
    assert result["resultTable"]["data"] == [
        {"whName": "自动化堆栈", "locationCode": "1-1", "materialName": "样品A", "quantity": "1"}
    ]
    assert result["used_materials"] == [{"materialId": "mat-1", "usedQuantity": 1}]


def test_wait_for_order_finish_retries_when_callback_material_sync_failed() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForWait()
    retry_sync = {
        "requested": True,
        "status": "synced",
        "order_id": "OID-1",
        "materials": [{"id": "m-retry", "name": "重试物料", "quantity": 1, "locations": []}],
        "material_count": 1,
    }
    station._construct_unload_table_payload = MagicMock(return_value={
        "materials_by_order_id": retry_sync["materials"],
        "resultTable": {"data": [{"materialName": "重试物料"}], "columns": []},
        "material_sync": retry_sync,
    })

    class _TriggeredEvent:
        def clear(self) -> None:
            pass

        def wait(self, timeout: Optional[float] = None) -> bool:
            station.last_order_report = {"orderCode": "EXP-001", "status": "30"}
            station.last_order_material_sync = {
                "requested": True,
                "status": "failed",
                "order_id": "OID-1",
                "error": "temporary",
            }
            return True

    station.order_finish_event = _TriggeredEvent()

    result = station.wait_for_order_finish(
        order_id="OID-1",
        order_code="EXP-001",
        timeout_seconds=2,
        poll_mode=False,
    )

    station._construct_unload_table_payload.assert_called_once_with("OID-1")
    assert result["material_sync"] == retry_sync
    assert result["materials_by_order_id"] == retry_sync["materials"]


def test_wait_for_order_finish_returns_sorted_unload_result_table() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForWait(
        all_stock_response=[
            {"id": "m5", "name": "WH-B 样品", "quantity": 1, "locations": [{"code": "1-01", "whName": "WH-B"}]},
            {"id": "m4", "name": "样品C", "quantity": 1, "locations": [{"code": "10-2", "whName": "WH-A"}]},
            {"id": "m3", "name": "样品B", "quantity": 1, "locations": [{"code": "2-02", "whName": "WH-A"}]},
            {"id": "m2", "name": "", "quantity": 1, "locations": [{"code": "2-01", "whName": "WH-A"}]},
            {"id": "m1", "name": "样品A", "quantity": 1, "locations": [{"code": "2-01", "whName": "WH-A"}]},
        ],
    )
    station.hardware_interface = rpc

    def trigger_later() -> None:
        time.sleep(0.05)
        station.last_order_report = {"orderCode": "EXP-001", "status": "30"}
        station.order_finish_event.set()

    threading.Thread(target=trigger_later, daemon=True).start()

    result = station.wait_for_order_finish(
        order_id="OID-1",
        order_code="EXP-001",
        timeout_seconds=2,
        poll_mode=True,
        poll_interval_seconds=0.01,
    )

    assert [
        (row["whName"], row["locationCode"], row["materialName"])
        for row in result["resultTable"]["data"]
    ] == [
        ("WH-A", "2-01", "样品A"),
        ("WH-A", "2-01", ""),
        ("WH-A", "2-02", "样品B"),
        ("WH-A", "10-2", "样品C"),
        ("WH-B", "1-01", "WH-B 样品"),
    ]


@pytest.mark.parametrize("raw_status,expected_status,expected_success", [
    ("30", "success", True),
    ("-11", "abnormal_stop", True),
    ("-12", "manual_stop", True),
    ("999", "unknown_999", False),
    ("", "missing_status", False),
])
def test_wait_for_order_finish_maps_status(
    raw_status: str, expected_status: str, expected_success: bool,
) -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForWait()

    def re_set() -> None:
        time.sleep(0.02)
        report: Dict[str, Any] = {"orderCode": "EXP-X"}
        if raw_status:
            report["status"] = raw_status
        station.last_order_report = report
        station.order_finish_event.set()

    threading.Thread(target=re_set, daemon=True).start()

    result = station.wait_for_order_finish(
        order_id="OID-1",
        order_code="EXP-X",
        timeout_seconds=2,
        poll_mode=True,
        poll_interval_seconds=0.01,
    )
    assert result["order_finish_status"] == expected_status
    assert result["success"] is expected_success


def test_wait_for_order_finish_resolves_order_code_via_order_report() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForWait(order_report_response={"code": "EXP-FROM-REPORT"})
    station.hardware_interface = rpc

    def re_set() -> None:
        time.sleep(0.02)
        station.last_order_report = {"orderCode": "EXP-FROM-REPORT", "status": "30"}
        station.order_finish_event.set()

    threading.Thread(target=re_set, daemon=True).start()

    result = station.wait_for_order_finish(
        order_id="OID-Z",
        order_code="",
        timeout_seconds=2,
        poll_mode=True,
        poll_interval_seconds=0.01,
    )
    assert rpc.order_report_calls == ["OID-Z"]
    assert result["order_code"] == "EXP-FROM-REPORT"
    assert station.last_order_code == "EXP-FROM-REPORT"


def test_wait_for_order_finish_raises_when_no_order_id_or_code() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForWait()
    with pytest.raises(ValueError):
        station.wait_for_order_finish(order_id="", order_code="", timeout_seconds=0)


def test_wait_for_order_finish_raises_when_ambiguous_order_ids() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForWait()
    with pytest.raises(ValueError):
        station.wait_for_order_finish(
            order_id="",
            order_code="",
            order_ids=["OID-A", "OID-B"],
            timeout_seconds=0,
        )


# ---------------------------------------------------------------------------
# 4. unload_materials
# ---------------------------------------------------------------------------


_UNLOAD_FAKE_DEFAULT = object()


class _FakeRPCForUnload:
    def __init__(self, response: Any = _UNLOAD_FAKE_DEFAULT) -> None:
        self.take_out_calls: List[tuple] = []
        if response is _UNLOAD_FAKE_DEFAULT:
            self._response: Any = {"code": 1, "data": {}, "message": ""}
        else:
            self._response = response

    def take_out(self, order_id: str, preintake_ids: List[str], material_ids: List[str]) -> Any:
        self.take_out_calls.append((order_id, list(preintake_ids), list(material_ids)))
        return self._response


def test_unload_materials_raises_without_confirmation() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForUnload()
    with pytest.raises(RuntimeError, match="下料未确认"):
        station.unload_materials(order_id="OID-1", materials_unloaded=False)


def test_unload_materials_raises_without_order_id() -> None:
    station = _fresh_station()
    station.hardware_interface = _FakeRPCForUnload()
    with pytest.raises(ValueError, match="order_id"):
        station.unload_materials(order_id="", materials_unloaded=True)


def test_unload_materials_calls_take_out_with_empty_id_lists() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForUnload(response={"code": 1, "data": {}, "message": "OK"})
    station.hardware_interface = rpc
    unload_table = {"tableName": "resultTable", "data": [{"materialName": "样品A"}]}

    result = station.unload_materials(
        order_id="OID-1",
        resultTable=unload_table,
        materials_unloaded=True,
    )

    assert rpc.take_out_calls == [("OID-1", [], [])]
    assert result["success"] is True
    assert result["order_id"] == "OID-1"
    assert "resultTable" not in result
    assert result["take_out_result"] == {"code": 1, "data": {}, "message": "OK"}


def test_unload_materials_reports_failure_when_take_out_code_not_one() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForUnload(response={"code": 99, "message": "service error"})
    station.hardware_interface = rpc

    result = station.unload_materials(order_id="OID-1", materials_unloaded=True)
    assert rpc.take_out_calls == [("OID-1", [], [])]
    assert result["success"] is False
    assert "service error" in result["confirmation_message"]


def test_unload_materials_handles_non_dict_take_out_response() -> None:
    station = _fresh_station()
    rpc = _FakeRPCForUnload(response=None)
    station.hardware_interface = rpc

    result = station.unload_materials(order_id="OID-1", materials_unloaded=True)
    assert result["success"] is False
    assert result["take_out_result"] == {}


# ---------------------------------------------------------------------------
# 5. AST 可见性
# ---------------------------------------------------------------------------


def _ast_metadata() -> Dict[str, Dict[str, Any]]:
    scanner = pytest.importorskip("unilabos.registry.ast_registry_scanner")
    devices, _ = scanner._parse_file(PEPTIDE_STATION_PATH, REPO_ROOT)
    return {device["device_id"]: device for device in devices if device.get("device_id")}


def _peptide_device_meta() -> Dict[str, Any]:
    metadata = _ast_metadata()
    device = metadata.get(DEVICE_ID)
    if device is None:
        pytest.skip("多肽工作站 AST metadata 解析为空，跳过 AST 可见性测试")
    return device


def test_wait_for_order_finish_is_ast_visible_with_expected_handles() -> None:
    device = _peptide_device_meta()
    actions = device["actions"]
    assert "wait_for_order_finish" in actions

    meta = actions["wait_for_order_finish"]
    args = meta["action_args"]
    assert args["always_free"] is True
    assert not args.get("node_type"), "wait 节点应为 normal action，非 manual_confirm"

    goal_default = args["goal_default"]
    assert goal_default["order_id"] == ""
    assert goal_default["order_code"] == ""
    assert goal_default["timeout_seconds"] == 36000
    assert goal_default["poll_mode"] is True

    handle_keys = {handle["key"] for handle in args["handles"]}
    assert {"order_id", "order_ids", "order_code"} <= handle_keys
    assert {
        "order_id", "order_code",
        "order_finish_status", "order_finish_report",
        "used_materials", "materials_by_order_id", "resultTable",
    } <= handle_keys


def test_unload_materials_is_ast_visible_as_manual_confirm() -> None:
    device = _peptide_device_meta()
    actions = device["actions"]
    assert "unload_materials" in actions

    meta = actions["unload_materials"]
    args = meta["action_args"]
    node_type = args["node_type"]
    assert node_type == "MANUAL_CONFIRM" or node_type.endswith(":NodeType.MANUAL_CONFIRM")
    assert args["always_free"] is True
    assert args["placeholder_keys"]["resultTable"] == "unilabos_manual_confirm"
    assert args["placeholder_keys"]["assignee_user_ids"] == "unilabos_manual_confirm"

    goal_default = args["goal_default"]
    assert goal_default["order_id"] == ""
    assert goal_default["materials_unloaded"] is False
    assert goal_default["timeout_seconds"] == 3600

    handle_keys = {handle["key"] for handle in args["handles"]}
    assert {
        "order_id", "order_code", "resultTable", "used_materials", "order_finish_report",
    } <= handle_keys
    result_table_handles = [handle for handle in args["handles"] if handle["key"] == "resultTable"]
    assert result_table_handles
    assert len(result_table_handles) == 1
    assert result_table_handles[0]["data_type"] == "table"
    for forbidden in ("preintakeIds", "materialIds", "preintake_ids", "material_ids"):
        assert forbidden not in handle_keys, (
            f"unload_materials 决策上不应再接收 {forbidden}：take-out 只传 [] / []"
        )


def test_start_experiment_exposes_order_code_output() -> None:
    device = _peptide_device_meta()
    actions = device["actions"]
    assert "start_experiment" in actions

    handles = actions["start_experiment"]["action_args"]["handles"]
    output_keys: List[str] = []
    for handle in handles:
        ds = handle.get("data_source")
        ds_text = str(ds) if ds is not None else ""
        if "EXECUTOR" in ds_text:
            output_keys.append(handle["key"])
    assert {"order_id", "order_ids", "order_code"} <= set(output_keys), (
        f"start_experiment 应将 order_id/order_ids/order_code 暴露为 EXECUTOR 输出，"
        f"当前 EXECUTOR handles={output_keys}"
    )


# ---------------------------------------------------------------------------
# 6. 常量（防御性）
# ---------------------------------------------------------------------------


def test_order_finish_status_map_covers_known_bioyond_statuses() -> None:
    module = _import_module()
    status_map = getattr(module, "ORDER_FINISH_STATUS_MAP")
    assert status_map == {"30": "success", "-11": "abnormal_stop", "-12": "manual_stop"}


def test_unload_table_columns_multi_order_constant_removed() -> None:
    """plan v2 决策：废弃多订单合并下料表，常量必须删除。"""
    module = _import_module()
    assert getattr(module, "UNLOAD_TABLE_COLUMNS_MULTI_ORDER", None) is None
