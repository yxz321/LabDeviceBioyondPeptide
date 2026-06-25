"""多肽站 AST/参数/结果表 离线契约测试。"""

from __future__ import annotations

import importlib
import inspect
import json
from contextlib import contextmanager
from types import SimpleNamespace
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = "bioyond_peptide_station.peptide_station"
CLASS_NAME = "BioyondPeptideStation"

ORDER_GUID = "3a20eabe-bad5-ef95-49bd-7ffbd5df189d"
CREATE_ALLOCATION = {
    ORDER_GUID: [
        {
            "materialId": "mat-tip",
            "materialName": "200μL枪头盒",
            "materialCode": "0008-00105",
            "quantity": "1个",
            "materialTypeMode": "Consumables",
            "locationCode": "1-01",
            "locationShowName": "1-01",
        },
        {
            "materialId": "mat-plate",
            "materialName": "96孔板",
            "materialCode": "PLATE-96",
            "quantity": "1",
            "materialTypeMode": "Sample",
            "locationCode": "A1",
            "locationShowName": "A1-show",
        },
        {
            "materialId": "mat-extra",
            "materialName": "未知耗材",
            "materialCode": "X-1",
            "quantity": "2",
            "materialTypeMode": "Future",
            "locationCode": "Z9",
            "locationShowName": "",
        },
    ]
}

FLATTENED_LIVE = [
    {"step": "39c78d4b-b5d3-f721-2001-9d52000084c3", "step_name": "S1", "Key": "SampleFile", "m": 0, "n": 0, "Value": "", "DisplayValue": "", "TaskDisplayable": 1},
    {"step": "39c78d4b-b5d3-f721-2001-9d52000084c3", "step_name": "S1", "Key": "Example", "m": 0, "n": 0, "Value": "x", "DisplayValue": "x", "TaskDisplayable": 1},
    {"step": "39c78d4b-b5d3-f721-2001-9d52000084c4", "step_name": "S2", "Key": "protocol", "m": 14, "n": 28, "Value": "", "DisplayValue": "", "TaskDisplayable": 1},
    {"step": "39c78d4b-b5d3-f721-2001-9d52000084c5", "step_name": "S3", "Key": "CEMMethodFileName", "m": 0, "n": 0, "Value": "", "DisplayValue": "", "TaskDisplayable": 1},
]
ERROR_HANDLING_REPORT = {
    "task": "3a21938a-9888-85a7-95ce-ffdbff4513a2",
    "ijk": "0_0_2",
    "token": "c130d6a5-0bfd-4a8d-830d-202981714318",
    "sampleId": "3a21938a-988c-fd90-be55-f84428b844b9",
    "level": 2,
    "module": 4,
    "code": 4005,
    "errMessage": "步骤故障",
    "errInnerMessage": "执行设备LabelPrinterA,目标设备LabelPrinterA 执行步骤 BY_Print 失败。",
    "errInnerMessage2": "执行设备LabelPrinterA，目标设备LabelPrinterA步骤 BY_Print 执行指令 LabelPrinter-BY_Print 失败。",
    "errInnerMessage3": "executeDeviceCommand: 0_0_2 BY_Print LabelPrinter-BY_Print failed.",
    "optionMessage": "Please choose option: 1:RetryCmd, 2:SkipCmd, 5:StopCurrent.",
    "creationTime": "2026-06-01T12:08:01.4912763+08:00",
}


def _import_module() -> Any:
    return importlib.import_module(MODULE_PATH)


