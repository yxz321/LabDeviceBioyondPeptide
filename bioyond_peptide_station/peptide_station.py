"""Bioyond 多肽工作站：LIMS 提交/复位/调度与样品 Excel 工作流。"""

from __future__ import annotations

import ast
import copy
import json
import mimetypes
import re
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Tuple
from urllib.parse import quote
from uuid import UUID

import requests

try:
    from typing_extensions import TypedDict
except ImportError:  # pragma: no cover
    from typing import TypedDict  # type: ignore

try:
    from pydantic import Field
except Exception:  # pragma: no cover
    def Field(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return kwargs

if __package__ in {None, ""}:
    # 直接以脚本方式运行时，把包父目录（含 bioyond_peptide_station/）加入 sys.path，
    # 使 `import bioyond_peptide_station...` 可用。外部包布局下父目录是 parents[1]
    # （旧 monorepo 的 parents[5] 在浅路径下会 IndexError）。
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

from unilabos.utils.log import logger
from bioyond_peptide_station.resources.peptide_materials import DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS

try:
    from unilabos.registry.decorators import (
        ActionInputHandle,
        ActionOutputHandle,
        DataSource,
        NodeType,
        action,
        device,
    )
    _REGISTRY_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover
    _REGISTRY_IMPORT_ERROR = exc

    class NodeType:  # type: ignore[no-redef]
        MANUAL_CONFIRM = "manual_confirm"

    class DataSource:  # type: ignore[no-redef]
        HANDLE = "handle"
        EXECUTOR = "executor"

    class _FallbackActionHandle:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class ActionInputHandle(_FallbackActionHandle):  # type: ignore[no-redef]
        pass

    class ActionOutputHandle(_FallbackActionHandle):  # type: ignore[no-redef]
        pass

    def device(*args: Any, **kwargs: Any):
        def decorator(cls):
            return cls

        return decorator

    def action(*args: Any, **kwargs: Any):
        if len(args) == 1 and callable(args[0]):
            func = args[0]
            func._action_registry_meta = {}  # type: ignore[attr-defined]
            return func

        def decorator(func):
            func._action_registry_meta = dict(kwargs)  # type: ignore[attr-defined]
            return func

        return decorator

try:
    from unilabos.devices.workstation.workstation_base import WorkstationBase
    from bioyond_peptide_station._vendored.station import BioyondWorkstation
    _BIOYOND_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover
    WorkstationBase = object  # type: ignore[assignment,misc]
    BioyondWorkstation = object  # type: ignore[assignment,misc]
    _BIOYOND_IMPORT_ERROR = exc


DEBUG_CLI_ENABLED = False
RESET_OPERATION_KEYS: Tuple[str, ...] = (
    "reset_scheduler",
    "reset_order_status",
    "reset_location",
    "reset_devices",
)
RESET_OPERATION_LABELS: Dict[str, str] = {
    "reset_scheduler": "调度器复位",
    "reset_order_status": "订单状态复位",
    "reset_location": "库位复位",
    "reset_devices": "仪器复位",
}
RESET_OPERATION_ENDPOINTS: Dict[str, str] = {
    "reset_scheduler": "/api/lims/scheduler/reset",
    "reset_order_status": "/api/lims/order/reset-order-status",
    "reset_location": "/api/lims/storage/reset-location",
    "reset_devices": "/api/lims/device/reset-devices",
}
RESET_MANUAL_CONFIRM_MESSAGE = (
    "请确认G3、CEM、Tecan、撕膜机、封膜机、打标机、旋转堆栈上下料位、3个转台等位置的物料已清理完毕；\n"
    "请开门检查冰箱、IDOT、酶标仪、离心机、LCMS内部没有遗留物料。"
)
CEM_INFO_CONFIRM_MESSAGE = "打开下述链接查看CEM校验信息，确认无误后勾选 cem_info_confirmed。"
RESULT_TABLE_COLUMNS = [
    {"name": "设备", "key": "whName"},
    {"name": "位置", "key": "locationCode"},
    {"name": "物料名称", "key": "materialName"},
    {"name": "数量", "key": "quantity"},
]
# 「下料指引表」列结构（plan v2：4 列，与上料表 _build_result_table 一致，便于前端复用渲染逻辑）
UNLOAD_TABLE_COLUMNS: List[Dict[str, str]] = [
    {"name": "设备", "key": "whName"},
    {"name": "位置", "key": "locationCode"},
    {"name": "物料名称", "key": "materialName"},
    {"name": "数量", "key": "quantity"},
]


# Bioyond /report/order_finish 推送 status 字段语义映射（与 cell workstation 对齐）。
ORDER_FINISH_STATUS_MAP: Dict[str, str] = {
    "30": "success",
    "-11": "abnormal_stop",
    "-12": "manual_stop",
}
OrderStatus = Literal['全部（""）', "成功（80）", "失败（90）", "执行中（60）", "已取出（100）"]
ORDER_STATUS_VALUE_MAP: Dict[str, str] = {
    '全部（""）': "",
    "成功（80）": "80",
    "失败（90）": "90",
    "执行中（60）": "60",
    "已取出（100）": "100",
    "": "",
    "80": "80",
    "90": "90",
    "60": "60",
    "100": "100",
}
MATERIAL_TYPE_ORDER = ("Sample", "Consumables", "Reagent")
PEPTIDE_SAMPLE_FILE_KEY = "SampleFile"
DAY1_CEM_METHOD_KEY = "CEMMethodFileName"
DAY1_CEM_METHOD_DEFAULT = "5microdouble-20250911.MPM"

# 绑定信息（最后更新 2026-05-16）
DAY1_PEPTIDE_WORKFLOW_NAME = "Day1线肽合成"
DAY2_PEPTIDE_WORKFLOW_NAME = "DAY2多肽定量"
DAY3_PEPTIDE_WORKFLOW_NAME = "Day3线肽环化"
DAY4_PEPTIDE_WORKFLOW_NAME = "Day4环肽酰化-酶标"
DAY4_LCMS_PEPTIDE_WORKFLOW_NAME = "Day4环肽酰化-酶标+LCMS"
DAY4_LCMS_SUB_WORKFLOW_NAME = "Day4环肽酰化-酶标LCMS"

DAY_WORKFLOW_BINDINGS: Dict[str, Dict[str, str]] = {
    "day1": {"root_name": DAY1_PEPTIDE_WORKFLOW_NAME, "sub_name": DAY1_PEPTIDE_WORKFLOW_NAME},
    "day2": {"root_name": DAY2_PEPTIDE_WORKFLOW_NAME, "sub_name": DAY2_PEPTIDE_WORKFLOW_NAME},
    "day3": {"root_name": DAY3_PEPTIDE_WORKFLOW_NAME, "sub_name": DAY3_PEPTIDE_WORKFLOW_NAME},
    "day4": {"root_name": DAY4_PEPTIDE_WORKFLOW_NAME, "sub_name": DAY4_PEPTIDE_WORKFLOW_NAME},
    "day4_lcms": {"root_name": DAY4_LCMS_PEPTIDE_WORKFLOW_NAME, "sub_name": DAY4_LCMS_SUB_WORKFLOW_NAME},
}


class PeptideWorkflowError(RuntimeError):
    """多肽工作流可恢复错误。"""


class PeptideCommonSubmitOptionalParams(TypedDict, total=False):
    order_name: Annotated[str, Field(description="订单名称；为空时自动生成，用户可覆盖。")]
    auto_register_materials: Annotated[bool, Field(default=True, description="是否自动按订单ID查询并缓存返回的物料信息；默认勾选，不做全量库存同步。")]
    parameter_overrides: Annotated[
        List[Dict[str, Any]],
        Field(
            default_factory=list,
            description=(
                "参数覆盖列表，默认留空（不覆盖）。"
                "如需覆盖子工作流某个步骤参数，按 [{\"Key\": \"参数名\", \"Value\": \"值\", \"m\": 0, \"n\": 0}] 格式填写。"
                "Key 必须与 Bioyond 子工作流里某个 step 参数名精确匹配；m/n 可选，省略时 Key 在工作流内必须唯一。"
            ),
        ),
    ]
    border_number: Annotated[int, Field(default=1, description="LIMS 创建订单 borderNumber，默认 1。")]
    extend_properties: Annotated[str, Field(description="LIMS extendProperties 字符串；默认不传或传空。")]


class PeptideGenericSubmitRequiredParams(TypedDict):
    workflow_name: Annotated[str, Field(description="Bioyond 根工作流名称；用于解析一个非 Day1 子工作流。")]
    sample_excel_pattern: Annotated[str, Field(description="样品 Excel 文件名匹配模式。若通过上游句柄提供 sample_excel_relative_path，可留空。")]


class PeptideGenericSubmitOptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    subworkflow_name: Annotated[str, Field(description="Bioyond 子工作流名称过滤；为空时 workflow_name 下必须只有一个可用子工作流。")]


class PeptideDay1RequiredParams(TypedDict):
    sample_excel_pattern: Annotated[str, Field(description="样品 Excel 文件名匹配模式。若通过上游句柄提供 sample_excel_relative_path，可留空。")]
    cem_method_file_name: Annotated[
        str,
        Field(default=DAY1_CEM_METHOD_DEFAULT, description="Day1 CEM 方法文件名称，默认 5microdouble-20250911.MPM。"),
    ]


class PeptideDay1OptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    pass


class PeptideDay2RequiredParams(TypedDict):
    sample_excel_pattern: Annotated[str, Field(description="样品 Excel 文件名匹配模式。若通过上游句柄提供 sample_excel_relative_path，可留空。")]


class PeptideDay2OptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    pass


class PeptideDay3RequiredParams(PeptideDay2RequiredParams):
    pass


class PeptideDay3OptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    pass


class PeptideDay4RequiredParams(PeptideDay2RequiredParams):
    pass


class PeptideDay4OptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    pass


class PeptideDay4LCMSRequiredParams(PeptideDay2RequiredParams):
    pass


class PeptideDay4LCMSOptionalParams(PeptideCommonSubmitOptionalParams, total=False):
    pass


def _apply_default_peptide_material_type_mappings(config: Dict[str, Any]) -> None:
    configured = config.get("material_type_mappings")
    if not isinstance(configured, dict):
        configured = {}
    merged = dict(DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS)
    merged.update(configured)
    config["material_type_mappings"] = merged


def _utc_now_iso8601_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_peptide_config(config_path: str | Path) -> Dict[str, Any]:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def fetch_workflow_list(
    config: Optional[Dict[str, Any]] = None,
    config_path: Optional[str | Path] = None,
    workflow_type: int = 0,
    filter_text: str = "",
    include_detail: bool = True,
) -> Dict[str, Any]:
    """调试专用：直接 HTTP 拉取工作流列表。运行时应使用 BioyondPeptideStation.fetch_workflow_list。"""
    assert DEBUG_CLI_ENABLED, "模块级 fetch_workflow_list 仅供调试；运行时请调用站点实例方法"
    resolved_config = dict(config or {})
    if config_path is not None:
        resolved_config.update(load_peptide_config(config_path))
    api_host = str(resolved_config.get("api_host", "")).rstrip("/")
    api_key = str(resolved_config.get("api_key", ""))
    timeout = int(resolved_config.get("timeout", 10))
    if not api_host or not api_key:
        raise ValueError("缺少 api_host/api_key 配置")
    url = f"{api_host}/api/lims/workflow/work-flow-list"
    payload = {
        "apiKey": api_key,
        "requestTime": _utc_now_iso8601_ms(),
        "data": {"type": workflow_type, "filter": filter_text, "includeDetail": include_detail},
    }
    result: Dict[str, Any] = {"url": url, "request_payload": payload}
    try:
        response = requests.post(url, json=payload, timeout=timeout, headers={"Content-Type": "application/json"})
        result["http_status"] = response.status_code
        try:
            result["response"] = response.json()
        except ValueError:
            result["response"] = {"raw_text": response.text}
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


@device(
    id="bioyond_peptide_station",
    category=["workstation", "bioyond", "bioyond_peptide_station"],
    description="Bioyond 多肽合成工作站",
    display_name="Bioyond Peptide Station",
    icon="preparation_station.webp",
)
class BioyondPeptideStation(BioyondWorkstation):
    """多肽 LIMS 工作站。"""

    _REQUIRED_CONFIG_KEYS = ("api_key", "api_host", "warehouse_mapping")

    def __init__(
        self,
        bioyond_config: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str | Path] = None,
        deck: Optional[Any] = None,
        protocol_type: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        if _BIOYOND_IMPORT_ERROR is not None:
            raise RuntimeError(f"BioyondPeptideStation 基类导入失败: {_BIOYOND_IMPORT_ERROR}") from _BIOYOND_IMPORT_ERROR
        kwargs.pop("children", None)
        merged_config: Dict[str, Any] = {}
        if config_path is not None:
            merged_config.update(load_peptide_config(config_path))
        if isinstance(config, (str, Path)):
            merged_config.update(load_peptide_config(config))
        elif config:
            merged_config.update(config)
        if bioyond_config:
            merged_config.update(bioyond_config)
        merged_config.update(kwargs)
        _apply_default_peptide_material_type_mappings(merged_config)
        missing = [k for k in self._REQUIRED_CONFIG_KEYS if not merged_config.get(k)]
        if missing:
            raise ValueError(f"BioyondPeptideStation 缺少必要配置: {', '.join(missing)}")
        self.protocol_type = protocol_type
        self.bioyond_config = merged_config
        super().__init__(bioyond_config=self.bioyond_config, deck=deck)
        # 订单完成报送等待机制（多肽场景）：
        # - last_order_code 记录当前正在等待的 orderCode（业务编号），用于回调侧多订单隔离
        # - last_order_report 缓存最近一次匹配到的 report.data
        # - order_finish_event 用于阻塞等待 + 唤醒 wait_for_order_finish 动作
        self.order_finish_event = threading.Event()
        self.last_order_code: Optional[str] = None
        self.last_order_id: Optional[str] = None
        self.last_order_report: Optional[Dict[str, Any]] = None
        self.last_order_material_sync: Optional[Dict[str, Any]] = None
        self.last_used_materials: List[Any] = []
        logger.info("BioyondPeptideStation 初始化完成: %s", self.bioyond_config.get("api_host", ""))

    @action(always_free=True, description="从 Bioyond 全量同步物料并发布资源树")
    def sync_materials_from_bioyond(
        self,
        publish_tree: bool = True,
        clear_stale: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """从 Bioyond 库存接口全量同步物料。

        Args:
            publish_tree: 同步成功后是否发布整棵 deck 资源树到 UniLabOS。
            clear_stale: 是否清理本次库存快照未出现的旧物料；默认不清理。
        """
        del kwargs
        synchronizer = getattr(self, "resource_synchronizer", None)
        if synchronizer is None or not hasattr(synchronizer, "sync_from_external"):
            logger.warning("[sync_materials_from_bioyond] 资源同步器未初始化")
            return {
                "success": False,
                "action": "sync_materials_from_bioyond",
                "message": "资源同步器未初始化或不支持 sync_from_external",
                "published": False,
            }

        with self._debug_call_session("sync_materials_from_bioyond"):
            synced = bool(synchronizer.sync_from_external(clear_stale=bool(clear_stale)))
        sync_result = dict(getattr(synchronizer, "last_sync_result", {}) or {})
        published = False
        if synced and publish_tree:
            publisher = getattr(self, "_publish_material_tree_update", None)
            if callable(publisher):
                published = bool(publisher("manual_full_sync"))
            else:
                logger.warning("[sync_materials_from_bioyond] 资源树发布接口不可用")

        return {
            "success": synced,
            "action": "sync_materials_from_bioyond",
            "message": "Bioyond 物料全量同步完成" if synced else "Bioyond 物料全量同步失败",
            "published": published,
            "publish_requested": bool(publish_tree),
            "clear_stale": bool(clear_stale),
            "sync_result": sync_result,
        }

    def _debug_call_session(self, action_name: str):
        parent_debug_session = getattr(super(), "_debug_call_session", None)
        if parent_debug_session is not None:
            return parent_debug_session(action_name)
        return nullcontext()

    def handle_external_error(self, error_data: Dict[str, Any]) -> Dict[str, Any]:
        """处理奔曜错误报送，并为后续 SSE 人工选择回复预留上下文。"""
        parent_handler = getattr(super(), "handle_external_error", None)
        if parent_handler is not None:
            base_result = parent_handler(error_data)
        else:
            base_result = {
                "handled": True,
                "error_type": "bioyond_error" if isinstance(error_data, dict) and "code" in error_data else "unknown",
                "timestamp": datetime.now().isoformat(),
            }
        if not isinstance(error_data, dict) or not any(
            error_data.get(key) for key in ("ijk", "token", "optionMessage")
        ):
            return base_result

        result = dict(base_result) if isinstance(base_result, dict) else {"base_result": base_result}
        result.update(
            {
                "reply_status": "pending_sse_option",
                "error_reply_context": {
                    "ijk": error_data.get("ijk"),
                    "token": error_data.get("token"),
                    "optionMessage": error_data.get("optionMessage"),
                },
            }
        )
        # TODO: 待错误 SSE/人工确认工具提供 reply_option 后，调用
        # build_scheduler_error_handling_reply_data(error_data, reply_option)，
        # 再通过 self._require_hardware_interface().scheduler_reply_error_handling(reply_data) 回复奔曜。
        return result

    def fetch_workflow_list(
        self,
        workflow_type: int = 0,
        filter_text: str = "",
        include_detail: bool = True,
    ) -> Dict[str, Any]:
        """运行时通过 RPC 拉取工作流列表。"""
        payload = {"type": workflow_type, "filter": filter_text, "includeDetail": include_detail}
        data = self._require_hardware_interface().query_workflow(json.dumps(payload, ensure_ascii=False))
        items = self._as_list(data.get("items") if isinstance(data, dict) else data)
        return {"items": items, "totalCount": data.get("totalCount") if isinstance(data, dict) else len(items), "raw": data}

    @action(auto_prefix=True, description="上传多肽样品 Excel 文件")
    def upload_sample_excel(self, file_path: str, content_type: Optional[str] = None) -> Dict[str, Any]:
        with self._debug_call_session("upload_sample_excel"):
            result = self._upload_sample_excel_file(file_path, content_type=content_type)
        file_info = result.get("lims_file_info") if isinstance(result.get("lims_file_info"), dict) else {}
        return {
            "success": True,
            "data": file_info,
            "relative_path": str(result.get("relative_path") or ""),
            "sample_file_parameter": str(result.get("sample_file_parameter") or ""),
            "upload_result": result,
        }

    @action(
        always_free=True,
        description="查询 LIMS 样品 Excel 列表，可选确定性解析",
        handles=[
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="sample_excel_data",
                data_type="json",
                label="样品 Excel 列表",
                data_key="sample_excel_data",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def list_sample_excels(
        self,
        begin_date: str = "",
        end_date: str = "",
        name_filter: str = "",
        sample_excel_pattern: str = "",
        deterministic_resolve: bool = False,
    ) -> Dict[str, Any]:
        with self._debug_call_session("list_sample_excels"):
            records = self._list_sample_excels(
                name_filter=name_filter or sample_excel_pattern.replace("*", ""),
                begin_date=begin_date or None,
                end_date=end_date or None,
            )
            payload: Dict[str, Any] = {"success": True, "sample_excel_data": records}
            if deterministic_resolve:
                pattern = sample_excel_pattern or name_filter
                if not pattern:
                    raise PeptideWorkflowError("确定性解析模式需要 sample_excel_pattern 或 name_filter")
                selected = self._select_sample_excel_record(records, pattern)
                payload["sample_excel_relative_path"] = str(selected.get("relativePath") or "").replace("/", "\\")
                payload["selected_sample_excel"] = selected
            return payload

    @action(
        always_free=True,
        description="查询子工作流步骤参数（支持必填/可选/隐藏过滤）",
        handles=[
            ActionOutputHandle(
                key="step_parameters_raw_json",
                data_type="json",
                label="步骤参数 JSON",
                data_key="step_parameters_raw_json",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="filtered_subworkflows",
                data_type="json",
                label="匹配子工作流",
                data_key="filtered_subworkflows",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def get_step_parameters(
        self,
        sub_workflow_id: str = "",
        workflow_name_filter: str = "",
        subworkflow_name_filter: str = "",
        required_para: bool = True,
        optional_parameter: bool = True,
        hidden_para: bool = False,
    ) -> Dict[str, Any]:
        with self._debug_call_session("get_step_parameters"):
            if sub_workflow_id.strip():
                step_data = self._query_step_parameters(sub_workflow_id.strip())
                flattened = self._flatten_step_parameters(step_data)
                filtered = self._filter_step_parameter_records(flattened, required_para, optional_parameter, hidden_para)
                augmented = {
                    "subworkflowId": sub_workflow_id.strip(),
                    "code": 1,
                    "data": {"filteredParameters": filtered, "raw": step_data},
                }
                return {"step_parameters_raw_json": augmented, "filtered_subworkflows": []}

            bindings = self._filter_workflow_records(
                self._query_workflow_records(workflow_name_filter),
                workflow_name_filter=workflow_name_filter,
                subworkflow_name_filter=subworkflow_name_filter,
            )
            if len(bindings) != 1:
                message = f"匹配到 {len(bindings)} 个子工作流，请收窄 workflow_name_filter/subworkflow_name_filter"
                status = {
                    "code": 0 if bindings else -1,
                    "message": message,
                    "data": {"matches": bindings},
                    "matches": len(bindings),
                }
                return {"step_parameters_raw_json": status, "filtered_subworkflows": bindings}

            binding = bindings[0]
            step_data = self._query_step_parameters(binding["subworkflowId"])
            flattened = self._flatten_step_parameters(step_data)
            filtered = self._filter_step_parameter_records(flattened, required_para, optional_parameter, hidden_para)
            augmented = {
                "workflowId": binding.get("workflowId"),
                "workflowName": binding.get("workflowName"),
                "subworkflowId": binding.get("subworkflowId"),
                "subworkflowName": binding.get("subworkflowName"),
                "code": 1,
                "data": {"filteredParameters": filtered, "rawData": step_data},
            }
            return {"step_parameters_raw_json": augmented, "filtered_subworkflows": bindings}

    @action(
        always_free=True,
        description="按工作流名称提交多肽实验（非 Day1）",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment(
        self,
        required_params: PeptideGenericSubmitRequiredParams,
        optional_params: Optional[PeptideGenericSubmitOptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        return self._submit_experiment_core(
            day_key=None,
            required_params=required_params,
            optional_params=optional_params,
            sample_excel_relative_path=sample_excel_relative_path,
            generic=True,
        )

    @action(
        always_free=True,
        description="提交 Day2 多肽定量实验",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment_day2(
        self,
        required_params: PeptideDay2RequiredParams,
        optional_params: Optional[PeptideDay2OptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        return self._submit_experiment_core("day2", required_params, optional_params, sample_excel_relative_path)

    @action(
        always_free=True,
        description="提交 Day3 线肽环化实验",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment_day3(
        self,
        required_params: PeptideDay3RequiredParams,
        optional_params: Optional[PeptideDay3OptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        return self._submit_experiment_core("day3", required_params, optional_params, sample_excel_relative_path)

    @action(
        always_free=True,
        description="提交 Day4 环肽酰化（酶标）实验",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment_day4(
        self,
        required_params: PeptideDay4RequiredParams,
        optional_params: Optional[PeptideDay4OptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        return self._submit_experiment_core("day4", required_params, optional_params, sample_excel_relative_path)

    @action(
        always_free=True,
        description="提交 Day4 环肽酰化 LCMS 实验",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment_day4_LCMS(
        self,
        required_params: PeptideDay4LCMSRequiredParams,
        optional_params: Optional[PeptideDay4LCMSOptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        return self._submit_experiment_core("day4_lcms", required_params, optional_params, sample_excel_relative_path)

    @action(
        always_free=True,
        description="提交 Day1 线肽合成实验",
        handles=[
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="cem_method_file_name", data_type="str", label="CEM 方法文件", data_key="cem_method_file_name", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def submit_experiment_day1(
        self,
        required_params: PeptideDay1RequiredParams,
        optional_params: Optional[PeptideDay1OptionalParams] = None,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        required = dict(required_params or {})
        cem_method = str(required.get("cem_method_file_name") or DAY1_CEM_METHOD_DEFAULT).strip() or DAY1_CEM_METHOD_DEFAULT
        required["cem_method_file_name"] = cem_method
        result = self._submit_experiment_core("day1", required, optional_params, sample_excel_relative_path)
        result["cem_method_file_name"] = cem_method
        return result

    @action(
        always_free=True,
        description="生成 Day1 CEM 校验信息",
        goal_default={"cem_method_file_name": DAY1_CEM_METHOD_DEFAULT},
        handles=[
            ActionInputHandle(
                key="cem_method_file_name",
                data_type="str",
                label="CEM 方法文件",
                data_key="cem_method_file_name",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="success", data_type="bool", label="是否成功", data_key="success", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="cem_method_file_name", data_type="str", label="CEM 方法文件", data_key="cem_method_file_name", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(key="cem_pdf_path", data_type="str", label="CEM 校验文件路径", data_key="cem_pdf_path", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="cem_info_url", data_type="str", label="CEM 校验链接", data_key="cem_info_url", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="prepare_cem_response", data_type="json", label="prepare-cEM 响应", data_key="prepare_cem_response", data_source=DataSource.EXECUTOR),
        ],
    )
    def prepare_cem(
        self,
        cem_method_file_name: str = DAY1_CEM_METHOD_DEFAULT,
        sample_excel_relative_path: str = "",
    ) -> Dict[str, Any]:
        excel_path = str(sample_excel_relative_path or "").strip().replace("/", "\\")
        if not excel_path:
            raise PeptideWorkflowError("prepare_cem 缺少 sample_excel_relative_path")
        method = str(cem_method_file_name or DAY1_CEM_METHOD_DEFAULT).strip() or DAY1_CEM_METHOD_DEFAULT
        rpc = self._require_hardware_interface()
        api_host = str(getattr(rpc, "host", "") or self.bioyond_config.get("api_host", "")).rstrip("/")
        request_body = {
            "apiKey": rpc.api_key,
            "requestTime": _utc_now_iso8601_ms(),
            "data": {"methodFileName": method, "excelPath": excel_path},
        }
        with self._debug_call_session("prepare_cem"):
            response = rpc.post(
                url=f"{api_host}/api/lims/order/prepare-cEM",
                params=request_body,
            )
        if not isinstance(response, dict) or response.get("code") != 1:
            raise RuntimeError(f"prepare-cEM 调用失败: {response}")
        data = response.get("data")
        cem_pdf_path = self._extract_cem_pdf_path(data)
        if not cem_pdf_path:
            raise RuntimeError(f"prepare-cEM 响应缺少 data: {response}")
        return {
            "success": True,
            "cem_method_file_name": method,
            "sample_excel_relative_path": excel_path,
            "cem_pdf_path": cem_pdf_path,
            "cem_info_url": self._join_api_url(api_host, cem_pdf_path),
            "prepare_cem_response": response,
        }

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
        goal_default={"cem_info_confirmed": False, "timeout_seconds": 3600, "assignee_user_ids": []},
        feedback_interval=300,
        description=CEM_INFO_CONFIRM_MESSAGE,
        handles=[
            ActionInputHandle(key="cem_pdf_path", data_type="str", label="CEM 校验文件路径", data_key="cem_pdf_path", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(key="cem_info_url", data_type="str", label="CEM 校验链接", data_key="cem_info_url", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(key="cem_method_file_name", data_type="str", label="CEM 方法文件", data_key="cem_method_file_name", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(key="cem_pdf_path", data_type="str", label="CEM 校验文件路径", data_key="cem_pdf_path", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="cem_info_url", data_type="str", label="CEM 校验链接", data_key="cem_info_url", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="cem_method_file_name", data_type="str", label="CEM 方法文件", data_key="cem_method_file_name", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(
                key="sample_excel_relative_path",
                data_type="bioyond_sample_file",
                label="样品 Excel 相对路径",
                data_key="sample_excel_relative_path",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(key="instruction_text", data_type="str", label="确认说明", data_key="instruction_text", data_source=DataSource.EXECUTOR),
        ],
    )
    def confirm_cem_info(
        self,
        cem_pdf_path: str = "",
        cem_info_url: str = "",
        cem_method_file_name: str = "",
        sample_excel_relative_path: str = "",
        cem_info_confirmed: bool = False,
        timeout_seconds: int = 3600,
        assignee_user_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del timeout_seconds, assignee_user_ids, kwargs
        if not bool(cem_info_confirmed):
            raise RuntimeError("CEM 校验信息未确认，拒绝继续工作流")
        return {
            "success": True,
            "cem_pdf_path": str(cem_pdf_path or ""),
            "cem_info_url": str(cem_info_url or ""),
            "cem_method_file_name": str(cem_method_file_name or DAY1_CEM_METHOD_DEFAULT).strip() or DAY1_CEM_METHOD_DEFAULT,
            "sample_excel_relative_path": str(sample_excel_relative_path or "").replace("/", "\\"),
            "cem_info_confirmed": True,
            "instruction_text": CEM_INFO_CONFIRM_MESSAGE,
        }

    def _submit_experiment_core(
        self,
        day_key: Optional[str],
        required_params: Dict[str, Any],
        optional_params: Optional[Dict[str, Any]],
        sample_excel_relative_path: str = "",
        *,
        generic: bool = False,
    ) -> Dict[str, Any]:
        optional = dict(optional_params or {})
        warnings: List[str] = []
        action_name = "submit_experiment" if generic else f"submit_experiment_{day_key}"
        with self._debug_call_session(action_name):
            if generic:
                workflow_name = str(required_params.get("workflow_name") or "").strip()
                if not workflow_name:
                    raise PeptideWorkflowError("submit_experiment 必须提供 workflow_name")
                subworkflow_name = str(optional.get("subworkflow_name") or "").strip()
                binding = self._resolve_workflow_binding_from_names(workflow_name, subworkflow_name)
            else:
                binding = self._resolve_workflow_binding(day_key or "")

            resolved_sample_excel_path, selected = self._resolve_submit_sample_file(required_params, optional, sample_excel_relative_path)
            partial_entries, override_warnings = self._build_partial_parameter_entries(
                sample_excel_relative_path=resolved_sample_excel_path,
                day_key=day_key,
                required_params=required_params,
                parameter_overrides=optional.get("parameter_overrides"),
            )
            warnings.extend(override_warnings)

            step_data = self._query_step_parameters(binding["sub_workflow_id"])
            flattened = self._flatten_step_parameters(step_data)
            # 未来校验可能基于 TaskDisplayable/Value/DisplayValue 分类（见 get_step_parameters）。
            resolved_entries = self._resolve_parameter_entries_against_live_steps(partial_entries, flattened)
            param_values = self._group_resolved_entries_to_param_values(resolved_entries)
            order_code, order_name = self._build_order_identity(day_key or "generic", optional.get("order_name"))
            order_payload = self._create_order_payload(
                order_code=order_code,
                order_name=order_name,
                sub_workflow_id=binding["sub_workflow_id"],
                param_values=param_values,
                border_number=int(optional.get("border_number") or 1),
                extend_properties=optional.get("extend_properties"),
            )
            create_order_raw = self._create_order(order_payload)
            allocation = self._parse_create_order_allocation_map(create_order_raw)
            order_ids = allocation["order_ids"]
            order_id = order_ids[0] if order_ids else ""
            if not allocation["allocation_rows"]:
                warnings.append("create_order_allocation_unavailable_for_result_table")
            result_table = self._build_result_table(allocation["materials_by_type"])
            auto_register = bool(optional.get("auto_register_materials", True))
            cache_result = self._cache_order_allocation_rows(
                allocation["allocation_rows"],
                order_ids=order_ids,
                source="submit_experiment",
            )
            materials_by_order_id: List[Dict[str, Any]] = []
            order_id_sync = {
                "requested": bool(auto_register and order_id),
                "status": "skipped",
                "material_count": 0,
            }
            if auto_register and order_id:
                try:
                    material_sync = self._sync_order_materials_from_bioyond(
                        order_id,
                        reason="submit_experiment",
                    )
                    materials_by_order_id = list(material_sync.get("materials") or [])
                    order_id_sync.update({
                        "status": material_sync.get("status", "synced"),
                        "material_count": len(materials_by_order_id),
                        "deck_sync": material_sync,
                    })
                except Exception as exc:
                    warning = f"materials_by_order_id_sync_failed:{exc}"
                    warnings.append(warning)
                    order_id_sync.update({"status": "failed", "error": str(exc)})
                    logger.warning(
                        f"[peptide] submit_experiment 订单物料同步失败: "
                        f"order_id={order_id} error={exc}"
                    )
            material_registration = {
                "requested": auto_register,
                "status": (
                    (
                        "cached"
                        if order_id_sync.get("status") == "cached" or cache_result.get("updated_count")
                        else order_id_sync.get("status", "skipped")
                    )
                    if auto_register
                    else "skipped"
                ),
                "allocation_cache": cache_result,
                "cache": cache_result,
                "order_id_sync": order_id_sync,
            }
            return {
                "success": bool(order_ids),
                "order_id": order_id,
                "order_ids": order_ids,
                "order_code": order_code,
                "order_name": order_name,
                "workflow": binding,
                "sub_workflow_id": binding["sub_workflow_id"],
                "sample_excel_relative_path": resolved_sample_excel_path,
                "selected_sample_excel": selected,
                "payload_summary": {"borderNumber": int(optional.get("border_number") or 1), "orderCode": order_code},
                "create_order_data_raw": create_order_raw,
                "allocation_map": allocation["allocation_map"],
                "allocation_rows": allocation["allocation_rows"],
                "materials_by_order_id": materials_by_order_id,
                "resultTable": result_table,
                "start_experiment": {
                    "order_id": order_id,
                    "order_ids": order_ids,
                    "resultTable": result_table,
                    "materials_loaded": False,
                },
                "auto_register_materials": auto_register,
                "material_registration": material_registration,
                "warnings": warnings,
            }

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
        goal_default={"materials_loaded": False, "timeout_seconds": 3600, "assignee_user_ids": []},
        feedback_interval=300,
        description="确认物料装载后启动调度器",
        handles=[
            ActionInputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_code", data_type="bioyond_order_code", label="订单编号", data_key="order_code", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="table", label="装载确认表", data_key="resultTable", data_source=DataSource.EXECUTOR),
        ],
    )
    def start_experiment(
        self,
        order_id: str = "",
        order_ids: Optional[List[str]] = None,
        resultTable: Optional[Dict[str, Any]] = None,
        materials_loaded: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        with self._debug_call_session("start_experiment"):
            resolved_order_ids = self._extract_order_ids(order_id=order_id, order_ids=order_ids, **kwargs)
            table_rows = resultTable.get("data") if isinstance(resultTable, dict) else []
            if table_rows and not materials_loaded:
                raise RuntimeError("多肽物料装载未确认，拒绝启动调度器")
            result = self._run_scheduler_action("scheduler_start", "启动")
            result["order_id"] = resolved_order_ids[0] if resolved_order_ids else str(order_id or "")
            result["order_ids"] = resolved_order_ids
            # peptide 的 _run_scheduler_action 不返回 order_code；先占位空串，
            # 下游 wait_for_order_finish 会走 rpc.order_report(order_id).code 兜底反查。
            result["order_code"] = ""
            result["materials_loaded"] = bool(materials_loaded)
            result["resultTable"] = resultTable or {}
            return result

    def process_order_finish_report(
        self,
        report_request: Any,
        used_materials: Optional[List[Any]] = None,
    ) -> Any:
        """处理订单完成报送，按 peptide 触发矩阵发布状态/唤醒事件但不做全量库存同步。

        订单范围的新鲜度与下料物料查询由 ``wait_for_order_finish`` 侧处理；后续如需更细粒度
        的 order-scoped sync，应单独实现，避免恢复基类全量同步副作用。
        """
        try:
            materials = list(used_materials or [])
            data = getattr(report_request, "data", None) or {}
            status_names = {"30": "完成", "-11": "异常停止", "-12": "人工停止"}
            status_desc = status_names.get(str(data.get("status")), f"状态{data.get('status')}")

            logger.info(f"[任务完成报送] 订单: {data.get('orderCode')} - {data.get('orderName')}")
            logger.info(f"  状态: {status_desc}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")
            logger.info(f"  使用物料数量: {len(materials)}")

            for material in materials:
                logger.debug(
                    f"  物料: {getattr(material, 'materialId', '')}, "
                    f"用量: {getattr(material, 'usedQuantity', '')}"
                )

            event_status = "completed"
            if str(data.get("status")) in ["-11", "-12"]:
                event_status = "error"
            elif str(data.get("status")) == "30":
                event_status = "completed"
            else:
                event_status = "running"

            publish_task_status = getattr(self, "_publish_task_status", None)
            if callable(publish_task_status):
                publish_task_status(
                    task_id=data.get("orderCode"),
                    task_code=data.get("orderCode"),
                    task_type="bioyond_order",
                    status=event_status,
                    progress=1.0 if event_status in ["completed", "error"] else 0.9,
                    result={
                        "order_name": data.get("orderName"),
                        "status": status_desc,
                        "materials_count": len(materials),
                    },
                )

            order_code = str(data.get("orderCode") or "")
            expected_order_code = getattr(self, "last_order_code", None)
            self.last_order_report = data
            self.last_used_materials = materials

            logger.info(
                f"[peptide] /report/order_finish 收到: orderCode={order_code} status={data.get('status')} "
                f"expected={expected_order_code!r} used_materials={len(materials)}"
            )

            should_set_order_event = bool(expected_order_code and order_code == expected_order_code)
            if should_set_order_event:
                logger.info("[peptide] order_finish orderCode 匹配，准备同步订单物料后触发 order_finish_event")
            else:
                logger.info(
                    f"[peptide] order_finish orderCode 不匹配当前等待项，仅记录 "
                    f"(expected={expected_order_code!r} got={order_code!r})"
                )

            material_sync: Dict[str, Any] = {"requested": False, "status": "skipped"}
            reported_order_id = str(
                data.get("orderId")
                or data.get("order_id")
                or ""
            ).strip()
            if not reported_order_id and expected_order_code and order_code == expected_order_code:
                reported_order_id = str(getattr(self, "last_order_id", None) or "").strip()
            if reported_order_id:
                try:
                    material_sync = self._sync_order_materials_from_bioyond(
                        reported_order_id,
                        reason="order_finish_report",
                    )
                except Exception as exc:
                    logger.warning(
                        f"[peptide] order_finish 订单物料同步失败: "
                        f"order_id={reported_order_id} error={exc}"
                    )
                    material_sync = {
                        "requested": True,
                        "status": "failed",
                        "order_id": reported_order_id,
                        "error": str(exc),
                    }
            try:
                self.last_order_material_sync = material_sync
            except Exception:
                pass
            if should_set_order_event:
                order_finish_event = getattr(self, "order_finish_event", None)
                if order_finish_event is not None:
                    order_finish_event.set()

            return {
                "processed": True,
                "order_code": data.get("orderCode"),
                "status": data.get("status"),
                "materials_count": len(materials),
                "material_sync": material_sync,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as exc:
            logger.error(f"处理任务完成报送失败: {exc}")
            return {"processed": False, "error": str(exc)}

    def process_step_finish_report(self, report_request: Any) -> Dict[str, Any]:
        """处理步骤完成报送，发布状态但不触发全量物料同步。"""
        try:
            data = getattr(report_request, "data", None) or {}
            logger.info(f"[步骤完成报送] 订单: {data.get('orderCode')}, 步骤: {data.get('stepName')}")
            logger.info(f"  样品ID: {data.get('sampleId')}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")

            publish_task_status = getattr(self, "_publish_task_status", None)
            if callable(publish_task_status):
                publish_task_status(
                    task_id=data.get("orderCode"),
                    task_code=data.get("orderCode"),
                    task_type="bioyond_step",
                    status="running",
                    progress=0.5,
                    result={"step_name": data.get("stepName"), "step_id": data.get("stepId")},
                )

            return {
                "processed": True,
                "step_id": data.get("stepId"),
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as exc:
            logger.error(f"处理步骤完成报送失败: {exc}")
            return {"processed": False, "error": str(exc)}

    @action(
        always_free=True,
        goal_default={
            "order_id": "",
            "order_code": "",
            "timeout_seconds": 36000,
            "poll_mode": True,
            "poll_interval_seconds": 0.5,
        },
        description=(
            "阻塞等待奔耀通过 /report/order_finish 推送任务完成，"
            "并调用 /api/lims/order/materials-by-order-id 整理「下料指引表」给下游节点。"
            "v1 仅等待单个订单：order_ids 长度>1 且未指定 order_id/order_code 时报错。"
        ),
        handles=[
            ActionInputHandle(
                key="order_id",
                data_type="bioyond_order_id",
                label="实验ID",
                data_key="order_id",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="order_ids",
                data_type="bioyond_order_ids",
                label="实验ID列表",
                data_key="order_ids",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="order_code",
                data_type="bioyond_order_code",
                label="订单编号",
                data_key="order_code",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(
                key="order_id",
                data_type="bioyond_order_id",
                label="实验ID",
                data_key="order_id",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="order_code",
                data_type="bioyond_order_code",
                label="订单编号",
                data_key="order_code",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="order_finish_status",
                data_type="string",
                label="完成状态",
                data_key="order_finish_status",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="order_finish_report",
                data_type="object",
                label="订单完成推送报文",
                data_key="order_finish_report",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="used_materials",
                data_type="array",
                label="使用物料列表",
                data_key="used_materials",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="materials_by_order_id",
                data_type="array",
                label="订单实验台物料",
                data_key="materials_by_order_id",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="resultTable",
                data_type="object",
                label="下料指引表",
                data_key="resultTable",
                data_source=DataSource.EXECUTOR,
                io_type="target",
            ),
        ],
    )
    def wait_for_order_finish(
        self,
        order_id: str = "",
        order_code: str = "",
        order_ids: Optional[List[str]] = None,
        timeout_seconds: int = 36000,
        poll_mode: bool = True,
        poll_interval_seconds: float = 0.5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """阻塞等待奔耀订单完成推送，并整理「下料指引表」给下游节点。

        Args:
            order_id: 实验 UUID（用于调 materials-by-order-id 与 order_code 兜底反查）。
            order_code: 订单编号字符串（用于匹配 /report/order_finish 推送）；缺省时
                内部通过 ``rpc.order_report(order_id)`` 反查 ``code`` 字段。
            order_ids: 兼容 submit_experiment 多订单输出；当 ``order_id`` 为空且长度 == 1 时
                自动取第一个；长度 > 1 且未显式指定 ``order_id`` / ``order_code`` 时 ``raise``。
            timeout_seconds: 超时秒数；0 表示不限时（沿用 ``threading.Event.wait(timeout=None)``）。
            poll_mode: True 走 0.5s 轮询 + 超时（不挡 ROS2 feedback 派发线程），
                False 走单次 ``event.wait()``。
            poll_interval_seconds: 轮询间隔（仅 poll_mode 生效），测试用例可调小。

        Returns:
            含 ``success``/``order_id``/``order_code``/``order_finish_status``/``order_finish_report``/
            ``used_materials``/``materials_by_order_id``/``resultTable``/``confirmation_message`` 的字典。
        """
        with self._debug_call_session("wait_for_order_finish"):
            del kwargs

            # 1) 解析 order_id：优先入参；缺省时尝试从 order_ids 取唯一一个。
            normalized_order_id = str(order_id or "").strip()
            candidates = [str(v).strip() for v in (order_ids or []) if str(v or "").strip()]
            if not normalized_order_id:
                if len(candidates) == 1:
                    normalized_order_id = candidates[0]
                elif len(candidates) > 1:
                    raise ValueError(
                        "wait_for_order_finish 在多 order_ids 场景下需要显式指定 order_id 或 order_code；"
                        f"当前 order_ids={candidates}"
                    )

            normalized_order_code = str(order_code or "").strip()
            if not normalized_order_id and not normalized_order_code:
                raise ValueError(
                    "wait_for_order_finish 需要提供 order_id 或 order_code（请连接上游 start_experiment 输出）"
                )

            # 2) 若仅有 order_id 没有 order_code，兜底反查（仅用于推送匹配，不参与 materials-by-order-id）。
            if not normalized_order_code and normalized_order_id:
                try:
                    rpc_for_report = self._require_hardware_interface("order_report")
                    report = rpc_for_report.order_report(normalized_order_id)
                    if isinstance(report, dict):
                        normalized_order_code = str(
                            report.get("code") or report.get("orderCode") or ""
                        ).strip()
                except Exception as exc:
                    logger.warning(
                        f"[peptide] wait_for_order_finish 反查 order_code 失败 "
                        f"(order_id={normalized_order_id}): {exc}"
                    )

            if not normalized_order_code:
                raise ValueError(
                    "wait_for_order_finish 无法解析 order_code（rpc.order_report 反查也失败）；"
                    "请显式传入 order_code 或确认 order_id 在 Bioyond LIMS 中存在"
                )

            # 3) 准备事件状态，必须在 last_order_code 赋值后再 clear()，避免基类回调竞态。
            self.last_order_code = normalized_order_code
            self.last_order_id = normalized_order_id
            self.last_order_report = None
            self.last_order_material_sync = None
            self.last_used_materials = []
            self.order_finish_event.clear()

            logger.info(
                f"[peptide] wait_for_order_finish 开始等待: order_id={normalized_order_id} "
                f"order_code={normalized_order_code} timeout={timeout_seconds}s poll={poll_mode}"
            )

            # 4) 阻塞等待推送。timeout_seconds<=0 当作不限时。
            timeout_effective: Optional[float] = float(timeout_seconds) if timeout_seconds and timeout_seconds > 0 else None
            triggered = False
            if poll_mode:
                interval = max(float(poll_interval_seconds or 0.5), 0.001)
                deadline = (time.monotonic() + timeout_effective) if timeout_effective else None
                while True:
                    if self.order_finish_event.wait(timeout=interval):
                        triggered = True
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
            else:
                triggered = bool(self.order_finish_event.wait(timeout=timeout_effective))

            # 5) 解析推送状态。
            report = self.last_order_report or {}
            if not triggered:
                mapped_status = "timeout"
                logger.warning(
                    f"[peptide] wait_for_order_finish 超时: order_code={normalized_order_code}"
                )
            else:
                raw_status = str(report.get("status", "")).strip() if isinstance(report, dict) else ""
                if raw_status in ORDER_FINISH_STATUS_MAP:
                    mapped_status = ORDER_FINISH_STATUS_MAP[raw_status]
                elif raw_status:
                    mapped_status = f"unknown_{raw_status}"
                else:
                    mapped_status = "missing_status"

            # 6) 仅在 status 命中已知正常状态时拉取实验台物料；timeout / unknown / missing 不调。
            materials_by_order_id: List[Dict[str, Any]] = []
            unload_table = self._build_unload_table([])
            material_sync: Dict[str, Any] = {"requested": False, "status": "skipped"}
            if mapped_status in {"success", "abnormal_stop", "manual_stop"} and normalized_order_id:
                try:
                    cached_sync = getattr(self, "last_order_material_sync", None)
                    if (
                        isinstance(cached_sync, dict)
                        and cached_sync.get("requested")
                        and cached_sync.get("order_id") == normalized_order_id
                        and cached_sync.get("status") == "synced"
                    ):
                        material_sync = cached_sync
                        materials_by_order_id = list(cached_sync.get("materials") or [])
                        unload_rows = self._build_unload_rows_from_materials_by_order_id(materials_by_order_id)
                        unload_table = self._build_unload_table(unload_rows)
                    else:
                        unload_payload = self._construct_unload_table_payload(normalized_order_id)
                        materials_by_order_id = unload_payload["materials_by_order_id"]
                        unload_table = unload_payload["resultTable"]
                        material_sync = unload_payload.get("material_sync", material_sync)
                except Exception as exc:
                    logger.error(
                        f"[peptide] wait_for_order_finish 调用 materials_by_order_id 失败: {exc}",
                        exc_info=True,
                    )

            # 7) 整理 resultTable（4 列 v2 结构）+ 序列化 used_materials。
            used_materials_serialized = [
                self._used_material_to_dict(item) for item in self.last_used_materials
            ]

            return {
                "success": mapped_status in {"success", "abnormal_stop", "manual_stop"},
                "order_id": normalized_order_id,
                "order_code": normalized_order_code,
                "order_finish_status": mapped_status,
                "order_finish_report": report if isinstance(report, dict) else {},
                "used_materials": used_materials_serialized,
                "materials_by_order_id": materials_by_order_id,
                "material_sync": material_sync,
                "resultTable": unload_table,
                "confirmation_message": (
                    f"任务完成: status={mapped_status}; 已整理 {len(unload_table.get('data', []))} 行下料指引"
                ),
            }

    @action(
        always_free=True,
        goal_default={"order_id": ""},
        description="按实验ID查询订单实验台物料并构造下料指引表，作为 wait_for_order_finish 的备用节点",
        handles=[
            ActionInputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="materials_by_order_id", data_type="array", label="订单实验台物料", data_key="materials_by_order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="resultTable", data_type="object", label="下料指引表", data_key="resultTable", data_source=DataSource.EXECUTOR, io_type="target"),
        ],
    )
    def construct_unload_table(self, order_id: str, **kwargs: Any) -> Dict[str, Any]:
        """按 orderId UUID 查询物料并构造下料指引表。"""
        del kwargs
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise ValueError("construct_unload_table 需要 order_id")
        with self._debug_call_session("construct_unload_table"):
            return self._construct_unload_table_payload(normalized_order_id)

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={
            "resultTable": "unilabos_manual_confirm",
            "assignee_user_ids": "unilabos_manual_confirm",
        },
        goal_default={
            "order_id": "",
            "materials_unloaded": False,
            "timeout_seconds": 3600,
            "assignee_user_ids": [],
        },
        feedback_interval=300,
        description=(
            "展示上一节点 wait_for_order_finish 整理的下料指引表；"
            "操作员物理取出后勾选 materials_unloaded=True，本节点再调用 "
            "/api/lims/order/take-out 通知奔耀下料完成（preintakeIds=[], materialIds=[]）。"
        ),
        handles=[
            ActionInputHandle(
                key="order_id",
                data_type="bioyond_order_id",
                label="实验ID",
                data_key="order_id",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="order_code",
                data_type="bioyond_order_code",
                label="订单编号",
                data_key="order_code",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="resultTable",
                data_type="table",
                label="下料指引表",
                data_key="resultTable",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="used_materials",
                data_type="array",
                label="使用物料列表",
                data_key="used_materials",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionInputHandle(
                key="order_finish_report",
                data_type="object",
                label="订单完成推送报文",
                data_key="order_finish_report",
                data_source=DataSource.HANDLE,
                io_type="source",
            ),
            ActionOutputHandle(
                key="success",
                data_type="boolean",
                label="是否成功",
                data_key="success",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="order_id",
                data_type="bioyond_order_id",
                label="实验ID",
                data_key="order_id",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="take_out_result",
                data_type="object",
                label="take-out 返回包",
                data_key="take_out_result",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def unload_materials(
        self,
        order_id: str = "",
        resultTable: Optional[Dict[str, Any]] = None,
        materials_unloaded: bool = False,
        timeout_seconds: int = 3600,
        assignee_user_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """人工下料确认节点：勾选「已完成下料」后调用 ``take-out`` 通知奔耀。

        plan 决策：``take_out`` 形参恒为 ``(order_id, [], [])`` —— 不按物料挑选，
        由奔耀根据订单自己决定取出范围；本节点只负责"展示给人看 + 勾选后通知"。

        Args:
            order_id: 上游 ``wait_for_order_finish`` 提供的订单 UUID（必填）。
            materials_unloaded: 操作员勾选确认物理下料已完成；未勾选则 ``raise RuntimeError``。
            timeout_seconds: 框架超时时间（秒，本动作不读）。
            assignee_user_ids: 框架分配用户 ID 列表（本动作不读）。

        Returns:
            含 ``success`` / ``order_id`` / ``take_out_result`` / ``confirmation_message`` 的字典。
        """
        with self._debug_call_session("unload_materials"):
            del timeout_seconds, assignee_user_ids, kwargs

            normalized_order_id = str(order_id or "").strip()
            if not normalized_order_id:
                raise ValueError(
                    "unload_materials 需要 order_id（请连接 wait_for_order_finish.order_id 或显式传入）"
                )

            if not self._as_manual_gate(materials_unloaded):
                raise RuntimeError("下料未确认，拒绝调用 take-out")

            rpc = self._require_hardware_interface("take_out")
            order_material_ids = self._collect_order_material_ids(normalized_order_id)
            logger.info(
                f"[peptide] unload_materials 调用 take_out: order_id={normalized_order_id}"
            )
            take_out_response = rpc.take_out(normalized_order_id, [], [])
            logger.info(
                f"[peptide] unload_materials take_out 返回: {take_out_response}"
            )

            if isinstance(take_out_response, dict):
                success = take_out_response.get("code") == 1
                message = str(take_out_response.get("message", "") or "")
                normalized_response: Dict[str, Any] = dict(take_out_response)
            else:
                success = False
                message = ""
                normalized_response = {}

            deleted_materials = {"material_ids": order_material_ids, "removed_from_deck": 0, "removed_from_cache": 0}
            post_take_out_sync: Dict[str, Any] = {"requested": False, "status": "skipped"}
            if success:
                try:
                    post_take_out_sync = self._sync_order_materials_from_bioyond(
                        normalized_order_id,
                        reason="unload_materials",
                    )
                except Exception as exc:
                    logger.warning(
                        f"[peptide] unload_materials take_out 后订单物料同步失败: "
                        f"order_id={normalized_order_id} error={exc}"
                    )
                    post_take_out_sync = {
                        "requested": True,
                        "status": "failed",
                        "order_id": normalized_order_id,
                        "error": str(exc),
                    }
                if (
                    post_take_out_sync.get("status") == "failed"
                    or (not post_take_out_sync.get("material_count") and order_material_ids)
                ):
                    deleted_materials = self._delete_bioyond_materials_from_cache_and_deck(
                        order_material_ids,
                        publish_tree=True,
                        reason=f"unload_materials:{normalized_order_id}",
                    )

            return {
                "success": bool(success),
                "order_id": normalized_order_id,
                "take_out_result": normalized_response,
                "deleted_materials": deleted_materials,
                "post_take_out_sync": post_take_out_sync,
                "confirmation_message": (
                    "下料确认，已通知奔耀 take-out 成功"
                    if success
                    else f"下料确认，但 take-out 返回失败/异常，请检查 LIMS 状态: {message}"
                ),
            }

    @action(
        always_free=True,
        goal_default={
            "reset_scheduler": True,
            "reset_order_status": True,
            "reset_location": True,
            "reset_devices": False,
            "sync_materials_after_reset": True,
            "clear_stale_after_reset": True,
            "publish_tree_after_reset": True,
        },
        description="自动复位调度器/订单状态/库位，可选仪器复位",
    )
    def reset_auto(
        self,
        reset_scheduler: bool = True,
        reset_order_status: bool = True,
        reset_location: bool = True,
        reset_devices: bool = False,
        sync_materials_after_reset: bool = True,
        clear_stale_after_reset: bool = True,
        publish_tree_after_reset: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """自动复位调度器/订单状态/库位，可选仪器复位。

        Args:
            reset_scheduler[调度器复位]: 调用 /api/lims/scheduler/reset，默认勾选。
            reset_order_status[订单状态复位]: 调用 /api/lims/order/reset-order-status，默认勾选。
            reset_location[库位复位]: 调用 /api/lims/storage/reset-location，默认勾选。
            reset_devices[仪器复位]: 调用 /api/lims/device/reset-devices，默认不勾选。
            sync_materials_after_reset[复位后同步物料]: 库位复位成功后拉取库存并更新本地资源树，默认勾选。
            clear_stale_after_reset[同步时清理陈旧缓存]: 复位后同步时清理本次库存快照外的旧缓存，默认勾选。
            publish_tree_after_reset[同步后发布资源树]: 复位后同步成功时发布资源树，默认勾选。
        """
        del kwargs
        with self._debug_call_session("reset_auto"):
            payload = self._execute_reset_operations(
                reset_scheduler=bool(reset_scheduler),
                reset_order_status=bool(reset_order_status),
                reset_location=bool(reset_location),
                reset_devices=bool(reset_devices),
            )
            self._attach_reset_material_sync(
                payload,
                sync_materials_after_reset=bool(sync_materials_after_reset),
                clear_stale_after_reset=bool(clear_stale_after_reset),
                publish_tree_after_reset=bool(publish_tree_after_reset),
            )
            return payload

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
        goal_default={
            "reset_scheduler": True,
            "reset_order_status": True,
            "reset_location": True,
            "reset_devices": False,
            "sync_materials_after_reset": True,
            "clear_stale_after_reset": True,
            "publish_tree_after_reset": True,
            "physical_cleanup_confirmed": False,
            "timeout_seconds": 3600,
            "assignee_user_ids": [],
        },
        feedback_interval=300,
        description=RESET_MANUAL_CONFIRM_MESSAGE,
    )
    def reset_manual(
        self,
        reset_scheduler: bool = True,
        reset_order_status: bool = True,
        reset_location: bool = True,
        reset_devices: bool = False,
        sync_materials_after_reset: bool = True,
        clear_stale_after_reset: bool = True,
        publish_tree_after_reset: bool = True,
        physical_cleanup_confirmed: bool = False,
        timeout_seconds: int = 3600,
        assignee_user_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """人工确认物理清理完毕后执行复位。

        操作员需先按弹窗提示完成 G3/CEM/Tecan/撕膜机/封膜机/打标机/旋转堆栈/3 个转台
        等位置的物料清理，并开门检查冰箱/IDOT/酶标仪/离心机/LCMS 内部无遗留，再勾选
        ``physical_cleanup_confirmed``，节点才会真正调用复位接口。

        Args:
            reset_scheduler[调度器复位]: 调用 /api/lims/scheduler/reset，默认勾选。
            reset_order_status[订单状态复位]: 调用 /api/lims/order/reset-order-status，默认勾选。
            reset_location[库位复位]: 调用 /api/lims/storage/reset-location，默认勾选。
            reset_devices[仪器复位]: 调用 /api/lims/device/reset-devices，默认不勾选。
            sync_materials_after_reset[复位后同步物料]: 库位复位成功后拉取库存并更新本地资源树，默认勾选。
            clear_stale_after_reset[同步时清理陈旧缓存]: 复位后同步时清理本次库存快照外的旧缓存，默认勾选。
            publish_tree_after_reset[同步后发布资源树]: 复位后同步成功时发布资源树，默认勾选。
            physical_cleanup_confirmed[物理清理确认]: 确认弹窗中的物料检查已完成，默认不勾选；未勾选时不会调用任何 RPC。
        """
        del kwargs, timeout_seconds, assignee_user_ids
        with self._debug_call_session("reset_manual"):
            if not bool(physical_cleanup_confirmed):
                logger.info("[reset_manual] 物理清理未确认，拒绝执行复位 RPC")
                return {
                    "status": "blocked",
                    "physical_cleanup_confirmed": False,
                    "confirmation_message": RESET_MANUAL_CONFIRM_MESSAGE,
                    "selected_operations": self._build_selected_operations_summary(
                        reset_scheduler=bool(reset_scheduler),
                        reset_order_status=bool(reset_order_status),
                        reset_location=bool(reset_location),
                        reset_devices=bool(reset_devices),
                    ),
                    "executed_calls": [],
                    "skipped_operations": [
                        {"operation": op, "reason": "physical_cleanup_not_confirmed"}
                        for op in RESET_OPERATION_KEYS
                    ],
                    "warnings": ["physical_cleanup_not_confirmed"],
                }
            payload = self._execute_reset_operations(
                reset_scheduler=bool(reset_scheduler),
                reset_order_status=bool(reset_order_status),
                reset_location=bool(reset_location),
                reset_devices=bool(reset_devices),
            )
            self._attach_reset_material_sync(
                payload,
                sync_materials_after_reset=bool(sync_materials_after_reset),
                clear_stale_after_reset=bool(clear_stale_after_reset),
                publish_tree_after_reset=bool(publish_tree_after_reset),
            )
            payload["physical_cleanup_confirmed"] = True
            payload["confirmation_message"] = RESET_MANUAL_CONFIRM_MESSAGE
            return payload

    @action(always_free=True, description="启动 Bioyond 调度器")
    def scheduler_start(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return self._run_scheduler_action("scheduler_start", "启动")

    @action(always_free=True, description="停止 Bioyond 调度器")
    def scheduler_stop(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return self._run_scheduler_action("scheduler_stop", "停止")

    @action(always_free=True, description="暂停 Bioyond 调度器")
    def scheduler_pause(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return self._run_scheduler_action("scheduler_pause", "暂停")

    @action(always_free=True, description="继续 Bioyond 调度器")
    def scheduler_continue(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return self._run_scheduler_action("scheduler_continue", "继续")

    @action(always_free=True, description="设置 Bioyond LIMS 推送到本机 HTTP 服务的 IP 和端口")
    def update_push_ip(self, ip: str = "", port: int = 0) -> Dict[str, Any]:
        """设置 Bioyond LIMS 回调/推送目标地址。

        Args:
            ip: HTTP 服务 IP；留空时使用配置 ``HTTP_host``。
            port: HTTP 服务端口；传 0 时使用配置 ``HTTP_port``。
        """
        target_ip = str(ip or self.bioyond_config.get("HTTP_host") or "").strip()
        target_port = int(port or self.bioyond_config.get("HTTP_port") or 0)
        rpc = self._require_hardware_interface("set_ip_config")
        with self._debug_call_session("update_push_ip"):
            raw = rpc.set_ip_config(target_ip, target_port)
        success = isinstance(raw, dict) and raw.get("code") == 1
        message = str(raw.get("message", "") or "") if isinstance(raw, dict) else ""
        return {
            "success": bool(success),
            "ip": target_ip,
            "port": target_port,
            "raw": raw if isinstance(raw, dict) else {},
            "message": message if success else (message or "设置 Bioyond LIMS 推送地址失败"),
        }

    @action(
        always_free=True,
        goal_default={
            "status": '全部（""）',
            "max_results": 10,
            "filter_text": "",
            "sorting": "creationTime desc",
            "skipCount": 0,
            "timeType": "",
            "beginTime": None,
            "endTime": None,
            "latest_only": True,
        },
        description=(
            "只读查询 Bioyond LIMS 订单列表。"
            "status 必填：全部（\"\"）/成功（80）/失败（90）/执行中（60）/已取出（100）。"
            "max_results 对应 pageCount，默认 10。其余查询条件可选。"
        ),
        handles=[
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_ids", data_type="bioyond_order_ids", label="实验ID列表", data_key="order_ids", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_code", data_type="bioyond_order_code", label="实验编号", data_key="order_code", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="order_codes", data_type="bioyond_order_codes", label="实验编号列表", data_key="order_codes", data_source=DataSource.EXECUTOR),
        ],
    )
    def get_order_list(
        self,
        status: OrderStatus = '全部（""）',
        max_results: int = 10,
        filter_text: str = "",
        sorting: str = "creationTime desc",
        skipCount: int = 0,
        timeType: str = "",
        beginTime: Optional[str] = None,
        endTime: Optional[str] = None,
        latest_only: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        timeType = str(kwargs.pop("time_type", timeType) or "")
        beginTime = kwargs.pop("begin_time", beginTime)
        endTime = kwargs.pop("end_time", endTime)
        skipCount = int(kwargs.pop("skip_count", skipCount) or 0)
        max_results = int(kwargs.pop("page_count", max_results) or 10)
        del kwargs
        normalized_status = ORDER_STATUS_VALUE_MAP.get(str(status), str(status or ""))
        params = self._normalize_order_list_params(
            {
                "timeType": timeType,
                "beginTime": beginTime,
                "endTime": endTime,
                "status": normalized_status,
                "filter": filter_text,
                "skipCount": skipCount,
                "pageCount": max_results,
                "sorting": sorting,
            }
        )
        with self._debug_call_session("get_order_list"):
            raw = self._require_hardware_interface().order_query(json.dumps(params, ensure_ascii=False))
        items = self._as_list(raw.get("items") if isinstance(raw, dict) else raw)
        if latest_only and items:
            items = items[:1]
        orders: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("id") or "").strip()
            order_code = str(item.get("orderCode") or item.get("code") or "").strip()
            if not order_id:
                warnings.append("order_list_item_missing_id")
                continue
            if not order_code:
                warnings.append("order_list_item_missing_order_code")
            orders.append({
                "order_id": order_id,
                "order_code": order_code,
                "order_name": str(item.get("name") or item.get("orderName") or ""),
                "status": str(item.get("status") or ""),
                "created_at": str(item.get("creationTime") or item.get("createdAt") or ""),
                "raw": item,
            })
        order_ids = [item["order_id"] for item in orders]
        order_codes = [item["order_code"] for item in orders if item.get("order_code")]
        return {
            "success": bool(orders),
            "raw": raw,
            "query": params,
            "items": items,
            "orders": orders,
            "total_count": raw.get("totalCount") if isinstance(raw, dict) else len(items),
            "order_ids": order_ids,
            "order_id": order_ids[0] if order_ids else "",
            "order_codes": order_codes,
            "order_code": order_codes[0] if order_codes else "",
            "warnings": warnings,
        }

    @action(
        always_free=True,
        goal_default={"order_id": "", "preintake_ids": [], "material_ids": []},
        description="按订单取出 Bioyond LIMS 中已分配/预占的物料",
        handles=[
            ActionInputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
        ],
    )
    def take_out(
        self,
        order_id: str,
        preintake_ids: Optional[List[str]] = None,
        material_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise ValueError("take_out 需要 order_id")
        normalized_preintake_ids = self._normalize_string_list(preintake_ids)
        normalized_material_ids = self._normalize_string_list(material_ids)
        rpc = self._require_hardware_interface("take_out")
        order_material_ids = self._collect_order_material_ids(
            normalized_order_id,
            explicit_material_ids=normalized_material_ids,
        )
        with self._debug_call_session("take_out"):
            raw = rpc.take_out(normalized_order_id, normalized_preintake_ids, normalized_material_ids)
        code = raw.get("code") if isinstance(raw, dict) else None
        message = str(raw.get("message", "") or "") if isinstance(raw, dict) else ""
        post_take_out_sync: Dict[str, Any] = {"requested": False, "status": "skipped"}
        deleted_materials = {"material_ids": order_material_ids, "removed_from_deck": 0, "removed_from_cache": 0}
        if code == 1:
            try:
                post_take_out_sync = self._sync_order_materials_from_bioyond(
                    normalized_order_id,
                    reason="take_out",
                )
            except Exception as exc:
                logger.warning(
                    f"[peptide] take_out 后订单物料同步失败: order_id={normalized_order_id} error={exc}"
                )
                post_take_out_sync = {
                    "requested": True,
                    "status": "failed",
                    "order_id": normalized_order_id,
                    "error": str(exc),
                }
            if (
                post_take_out_sync.get("status") == "failed"
                or (not post_take_out_sync.get("material_count") and order_material_ids)
            ):
                deleted_materials = self._delete_bioyond_materials_from_cache_and_deck(
                    order_material_ids,
                    publish_tree=True,
                    reason=f"take_out:{normalized_order_id}",
                )
        return {
            "success": code == 1,
            "order_id": normalized_order_id,
            "preintake_ids": normalized_preintake_ids,
            "material_ids": normalized_material_ids,
            "order_material_ids": order_material_ids,
            "deleted_materials": deleted_materials,
            "post_take_out_sync": post_take_out_sync,
            "take_out": raw if isinstance(raw, dict) else {},
            "raw_result": raw if isinstance(raw, dict) else {},
            "code": code,
            "message": message,
        }

    @action(
        always_free=True,
        goal_default={"order_id": ""},
        description="按实验ID查询 Bioyond LIMS 订单实验台物料（materials-by-order-id）",
        handles=[
            ActionInputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="materials", data_type="array", label="订单实验台物料", data_key="materials", data_source=DataSource.EXECUTOR),
        ],
    )
    def materials_by_order_id(self, order_id: str, **kwargs: Any) -> Dict[str, Any]:
        """按 orderId UUID 查询订单实验台物料。

        Args:
            order_id: Bioyond LIMS 订单 UUID；不是 orderCode/实验编号。
        """
        del kwargs
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise ValueError("materials_by_order_id 需要 order_id")
        with self._debug_call_session("materials_by_order_id"):
            material_sync = self._sync_order_materials_from_bioyond(
                normalized_order_id,
                reason="materials_by_order_id",
            )
        materials = list(material_sync.get("materials") or [])
        return {
            "success": bool(materials),
            "order_id": normalized_order_id,
            "materials": materials,
            "material_count": len(materials),
            "cache": {"available": True, "status": material_sync.get("status")},
            "material_sync": material_sync,
        }

    @action(
        always_free=True,
        goal_default={"order_codes": []},
        description="按实验编号批量取消 Bioyond 实验，仅调用批量取消接口，不执行 take_out",
        handles=[
            ActionInputHandle(key="order_codes", data_type="bioyond_order_codes", label="实验编号列表", data_key="order_codes", data_source=DataSource.HANDLE, io_type="source"),
        ],
    )
    def batch_cancel_experiment(
        self,
        order_codes: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        normalized_order_codes = self._normalize_string_list(order_codes)
        if not normalized_order_codes:
            raise ValueError("batch_cancel_experiment 需要 order_codes")
        rpc = self._require_hardware_interface("batch_cancel_experiment")
        with self._debug_call_session("batch_cancel_experiment"):
            code = rpc.batch_cancel_experiment(normalized_order_codes)
        return {
            "success": code == 1,
            "order_codes": normalized_order_codes,
            "code": code,
            "message": "批量取消成功" if code == 1 else "批量取消失败",
        }

    @action(always_free=True, description="查询单订单实验报告")
    def get_order_report(self, order_id: str) -> Dict[str, Any]:
        resolved = self._require_uuid(order_id, "order_id")
        with self._debug_call_session("get_order_report"):
            raw = self._require_hardware_interface().order_report(resolved)
        return {"success": True, "order_id": resolved, "raw": raw, "summary": self._normalize_order_report(raw)}

    @action(always_free=True, description="聚合订单报告（占位）")
    def get_aggregated_order_report(self, order_id: str) -> Dict[str, Any]:
        # TODO: 待多肽侧确认聚合需求后再实现。
        # Sirna 风格聚合通常组合以下接口：
        #   - /api/lims/order/order-report
        #   - /api/lims/order/order-list （order-query）
        #   - /api/lims/order/gantt-with-simulation-by-order-id
        #   - /api/lims/order/gantts-by-order-id
        #   - /api/lims/storage/material-info
        resolved = self._require_uuid(order_id, "order_id")
        return {
            "success": False,
            "status": "not_implemented",
            "order_id": resolved,
            "message": "聚合报告尚未实现，请使用 get_order_report。",
        }

    @action(
        always_free=True,
        description="查询订单报告文件列表",
        handles=[
            ActionInputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="order_id", data_type="bioyond_order_id", label="实验ID", data_key="order_id", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="file_zip", data_type="str", label="报告 ZIP 文件", data_key="file_zip", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="files", data_type="array", label="报告文件列表", data_key="files", data_source=DataSource.EXECUTOR),
        ],
    )
    def get_order_report_files(self, order_id: str) -> Dict[str, Any]:
        resolved = self._require_uuid(order_id, "order_id")
        rpc = self._require_hardware_interface()
        with self._debug_call_session("get_order_report_files"):
            files = rpc.order_report_files(resolved)
        api_host = str(getattr(rpc, "host", "") or self.bioyond_config.get("api_host", "")).rstrip("/")
        file_urls = [self._join_api_url(api_host, path) for path in files]
        zip_urls = [url for url in file_urls if url.lower().endswith(".zip")]
        file_zip = zip_urls[-1] if zip_urls else ""
        return {"success": True, "order_id": resolved, "file_zip": file_zip, "files": file_urls, "file_count": len(file_urls)}

    @action(
        always_free=True,
        goal_default={"title": "", "values": None},
        description="展示上游传入的任意内容",
        handles=[
            ActionInputHandle(key="values", data_type="str", label="内容", data_key="values", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="title", data_type="str", label="标题", data_key="title", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="values", data_type="str", label="内容", data_key="values", data_source=DataSource.EXECUTOR),
        ],
    )
    def display_values(self, title: str = "", values: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """普通展示节点：透传任意上游内容。"""
        del kwargs
        return {"success": True, "title": str(title or ""), "values": self._display_text(values)}

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
        goal_default={
            "title": "",
            "values": None,
            "display_confirmed": False,
            "timeout_seconds": 3600,
            "assignee_user_ids": [],
        },
        feedback_interval=300,
        description="展示上游传入的任意内容，并等待人工确认",
        handles=[
            ActionInputHandle(key="values", data_type="str", label="内容", data_key="values", data_source=DataSource.HANDLE, io_type="source"),
            ActionInputHandle(key="assignee_user_ids", data_type="array", label="确认人", data_key="assignee_user_ids", data_source=DataSource.HANDLE, io_type="source"),
            ActionOutputHandle(key="title", data_type="str", label="标题", data_key="title", data_source=DataSource.EXECUTOR),
            ActionOutputHandle(key="values", data_type="str", label="内容", data_key="values", data_source=DataSource.EXECUTOR),
        ],
    )
    def display_values_manual_confirm(
        self,
        title: str = "",
        values: Any = None,
        display_confirmed: bool = False,
        timeout_seconds: int = 3600,
        assignee_user_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """人工确认展示内容已查看。"""
        del timeout_seconds, kwargs
        if not self._as_manual_gate(display_confirmed):
            raise RuntimeError("展示内容尚未确认")
        return {
            "success": True,
            "title": str(title or ""),
            "values": self._display_text(values),
            "display_confirmed": True,
            "assignee_user_ids": list(assignee_user_ids or []),
            "instruction_text": "请查看内容，确认完成后勾选 display_confirmed。",
        }

    # ---------- 样品 Excel ----------

    def _resolve_submit_sample_file(
        self,
        required_params: Dict[str, Any],
        optional_params: Dict[str, Any],
        sample_excel_relative_path: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        del optional_params  # local upload 已在 plan 移除范围内
        direct = str(sample_excel_relative_path or "").strip()
        if direct:
            return direct.replace("/", "\\"), {"relativePath": direct, "fileName": Path(direct).name}
        pattern = str(required_params.get("sample_excel_pattern") or "").strip()
        if not pattern:
            raise PeptideWorkflowError("必须提供 sample_excel_relative_path 或 sample_excel_pattern")
        selected = self._select_sample_excel_record(
            self._list_sample_excels(name_filter=pattern.replace("*", "")),
            pattern,
        )
        relative = str(selected.get("relativePath") or "").replace("/", "\\")
        if not relative:
            raise PeptideWorkflowError(f"样品 Excel 记录缺少 relativePath: {selected}")
        return relative, selected

    def _select_sample_excel_record(self, records: List[Dict[str, Any]], pattern: str) -> Dict[str, Any]:
        matched = [
            record
            for record in records
            if self._filename_matches_pattern(str(record.get("fileName") or ""), pattern)
            or self._filename_matches_pattern(str(record.get("relativePath") or ""), pattern)
        ]
        if not matched:
            raise PeptideWorkflowError(f"未找到匹配 {pattern!r} 的样品 Excel")
        if len(matched) > 1:
            names = ", ".join(str(item.get("fileName") or "") for item in matched)
            raise PeptideWorkflowError(f"找到多个匹配 {pattern!r} 的样品 Excel: {names}")
        return matched[0]

    @staticmethod
    def _extract_cem_pdf_path(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("cemPdfPath", "cem_pdf_path", "pdfPath", "path", "url"):
                value = data.get(key)
                if value:
                    return str(value)
            return ""
        return str(data or "")

    @staticmethod
    def _join_api_url(api_host: str, path: str) -> str:
        base = str(api_host or "").rstrip("/")
        suffix = quote(str(path or "").replace("\\", "/").lstrip("/"), safe="/")
        return f"{base}/{suffix}" if base else suffix

    def _list_sample_excels(self, name_filter: str = "", begin_date: Any = None, end_date: Any = None) -> List[Dict[str, Any]]:
        rpc = self._require_hardware_interface()
        payload = {"beginDate": begin_date, "endDate": end_date, "nameFilter": name_filter or None}
        response = rpc.post(
            url=f"{rpc.host}/api/lims/order/sample-info-excels",
            params={"apiKey": rpc.api_key, "requestTime": _utc_now_iso8601_ms(), "data": payload},
        )
        if not response or response.get("code") != 1:
            raise RuntimeError(f"样品 Excel 列表查询失败: {response}")
        data = response.get("data")
        return data if isinstance(data, list) else []

    def _upload_sample_excel_file(self, local_excel_path: str | Path, content_type: Optional[str] = None) -> Dict[str, Any]:
        api_host = str(self.bioyond_config.get("api_host", "")).rstrip("/")
        timeout = int(self.bioyond_config.get("timeout", 30) or 30)
        local_path = Path(str(local_excel_path).strip()).expanduser()
        if not local_path.is_absolute() and not str(local_excel_path).startswith("./"):
            raise ValueError("样品 Excel 路径请使用完整路径；相对路径必须以 ./ 开头")
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"样品 Excel 不存在: {local_path}")
        resolved_content_type = (
            content_type
            or mimetypes.guess_type(local_path.name)[0]
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        with local_path.open("rb") as file:
            response = requests.post(
                f"{api_host}/api/lims/order/up-load-sample-file",
                files={"file": (local_path.name, file, resolved_content_type)},
                timeout=timeout,
            )
        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw_text": response.text}
        if response.status_code >= 400 or not isinstance(body, dict) or body.get("code") != 1:
            raise RuntimeError(f"样品 Excel 上传失败: status={response.status_code}, body={body}")
        file_info = body.get("data") if isinstance(body.get("data"), dict) else {}
        remote = str(file_info.get("filePath") or "")
        return {
            "success": True,
            "lims_file_info": file_info,
            "relative_path": remote,
            "sample_file_parameter": remote.replace("/", "\\") if remote else "",
            "response": body,
        }

    @staticmethod
    def _filename_matches_pattern(file_name: str, pattern: str) -> bool:
        if not pattern or pattern == "*":
            return True
        if "*" in pattern:
            from fnmatch import fnmatch

            return fnmatch(file_name, pattern)
        # 无通配符 → 子串匹配（plan 中样品 pattern 通常是文件名前缀，如 "DPR019"）。
        return pattern in file_name

    # ---------- 工作流 / 步骤参数 ----------

    def _resolve_workflow_binding(self, day_key: str) -> Dict[str, Any]:
        config = DAY_WORKFLOW_BINDINGS.get(day_key)
        if not config:
            raise PeptideWorkflowError(f"未知 day_key: {day_key}")
        return self._resolve_workflow_binding_from_names(config["root_name"], config["sub_name"])

    def _resolve_workflow_binding_from_names(self, workflow_name: str, subworkflow_name: str = "") -> Dict[str, Any]:
        bindings = self._filter_workflow_records(
            self._query_workflow_records(workflow_name),
            workflow_name_filter=workflow_name,
            subworkflow_name_filter=subworkflow_name,
        )
        if not bindings:
            raise PeptideWorkflowError(f"未找到工作流 {workflow_name!r} 的可用子工作流")
        if len(bindings) > 1:
            names = ", ".join(item.get("subworkflowName", "") for item in bindings)
            raise PeptideWorkflowError(f"工作流 {workflow_name!r} 匹配到多个子工作流: {names}")
        item = bindings[0]
        return {
            "workflow_name": item.get("workflowName"),
            "root_workflow_id": item.get("workflowId"),
            "sub_workflow_id": item.get("subworkflowId"),
            "sub_workflow_name": item.get("subworkflowName"),
            "raw": item,
        }

    def _query_workflow_records(self, workflow_name_filter: str, include_detail: bool = True) -> List[Dict[str, Any]]:
        payload = {"type": 0, "filter": workflow_name_filter, "includeDetail": include_detail}
        data = self._require_hardware_interface().query_workflow(json.dumps(payload, ensure_ascii=False))
        records: List[Dict[str, Any]] = []
        for item in self._as_list(data.get("items") if isinstance(data, dict) else data):
            if not isinstance(item, dict):
                continue
            root_id = str(item.get("id") or "")
            root_name = str(item.get("name") or "")
            for sub in self._as_list(item.get("subWorkflows")):
                if not isinstance(sub, dict):
                    continue
                if sub.get("isSaved") is False:
                    continue
                records.append(
                    {
                        "workflowId": root_id,
                        "workflowName": root_name,
                        "subworkflowId": str(sub.get("id") or ""),
                        "subworkflowName": str(sub.get("name") or ""),
                        "sequence": sub.get("sequence"),
                        "status": sub.get("status"),
                    }
                )
        return records

    def _filter_workflow_records(
        self,
        workflow_records: List[Dict[str, Any]],
        *,
        workflow_name_filter: str = "",
        subworkflow_name_filter: str = "",
    ) -> List[Dict[str, Any]]:
        wf_filter = workflow_name_filter.strip()
        sub_filter = subworkflow_name_filter.strip()

        def _match_name(value: str, needle: str) -> bool:
            if not needle:
                return True
            return needle in value

        filtered = [
            record
            for record in workflow_records
            if _match_name(str(record.get("workflowName") or ""), wf_filter)
            and _match_name(str(record.get("subworkflowName") or ""), sub_filter)
        ]
        if wf_filter:
            exact = [r for r in filtered if str(r.get("workflowName") or "") == wf_filter]
            if exact:
                filtered = exact
        if sub_filter:
            exact_sub = [r for r in filtered if str(r.get("subworkflowName") or "") == sub_filter]
            if exact_sub:
                filtered = exact_sub
        return filtered

    def _query_step_parameters(self, sub_workflow_id: str) -> Any:
        data = self._require_hardware_interface().workflow_step_query(self._require_uuid(sub_workflow_id, "sub_workflow_id"))
        return data or {}

    def _flatten_step_parameters(self, step_data: Any) -> List[Dict[str, Any]]:
        parsed = self._json_loads_if_string(step_data)
        if not isinstance(parsed, dict):
            return []
        flattened: List[Dict[str, Any]] = []
        for step_id, modules in parsed.items():
            if not self._looks_like_uuid_text(step_id):
                continue
            for module in self._as_list(modules):
                if not isinstance(module, dict):
                    continue
                step_name = str(module.get("name") or "")
                module_m = module.get("m")
                module_n = module.get("n")
                for parameter in self._as_list(module.get("parameterList") or module.get("ParameterList")):
                    if not isinstance(parameter, dict):
                        continue
                    key = parameter.get("Key") or parameter.get("key")
                    if not key:
                        continue
                    flattened.append(
                        {
                            "step": str(step_id),
                            "step_name": step_name,
                            "Key": str(key),
                            "display_para_name": (
                                parameter.get("display_para_name")
                                or parameter.get("displayParaName")
                                or parameter.get("DisplayName")
                                or parameter.get("name")
                                or str(key)
                            ),
                            "m": parameter.get("m", module_m),
                            "n": parameter.get("n", module_n),
                            "TaskDisplayable": parameter.get("TaskDisplayable", parameter.get("task_displayable", parameter.get("taskDisplayable"))),
                            "Value": parameter.get("Value", parameter.get("value")),
                            "DisplayValue": parameter.get("DisplayValue", parameter.get("displayValue")),
                        }
                    )
        return flattened

    def _filter_step_parameter_records(
        self,
        records: List[Dict[str, Any]],
        required_para: bool,
        optional_parameter: bool,
        hidden_para: bool,
    ) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for record in records:
            displayable = record.get("TaskDisplayable")
            is_hidden = displayable in (0, "0", False)
            is_displayable = displayable in (1, "1", True)
            has_value = self._parameter_value_present(record.get("Value")) or self._parameter_value_present(record.get("DisplayValue"))
            if is_hidden and hidden_para:
                filtered.append(record)
            elif is_displayable and not has_value and required_para:
                filtered.append(record)
            elif is_displayable and has_value and optional_parameter:
                filtered.append(record)
            # TaskDisplayable 既非 0/1 时（None 或其它），保守跳过避免误归类。
        return filtered

    @staticmethod
    def _parameter_value_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str) and value == "":
            return False
        return True

    def _build_partial_parameter_entries(
        self,
        *,
        sample_excel_relative_path: str,
        day_key: Optional[str],
        required_params: Optional[Dict[str, Any]] = None,
        parameter_overrides: Any = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        warnings: List[str] = []
        entries: List[Dict[str, Any]] = [{"Key": PEPTIDE_SAMPLE_FILE_KEY, "Value": sample_excel_relative_path}]
        if day_key == "day1":
            required = dict(required_params or {})
            cem_method = str(required.get("cem_method_file_name") or DAY1_CEM_METHOD_DEFAULT).strip() or DAY1_CEM_METHOD_DEFAULT
            entries.append({"Key": DAY1_CEM_METHOD_KEY, "Value": cem_method})
        entries.extend(self._normalize_override_list(parameter_overrides, warnings))
        return entries, warnings

    def _normalize_override_list(self, overrides: Any, warnings: List[str]) -> List[Dict[str, Any]]:
        if overrides is None or overrides == "" or overrides == []:
            return []
        if isinstance(overrides, dict):
            # 容错：用户传 dict 时按 {Key: Value} 展开。
            overrides = [{"Key": k, "Value": v} for k, v in overrides.items()]
        if not isinstance(overrides, list):
            raise PeptideWorkflowError("parameter_overrides 必须是列表或字典")
        normalized: List[Dict[str, Any]] = []
        seen: Dict[Tuple[Any, ...], int] = {}
        for raw in overrides:
            entry = self._normalize_parameter_entry(raw, allow_key_only=True)
            if entry is None:
                continue
            key = (entry.get("Key"), entry.get("m"), entry.get("n"))
            if key in seen:
                warnings.append(f"parameter_overrides 重复项 {key}，采用最后一次覆盖")
                normalized[seen[key]] = entry
            else:
                seen[key] = len(normalized)
                normalized.append(entry)
        return normalized

    def _normalize_parameter_entry(self, entry: Any, *, allow_key_only: bool = False) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        key = entry.get("Key") or entry.get("key")
        value = entry.get("Value", entry.get("value"))
        if not key:
            return None
        if value is None and not allow_key_only:
            return None
        normalized: Dict[str, Any] = {"Key": str(key), "Value": value}
        for axis in ("m", "n"):
            if axis in entry and entry[axis] is not None:
                try:
                    normalized[axis] = int(entry[axis])
                except (TypeError, ValueError):
                    normalized[axis] = entry[axis]
        return normalized

    def _resolve_parameter_entries_against_live_steps(
        self,
        partial_entries: List[Dict[str, Any]],
        flattened: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        for partial in partial_entries:
            key = partial.get("Key")
            m_filter = partial.get("m")
            n_filter = partial.get("n")
            matches = [
                live
                for live in flattened
                if live.get("Key") == key
                and (m_filter is None or live.get("m") == m_filter)
                and (n_filter is None or live.get("n") == n_filter)
            ]
            if len(matches) != 1:
                raise PeptideWorkflowError(
                    f"参数 Key={key} m={m_filter if m_filter is not None else '<omitted>'} "
                    f"n={n_filter if n_filter is not None else '<omitted>'} 期望唯一匹配，实际 {len(matches)} 条"
                )
            live = matches[0]
            resolved.append(
                {
                    "step": live.get("step"),
                    "Key": key,
                    "Value": partial.get("Value"),
                    "m": partial.get("m", live.get("m")),
                    "n": partial.get("n", live.get("n")),
                }
            )
        return resolved

    def _group_resolved_entries_to_param_values(self, resolved_entries: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in resolved_entries:
            step_id = str(entry.get("step") or "")
            if not self._looks_like_uuid_text(step_id):
                continue
            payload_entry: Dict[str, Any] = {
                "key": str(entry.get("Key")),
                "value": "" if entry.get("Value") is None else str(entry.get("Value")),
            }
            for axis in ("m", "n"):
                if entry.get(axis) is not None:
                    try:
                        payload_entry[axis] = int(entry[axis])
                    except (TypeError, ValueError):
                        payload_entry[axis] = entry[axis]
            grouped.setdefault(step_id, []).append(payload_entry)
        return grouped

    def _build_order_identity(self, day_key: str, order_name_override: Any = None) -> Tuple[str, str]:
        stamp = datetime.now().strftime("%y%m%d-%H%M%S")
        order_code = f"EXP{stamp}"
        if order_name_override:
            return order_code, str(order_name_override)
        return order_code, f"实验{stamp}"

    def _create_order_payload(
        self,
        *,
        order_code: str,
        order_name: str,
        sub_workflow_id: str,
        param_values: Dict[str, List[Dict[str, Any]]],
        border_number: int,
        extend_properties: Any,
    ) -> List[Dict[str, Any]]:
        item: Dict[str, Any] = {
            "orderCode": order_code,
            "orderName": order_name,
            "borderNumber": border_number,
            "workFlowId": self._require_uuid(sub_workflow_id, "workFlowId"),
            "paramValues": param_values,
            "extendProperties": "" if extend_properties in (None, "") else str(extend_properties),
        }
        return [item]

    def _create_order(self, order_payload: List[Dict[str, Any]]) -> Any:
        return self._require_hardware_interface().create_order(json.dumps(order_payload, ensure_ascii=False))

    def _parse_create_order_allocation_map(self, create_order_data_raw: Any) -> Dict[str, Any]:
        parsed = self._parse_result(create_order_data_raw)
        allocation_map: Dict[str, Any] = {}
        if isinstance(parsed, dict):
            allocation_map = parsed
        order_ids = [key for key in allocation_map if self._looks_like_uuid_text(key)]
        rows: List[Dict[str, Any]] = []
        for order_key in order_ids:
            for row in self._as_list(allocation_map.get(order_key)):
                if isinstance(row, dict):
                    rows.append(row)
        materials_by_type: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            mode = str(row.get("materialTypeMode") or "Unknown")
            materials_by_type.setdefault(mode, []).append(row)
        return {
            "allocation_map": allocation_map,
            "allocation_rows": rows,
            "order_ids": order_ids,
            "materials_by_type": materials_by_type,
        }

    @staticmethod
    def _sort_result_table_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def text_key(value: Any) -> Tuple[int, str]:
            text = str(value or "").strip()
            return (1, "") if not text else (0, text)

        def location_key(value: Any) -> Tuple[int, Tuple[Any, ...]]:
            text = str(value or "").strip()
            if not text:
                return (1, ())
            chunks: List[Any] = []
            for chunk in re.split(r"(\d+)", text):
                if not chunk:
                    continue
                chunks.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
            return (0, tuple(chunks))

        return sorted(
            rows,
            key=lambda row: (
                text_key(row.get("materialName")),
                text_key(row.get("whName")),
                location_key(row.get("locationCode")),
            ),
        )

    def _build_result_table(self, materials_by_type: Dict[str, List[Dict[str, Any]]], table_name: str = "resultTable") -> Dict[str, Any]:
        material_info_cache: Dict[str, Dict[str, Any]] = {}
        ordered_modes: List[str] = []
        for mode in MATERIAL_TYPE_ORDER:
            if mode in materials_by_type:
                ordered_modes.append(mode)
        for mode in materials_by_type:
            if mode not in ordered_modes:
                ordered_modes.append(mode)
        rows: List[Dict[str, Any]] = []
        for mode in ordered_modes:
            for record in materials_by_type.get(mode, []):
                material_id = str(record.get("materialId") or "")
                location_code = str(record.get("locationShowName") or record.get("locationCode") or "")
                rows.append(
                    {
                        "whName": self._resolve_wh_name_by_material_id(material_id, material_info_cache),
                        "locationCode": location_code,
                        "materialName": str(record.get("materialName") or ""),
                        "quantity": str(record.get("quantity") or ""),
                    }
                )
        return {"data": self._sort_result_table_rows(rows), "columns": copy.deepcopy(RESULT_TABLE_COLUMNS), "tableName": table_name}

    def _resolve_wh_name_by_material_id(self, material_id: str, cache: Dict[str, Dict[str, Any]]) -> str:
        if not material_id:
            return ""
        if material_id not in cache:
            try:
                cache[material_id] = self._require_hardware_interface().material_info(material_id) or {}
            except Exception as exc:
                logger.warning("material_info 查询失败 material_id=%s: %s", material_id, exc)
                cache[material_id] = {}
        locations = self._as_list(cache[material_id].get("locations"))
        location = next((loc for loc in locations if isinstance(loc, dict)), {})
        return str(location.get("whName") or "")

    def _normalize_order_list_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "timeType": params.get("timeType") or "",
            "beginTime": params.get("beginTime"),
            "endTime": params.get("endTime"),
            "status": params.get("status") or "",
            "filter": params.get("filter") or "",
            "skipCount": int(params.get("skipCount") or 0),
            "pageCount": int(params.get("pageCount") or 20),
            "sorting": params.get("sorting") or "",
        }

    def _normalize_order_report(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        return {
            "id": raw.get("id"),
            "name": raw.get("name"),
            "code": raw.get("code"),
            "workflow_name": raw.get("workflowName"),
            "status": raw.get("status"),
            "status_name": raw.get("statusName"),
            "pre_intakes_count": len(self._as_list(raw.get("preIntakes"))),
            "result_list_count": len(self._as_list(raw.get("resultList"))),
        }

    # ---------- wait_for_order_finish / unload_materials 辅助 ----------

    @staticmethod
    def _as_manual_gate(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on", "checked"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", "unchecked", ""}:
                return False
        return bool(value)

    @staticmethod
    def _display_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except TypeError:
            return str(value)

    @staticmethod
    def _format_unload_quantity(q: Any) -> str:
        """把 materials-by-order-id 返回的 ``quantity`` 字段格式化成展示字符串。

        - ``None`` / 空串 → ``""``（前端 No data 占位）
        - 整数值 float（``1.0`` / ``4.0``）→ ``"1"`` / ``"4"``，去掉无意义的 ``.0`` 尾巴
        - 其他保留 ``str(q)`` 原样
        """
        if q is None or q == "":
            return ""
        if isinstance(q, bool):
            return str(q)
        if isinstance(q, float) and q.is_integer():
            return str(int(q))
        return str(q)

    @classmethod
    def _format_unload_quantity_with_unit(cls, quantity: Any, unit: Any) -> str:
        formatted = cls._format_unload_quantity(quantity)
        normalized_unit = str(unit or "").strip()
        if not formatted or not normalized_unit:
            return formatted
        return f"{formatted} {normalized_unit}"

    @staticmethod
    def _normalize_string_list(value: Any) -> List[str]:
        if value is None:
            return []
        source = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in source if str(item or "").strip()]

    @staticmethod
    def _material_id_from_any(item: Any) -> str:
        if isinstance(item, dict):
            value = item.get("id") or item.get("materialId") or item.get("material_id")
        else:
            value = getattr(item, "materialId", None) or getattr(item, "id", None)
        return str(value or "").strip()

    def _cache_order_materials(
        self,
        materials: List[Dict[str, Any]],
        *,
        source: str,
        order_id: str = "",
    ) -> Dict[str, Any]:
        """订单粒度更新物料缓存；不清理全局库存。"""
        synchronizer = getattr(self, "resource_synchronizer", None)
        if synchronizer is None or not hasattr(synchronizer, "_update_material_cache_from_stock"):
            return {"updated_count": 0, "records_count": 0, "available": False}

        normalized: List[Dict[str, Any]] = []
        for material in materials or []:
            if not isinstance(material, dict):
                continue
            payload = dict(material)
            if "id" not in payload and payload.get("materialId"):
                payload["id"] = payload.get("materialId")
            if "name" not in payload and payload.get("materialName"):
                payload["name"] = payload.get("materialName")
            if "code" not in payload and payload.get("materialCode"):
                payload["code"] = payload.get("materialCode")
            if "typeName" not in payload and payload.get("materialTypeName"):
                payload["typeName"] = payload.get("materialTypeName")
            payload.setdefault("locations", payload.get("locations") or [])
            if order_id:
                payload["_order_id"] = order_id
            normalized.append(payload)

        if hasattr(synchronizer, "_resolve_material_payloads"):
            normalized = synchronizer._resolve_material_payloads(normalized, source=source)
        result = synchronizer._update_material_cache_from_stock(normalized, source=source)
        result["available"] = True
        return result

    def _cache_order_allocation_rows(
        self,
        allocation_rows: List[Dict[str, Any]],
        *,
        order_ids: List[str],
        source: str,
    ) -> Dict[str, Any]:
        materials: List[Dict[str, Any]] = []
        for row in allocation_rows or []:
            if not isinstance(row, dict):
                continue
            material_id = str(row.get("materialId") or "").strip()
            if not material_id:
                continue
            material = {
                "id": material_id,
                "name": row.get("materialName"),
                "code": row.get("materialCode"),
                "typeName": row.get("materialTypeName"),
                "materialTypeMode": row.get("materialTypeMode"),
                "quantity": row.get("quantity"),
                "unit": row.get("unit"),
                "combinedMaterialId": row.get("combinedMaterialId"),
                "parameters": row.get("materialParams") or row.get("parameters"),
                "locations": row.get("locations") if isinstance(row.get("locations"), list) else [],
                "_order_ids": list(order_ids or []),
                "raw_allocation_row": dict(row),
            }
            materials.append(material)
        result = self._cache_order_materials(materials, source=source)
        result["allocation_count"] = len(materials)
        return result

    def _collect_order_material_ids(
        self,
        order_id: str,
        *,
        explicit_material_ids: Optional[List[str]] = None,
    ) -> List[str]:
        material_ids = set(self._normalize_string_list(explicit_material_ids))
        try:
            order_materials = self._fetch_materials_by_order_id(order_id)
        except Exception as exc:
            logger.warning(f"[peptide] take_out 前查询订单物料失败: order_id={order_id} error={exc}")
            order_materials = []
        for item in order_materials:
            material_id = self._material_id_from_any(item)
            if material_id:
                material_ids.add(material_id)
        return sorted(material_ids)

    @classmethod
    def _build_unload_rows_from_materials_by_order_id(
        cls,
        all_materials: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """把 ``materials-by-order-id`` 返回数据展平成下料表行（plan v2：4 列）。

        每条物料若有多 ``locations`` 则按 location 拆多行，方便操作员一对一物理取出；
        ``locations`` 为空时仍保留一行空坐标占位，提示该物料无法定位但需要操作员处理。

        Quantity 语义（实测奔曜实现）：
        - 物料级 ``quantity`` 是该物料在订单里的真实总量（操作员关心的"几个"）。
        - location 级 ``quantity`` 是运行时计数，实验未开始 / 已结束时通常为 0；
          下料指引场景没有展示价值。
        因此 location 级为 0 / None / 空时一律回退到物料级 ``top_quantity``，
        避免前端表格里全是 0 的误导。
        """
        rows: List[Dict[str, Any]] = []
        for mat in all_materials or []:
            if not isinstance(mat, dict):
                continue
            material_name = str(mat.get("name") or "")
            top_quantity = mat.get("quantity")
            unit = mat.get("unit")
            locations = mat.get("locations") or []
            if not isinstance(locations, list) or not locations:
                rows.append({
                    "whName": "",
                    "locationCode": "",
                    "materialName": material_name,
                    "quantity": cls._format_unload_quantity_with_unit(top_quantity, unit),
                })
                continue
            for loc in locations:
                if not isinstance(loc, dict):
                    continue
                loc_quantity = loc.get("quantity")
                # 关键：奔曜实际返回 location.quantity=0（运行时计数），
                # 必须用 truthy 判断回退到物料级 quantity，不能用 `is None`。
                if not loc_quantity:
                    loc_quantity = top_quantity
                rows.append({
                    "whName": str(loc.get("whName") or ""),
                    "locationCode": str(loc.get("code") or ""),
                    "materialName": material_name,
                    "quantity": cls._format_unload_quantity_with_unit(loc_quantity, unit),
                })
        return rows

    def _fetch_materials_by_order_id(self, order_id: str) -> List[Dict[str, Any]]:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise ValueError("materials_by_order_id 需要 order_id")
        rpc_for_stock = self._require_hardware_interface("materials_by_order_id")
        payload = {"orderId": normalized_order_id}
        raw = rpc_for_stock.materials_by_order_id(json.dumps(payload, ensure_ascii=False))
        materials = list(raw) if isinstance(raw, list) else []
        self._cache_order_materials(
            materials,
            source="materials_by_order_id",
            order_id=normalized_order_id,
        )
        return materials

    def _normalize_order_material_payload(
        self,
        material: Dict[str, Any],
        *,
        order_id: str = "",
    ) -> Dict[str, Any]:
        payload = dict(material)
        if "id" not in payload and payload.get("materialId"):
            payload["id"] = payload.get("materialId")
        if "name" not in payload and payload.get("materialName"):
            payload["name"] = payload.get("materialName")
        if "code" not in payload and payload.get("materialCode"):
            payload["code"] = payload.get("materialCode")
        if "typeName" not in payload and payload.get("materialTypeName"):
            payload["typeName"] = payload.get("materialTypeName")
        payload.setdefault("locations", payload.get("locations") if isinstance(payload.get("locations"), list) else [])
        if order_id:
            payload["_order_id"] = order_id
        return payload

    def _order_material_mutation_rank(self, material: Dict[str, Any]) -> int:
        payload = self._normalize_order_material_payload(material)
        locations = payload.get("locations") or []
        combined_parent_id = str(payload.get("combinedMaterialId") or "").strip()
        if not locations and not combined_parent_id:
            return 0
        if self._find_material_resource(payload) is not None:
            return 1
        return 2

    def _sync_order_materials_from_bioyond(
        self,
        order_id: str,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        """按订单拉取物料，按 delete/change/add 顺序更新本地资源树。"""
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return {
                "requested": False,
                "status": "skipped",
                "reason": "missing_order_id",
                "materials": [],
                "material_count": 0,
                "mutations": [],
            }

        materials = self._fetch_materials_by_order_id(normalized_order_id)
        normalized_materials = [
            self._normalize_order_material_payload(material, order_id=normalized_order_id)
            for material in materials
            if isinstance(material, dict)
        ]
        mutation_results: List[Dict[str, Any]] = []
        for material in sorted(normalized_materials, key=self._order_material_mutation_rank):
            try:
                mutation_results.append(self.process_material_change_report(material))
            except Exception as exc:
                logger.warning(
                    f"[peptide] 订单物料资源树更新失败: order_id={normalized_order_id} "
                    f"reason={reason} material={self._material_id_from_any(material)} error={exc}"
                )
                mutation_results.append({
                    "processed": False,
                    "material_id": self._material_id_from_any(material),
                    "error": str(exc),
                })

        return {
            "requested": True,
            "status": "synced" if all(item.get("processed") for item in mutation_results) else "partial",
            "reason": reason,
            "order_id": normalized_order_id,
            "materials": materials,
            "material_count": len(materials),
            "mutations": mutation_results,
            "published_count": sum(1 for item in mutation_results if item.get("published")),
        }

    def _construct_unload_table_payload(self, order_id: str) -> Dict[str, Any]:
        normalized_order_id = str(order_id or "").strip()
        material_sync = self._sync_order_materials_from_bioyond(
            normalized_order_id,
            reason="order_finish",
        )
        materials_by_order_id = list(material_sync.get("materials") or [])
        unload_rows = self._build_unload_rows_from_materials_by_order_id(materials_by_order_id)
        unload_table = self._build_unload_table(unload_rows)
        return {
            "success": True,
            "order_id": normalized_order_id,
            "materials_by_order_id": materials_by_order_id,
            "resultTable": unload_table,
            "material_sync": material_sync,
            "confirmation_message": f"已整理 {len(unload_rows)} 行下料指引",
        }

    @staticmethod
    def _build_unload_table(
        unload_rows: Optional[List[Dict[str, Any]]],
        table_name: str = "下料指引",
    ) -> Dict[str, Any]:
        """按 ``UNLOAD_TABLE_COLUMNS`` 渲染下料指引表的 ``data/columns/tableName`` 三段。"""
        return {
            "data": BioyondPeptideStation._sort_result_table_rows(list(unload_rows or [])),
            "columns": list(UNLOAD_TABLE_COLUMNS),
            "tableName": table_name,
        }

    @staticmethod
    def _used_material_to_dict(item: Any) -> Dict[str, Any]:
        """把基类 ``WorkstationReportRequest.usedMaterials`` 元素序列化成可 JSON 输出的 dict。

        基类把 usedMaterials 反序列化成对象（有 ``materialId``/``locationId``/``usedQuantity`` 等属性），
        本方法兜底 dict / 对象 / 其他三种情况，避免后续 json.dumps 抛。
        """
        if isinstance(item, dict):
            return dict(item)
        if item is None:
            return {}
        if hasattr(item, "__dict__"):
            return {key: value for key, value in vars(item).items() if not key.startswith("_")}
        return {"value": str(item)}

    # ---------- 基础设施 ----------

    def _run_scheduler_action(self, method_name: str, label: str) -> Dict[str, Any]:
        rpc = self._require_hardware_interface()
        method = getattr(rpc, method_name, None)
        if not callable(method):
            raise RuntimeError(f"RPC 缺少调度器方法: {method_name}")
        code = method()
        success = code == 1
        return {"success": success, "code": code, "message": f"调度器{label}{'成功' if success else '失败'}"}

    def _require_hardware_interface(self, method_name: Optional[str] = None) -> Any:
        interface = getattr(self, "hardware_interface", None)
        if interface is None:
            raise RuntimeError("BioyondPeptideStation 未绑定 hardware_interface")
        if method_name is not None and not hasattr(interface, method_name):
            raise RuntimeError(f"Bioyond RPC 客户端缺少 {method_name} 方法")
        return interface

    @staticmethod
    def _build_selected_operations_summary(
        *,
        reset_scheduler: bool,
        reset_order_status: bool,
        reset_location: bool,
        reset_devices: bool,
    ) -> List[Dict[str, Any]]:
        flags: Dict[str, bool] = {
            "reset_scheduler": bool(reset_scheduler),
            "reset_order_status": bool(reset_order_status),
            "reset_location": bool(reset_location),
            "reset_devices": bool(reset_devices),
        }
        return [
            {"key": key, "label": RESET_OPERATION_LABELS[key], "selected": flags[key]}
            for key in RESET_OPERATION_KEYS
        ]

    @staticmethod
    def _reset_operation_succeeded(payload: Dict[str, Any], operation: str) -> bool:
        for call in payload.get("executed_calls", []) or []:
            if call.get("operation") != operation:
                continue
            result = call.get("result") if isinstance(call.get("result"), dict) else {}
            return result.get("code") == 1 and not call.get("error")
        return False

    def _attach_reset_material_sync(
        self,
        payload: Dict[str, Any],
        *,
        sync_materials_after_reset: bool,
        clear_stale_after_reset: bool,
        publish_tree_after_reset: bool,
    ) -> None:
        if not sync_materials_after_reset:
            payload["material_sync"] = {"requested": False, "status": "disabled"}
            return
        selected = {
            item.get("key"): bool(item.get("selected"))
            for item in payload.get("selected_operations", []) or []
            if isinstance(item, dict)
        }
        if not selected.get("reset_location"):
            payload["material_sync"] = {"requested": False, "status": "reset_location_not_selected"}
            return
        if not self._reset_operation_succeeded(payload, "reset_location"):
            payload["material_sync"] = {"requested": False, "status": "reset_location_failed"}
            return

        payload["material_sync"] = self.sync_materials_from_bioyond(
            publish_tree=publish_tree_after_reset,
            clear_stale=clear_stale_after_reset,
        )

    def _execute_reset_operations(
        self,
        *,
        reset_scheduler: bool,
        reset_order_status: bool,
        reset_location: bool,
        reset_devices: bool,
    ) -> Dict[str, Any]:
        """根据 4 个 checkbox 选择顺序调用对应 RPC。

        - 调用顺序固定为 scheduler → order_status → location → devices；
        - 单步失败（``code != 1`` 或 RPC 抛异常）记 warning 但继续执行后续选中的步骤，
          不做 fail-fast，便于操作员在遇到部分故障时仍能完成可恢复的复位。
        """
        rpc = self._require_hardware_interface()
        flags: Dict[str, bool] = {
            "reset_scheduler": bool(reset_scheduler),
            "reset_order_status": bool(reset_order_status),
            "reset_location": bool(reset_location),
            "reset_devices": bool(reset_devices),
        }
        selected_operations = self._build_selected_operations_summary(
            reset_scheduler=reset_scheduler,
            reset_order_status=reset_order_status,
            reset_location=reset_location,
            reset_devices=reset_devices,
        )
        result: Dict[str, Any] = {
            "selected_operations": selected_operations,
            "executed_calls": [],
            "skipped_operations": [],
            "warnings": [],
        }

        rpc_method_map: Dict[str, str] = {
            "reset_scheduler": "scheduler_reset",
            "reset_order_status": "reset_order_status",
            "reset_location": "reset_location",
            "reset_devices": "reset_devices",
        }

        for operation in RESET_OPERATION_KEYS:
            if not flags[operation]:
                result["skipped_operations"].append(
                    {"operation": operation, "reason": "checkbox_disabled"}
                )
                continue
            method_name = rpc_method_map[operation]
            method = getattr(rpc, method_name, None)
            endpoint = RESET_OPERATION_ENDPOINTS[operation]
            if not callable(method):
                msg = f"RPC 缺少方法: {method_name}"
                logger.warning("[reset] %s", msg)
                result["executed_calls"].append({
                    "operation": operation,
                    "endpoint": endpoint,
                    "result": {"code": 0},
                    "error": msg,
                })
                result["warnings"].append(f"{operation}: {msg}")
                continue
            try:
                code = method()
            except Exception as exc:  # 单步异常不阻断其余 reset
                logger.warning("[reset] %s 调用异常: %s", method_name, exc)
                result["executed_calls"].append({
                    "operation": operation,
                    "endpoint": endpoint,
                    "result": {"code": 0},
                    "error": str(exc),
                })
                result["warnings"].append(f"{operation}: {exc}")
                continue
            result["executed_calls"].append({
                "operation": operation,
                "endpoint": endpoint,
                "result": {"code": code},
            })
            if code != 1:
                result["warnings"].append(f"{operation}: rpc_returned_non_one_code={code}")
        return result

    @staticmethod
    def _extract_order_ids(order_id: str = "", order_ids: Optional[List[str]] = None, **kwargs: Any) -> List[str]:
        resolved: List[str] = []
        if order_id:
            resolved.append(str(order_id))
        raw = order_ids if order_ids is not None else kwargs.get("order_ids")
        if isinstance(raw, list):
            resolved.extend(str(value) for value in raw if value)
        return list(dict.fromkeys(resolved))

    def _parse_result(self, result: Any) -> Any:
        if not isinstance(result, str):
            return result
        text = result.strip()
        if not text:
            return text
        try:
            return json.loads(text)
        except ValueError:
            pass
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text

    @staticmethod
    def _looks_like_uuid_text(value: Any) -> bool:
        text = str(value)
        return len(text) == 36 and text.count("-") == 4

    def _require_uuid(self, value: Any, field_name: str) -> str:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{field_name} 必须是 UUID: {value!r}") from exc

    @staticmethod
    def _json_loads_if_string(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except ValueError:
            return value

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]