class _FakeMaterialSynchronizer:
    def __init__(self) -> None:
        self.cache: Dict[str, Dict[str, Any]] = {}

    # --- helpers process_material_change_report 现在会调用（移动优先 upsert）---
    @staticmethod
    def _normalize_source(source: str) -> str:
        return str(source or "material_change")

    @staticmethod
    def normalize_material_aliases(material: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(material, dict):
            return material
        normalized = dict(material)
        for target, aliases in (
            ("id", ("materialId", "material_id", "bioyond_id")),
            ("name", ("materialName", "material_name")),
            ("code", ("materialCode", "barCode", "material_code")),
            ("typeName", ("materialTypeName",)),
        ):
            if normalized.get(target):
                continue
            for alias in aliases:
                if normalized.get(alias):
                    normalized[target] = normalized[alias]
                    break
        return normalized

    @staticmethod
    def _material_bioyond_id(material: Dict[str, Any]) -> str:
        return str(
            material.get("id")
            or material.get("materialId")
            or material.get("material_id")
            or material.get("bioyond_id")
            or ""
        ).strip()

    def _resolve_material_payloads(self, materials: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
        return [dict(item, _source=source) for item in materials]

    def _update_material_cache_from_stock(
        self,
        materials: List[Dict[str, Any]],
        source: str = "stock-material",
        source_scope: Any = None,
    ) -> Dict[str, int]:
        updated = 0
        for material in materials:
            material_id = str(material.get("id") or material.get("materialId") or "").strip()
            if not material_id:
                continue
            self.cache[material_id] = dict(material, source=source)
            updated += 1
        return {"records_count": len(self.cache), "updated_count": updated}


def _make_station() -> Any:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    station = object.__new__(cls)
    station.bioyond_config = {"api_host": "http://test", "api_key": "k", "warehouse_mapping": {}}
    rpc = MagicMock()
    rpc.host = "http://test"
    rpc.api_key = "k"
    rpc.material_info.return_value = {"locations": [{"whName": "自动化堆栈", "code": "1-01"}]}
    # 同步路径已切到 result-aware 取数：默认空成功，避免 MagicMock 自动属性
    # 让 _sync_order_materials_from_bioyond 误判 ok / data 形状。需要返回行的
    # 用例自行覆盖 return_value。
    rpc.materials_by_order_id_result.return_value = {"ok": True, "data": [], "message": "", "code": 1}
    station.hardware_interface = rpc
    station.resource_synchronizer = _FakeMaterialSynchronizer()
    return station


def _action_handle_items(meta: Dict[str, Any]) -> List[Any]:
    handles = meta.get("handles", [])
    if isinstance(handles, dict):
        return list(handles.get("input", [])) + list(handles.get("output", []))
    return list(handles)


def _action_handle_key(handle: Any) -> Any:
    if isinstance(handle, dict):
        return handle.get("handler_key") or handle.get("key")
    return handle.key


def _action_handle_attr(handle: Any, key: str) -> Any:
    if isinstance(handle, dict):
        return handle.get(key)
    return getattr(handle, key)


def _action_handle_keys(meta: Dict[str, Any]) -> List[Any]:
    return [_action_handle_key(handle) for handle in _action_handle_items(meta)]


def _import_bioyond_rpc_module() -> Any:
    pytest.importorskip("rclpy.logging", reason="Bioyond RPC 依赖 UniLab/ROS 运行环境")
    return importlib.import_module("bioyond_peptide_station._vendored.bioyond_rpc")


def test_build_scheduler_error_handling_reply_data_accepts_advertised_options() -> None:
    rpc_module = _import_bioyond_rpc_module()
    for option in (1, 2, 5):
        out = rpc_module.build_scheduler_error_handling_reply_data(
            ERROR_HANDLING_REPORT,
            option,
            creation_time="2026-06-01T04:09:18.271Z",
        )
        assert out == {
            "ijk": "0_0_2",
            "token": "c130d6a5-0bfd-4a8d-830d-202981714318",
            "errorHandlingOption": option,
            "creationTime": "2026-06-01T04:09:18.271Z",
        }


def test_build_scheduler_error_handling_reply_data_validates_required_fields() -> None:
    rpc_module = _import_bioyond_rpc_module()
    for field_name in ("ijk", "token"):
        payload = dict(ERROR_HANDLING_REPORT)
        payload.pop(field_name)
        with pytest.raises(ValueError, match=field_name):
            rpc_module.build_scheduler_error_handling_reply_data(payload, 1)


def test_build_scheduler_error_handling_reply_data_rejects_unadvertised_option() -> None:
    rpc_module = _import_bioyond_rpc_module()
    with pytest.raises(ValueError, match="optionMessage"):
        rpc_module.build_scheduler_error_handling_reply_data(ERROR_HANDLING_REPORT, 3)


def test_bioyond_external_error_logging_includes_structured_message(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        station_module = importlib.import_module("bioyond_peptide_station._vendored.station")
    except ModuleNotFoundError as exc:
        pytest.skip(f"BioyondWorkstation optional dependency is unavailable: {exc.name}")
    workstation = object.__new__(station_module.BioyondWorkstation)
    messages: List[str] = []

    monkeypatch.setattr(station_module.logger, "error", lambda message, *args, **kwargs: messages.append(str(message)))
    out = workstation.handle_external_error(ERROR_HANDLING_REPORT)
    joined = "\n".join(messages)

    assert out["handled"] is True
    assert out["error_type"] == "bioyond_error"
    assert "错误信息: 步骤故障\n执行设备LabelPrinterA" in joined
    assert "LabelPrinter-BY_Print failed." in joined
    assert "Please choose option: 1:RetryCmd, 2:SkipCmd, 5:StopCurrent." in joined


# ---------------------------------------------------------------------------
# 1. AST/导入面
# ---------------------------------------------------------------------------


def test_required_actions_exposed() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    required = {
        "upload_sample_excel",
        "list_sample_excels",
        "get_step_parameters",
        "submit_experiment",
        "submit_experiment_day1",
        "prepare_cem",
        "confirm_cem_info",
        "submit_experiment_day2",
        "submit_experiment_day3",
        "submit_experiment_day4",
        "submit_experiment_day4_LCMS",
        "start_experiment",
        "reset_auto",
        "reset_manual",
        "scheduler_start",
        "scheduler_stop",
        "scheduler_pause",
        "scheduler_continue",
        "update_push_ip",
        "sync_materials_from_bioyond",
        "get_order_list",
        "take_out",
        "materials_by_order_id",
        "construct_unload_table",
        "batch_cancel_experiment",
        "get_order_report",
        "get_aggregated_order_report",
        "get_order_report_files",
        "display_values",
        "display_values_manual_confirm",
    }
    have = {name for name, _ in inspect.getmembers(cls, inspect.isfunction)}
    missing = sorted(required - have)
    assert not missing, f"缺少动作: {missing}"


def test_manual_confirm_node_types() -> None:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    manual = {"confirm_cem_info", "start_experiment", "reset_manual", "display_values_manual_confirm"}
    normal = {
        "submit_experiment",
        "submit_experiment_day1",
        "prepare_cem",
        "submit_experiment_day2",
        "submit_experiment_day3",
        "submit_experiment_day4",
        "submit_experiment_day4_LCMS",
        "reset_auto",
        "scheduler_start",
        "list_sample_excels",
        "get_step_parameters",
        "sync_materials_from_bioyond",
        "get_order_list",
        "get_order_report",
        "get_order_report_files",
        "display_values",
    }
    for name in manual:
        meta = getattr(getattr(cls, name), "_action_registry_meta", {})
        assert meta.get("node_type") == module.NodeType.MANUAL_CONFIRM, name
    for name in normal:
        meta = getattr(getattr(cls, name), "_action_registry_meta", {})
        assert meta.get("node_type") != module.NodeType.MANUAL_CONFIRM, name


def test_submit_and_reset_signatures_exclude_legacy_manual_confirm() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    for name in (
        "submit_experiment",
        "submit_experiment_day2",
        "submit_experiment_day3",
        "submit_experiment_day4",
        "submit_experiment_day4_LCMS",
        "reset_auto",
    ):
        params = inspect.signature(getattr(cls, name)).parameters
        assert "timeout_seconds" not in params, name
        assert "assignee_user_ids" not in params, name


def test_day1_submit_is_normal_action_signature() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    sig = inspect.signature(cls.submit_experiment_day1)
    has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    assert not has_kwargs, "submit_experiment_day1 已是普通 action，不应接收人工确认框架字段"


def test_sync_materials_from_bioyond_syncs_and_publishes() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    station = object.__new__(cls)
    synchronizer = MagicMock()
    synchronizer.sync_from_external.return_value = True
    synchronizer.last_sync_result = {"fetched_count": 2, "converted_count": 2}
    station.resource_synchronizer = synchronizer
    station._publish_material_tree_update = MagicMock(return_value=True)
    debug_sessions = []

    @contextmanager
    def debug_session(action_name: str):
        debug_sessions.append(action_name)
        yield None

    station._debug_call_session = debug_session

    result = station.sync_materials_from_bioyond()

    assert result["success"] is True
    assert result["published"] is True
    assert result["publish_requested"] is True
    assert result["sync_result"] == {"fetched_count": 2, "converted_count": 2}
    assert result["clear_stale"] is False
    assert debug_sessions == ["sync_materials_from_bioyond"]
    synchronizer.sync_from_external.assert_called_once_with(clear_stale=False)
    station._publish_material_tree_update.assert_called_once_with("manual_full_sync")


def test_sync_materials_from_bioyond_can_skip_publish() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    station = object.__new__(cls)
    synchronizer = MagicMock()
    synchronizer.sync_from_external.return_value = True
    station.resource_synchronizer = synchronizer
    station._publish_material_tree_update = MagicMock(return_value=True)

    result = station.sync_materials_from_bioyond(publish_tree=False)

    assert result["success"] is True
    assert result["published"] is False
    assert result["publish_requested"] is False
    station._publish_material_tree_update.assert_not_called()


def test_typed_dicts_present() -> None:
    module = _import_module()
    for cls_name in (
        "PeptideGenericSubmitRequiredParams",
        "PeptideGenericSubmitOptionalParams",
        "PeptideDay1RequiredParams",
        "PeptideDay1OptionalParams",
        "PeptideDay2RequiredParams",
        "PeptideDay2OptionalParams",
        "PeptideDay3RequiredParams",
        "PeptideDay3OptionalParams",
        "PeptideDay4RequiredParams",
        "PeptideDay4OptionalParams",
        "PeptideDay4LCMSRequiredParams",
        "PeptideDay4LCMSOptionalParams",
    ):
        assert hasattr(module, cls_name), cls_name


def test_workflow_constants_split() -> None:
    module = _import_module()
    assert module.DAY4_PEPTIDE_WORKFLOW_NAME == "Day4环肽酰化-酶标"
    assert module.DAY4_LCMS_PEPTIDE_WORKFLOW_NAME == "Day4环肽酰化-酶标+LCMS"
    assert module.DAY_WORKFLOW_BINDINGS["day4_lcms"]["sub_name"] == "Day4环肽酰化-酶标LCMS"
    assert module.DAY1_CEM_METHOD_DEFAULT == "5microdouble-20250911.MPM"


# ---------------------------------------------------------------------------
# 2. Sample Excel
# ---------------------------------------------------------------------------


def test_list_sample_excels_modes() -> None:
    station = _make_station()
    records = [
        {"fileName": "DPR019-a.xlsx", "relativePath": "upload\\sample\\DPR019-a.xlsx"},
        {"fileName": "DPR019-b.xlsx", "relativePath": "upload\\sample\\DPR019-b.xlsx"},
    ]
    station._list_sample_excels = MagicMock(return_value=records)  # type: ignore[method-assign]

    info = station.list_sample_excels(sample_excel_pattern="DPR019-a", deterministic_resolve=False)
    assert "sample_excel_data" in info
    assert "sample_excel_relative_path" not in info

    resolved = station.list_sample_excels(sample_excel_pattern="DPR019-a", deterministic_resolve=True)
    assert resolved["sample_excel_relative_path"] == "upload\\sample\\DPR019-a.xlsx"

    with pytest.raises(Exception):
        station.list_sample_excels(sample_excel_pattern="DPR019", deterministic_resolve=True)


def test_resolve_submit_sample_file_direct_path() -> None:
    station = _make_station()
    relative, selected = station._resolve_submit_sample_file({}, {}, "upload/sample/x.xlsx")
    assert relative == "upload\\sample\\x.xlsx"
    assert selected["fileName"] == "x.xlsx"


def test_filename_matches_pattern_substring_and_glob() -> None:
    station = _make_station()
    assert station._filename_matches_pattern("DPR019-20260421-thrombin-5.xlsx", "DPR019")
    assert station._filename_matches_pattern("a.xlsx", "*.xlsx")
    assert not station._filename_matches_pattern("a.xlsx", "*.docx")
    assert station._filename_matches_pattern("a.xlsx", "")


# ---------------------------------------------------------------------------
# 3. Step parameter helper
# ---------------------------------------------------------------------------


def test_filter_step_parameters_preserves_zero_and_skips_unknown() -> None:
    station = _make_station()
    records = [
        {"TaskDisplayable": 1, "Value": 0, "DisplayValue": ""},
        {"TaskDisplayable": 1, "Value": "", "DisplayValue": ""},
        {"TaskDisplayable": 0, "Value": "", "DisplayValue": ""},
        {"TaskDisplayable": None, "Value": "", "DisplayValue": ""},
    ]
    filtered = station._filter_step_parameter_records(records, True, True, True)
    assert {(r.get("Value"), r.get("TaskDisplayable")) for r in filtered} == {(0, 1), ("", 1), ("", 0)}


def test_get_step_parameters_zero_match_returns_status() -> None:
    station = _make_station()
    station._query_workflow_records = MagicMock(return_value=[])  # type: ignore[method-assign]
    out = station.get_step_parameters(workflow_name_filter="不存在")
    status = out["step_parameters_raw_json"]
    assert status.get("code") == -1
    assert out["filtered_subworkflows"] == []


def test_get_step_parameters_multi_match_returns_status() -> None:
    station = _make_station()
    station._query_workflow_records = MagicMock(return_value=[  # type: ignore[method-assign]
        {"workflowId": "w1", "workflowName": "A", "subworkflowId": "s1", "subworkflowName": "A1"},
        {"workflowId": "w1", "workflowName": "A", "subworkflowId": "s2", "subworkflowName": "A2"},
    ])
    out = station.get_step_parameters(workflow_name_filter="A")
    assert out["step_parameters_raw_json"].get("code") == 0
    assert len(out["filtered_subworkflows"]) == 2


def test_get_step_parameters_direct_sub_workflow_id() -> None:
    station = _make_station()
    station._query_step_parameters = MagicMock(return_value={  # type: ignore[method-assign]
        "39c78d4b-b5d3-f721-2001-9d52000084c3": [
            {"name": "S1", "m": 0, "n": 0, "parameterList": [
                {"Key": "SampleFile", "TaskDisplayable": 1, "Value": "", "DisplayValue": ""},
            ]},
        ]
    })
    out = station.get_step_parameters(sub_workflow_id="39c78d4b-b5d3-f721-2001-9d52000084c3")
    augmented = out["step_parameters_raw_json"]
    assert augmented["code"] == 1
    assert any(p["Key"] == "SampleFile" for p in augmented["data"]["filteredParameters"])


# ---------------------------------------------------------------------------
# 4. Partial parameter entries + live resolution
# ---------------------------------------------------------------------------


def test_partial_entries_inject_samplefile_and_overrides() -> None:
    station = _make_station()
    entries, warnings = station._build_partial_parameter_entries(
        sample_excel_relative_path="upload\\sample\\f.xlsx",
        day_key="day2",
        parameter_overrides=[{"Key": "Example", "Value": 0}],
    )
    assert entries[0] == {"Key": "SampleFile", "Value": "upload\\sample\\f.xlsx"}
    assert any(e["Key"] == "Example" and e["Value"] == 0 for e in entries)
    assert warnings == []


def test_day1_cem_default_enters_required_param_flow() -> None:
    station = _make_station()
    entries, _ = station._build_partial_parameter_entries(
        sample_excel_relative_path="upload\\sample\\f.xlsx",
        day_key="day1",
        required_params={"sample_excel_pattern": "", "cem_method_file_name": ""},
        parameter_overrides=[],
    )
    assert any(e["Key"] == "CEMMethodFileName" and e["Value"] == "5microdouble-20250911.MPM" for e in entries)


def test_overrides_duplicate_last_write_wins_warning() -> None:
    station = _make_station()
    entries, warnings = station._build_partial_parameter_entries(
        sample_excel_relative_path="x",
        day_key="day2",
        parameter_overrides=[
            {"Key": "Example", "m": 0, "n": 0, "Value": "first"},
            {"Key": "Example", "m": 0, "n": 0, "Value": "second"},
        ],
    )
    example_entries = [e for e in entries if e["Key"] == "Example"]
    assert len(example_entries) == 1
    assert example_entries[0]["Value"] == "second"
    assert any("重复" in w for w in warnings)


def test_resolve_against_live_unique_match_and_failure() -> None:
    station = _make_station()
    resolved = station._resolve_parameter_entries_against_live_steps(
        [{"Key": "SampleFile", "Value": "upload\\sample\\f.xlsx"}], FLATTENED_LIVE
    )
    assert resolved[0]["step"] == "39c78d4b-b5d3-f721-2001-9d52000084c3"
    assert resolved[0]["m"] == 0 and resolved[0]["n"] == 0
    # 没有 protocol 在 m/n=0/0 处 → 0 匹配
    with pytest.raises(Exception) as exc:
        station._resolve_parameter_entries_against_live_steps(
            [{"Key": "protocol", "m": 0, "n": 0, "Value": "v"}], FLATTENED_LIVE
        )
    assert "0 条" in str(exc.value)


def test_group_resolved_entries_uses_lowercase_keys() -> None:
    station = _make_station()
    grouped = station._group_resolved_entries_to_param_values([
        {"step": "39c78d4b-b5d3-f721-2001-9d52000084c3", "Key": "SampleFile", "m": 0, "n": 0, "Value": "x"},
    ])
    step_entries = grouped["39c78d4b-b5d3-f721-2001-9d52000084c3"]
    assert step_entries[0] == {"key": "SampleFile", "value": "x", "m": 0, "n": 0}


def test_create_order_payload_shape() -> None:
    station = _make_station()
    payload = station._create_order_payload(
        order_code="EXP260518-103000",
        order_name="实验260518-103000",
        sub_workflow_id="3a1d35f9-63ce-67d6-1784-9f6abcca4eda",
        param_values={"39c78d4b-b5d3-f721-2001-9d52000084c3": [{"key": "SampleFile", "value": "x", "m": 0, "n": 0}]},
        border_number=1,
        extend_properties=None,
    )
    assert isinstance(payload, list) and len(payload) == 1
    item = payload[0]
    assert item["workFlowId"] == "3a1d35f9-63ce-67d6-1784-9f6abcca4eda"
    assert item["paramValues"]
    assert item["extendProperties"] == ""
    assert item["borderNumber"] == 1


def test_order_identity_format() -> None:
    station = _make_station()
    code, name = station._build_order_identity("day2")
    assert code.startswith("EXP") and len(code) == 16  # EXP + YYMMDD-HHmmss
    assert name.startswith("实验")
    code2, name2 = station._build_order_identity("day2", "自定义")
    assert name2 == "自定义"


# ---------------------------------------------------------------------------
# 5. Generic submit / day wrappers (含会抦住 BUG 1 的用例)
# ---------------------------------------------------------------------------


def _wire_submit_pipeline(station: Any) -> None:
    station._resolve_workflow_binding_from_names = MagicMock(return_value={  # type: ignore[method-assign]
        "workflow_name": "DAY2多肽定量",
        "root_workflow_id": "3a1d35f0-9436-895b-2eda-039a5465275e",
        "sub_workflow_id": "3a1d35f0-9f7e-c2c1-0bc0-8d94b81d90ca",
        "sub_workflow_name": "DAY2多肽定量",
        "raw": {},
    })
    station._resolve_workflow_binding = MagicMock(side_effect=lambda day_key: station._resolve_workflow_binding_from_names("DAY2多肽定量"))  # type: ignore[method-assign]
    station._query_step_parameters = MagicMock(return_value={})  # type: ignore[method-assign]
    station._flatten_step_parameters = MagicMock(return_value=FLATTENED_LIVE)  # type: ignore[method-assign]
    station._create_order = MagicMock(return_value=json.dumps(CREATE_ALLOCATION))  # type: ignore[method-assign]


def test_submit_experiment_generic_succeeds() -> None:
    """plan §「Generic And Day 1 Submit」line 919-924；这条同时抦住 BUG 1（binding= 关键字）。"""
    station = _make_station()
    _wire_submit_pipeline(station)
    result = station.submit_experiment(
        {"workflow_name": "DAY2多肽定量", "sample_excel_pattern": ""},
        {"parameter_overrides": []},
        sample_excel_relative_path="upload/sample/f.xlsx",
    )
    assert result["success"] is True
    assert result["order_id"] == ORDER_GUID
    assert result["resultTable"]["tableName"] == "resultTable"
    assert result["sample_excel_relative_path"] == "upload\\sample\\f.xlsx"


def test_submit_experiment_rejects_day1_alias() -> None:
    station = _make_station()
    with pytest.raises(Exception):
        station.submit_experiment(
            {"workflow_name": "Day1线肽合成", "sample_excel_pattern": "x"},
            {},
            sample_excel_relative_path="upload/sample/f.xlsx",
        )


def test_submit_experiment_day2_calls_pipeline() -> None:
    station = _make_station()
    _wire_submit_pipeline(station)
    station.hardware_interface.materials_by_order_id_result.return_value = {
        "ok": True,
        "data": [
            {
                "materialId": "order-mat-1",
                "materialName": "订单物料",
                "materialCode": "0007-00001",
                "quantity": 1,
                "locations": [],
            }
        ],
        "message": "",
        "code": 1,
    }
    result = station.submit_experiment_day2(
        {"sample_excel_pattern": ""},
        {"parameter_overrides": []},
        sample_excel_relative_path="upload/sample/f.xlsx",
    )
    assert result["success"] is True
    assert result["order_ids"] == [ORDER_GUID]
    assert result["auto_register_materials"] is True
    assert result["material_registration"]["status"] == "cached"
    assert result["material_registration"]["cache"]["updated_count"] == 3
    assert result["material_registration"]["order_id_sync"]["status"] == "synced"
    assert result["material_registration"]["order_id_sync"]["material_count"] == 1
    assert result["material_registration"]["order_id_sync"]["deck_sync"]["published_count"] == 0
    assert result["materials_by_order_id"][0]["materialId"] == "order-mat-1"
    order_payload = json.loads(
        station.hardware_interface.materials_by_order_id_result.call_args.args[0]
    )
    assert order_payload == {"orderId": ORDER_GUID}
    assert result["sample_excel_relative_path"] == "upload\\sample\\f.xlsx"


def test_submit_experiment_day2_can_skip_order_id_sync() -> None:
    station = _make_station()
    _wire_submit_pipeline(station)
    result = station.submit_experiment_day2(
        {"sample_excel_pattern": ""},
        {"parameter_overrides": [], "auto_register_materials": False},
        sample_excel_relative_path="upload/sample/f.xlsx",
    )

    assert result["auto_register_materials"] is False
    assert result["material_registration"]["order_id_sync"]["requested"] is False
    assert result["materials_by_order_id"] == []
    station.hardware_interface.materials_by_order_id_result.assert_not_called()


def test_submit_experiment_day1_calls_pipeline_and_injects_default_cem_method() -> None:
    station = _make_station()
    _wire_submit_pipeline(station)
    result = station.submit_experiment_day1(
        {"sample_excel_pattern": "", "cem_method_file_name": ""},
        {},
        sample_excel_relative_path="upload/sample/f.xlsx",
    )
    station._create_order.assert_called_once()
    order_payload = station._create_order.call_args.args[0]
    param_values = order_payload[0]["paramValues"]
    sent_params = [entry for values in param_values.values() for entry in values]
    assert result["success"] is True
    assert result["cem_method_file_name"] == "5microdouble-20250911.MPM"
    assert result["sample_excel_relative_path"] == "upload\\sample\\f.xlsx"
    assert any(
        entry["key"] == "CEMMethodFileName" and entry["value"] == "5microdouble-20250911.MPM"
        for entry in sent_params
    )
    assert any(entry["key"] == "SampleFile" and entry["value"] == "upload\\sample\\f.xlsx" for entry in sent_params)


def test_prepare_cem_uses_peptide_rpc_post_and_default_method() -> None:
    station = _make_station()
    station.hardware_interface.post.return_value = {"code": 1, "data": "/files/cem.pdf"}
    out = station.prepare_cem(cem_method_file_name="", sample_excel_relative_path="upload/sample/f.xlsx")
    args, kwargs = station.hardware_interface.post.call_args
    assert kwargs["url"] == "http://test/api/lims/order/prepare-cEM"
    body = kwargs["params"]
    assert body["apiKey"] == "k"
    assert body["data"] == {
        "methodFileName": "5microdouble-20250911.MPM",
        "excelPath": r"upload\sample\f.xlsx",
    }
    assert "commonlyOrderId" not in body["data"]
    assert out["success"] is True
    assert out["cem_method_file_name"] == "5microdouble-20250911.MPM"
    assert out["sample_excel_relative_path"] == "upload\\sample\\f.xlsx"
    assert out["cem_pdf_path"] == "/files/cem.pdf"
    assert out["cem_info_url"] == "http://test/files/cem.pdf"
    assert args == ()


def test_prepare_cem_preserves_raw_pdf_path_but_normalizes_url() -> None:
    station = _make_station()
    station.hardware_interface.post.return_value = {
        "code": 1,
        "data": r"upload\Report\DPR019\1-CEM.pdf",
    }
    out = station.prepare_cem(sample_excel_relative_path=r"upload\sample\f.xlsx")
    assert out["cem_pdf_path"] == r"upload\Report\DPR019\1-CEM.pdf"
    assert out["cem_info_url"] == "http://test/upload/Report/DPR019/1-CEM.pdf"


def test_prepare_cem_handle_keys() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    meta = getattr(cls.prepare_cem, "_action_registry_meta", {})
    handle_keys = _action_handle_keys(meta)
    assert "cem_method_file_name" in handle_keys
    assert "sample_excel_relative_path" in handle_keys
    assert "cem_pdf_path" in handle_keys
    assert "cem_info_url" in handle_keys
    assert "prepare_cem_response" in handle_keys


def test_prepare_cem_rejects_missing_excel_path() -> None:
    station = _make_station()
    with pytest.raises(Exception):
        station.prepare_cem(sample_excel_relative_path="")
    station.hardware_interface.post.assert_not_called()


def test_prepare_cem_rejects_non_success_response() -> None:
    station = _make_station()
    station.hardware_interface.post.return_value = {"code": 0, "message": "bad"}
    with pytest.raises(RuntimeError):
        station.prepare_cem(sample_excel_relative_path="upload/sample/f.xlsx")


def test_prepare_cem_rejects_missing_data() -> None:
    station = _make_station()
    station.hardware_interface.post.return_value = {"code": 1, "data": ""}
    with pytest.raises(RuntimeError):
        station.prepare_cem(sample_excel_relative_path="upload/sample/f.xlsx")


def test_confirm_cem_info_metadata_shape() -> None:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    meta = getattr(cls.confirm_cem_info, "_action_registry_meta", {})
    assert meta.get("node_type") == module.NodeType.MANUAL_CONFIRM
    assert meta.get("always_free") is True
    assert meta.get("placeholder_keys") == {"assignee_user_ids": "unilabos_manual_confirm"}
    assert meta.get("goal_default") == {
        "cem_info_confirmed": False,
        "timeout_seconds": 3600,
        "assignee_user_ids": [],
    }


def test_confirm_cem_info_returns_instruction_after_confirmation() -> None:
    station = _make_station()
    out = station.confirm_cem_info(
        cem_pdf_path="/files/cem.pdf",
        cem_info_url="http://test/files/cem.pdf",
        cem_method_file_name="method.MPM",
        sample_excel_relative_path="upload/sample/f.xlsx",
        cem_info_confirmed=True,
    )
    assert out["success"] is True
    assert out["cem_pdf_path"] == "/files/cem.pdf"
    assert out["cem_info_url"] == "http://test/files/cem.pdf"
    assert out["cem_method_file_name"] == "method.MPM"
    assert out["sample_excel_relative_path"] == "upload\\sample\\f.xlsx"
    assert "打开下述链接查看CEM校验信息" in out["instruction_text"]


def test_confirm_cem_info_blocks_without_confirmation() -> None:
    station = _make_station()
    with pytest.raises(RuntimeError):
        station.confirm_cem_info(cem_info_confirmed=False)


# ---------------------------------------------------------------------------
# 6. Allocation map parsing + resultTable
# ---------------------------------------------------------------------------


def test_parse_allocation_map_extracts_order_id_and_groups() -> None:
    station = _make_station()
    parsed = station._parse_create_order_allocation_map(json.dumps(CREATE_ALLOCATION))
    assert parsed["order_ids"] == [ORDER_GUID]
    assert len(parsed["allocation_rows"]) == 3
    assert set(parsed["materials_by_type"].keys()) == {"Consumables", "Sample", "Future"}


def test_parse_allocation_map_handles_python_str_repr() -> None:
    """RPC.create_order 返回的是 str(dict)，含单引号。"""
    station = _make_station()
    parsed = station._parse_create_order_allocation_map(str(CREATE_ALLOCATION))
    assert parsed["order_ids"] == [ORDER_GUID]


def test_parse_allocation_map_empty() -> None:
    station = _make_station()
    parsed = station._parse_create_order_allocation_map("{}")
    assert parsed["allocation_rows"] == []
    assert parsed["order_ids"] == []


def test_build_result_table_order_and_columns() -> None:
    station = _make_station()
    parsed = station._parse_create_order_allocation_map(json.dumps(CREATE_ALLOCATION))
    table = station._build_result_table(parsed["materials_by_type"])
    assert table["tableName"] == "resultTable"
    assert [c["key"] for c in table["columns"]] == ["whName", "locationCode", "materialName", "quantity"]
    names = [row["materialName"] for row in table["data"]]
    assert names == ["200μL枪头盒", "96孔板", "未知耗材"]
    # locationShowName 优先 locationCode
    assert table["data"][0]["locationCode"] == "1-01"
    assert table["data"][1]["locationCode"] == "A1-show"


def test_build_result_table_sorts_blank_last_and_location_naturally() -> None:
    station = _make_station()
    table = station._build_result_table({
        "Sample": [
            {"materialName": "样品", "materialId": "mat-1", "quantity": "1", "locationCode": "10-2"},
            {"materialName": "样品", "materialId": "mat-1", "quantity": "1", "locationCode": "2-02"},
            {"materialName": "", "materialId": "mat-1", "quantity": "1", "locationCode": "1-01"},
            {"materialName": "样品", "materialId": "mat-1", "quantity": "1", "locationCode": "2-01"},
        ]
    })
    assert [row["locationCode"] for row in table["data"]] == ["2-01", "2-02", "10-2", "1-01"]


def test_build_result_table_empty_returns_empty_data() -> None:
    station = _make_station()
    table = station._build_result_table({})
    assert table["data"] == []
    assert [c["key"] for c in table["columns"]] == ["whName", "locationCode", "materialName", "quantity"]


def test_resolve_wh_name_handles_material_info_failure() -> None:
    station = _make_station()
    station.hardware_interface.material_info.side_effect = RuntimeError("HTTP 500")
    cache: Dict[str, Dict[str, Any]] = {}
    assert station._resolve_wh_name_by_material_id("mat-1", cache) == ""


def test_submit_returns_warning_when_allocation_empty() -> None:
    station = _make_station()
    _wire_submit_pipeline(station)
    station._create_order = MagicMock(return_value="{}")  # type: ignore[method-assign]
    result = station.submit_experiment_day2(
        {"sample_excel_pattern": ""},
        {},
        sample_excel_relative_path="upload/sample/f.xlsx",
    )
    assert "create_order_allocation_unavailable_for_result_table" in result["warnings"]


# ---------------------------------------------------------------------------
# 7. Reports + workflow records
# ---------------------------------------------------------------------------


def test_get_order_list_passes_json_string() -> None:
    station = _make_station()
    station.hardware_interface.order_query.return_value = {"items": [], "totalCount": 0}
    station.get_order_list(filter_text="abc", page_count=10)
    args, kwargs = station.hardware_interface.order_query.call_args
    payload = json.loads(args[0])
    assert payload["filter"] == "abc"
    assert payload["pageCount"] == 10


def test_get_order_list_returns_order_handles_and_status_mapping() -> None:
    station = _make_station()
    station.hardware_interface.order_query.return_value = {
        "items": [
            {"id": "OID-1", "orderCode": "EXP-001", "name": "N1", "status": "80", "creationTime": "T1"},
            {"id": "OID-2", "code": "EXP-002", "name": "N2", "status": "60", "creationTime": "T2"},
        ],
        "totalCount": 2,
    }

    out = station.get_order_list(status="成功（80）", max_results=2, latest_only=False)

    args, _ = station.hardware_interface.order_query.call_args
    payload = json.loads(args[0])
    assert payload["status"] == "80"
    assert payload["pageCount"] == 2
    assert out["success"] is True
    assert out["order_ids"] == ["OID-1", "OID-2"]
    assert out["order_id"] == "OID-1"
    assert out["order_codes"] == ["EXP-001", "EXP-002"]
    assert out["order_code"] == "EXP-001"
    assert out["orders"][0]["order_name"] == "N1"


def test_get_order_list_warns_without_order_code() -> None:
    station = _make_station()
    station.hardware_interface.order_query.return_value = {"items": [{"id": "OID-1", "name": "N1"}], "totalCount": 1}
    out = station.get_order_list()
    assert out["order_ids"] == ["OID-1"]
    assert out["order_codes"] == []
    assert "order_list_item_missing_order_code" in out["warnings"]


def test_take_out_action_normalizes_lists_and_returns_raw_result() -> None:
    station = _make_station()
    station.hardware_interface.take_out.return_value = {"code": 1, "message": "ok"}
    out = station.take_out(" OID-1 ", preintake_ids=[" P1 ", ""], material_ids="M1")
    station.hardware_interface.take_out.assert_called_once_with("OID-1", ["P1"], ["M1"])
    assert out["success"] is True
    assert out["raw_result"] == {"code": 1, "message": "ok"}
    assert out["code"] == 1


def test_take_out_action_rejects_empty_order_id() -> None:
    station = _make_station()
    with pytest.raises(ValueError, match="order_id"):
        station.take_out("  ")
    station.hardware_interface.take_out.assert_not_called()


def test_materials_by_order_id_action_uses_order_id_payload() -> None:
    station = _make_station()
    station.hardware_interface.materials_by_order_id_result.return_value = {
        "ok": True,
        "data": [{"id": "m1", "name": "样品A"}],
        "message": "",
        "code": 1,
    }

    out = station.materials_by_order_id(" OID-1 ")

    station.hardware_interface.materials_by_order_id_result.assert_called_once()
    payload = json.loads(
        station.hardware_interface.materials_by_order_id_result.call_args.args[0]
    )
    assert payload == {"orderId": "OID-1"}
    assert out["success"] is True
    assert out["order_id"] == "OID-1"
    assert out["materials"] == [{"id": "m1", "name": "样品A"}]
    assert out["material_count"] == 1


def test_materials_by_order_id_action_rejects_empty_order_id() -> None:
    station = _make_station()
    with pytest.raises(ValueError, match="order_id"):
        station.materials_by_order_id("")
    station.hardware_interface.materials_by_order_id_result.assert_not_called()


def test_construct_unload_table_uses_shared_materials_by_order_id_core() -> None:
    station = _make_station()
    station.hardware_interface.materials_by_order_id_result.return_value = {
        "ok": True,
        "data": [
            {"id": "m1", "name": "样品A", "quantity": 1, "unit": "个", "locations": [{"code": "2-01", "whName": "WH"}]}
        ],
        "message": "",
        "code": 1,
    }

    out = station.construct_unload_table(" OID-1 ")

    payload = json.loads(
        station.hardware_interface.materials_by_order_id_result.call_args.args[0]
    )
    assert payload == {"orderId": "OID-1"}
    assert out["success"] is True
    assert out["order_id"] == "OID-1"
    assert out["materials_by_order_id"] == station.hardware_interface.materials_by_order_id_result.return_value["data"]
    assert out["resultTable"]["data"] == [
        {"whName": "WH", "locationCode": "2-01", "materialName": "样品A", "quantity": "1 个"}
    ]


def test_construct_unload_table_rejects_empty_order_id() -> None:
    station = _make_station()
    with pytest.raises(ValueError, match="order_id"):
        station.construct_unload_table("")
    station.hardware_interface.materials_by_order_id_result.assert_not_called()


def test_batch_cancel_experiment_uses_order_codes() -> None:
    station = _make_station()
    station.hardware_interface.batch_cancel_experiment.return_value = 1
    out = station.batch_cancel_experiment([" EXP-001 ", ""])
    station.hardware_interface.batch_cancel_experiment.assert_called_once_with(["EXP-001"])
    assert out == {"success": True, "order_codes": ["EXP-001"], "code": 1, "message": "批量取消成功"}


def test_batch_cancel_experiment_rejects_empty_order_codes() -> None:
    station = _make_station()
    with pytest.raises(ValueError, match="order_codes"):
        station.batch_cancel_experiment([])
    station.hardware_interface.batch_cancel_experiment.assert_not_called()


def test_direct_take_out_and_batch_cancel_metadata() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    take_out_meta = getattr(cls.take_out, "_action_registry_meta", {})
    materials_meta = getattr(cls.materials_by_order_id, "_action_registry_meta", {})
    construct_meta = getattr(cls.construct_unload_table, "_action_registry_meta", {})
    cancel_meta = getattr(cls.batch_cancel_experiment, "_action_registry_meta", {})
    assert take_out_meta.get("goal_default") == {"order_id": "", "preintake_ids": [], "material_ids": []}
    assert materials_meta.get("goal_default") == {"order_id": ""}
    assert "order_id" in _action_handle_keys(materials_meta)
    assert "materials" in _action_handle_keys(materials_meta)
    assert construct_meta.get("goal_default") == {"order_id": ""}
    construct_keys = _action_handle_keys(construct_meta)
    assert {"order_id", "materials_by_order_id", "resultTable"} <= set(construct_keys)
    assert cancel_meta.get("goal_default") == {"order_codes": []}
    assert "order_codes" in _action_handle_keys(cancel_meta)


def test_get_order_report_calls_typed_rpc() -> None:
    station = _make_station()
    station.hardware_interface.order_report.return_value = {"id": ORDER_GUID, "name": "x", "preIntakes": [], "resultList": []}
    out = station.get_order_report(ORDER_GUID)
    station.hardware_interface.order_report.assert_called_once_with(ORDER_GUID)
    assert out["success"] is True
    assert out["summary"]["id"] == ORDER_GUID


def test_get_order_report_files_handles_and_returns_file_outputs() -> None:
    station = _make_station()
    station.hardware_interface.host = "http://test"
    station.hardware_interface.order_report_files.return_value = [
        "/report/a.csv",
        "/report/result.zip",
    ]

    out = station.get_order_report_files(ORDER_GUID)

    station.hardware_interface.order_report_files.assert_called_once_with(ORDER_GUID)
    assert out["file_zip"] == "http://test/report/result.zip"
    assert out["files"] == ["http://test/report/a.csv", "http://test/report/result.zip"]
    cls = getattr(_import_module(), CLASS_NAME)
    meta = getattr(cls.get_order_report_files, "_action_registry_meta", {})
    handle_keys = _action_handle_keys(meta)
    assert {"order_id", "file_zip", "files"} <= set(handle_keys)


def test_display_values_returns_title_and_values() -> None:
    station = _make_station()
    values = {"files": ["http://test/report.zip"], "count": 1}

    out = station.display_values(title="报告内容", values=values)

    assert out == {
        "success": True,
        "title": "报告内容",
        "values": json.dumps(values, ensure_ascii=False, indent=2),
    }


def test_display_values_manual_confirm_metadata_and_return() -> None:
    module = _import_module()
    cls = getattr(module, CLASS_NAME)
    meta = getattr(cls.display_values_manual_confirm, "_action_registry_meta", {})
    assert meta.get("node_type") == module.NodeType.MANUAL_CONFIRM
    assert meta.get("placeholder_keys") == {"assignee_user_ids": "unilabos_manual_confirm"}
    assert meta.get("goal_default") == {
        "title": "",
        "values": None,
        "display_confirmed": False,
        "timeout_seconds": 3600,
        "assignee_user_ids": [],
    }
    handle_keys = _action_handle_keys(meta)
    assert {"values", "assignee_user_ids", "title"} <= set(handle_keys)
    values_handles = [handle for handle in _action_handle_items(meta) if _action_handle_key(handle) == "values"]
    assert values_handles
    assert all(_action_handle_attr(handle, "data_type") == "str" for handle in values_handles)

    station = _make_station()
    values = ["http://test/a.csv", "http://test/report.zip"]
    out = station.display_values_manual_confirm(
        title="报告文件",
        values=values,
        display_confirmed=True,
        assignee_user_ids=["u1"],
    )
    assert out["success"] is True
    assert out["title"] == "报告文件"
    assert out["values"] == json.dumps(values, ensure_ascii=False, indent=2)
    assert out["display_confirmed"] is True
    assert out["assignee_user_ids"] == ["u1"]


def test_display_values_manual_confirm_blocks_without_confirmation() -> None:
    station = _make_station()
    with pytest.raises(RuntimeError, match="展示内容"):
        station.display_values_manual_confirm(display_confirmed=False)


def test_get_aggregated_order_report_is_todo_placeholder() -> None:
    station = _make_station()
    out = station.get_aggregated_order_report(ORDER_GUID)
    assert out["status"] == "not_implemented"


def test_query_workflow_records_filters_unsaved_subworkflows() -> None:
    station = _make_station()
    station.hardware_interface.query_workflow.return_value = {
        "items": [
            {
                "id": "rid",
                "name": "Day3线肽环化",
                "subWorkflows": [
                    {"id": "saved-id", "name": "Day3线肽环化", "isSaved": True},
                    {"id": "draft-id", "name": "Day3线肽环化-草稿", "isSaved": False},
                ],
            }
        ]
    }
    records = station._query_workflow_records("Day3线肽环化")
    assert [r["subworkflowId"] for r in records] == ["saved-id"]


# ---------------------------------------------------------------------------
# 8. Debug / fetch_workflow_list 守护
# ---------------------------------------------------------------------------


def test_module_fetch_workflow_list_is_debug_guarded() -> None:
    module = _import_module()
    assert module.DEBUG_CLI_ENABLED is False
    with pytest.raises(AssertionError):
        module.fetch_workflow_list(config={"api_host": "http://x", "api_key": "k"})


def test_station_fetch_workflow_list_uses_rpc() -> None:
    station = _make_station()
    station.hardware_interface.query_workflow.return_value = {"items": [], "totalCount": 0}
    station.fetch_workflow_list(filter_text="Day2")
    args, _ = station.hardware_interface.query_workflow.call_args
    payload = json.loads(args[0])
    assert payload["filter"] == "Day2"
    assert payload["includeDetail"] is True


# ---------------------------------------------------------------------------
# 9. start_experiment 装载闸门
# ---------------------------------------------------------------------------


def test_start_experiment_blocks_when_materials_not_loaded() -> None:
    station = _make_station()
    station.hardware_interface.scheduler_start.return_value = 1
    with pytest.raises(RuntimeError):
        station.start_experiment(
            order_id=ORDER_GUID,
            resultTable={"data": [{"materialName": "x"}]},
            materials_loaded=False,
        )


def test_start_experiment_starts_when_table_empty() -> None:
    station = _make_station()
    station.hardware_interface.scheduler_start.return_value = 1
    result = station.start_experiment(order_id=ORDER_GUID, resultTable={"data": []})
    assert result["success"] is True
    assert result["order_ids"] == [ORDER_GUID]


# ---------------------------------------------------------------------------
# 10. Reset (plan 2026-05-21: reset_auto + reset_manual 四勾选)
# ---------------------------------------------------------------------------


RESET_BOOL_PARAMS = (
    "reset_scheduler",
    "reset_order_status",
    "reset_location",
    "reset_devices",
)


def _reset_meta(name: str) -> Dict[str, Any]:
    cls = getattr(_import_module(), CLASS_NAME)
    return dict(getattr(getattr(cls, name), "_action_registry_meta", {}))


# --- plan §Tests 1: reset_auto 不是 MANUAL_CONFIRM ---


def test_reset_auto_is_not_manual_confirm() -> None:
    module = _import_module()
    meta = _reset_meta("reset_auto")
    assert meta.get("node_type") != module.NodeType.MANUAL_CONFIRM


# --- plan §Tests 2: reset_manual 是 MANUAL_CONFIRM ---


def test_reset_manual_is_manual_confirm() -> None:
    module = _import_module()
    meta = _reset_meta("reset_manual")
    assert meta.get("node_type") == module.NodeType.MANUAL_CONFIRM


# --- plan §Tests 3: reset_manual 关键 metadata ---


def test_reset_manual_metadata_shape() -> None:
    meta = _reset_meta("reset_manual")
    assert meta.get("always_free") is True
    assert meta.get("placeholder_keys") == {
        "assignee_user_ids": "unilabos_manual_confirm",
    }
    goal_default = meta.get("goal_default") or {}
    assert goal_default.get("timeout_seconds") == 3600
    assert goal_default.get("assignee_user_ids") == []
    assert goal_default.get("physical_cleanup_confirmed") is False


# --- plan §Tests 4: 两个 action 都暴露 4 个真实 bool 参数 ---


def _resolved_bool_annotation(param: inspect.Parameter) -> Any:
    """`from __future__ import annotations` 下注解是字符串；统一解析回真实类型。"""
    annotation = param.annotation
    if annotation is bool:
        return bool
    if isinstance(annotation, str):
        # 既不是 Annotated[...] 也不是 Optional[...] 的纯 "bool" 字符串
        return bool if annotation.strip() == "bool" else annotation
    return annotation


def test_reset_actions_expose_four_real_bool_params() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    for action_name in ("reset_auto", "reset_manual"):
        params = inspect.signature(getattr(cls, action_name)).parameters
        for flag in RESET_BOOL_PARAMS:
            assert flag in params, f"{action_name} 缺少 {flag}"
            resolved = _resolved_bool_annotation(params[flag])
            assert resolved is bool, (
                f"{action_name}.{flag} 必须是裸 bool（不能用 Annotated[bool, Field(...)] 包裹），实际: {params[flag].annotation!r}"
            )
            assert params[flag].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ), f"{action_name}.{flag} 必须是真实参数，不能藏在 **kwargs"


# --- plan §Tests 5: registry 生成的 schema 标记 reset 字段为 boolean ---


def test_reset_action_param_annotations_are_bool_for_schema() -> None:
    """plan: 当前 AST registry 不 unwrap Annotated；裸 bool 才能映射成 type: boolean。

    这里直接检查 type_to_schema 在 Python 类型 ``bool`` 上返回 ``{"type": "boolean"}``，
    再校验 reset_auto/reset_manual 的真实参数注解就是 ``bool``（裸字符串 "bool" 也算），
    从而保证生成的 JSON Schema 一定是 boolean，不会被前端当成 object/string。
    """
    from unilabos.registry.utils import type_to_schema

    assert type_to_schema(bool) == {"type": "boolean"}

    cls = getattr(_import_module(), CLASS_NAME)
    for action_name in ("reset_auto", "reset_manual"):
        params = inspect.signature(getattr(cls, action_name)).parameters
        for flag in RESET_BOOL_PARAMS:
            resolved = _resolved_bool_annotation(params[flag])
            assert type_to_schema(resolved) == {"type": "boolean"}, (
                f"{action_name}.{flag} schema 必须是 boolean，实际注解: {params[flag].annotation!r}"
            )


# --- plan §Tests 6: reset_auto 替换旧 reset，未保留旧 id-shaped reset 别名 ---


def test_legacy_reset_action_removed() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    have = {name for name, _ in inspect.getmembers(cls, inspect.isfunction)}
    assert "reset" not in have, "旧 reset 应被 reset_auto 完全替换，不保留同名别名"
    assert "reset_auto" in have


# --- plan §Tests 7: goal_default 中前三项 True，reset_devices=False ---


def test_reset_goal_defaults_first_three_true_devices_false() -> None:
    for action_name in ("reset_auto", "reset_manual"):
        meta = _reset_meta(action_name)
        goal_default = meta.get("goal_default") or {}
        assert goal_default.get("reset_scheduler") is True, action_name
        assert goal_default.get("reset_order_status") is True, action_name
        assert goal_default.get("reset_location") is True, action_name
        assert goal_default.get("reset_devices") is False, action_name


# --- plan §Tests 8: reset_manual(physical_cleanup_confirmed=False) 不调任何 RPC ---


def test_reset_manual_blocks_when_not_confirmed() -> None:
    station = _make_station()
    out = station.reset_manual(
        reset_scheduler=True,
        reset_order_status=True,
        reset_location=True,
        reset_devices=True,
        physical_cleanup_confirmed=False,
    )
    rpc = station.hardware_interface
    rpc.scheduler_reset.assert_not_called()
    rpc.reset_order_status.assert_not_called()
    rpc.reset_location.assert_not_called()
    rpc.reset_devices.assert_not_called()
    assert out["status"] == "blocked"
    assert out["physical_cleanup_confirmed"] is False
    assert "请确认" in out["confirmation_message"]


# --- plan §Tests 9: reset_auto() 默认调三件、不调 reset_devices ---


def test_reset_auto_defaults_call_three_and_skip_devices() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    out = station.reset_auto()
    rpc.scheduler_reset.assert_called_once_with()
    rpc.reset_order_status.assert_called_once_with()
    rpc.reset_location.assert_called_once_with()
    rpc.reset_devices.assert_not_called()

    executed_ops = [item["operation"] for item in out["executed_calls"]]
    assert executed_ops == ["reset_scheduler", "reset_order_status", "reset_location"]
    skipped_ops = {item["operation"] for item in out["skipped_operations"]}
    assert skipped_ops == {"reset_devices"}
    selected = {item["key"]: item["selected"] for item in out["selected_operations"]}
    assert selected == {
        "reset_scheduler": True,
        "reset_order_status": True,
        "reset_location": True,
        "reset_devices": False,
    }


# --- plan §Tests 10: reset_auto(reset_devices=True) 也会调 reset_devices ---


def test_reset_auto_with_devices_true_calls_reset_devices() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    rpc.reset_devices.return_value = 1
    out = station.reset_auto(reset_devices=True)
    rpc.reset_devices.assert_called_once_with()
    executed_ops = [item["operation"] for item in out["executed_calls"]]
    assert executed_ops == [
        "reset_scheduler",
        "reset_order_status",
        "reset_location",
        "reset_devices",
    ]
    assert out["skipped_operations"] == []


def test_reset_auto_individual_checkboxes_drive_calls() -> None:
    """更细粒度：单独勾 reset_scheduler 时只调 scheduler_reset。"""
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    out = station.reset_auto(
        reset_scheduler=True,
        reset_order_status=False,
        reset_location=False,
        reset_devices=False,
    )
    rpc.scheduler_reset.assert_called_once_with()
    rpc.reset_order_status.assert_not_called()
    rpc.reset_location.assert_not_called()
    rpc.reset_devices.assert_not_called()
    skipped = {item["operation"] for item in out["skipped_operations"]}
    assert skipped == {"reset_order_status", "reset_location", "reset_devices"}


def test_reset_manual_after_confirmation_calls_same_helper() -> None:
    """plan §reset_manual 执行规则：勾选确认后等价于 reset_auto。"""
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    rpc.reset_devices.return_value = 1
    out = station.reset_manual(
        reset_scheduler=True,
        reset_order_status=True,
        reset_location=True,
        reset_devices=True,
        physical_cleanup_confirmed=True,
    )
    rpc.scheduler_reset.assert_called_once_with()
    rpc.reset_order_status.assert_called_once_with()
    rpc.reset_location.assert_called_once_with()
    rpc.reset_devices.assert_called_once_with()
    assert out["physical_cleanup_confirmed"] is True
    assert out["confirmation_message"]
    assert [item["operation"] for item in out["executed_calls"]] == [
        "reset_scheduler",
        "reset_order_status",
        "reset_location",
        "reset_devices",
    ]


# --- plan §Tests 11: RPC 包装层 reset_order_status / reset_location 不发送 data 键 ---


def test_rpc_reset_order_status_sends_no_data_key() -> None:
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC

    rpc = object.__new__(BioyondV1RPC)
    rpc.host = "http://test"
    rpc.api_key = "k"
    rpc._logger = MagicMock()
    rpc.post = MagicMock(return_value={"code": 1})  # type: ignore[method-assign]
    rpc.get_current_time_iso8601 = MagicMock(return_value="2026-05-21T08:00:00.000Z")  # type: ignore[method-assign]

    rpc.reset_order_status("ignored-uuid")
    args, kwargs = rpc.post.call_args
    sent_params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
    assert "data" not in sent_params, "reset_order_status 不应再发送 data 字段"
    assert set(sent_params.keys()) == {"apiKey", "requestTime"}


def test_rpc_reset_location_sends_no_data_key() -> None:
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC

    rpc = object.__new__(BioyondV1RPC)
    rpc.host = "http://test"
    rpc.api_key = "k"
    rpc._logger = MagicMock()
    rpc.post = MagicMock(return_value={"code": 1})  # type: ignore[method-assign]
    rpc.get_current_time_iso8601 = MagicMock(return_value="2026-05-21T08:00:00.000Z")  # type: ignore[method-assign]

    rpc.reset_location("ignored-loc-id")
    args, kwargs = rpc.post.call_args
    sent_params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
    assert "data" not in sent_params, "reset_location 不应再发送 data 字段"
    assert set(sent_params.keys()) == {"apiKey", "requestTime"}


def test_rpc_constructor_can_skip_startup_material_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC

    load_cache = MagicMock()
    monkeypatch.setattr(BioyondV1RPC, "_load_material_cache", load_cache)

    rpc = BioyondV1RPC({
        "api_key": "k",
        "api_host": "http://offline.invalid",
        "warehouse_mapping": {},
        "load_material_cache_on_init": False,
    })

    assert rpc.material_cache == {}
    load_cache.assert_not_called()


def test_rpc_constructor_loads_material_cache_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC

    load_cache = MagicMock()
    monkeypatch.setattr(BioyondV1RPC, "_load_material_cache", load_cache)

    BioyondV1RPC({
        "api_key": "k",
        "api_host": "http://offline.invalid",
        "warehouse_mapping": {},
    })

    load_cache.assert_called_once_with()


def test_update_push_ip_defaults_to_config_and_returns_raw() -> None:
    station = _make_station()
    station.bioyond_config.update({"HTTP_host": "127.0.0.1", "HTTP_port": 8090})
    rpc = station.hardware_interface
    rpc.set_ip_config.return_value = {"code": 1, "message": "ok", "data": None}

    out = station.update_push_ip()

    rpc.set_ip_config.assert_called_once_with("127.0.0.1", 8090)
    assert out == {
        "success": True,
        "ip": "127.0.0.1",
        "port": 8090,
        "raw": {"code": 1, "message": "ok", "data": None},
        "message": "ok",
    }


def test_update_push_ip_returns_failure_message() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.set_ip_config.return_value = {"code": 0, "message": "bad"}

    out = station.update_push_ip("127.0.0.1", 8090)

    assert out["success"] is False
    assert out["raw"] == {"code": 0, "message": "bad"}
    assert out["message"] == "bad"


def test_update_push_ip_exposes_ip_and_port_metadata() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    signature = inspect.signature(cls.update_push_ip)
    assert signature.parameters["ip"].default == ""
    assert signature.parameters["port"].default == 0
    meta = getattr(cls.update_push_ip, "_action_registry_meta", {})
    assert meta.get("always_free") is True
    assert "推送" in meta.get("description", "")


def test_rpc_set_ip_config_sends_put_envelope() -> None:
    rpc_module = _import_bioyond_rpc_module()

    rpc = object.__new__(rpc_module.BioyondV1RPC)
    rpc.host = "http://test"
    rpc.api_key = "k"
    rpc._logger = MagicMock()
    rpc.get_current_time_iso8601 = MagicMock(return_value="2026-06-07T08:00:00.000Z")  # type: ignore[method-assign]
    response = MagicMock(
        status_code=200,
        text='{"code": 1, "message": "ok"}',
        request=SimpleNamespace(body="request-body"),
    )
    response.json.return_value = {"code": 1, "message": "ok"}
    rpc_module.requests.put = MagicMock(return_value=response)

    out = rpc.set_ip_config("127.0.0.1", "8090")

    assert out == {"code": 1, "message": "ok"}
    rpc_module.requests.put.assert_called_once_with(
        url="http://test/api/lims/order/ip-config",
        data=json.dumps({
            "apiKey": "k",
            "requestTime": "2026-06-07T08:00:00.000Z",
            "data": {"ip": "127.0.0.1", "port": 8090},
        }),
        headers={"Content-Type": "application/json"},
        timeout=120,
    )


def test_rpc_set_ip_config_rejects_invalid_port() -> None:
    rpc_module = _import_bioyond_rpc_module()

    rpc = object.__new__(rpc_module.BioyondV1RPC)
    rpc.host = "http://test"
    rpc.api_key = "k"
    rpc_module.requests.put = MagicMock()
    with pytest.raises(ValueError, match="port"):
        rpc.set_ip_config("127.0.0.1", 70000)
    rpc_module.requests.put.assert_not_called()


def test_rpc_set_ip_config_rejects_blank_ip() -> None:
    rpc_module = _import_bioyond_rpc_module()

    rpc = object.__new__(rpc_module.BioyondV1RPC)
    rpc_module.requests.put = MagicMock()
    with pytest.raises(ValueError, match="ip"):
        rpc.set_ip_config("", 8090)
    rpc_module.requests.put.assert_not_called()


# --- reset: 不调用 take_out / legacy refresh；库位复位成功后走全量物料同步 ---


def test_reset_paths_do_not_call_take_out_or_legacy_material_cache_refresh() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    rpc.reset_devices.return_value = 1

    station.reset_auto(reset_devices=True)
    station.reset_manual(physical_cleanup_confirmed=True, reset_devices=True)
    station.reset_manual(physical_cleanup_confirmed=False)

    rpc.take_out.assert_not_called()
    refresh = getattr(rpc, "refresh_material_cache", None)
    if refresh is not None and hasattr(refresh, "assert_not_called"):
        refresh.assert_not_called()


def test_reset_location_success_syncs_materials_and_publishes() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 1
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    synchronizer = MagicMock()
    synchronizer.sync_from_external.return_value = True
    synchronizer.last_sync_result = {"fetched_count": 1, "converted_count": 1}
    station.resource_synchronizer = synchronizer
    station._publish_material_tree_update = MagicMock(return_value=True)

    out = station.reset_auto()

    assert out["material_sync"]["success"] is True
    assert out["material_sync"]["clear_stale"] is True
    synchronizer.sync_from_external.assert_called_once_with(clear_stale=True)
    station._publish_material_tree_update.assert_called_once_with("manual_full_sync")


# --- 失败/兜底用例：不 fail-fast，单步异常或 code!=1 仅记 warning ---


def test_reset_auto_records_warning_when_rpc_returns_non_one() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.return_value = 0  # 业务失败
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    out = station.reset_auto()
    rpc.scheduler_reset.assert_called_once_with()
    rpc.reset_order_status.assert_called_once_with()
    rpc.reset_location.assert_called_once_with()
    assert any("reset_scheduler" in w for w in out.get("warnings", []))


def test_reset_auto_continues_after_rpc_exception() -> None:
    station = _make_station()
    rpc = station.hardware_interface
    rpc.scheduler_reset.side_effect = RuntimeError("HTTP 500")
    rpc.reset_order_status.return_value = 1
    rpc.reset_location.return_value = 1
    out = station.reset_auto()
    rpc.reset_order_status.assert_called_once_with()
    rpc.reset_location.assert_called_once_with()
    error_entries = [item for item in out["executed_calls"] if "error" in item]
    assert any(item["operation"] == "reset_scheduler" for item in error_entries)


def test_reset_manual_confirmation_message_constant() -> None:
    module = _import_module()
    msg = module.RESET_MANUAL_CONFIRM_MESSAGE
    for keyword in ("G3", "CEM", "Tecan", "撕膜机", "封膜机", "打标机", "旋转堆栈", "转台", "冰箱", "IDOT", "酶标仪", "离心机", "LCMS"):
        assert keyword in msg, f"reset_manual 提示文案缺关键字: {keyword}"
    meta = _reset_meta("reset_manual")
    assert meta.get("description") == msg, "reset_manual 装饰器 description 应等于常量本身"


# ---------------------------------------------------------------------------
# 11. wait_for_order_finish + unload_materials（plan v2：节点注册契约）
# 注：wait/unload/process_order_finish_report 的行为测试已迁移到
#     test_peptide_wait_unload.py（deep_clone sirna v2）。
# ---------------------------------------------------------------------------


def test_new_actions_registered_on_class() -> None:
    cls = getattr(_import_module(), CLASS_NAME)
    have = {name for name, _ in inspect.getmembers(cls, inspect.isfunction)}
    assert "wait_for_order_finish" in have
    assert "unload_materials" in have

    module = _import_module()
    unload_meta = getattr(cls.unload_materials, "_action_registry_meta", {})
    assert unload_meta.get("node_type") == module.NodeType.MANUAL_CONFIRM
    wait_meta = getattr(cls.wait_for_order_finish, "_action_registry_meta", {})
    assert wait_meta.get("node_type") != module.NodeType.MANUAL_CONFIRM
    assert wait_meta.get("always_free") is True
