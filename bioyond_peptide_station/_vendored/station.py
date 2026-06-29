"""
Bioyond工作站实现
Bioyond Workstation Implementation

集成Bioyond物料管理的工作站示例
"""
import time
import traceback
import threading
import copy
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Any, List, Optional, Union
import json
from pathlib import Path

from unilabos.devices.workstation.workstation_base import WorkstationBase, ResourceSynchronizer
from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC
from bioyond_peptide_station._vendored import debug_call_log
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from unilabos.resources.warehouse import WareHouse
from unilabos.utils.log import logger
from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr, resource_plr_to_bioyond

from unilabos.ros.nodes.base_device_node import ROS2DeviceNode, BaseROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from unilabos.ros.msgs.message_converter import convert_to_ros_msg, Float64, String
from pylabrobot.resources import ResourceHolder
from pylabrobot.resources.resource import Resource as ResourcePLR


from .workstation_http_service import WorkstationHTTPService


class ConnectionMonitor:
    """Bioyond连接监控器"""
    def __init__(self, workstation, check_interval=30):
        self.workstation = workstation
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._last_status = "unknown"

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="BioyondConnectionMonitor")
        self._thread.start()
        logger.info("Bioyond连接监控器已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            logger.info("Bioyond连接监控器已停止")

    def _monitor_loop(self):
        while self._running:
            try:
                # 使用轻量级调度状态接口检查连接，避免启动时打印完整物料类型列表。
                result = self.workstation.hardware_interface.scheduler_status()

                status = "online" if result else "offline"
                if status == "online":
                    msg = (
                        f"Scheduler status={result.get('status')}, "
                        f"hasTask={result.get('hasTask')}"
                    )
                else:
                    msg = "Failed to get scheduler status"

                if status != self._last_status:
                    logger.info(f"Bioyond连接状态变更: {self._last_status} -> {status}")
                    self._publish_event(status, msg)
                    self._last_status = status

                # 发布心跳 (可选，或者只在状态变更时发布)
                # self._publish_event(status, msg)

            except Exception as e:
                logger.error(f"Bioyond连接检查异常: {e}")
                if self._last_status != "error":
                    self._publish_event("error", str(e))
                    self._last_status = "error"

            time.sleep(self.check_interval)

    def _publish_event(self, status, message):
        try:
            if hasattr(self.workstation, "_ros_node") and self.workstation._ros_node:
                event_data = {
                    "status": status,
                    "message": message,
                    "timestamp": datetime.now().isoformat()
                }

                # 动态发布消息，需要在 ROS2DeviceNode 中有对应支持
                # 这里假设通用事件发布机制，使用 String 类型的 topic
                # 话题: /<namespace>/events/device_status
                ns = self.workstation._ros_node.namespace
                topic = f"{ns}/events/device_status"

                # 使用 ROS2DeviceNode 的发布功能
                # 如果没有预定义的 publisher，需要动态创建
                # 注意：workstation base node 可能没有自动创建 arbitrary publishers 的机制
                # 这里我们先尝试用 String json 发布

                # 在 ROS2DeviceNode 中通常需要先 create_publisher
                # 为了简单起见，我们检查是否已有 publisher，没有则创建
                if not hasattr(self.workstation, "_device_status_pub"):
                    self.workstation._device_status_pub = self.workstation._ros_node.create_publisher(
                        String, topic, 10
                    )

                self.workstation._device_status_pub.publish(
                    convert_to_ros_msg(String, json.dumps(event_data, ensure_ascii=False))
                )
        except Exception as e:
            logger.error(f"发布设备状态事件失败: {e}")


class BioyondResourceSynchronizer(ResourceSynchronizer):
    """Bioyond资源同步器

    负责与Bioyond系统进行物料数据的同步
    """

    def __init__(self, workstation: 'BioyondWorkstation'):
        super().__init__(workstation)
        self.bioyond_api_client = None
        self.sync_interval = 60  # 默认60秒同步一次
        self.last_sync_time = 0
        self.last_sync_result: Dict[str, Any] = {}
        self.initialize()

    def initialize(self) -> bool:
        """初始化Bioyond资源同步器"""
        try:
            self.bioyond_api_client = self.workstation.hardware_interface
            if self.bioyond_api_client is None:
                logger.error("Bioyond API客户端未初始化")
                return False

            # 设置同步间隔
            self.sync_interval = self.workstation.bioyond_config.get("sync_interval", 600)

            logger.info("Bioyond资源同步器初始化完成")
            return True
        except Exception as e:
            logger.error(f"Bioyond资源同步器初始化失败: {e}")
            return False

    def sync_from_external(self, clear_stale: bool = False) -> bool:
        """从Bioyond系统全量同步物料数据（移动优先 + 可选删除缺失清扫）。

        - 用 *_result RPC 区分 API 失败 vs 真·空成功；任一必需 typeMode 失败 →
          返回失败、不改缓存/deck。
        - 每个返回物料走工作站 process_material_change_report 的 upsert（move 优先，
          on_empty_locations="park"，source=stock_material，复用并维护瞬态 lookup）。
        - 仅当 clear_stale：跑删除缺失清扫——删除 deck 上携带 bioyond_id 但不在本次
          返回集合中的根资源子树（排除仓库与虚拟暂存节点）。

        返回 bool（沿用历史契约）；详细/失败信息写入 self.last_sync_result，
        失败时 last_sync_result["success"]=False 且带 message。
        """
        try:
            if self.bioyond_api_client is None:
                logger.error("Bioyond API客户端未初始化")
                self.last_sync_result = {
                    "success": False,
                    "failed": True,
                    "message": "Bioyond API客户端未初始化",
                }
                return False

            workstation = self.workstation
            result_fn = getattr(self.bioyond_api_client, "stock_material_result", None)
            if not callable(result_fn):
                self.last_sync_result = {
                    "success": False,
                    "failed": True,
                    "message": "RPC 缺少 stock_material_result（result-aware 变体）",
                }
                logger.error("[Bioyond全量同步] RPC 缺少 stock_material_result")
                return False

            # 必需的 typeMode 集合（沿用历史 0/1/2）。任一失败即整体失败。
            required_type_modes = [0, 1, 2]
            all_bioyond_data: List[Dict[str, Any]] = []
            succeeded_type_modes: List[int] = []
            for type_mode in required_type_modes:
                query = json.dumps({"typeMode": type_mode, "includeDetail": True})
                result = result_fn(query)
                if not isinstance(result, dict) or not result.get("ok"):
                    message = (result or {}).get("message") if isinstance(result, dict) else ""
                    logger.error(
                        f"[Bioyond全量同步] stock_material typeMode={type_mode} 失败，"
                        f"不改缓存/deck: {message}"
                    )
                    self.last_sync_result = {
                        "success": False,
                        "failed": True,
                        "message": message or f"stock_material typeMode={type_mode} 失败",
                        "failed_type_mode": type_mode,
                        "succeeded_type_modes": list(succeeded_type_modes),
                    }
                    return False
                rows = result.get("data") or []
                succeeded_type_modes.append(type_mode)
                if rows:
                    all_bioyond_data.extend(rows)
                    logger.debug(f"从Bioyond查询到 typeMode={type_mode} 物料 {len(rows)} 个")

            # 解析载荷（补类型/编码元数据），构造瞬态 lookup 映射一次。
            all_bioyond_data = self._resolve_material_payloads(
                all_bioyond_data, source=self.SOURCE_STOCK
            )
            lookup = workstation._build_bioyond_lookup_map()

            converted_count = 0
            placed_count = 0
            returned_ids: set[str] = set()
            publish_roots: List[ResourcePLR] = []
            for material in all_bioyond_data:
                material_id = self._material_bioyond_id(material)
                if material_id:
                    returned_ids.add(material_id)
                result = workstation.process_material_change_report(
                    material,
                    on_empty_locations="park",
                    source=self.SOURCE_STOCK,
                    lookup=lookup,
                )
                if result.get("success"):
                    converted_count += 1
                    if result.get("action") in ("add", "move"):
                        placed_count += 1

            # 删除缺失清扫：仅 clear_stale。删除携带 bioyond_id 且不在 returned_ids
            # 的根资源子树（排除仓库 / 虚拟暂存节点）。
            removed_stale_count = 0
            removed_stale_cache_count = 0
            if clear_stale:
                removed_stale_count = workstation._sweep_absent_bioyond_materials(
                    returned_ids, publish_roots=publish_roots
                )
                removed_stale_cache_count = self._prune_bioyond_material_cache(returned_ids)

            dedupe_roots = getattr(workstation, "_dedupe_material_publish_roots", None)
            publish_roots = dedupe_roots(publish_roots) if callable(dedupe_roots) else publish_roots
            try:
                setattr(workstation, "_last_material_sync_publish_roots", publish_roots)
            except Exception:
                pass

            success = True
            self.last_sync_result = {
                "success": success,
                "failed": False,
                "fetched_count": len(all_bioyond_data),
                "converted_count": converted_count,
                "placed_count": placed_count,
                "removed_stale_count": removed_stale_count,
                "removed_stale_cache_count": removed_stale_cache_count,
                "unresolved_count": max(len(all_bioyond_data) - converted_count, 0),
                "clear_stale": bool(clear_stale),
                "succeeded_type_modes": list(succeeded_type_modes),
                "returned_ids_count": len(returned_ids),
                "publish_roots_count": len(publish_roots),
                "message": "Bioyond 物料全量同步完成",
            }
            logger.info(
                "从Bioyond同步了 %s/%s 个资源，放置 %s 个，删除缺失 %s 个，clear_stale=%s",
                converted_count,
                len(all_bioyond_data),
                placed_count,
                removed_stale_count,
                clear_stale,
            )
            return success
        except Exception as e:
            logger.exception(f"从Bioyond同步物料数据失败: {e}")
            self.last_sync_result = {
                "success": False,
                "failed": True,
                "error": str(e),
                "message": str(e),
            }
            return False

    # 源/作用域枚举：用于源感知的缓存簿记。
    #   stock_material   - 全局库存（typeMode 级权威）
    #   order_materials  - 单订单返回行（订单级权威）
    #   material_change  - 实时推送（单物料权威）
    #   material_info    - material-info(id) 回退（单物料完整状态权威，含 locations）
    SOURCE_STOCK = "stock_material"
    SOURCE_ORDER = "order_materials"
    SOURCE_PUSH = "material_change"
    SOURCE_MATERIAL_INFO = "material_info"

    # source -> 对应的 last_seen_* 簿记字段。
    _SOURCE_LAST_SEEN_FIELD = {
        SOURCE_STOCK: "last_seen_stock_at",
        SOURCE_ORDER: "last_seen_order_at",
        SOURCE_PUSH: "last_seen_push_at",
        SOURCE_MATERIAL_INFO: "last_seen_material_info_at",
        # 旧别名兼容
        "stock-material": "last_seen_stock_at",
    }

    @staticmethod
    def _normalize_source(source: Optional[str]) -> str:
        """把旧别名归一化为标准 source 枚举值。"""
        if source in ("stock-material", "stock_material"):
            return BioyondResourceSynchronizer.SOURCE_STOCK
        if source in ("order_materials", "order-materials", "materials_by_order_id"):
            return BioyondResourceSynchronizer.SOURCE_ORDER
        if source in ("material_change", "material-change", "push"):
            return BioyondResourceSynchronizer.SOURCE_PUSH
        if source in ("material_info", "material-info"):
            return BioyondResourceSynchronizer.SOURCE_MATERIAL_INFO
        return str(source or BioyondResourceSynchronizer.SOURCE_PUSH)

    @staticmethod
    def normalize_material_aliases(material: Dict[str, Any]) -> Dict[str, Any]:
        """归一化 Bioyond 字段别名（materialId→id 等），不丢弃原 key。

        仅在缺失目标 key 时填充，因此不会覆盖更权威的现有值；不触碰 locations
        （保留"未知 vs 权威空"差异）。返回浅拷贝。
        """
        if not isinstance(material, dict):
            return material
        normalized = dict(material)
        alias_plan = {
            "id": ("materialId", "material_id", "bioyond_id"),
            "name": ("materialName", "material_name"),
            "code": ("materialCode", "barCode", "material_code"),
            "typeName": ("materialTypeName",),
            "typeId": ("materialTypeId", "typeUUID"),
        }
        for target, aliases in alias_plan.items():
            if BioyondResourceSynchronizer._has_material_value(normalized, target):
                continue
            for alias in aliases:
                if BioyondResourceSynchronizer._has_material_value(normalized, alias):
                    normalized[target] = normalized[alias]
                    break
        return normalized

    def _update_material_cache_from_stock(
        self,
        materials: List[Dict[str, Any]],
        source: str = "stock_material",
        source_scope: Optional[str] = None,
    ) -> Dict[str, int]:
        """同步旧 name->id 缓存和新的 Bioyond id->record 物料缓存（源感知）。"""
        material_cache = getattr(self.bioyond_api_client, "material_cache", None)
        if isinstance(material_cache, dict):
            before_count = len(material_cache)
            for material in materials:
                material_name = self._material_display_name(material)
                material_id = self._material_bioyond_id(material)
                if material_name and material_id:
                    material_cache[material_name] = material_id

                for detail_material in material.get("detail", []) or []:
                    detail_name = self._material_display_name(detail_material)
                    detail_id = detail_material.get("detailMaterialId") or self._material_bioyond_id(detail_material)
                    if detail_name and detail_id:
                        material_cache[detail_name] = detail_id
            logger.debug(
                f"已用Bioyond库存同步旧物料名称缓存: {before_count} -> {len(material_cache)}"
            )

        return self._upsert_bioyond_material_cache(
            materials, source=source, source_scope=source_scope
        )

    def _bioyond_material_cache(self) -> Dict[str, Dict[str, Any]]:
        cache = getattr(self.bioyond_api_client, "bioyond_material_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            try:
                setattr(self.bioyond_api_client, "bioyond_material_cache", cache)
            except Exception:
                pass
        return cache

    @staticmethod
    def _clean_material_text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _material_bioyond_id(cls, material: Dict[str, Any]) -> str:
        return cls._clean_material_text(
            material.get("id")
            or material.get("materialId")
            or material.get("material_id")
            or material.get("bioyond_id")
        )

    @classmethod
    def _material_display_name(cls, material: Dict[str, Any]) -> str:
        return cls._clean_material_text(
            material.get("name")
            or material.get("materialName")
            or material.get("material_name")
        )

    @classmethod
    def _material_code(cls, material: Dict[str, Any]) -> str:
        return cls._clean_material_text(
            material.get("code")
            or material.get("materialCode")
            or material.get("material_code")
            or material.get("barCode")
        )

    @classmethod
    def _material_type_code(cls, material: Dict[str, Any]) -> str:
        code = cls._material_code(material)
        return code.split("-", 1)[0].strip() if code else ""

    def _material_type_code_mapping(self) -> Dict[str, Dict[str, str]]:
        mapping: Dict[str, Dict[str, str]] = {}
        try:
            for resource_id, value in (self.workstation.bioyond_config.get("material_type_mappings") or {}).items():
                display_name = value[0] if isinstance(value, (tuple, list)) and len(value) >= 1 else ""
                type_id = value[1] if isinstance(value, (tuple, list)) and len(value) >= 2 else ""
                try:
                    from unilabos.registry.registry import lab_registry

                    resource_config = lab_registry.resource_type_registry.get(resource_id, {})
                    class_config = resource_config.get("class")
                    resource_cls = class_config if isinstance(class_config, type) else None
                    if resource_cls is None and isinstance(class_config, dict) and "module" in class_config:
                        module_name, class_name = class_config["module"].split(":", 1)
                        module = __import__(module_name, fromlist=[class_name])
                        resource_cls = getattr(module, class_name, None)
                except Exception:
                    resource_cls = None

                type_code = getattr(resource_cls, "bioyond_material_type_code", "") if resource_cls else ""
                resolved_name = getattr(resource_cls, "bioyond_material_type_name", "") if resource_cls else ""
                if not type_code:
                    continue
                mapping[str(type_code)] = {
                    "resource_id": str(resource_id),
                    "type_id": str(type_id or ""),
                    "resolved_type_name": str(resolved_name or display_name or ""),
                }
        except Exception as exc:
            logger.debug(f"[Bioyond物料缓存] 构建类型编码映射失败: {exc}")
        return mapping

    def _normalize_material_record(
        self,
        material: Dict[str, Any],
        source: str,
        source_scope: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        material_id = self._material_bioyond_id(material)
        if not material_id:
            return None
        now = datetime.now().isoformat()
        norm_source = self._normalize_source(source)
        type_code = self._material_type_code(material)
        type_info = self._material_type_code_mapping().get(type_code, {})
        cache = self._bioyond_material_cache()
        previous = cache.get(material_id, {})
        if not isinstance(previous, dict):
            previous = {}
        first_seen_at = previous.get("first_seen_at") or now
        # 保留"未知库位 vs 权威空库位"差异：缺失 locations key 时回退到缓存中已知值，
        # 而非武断当作 []。显式提供（含 []）才覆盖。
        if "locations" in material:
            locations = copy.deepcopy(material.get("locations") or [])
        else:
            locations = copy.deepcopy(previous.get("locations") or previous.get("bioyond_locations") or [])
        code = self._material_code(material)
        raw_name = self._material_display_name(material)
        raw_type_name = self._clean_material_text(
            material.get("typeName") or material.get("materialTypeName")
        )
        warnings = list(material.get("_bioyond_material_warnings") or [])
        regular_container_fallback = bool(material.get("_bioyond_regular_container_fallback"))
        resolved_type_name = type_info.get("resolved_type_name") or raw_type_name
        if regular_container_fallback:
            resolved_type_name = "RegularContainer"

        # 簿记：保留各 source 的 last_seen_*，并记录 order_ids_seen。
        order_ids_seen = list(previous.get("order_ids_seen") or [])
        scope = self._clean_material_text(source_scope)
        if norm_source == self.SOURCE_ORDER and scope and scope not in order_ids_seen:
            order_ids_seen.append(scope)
        last_seen = {
            key: previous.get(key)
            for key in (
                "last_seen_stock_at",
                "last_seen_order_at",
                "last_seen_push_at",
                "last_seen_material_info_at",
            )
        }
        last_seen_field = self._SOURCE_LAST_SEEN_FIELD.get(norm_source)
        if last_seen_field:
            last_seen[last_seen_field] = now

        record = {
            "bioyond_id": material_id,
            "bioyond_name": raw_name,
            "bioyond_typeName": resolved_type_name,
            "bioyond_locations": locations,
            "bioyond_details": copy.deepcopy(material.get("detail") or material.get("details") or []),
            "bioyond_combinedmaterialId": material.get("combinedMaterialId"),
            "raw_name": raw_name,
            "display_name": raw_name,
            "code": code,
            "material_code": code,
            "type_code": type_code,
            "type_id": self._clean_material_text(
                material.get("typeId") or material.get("materialTypeId") or material.get("typeUUID")
            ),
            "raw_type_name": raw_type_name,
            "resolved_type_name": resolved_type_name,
            "resolved_resource_id": type_info.get("resource_id", ""),
            "locations": locations,
            "details": copy.deepcopy(material.get("detail") or material.get("details") or []),
            "combinedMaterialId": material.get("combinedMaterialId"),
            "parameters": copy.deepcopy(material.get("parameters") or material.get("materialParams")),
            "status": material.get("status"),
            "isUse": material.get("isUse"),
            "quantity": material.get("quantity"),
            "lockQuantity": material.get("lockQuantity"),
            "unit": material.get("unit"),
            "source": norm_source,
            "source_scope": scope or None,
            "order_ids_seen": order_ids_seen,
            "first_seen_at": first_seen_at,
            "updated_at": now,
            "raw_payload": copy.deepcopy(material),
            "regular_container_fallback": regular_container_fallback,
            "fallback_reason": material.get("_bioyond_regular_container_reason") or "",
            "warnings": warnings,
            "unresolved": False,
        }
        record.update(last_seen)
        return record

    def _upsert_bioyond_material_cache(
        self,
        materials: List[Dict[str, Any]],
        source: str,
        source_scope: Optional[str] = None,
    ) -> Dict[str, int]:
        cache = self._bioyond_material_cache()
        updated = 0
        for material in materials:
            if not isinstance(material, dict):
                continue
            record = self._normalize_material_record(material, source, source_scope=source_scope)
            if not record:
                continue
            cache[record["bioyond_id"]] = record
            updated += 1
        logger.debug(f"已更新Bioyond物料记录缓存: records={len(cache)} updated={updated}")
        return {"records_count": len(cache), "updated_count": updated}

    def _prune_bioyond_material_cache(self, keep_ids: set[str]) -> int:
        cache = self._bioyond_material_cache()
        removed = 0
        for material_id in list(cache):
            if material_id not in keep_ids:
                del cache[material_id]
                removed += 1
        if removed:
            logger.info(f"[Bioyond物料缓存] clear_stale 清理缓存记录 {removed} 条")
        return removed

    def _material_needs_info_fallback(self, material: Dict[str, Any]) -> bool:
        material_id = self._material_bioyond_id(material)
        if not material_id:
            return False
        cache = self._bioyond_material_cache()
        raw_type_name = self._clean_material_text(
            material.get("typeName") or material.get("materialTypeName")
        )
        if not self._material_code(material):
            return True
        type_code = self._material_type_code(material)
        type_mapping = self._material_type_code_mapping()
        if type_code and type_mapping and type_code not in type_mapping:
            return True
        if material_id not in cache and not raw_type_name:
            return True
        return False

    @staticmethod
    def _has_material_value(material: Dict[str, Any], key: str) -> bool:
        value = material.get(key)
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return True

    def _merge_material_payload_from_cache(self, material: Dict[str, Any]) -> Dict[str, Any]:
        """用 id 缓存补齐类型/编码元数据，但不复用旧库位。"""
        material_id = self._material_bioyond_id(material)
        if not material_id:
            return material
        cache_record = self._bioyond_material_cache().get(material_id)
        if not isinstance(cache_record, dict):
            return material

        merged = copy.deepcopy(material)
        raw_payload = cache_record.get("raw_payload")
        raw_payload = raw_payload if isinstance(raw_payload, dict) else {}

        fill_plan = {
            "id": ("bioyond_id", "id", "materialId"),
            "name": ("raw_name", "display_name", "bioyond_name", "name", "materialName"),
            "code": ("code", "material_code", "materialCode", "barCode"),
            "typeName": ("resolved_type_name", "bioyond_typeName", "raw_type_name", "typeName", "materialTypeName"),
            "materialTypeName": ("resolved_type_name", "bioyond_typeName", "raw_type_name", "materialTypeName", "typeName"),
            "typeId": ("type_id", "typeId", "materialTypeId", "typeUUID"),
            "materialTypeId": ("type_id", "materialTypeId", "typeId", "typeUUID"),
            "unit": ("unit",),
            "quantity": ("quantity",),
            "parameters": ("parameters", "materialParams"),
        }
        for target_key, source_keys in fill_plan.items():
            if self._has_material_value(merged, target_key):
                continue
            for source in (cache_record, raw_payload):
                for source_key in source_keys:
                    if self._has_material_value(source, source_key):
                        merged[target_key] = copy.deepcopy(source[source_key])
                        break
                if self._has_material_value(merged, target_key):
                    break

        if not self._has_material_value(merged, "id"):
            merged["id"] = material_id
        return merged

    def _fetch_material_info_for_cache(self, material_id: str) -> Dict[str, Any]:
        material_info = getattr(self.bioyond_api_client, "material_info", None)
        if not callable(material_info):
            return {}
        try:
            info = material_info(material_id) or {}
        except Exception as exc:
            logger.warning(f"[Bioyond物料缓存] material-info 查询失败: id={material_id} error={exc}")
            return {}
        return info if isinstance(info, dict) else {}

    def material_info_result(self, material_id: str) -> Dict[str, Any]:
        """material-info(id) 的非折叠结果封装。

        返回 {"ok": bool, "data": dict, "message": str, "code": int|None}。
        优先调用 RPC 的 material_info_result（区分 API 失败 vs 真·空），缺失时回退到
        旧 material_info（仅能返回 data）。API 失败时 ok=False，调用方据此短路、不改 deck。
        """
        client = self.bioyond_api_client
        result_fn = getattr(client, "material_info_result", None)
        if callable(result_fn):
            try:
                result = result_fn(material_id) or {}
            except Exception as exc:
                return {"ok": False, "data": {}, "message": str(exc), "code": None}
            if isinstance(result, dict) and "ok" in result:
                data = result.get("data")
                result["data"] = data if isinstance(data, dict) else {}
                return result
        info = self._fetch_material_info_for_cache(material_id)
        return {"ok": bool(info), "data": info, "message": "", "code": 1 if info else None}

    def _resolve_material_payloads(
        self,
        materials: List[Dict[str, Any]],
        source: str,
    ) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        for material in materials:
            if not isinstance(material, dict):
                continue
            payload = copy.deepcopy(material)
            payload = self._merge_material_payload_from_cache(payload)
            material_id = self._material_bioyond_id(payload)
            if self._material_needs_info_fallback(payload):
                info = self._fetch_material_info_for_cache(material_id)
                if info:
                    merged = copy.deepcopy(payload)
                    merged.update(info)
                    if "id" not in merged and material_id:
                        merged["id"] = material_id
                    payload = merged
                    logger.info(
                        f"[Bioyond物料缓存] 使用 material-info 覆盖{source}物料: id={material_id}"
                    )
            type_code = self._material_type_code(payload)
            type_mapping = self._material_type_code_mapping()
            warnings = list(payload.get("_bioyond_material_warnings") or [])
            if not type_code:
                warning = "missing_material_code_fallback_regular_container"
                if warning not in warnings:
                    warnings.append(warning)
                payload["_bioyond_regular_container_fallback"] = True
                payload["_bioyond_regular_container_reason"] = "missing_material_code"
                payload["_bioyond_material_warnings"] = warnings
                logger.warning(
                    f"[Bioyond物料缓存] 物料缺少 code/materialCode，使用 RegularContainer: "
                    f"id={material_id} name={self._material_display_name(payload)!r}"
                )
            elif type_code not in type_mapping:
                warning = f"unmapped_material_type_code_fallback_regular_container:{type_code}"
                if warning not in warnings:
                    warnings.append(warning)
                payload["_bioyond_regular_container_fallback"] = True
                payload["_bioyond_regular_container_reason"] = f"unmapped_material_type_code:{type_code}"
                payload["_bioyond_material_warnings"] = warnings
                logger.warning(
                    f"[Bioyond物料缓存] 物料类型编码未定义，使用 RegularContainer: "
                    f"id={material_id} code={self._material_code(payload)!r} name={self._material_display_name(payload)!r}"
                )
            resolved.append(payload)
        return resolved

    def sync_to_external(self, resource: Any) -> bool:
        """将本地物料数据变更同步到Bioyond系统"""
        try:
            # ✅ 跳过仓库类型的资源 - 仓库是容器，不是物料
            resource_category = getattr(resource, "category", None)
            if resource_category == "warehouse":
                logger.debug(f"[同步→Bioyond] 跳过仓库类型资源: {resource.name} (仓库是容器，不需要同步为物料)")
                return True

            logger.info(f"[同步→Bioyond] 收到物料变更: {resource.name}")

            # 获取物料的 Bioyond ID
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")

            # 🔥 查询所有物料，用于获取物料当前位置等信息
            existing_materials = []
            try:
                import json
                logger.info(f"[同步→Bioyond] 查询 Bioyond 系统中的所有物料...")
                all_materials = []

                for type_mode in [0, 1, 2]:  # 0=耗材, 1=样品, 2=试剂
                    query_params = json.dumps({
                        "typeMode": type_mode,
                        "filter": "",
                        "includeDetail": True
                    })
                    materials = self.bioyond_api_client.stock_material(query_params)
                    if materials:
                        all_materials.extend(materials)

                existing_materials = all_materials
                logger.info(f"[同步→Bioyond] 查询到 {len(all_materials)} 个物料")
            except Exception as e:
                logger.error(f"查询 Bioyond 物料失败: {e}")
                return False

            # ⭐ 如果没有 Bioyond ID，尝试从查询结果中按名称匹配
            if not material_bioyond_id:
                logger.warning(f"[同步→Bioyond] 物料 {resource.name} 没有 Bioyond ID，尝试按名称查询...")
                for mat in existing_materials:
                    if mat.get("name") == resource.name:
                        material_bioyond_id = mat.get("id")
                        mat_type = mat.get("typeName", "未知")
                        logger.info(f"✅ 找到物料 {resource.name} ({mat_type}) 的 Bioyond ID: {material_bioyond_id[:8]}...")
                        # 保存 ID 到资源对象
                        extra_info["material_bioyond_id"] = material_bioyond_id
                        setattr(resource, "unilabos_extra", extra_info)
                        break

                if not material_bioyond_id:
                    logger.warning(f"⚠️ 在 Bioyond 系统中未找到名为 {resource.name} 的物料")
                    logger.info(f"[同步→Bioyond] 这是一个新物料，将创建并入库到 Bioyond 系统")

            # 检查是否有位置更新请求
            update_site = extra_info.get("update_resource_site")

            if not update_site:
                logger.debug(f"[同步→Bioyond] 物料 {resource.name} 无位置更新请求，跳过同步")
                return True

            # ===== 物料移动/创建流程 =====
            logger.info(f"[同步→Bioyond] 📍 物料 {resource.name} 目标库位: {update_site}")

            if material_bioyond_id:
                logger.info(f"[同步→Bioyond] 🔄 物料已存在于 Bioyond (ID: {material_bioyond_id[:8]}...)，执行移动操作")
            else:
                logger.info(f"[同步→Bioyond] ➕ 物料不存在于 Bioyond，将创建新物料并入库")

            # 第1步：从配置中获取仓库配置
            warehouse_mapping = self.bioyond_config.get("warehouse_mapping", {})

            # 确定目标仓库名称
            parent_name = None
            target_location_uuid = None
            current_warehouse = None

            # 🔥 优先级1: 从 Bioyond 查询结果中获取物料当前所在的仓库
            if material_bioyond_id:
                for mat in existing_materials:
                    if mat.get("name") == resource.name or mat.get("id") == material_bioyond_id:
                        locations = mat.get("locations", [])
                        if locations and len(locations) > 0:
                            current_warehouse = locations[0].get("whName")
                            logger.info(f"[同步→Bioyond] 💡 物料当前位于 Bioyond 仓库: {current_warehouse}")
                        break

                # 优先在当前仓库中查找目标库位
                if current_warehouse and current_warehouse in warehouse_mapping:
                    site_uuids = warehouse_mapping[current_warehouse].get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = current_warehouse
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[同步→Bioyond] ✅ 在当前仓库找到目标库位: {parent_name}/{update_site}")
                        logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")
                    else:
                        logger.warning(f"⚠️ [同步→Bioyond] 当前仓库 {current_warehouse} 中没有库位 {update_site}，将搜索其他仓库")

            # 🔥 优先级2: 检查 PLR 父节点名称
            if not parent_name or not target_location_uuid:
                if resource.parent is not None:
                    parent_name_candidate = resource.parent.name
                    logger.info(f"[同步→Bioyond] 从 PLR 父节点获取仓库名称: {parent_name_candidate}")

                    if parent_name_candidate in warehouse_mapping:
                        site_uuids = warehouse_mapping[parent_name_candidate].get("site_uuids", {})
                        if update_site in site_uuids:
                            parent_name = parent_name_candidate
                            target_location_uuid = site_uuids[update_site]
                            logger.info(f"[同步→Bioyond] ✅ 在父节点仓库找到目标库位: {parent_name}/{update_site}")
                            logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")

            # 🔥 优先级3: 遍历所有仓库查找（兜底方案）
            if not parent_name or not target_location_uuid:
                logger.info(f"[同步→Bioyond] 从所有仓库中查找库位 {update_site}...")
                for warehouse_name, warehouse_info in warehouse_mapping.items():
                    site_uuids = warehouse_info.get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = warehouse_name
                        target_location_uuid = site_uuids[update_site]
                        logger.warning(f"[同步→Bioyond] ⚠️ 在其他仓库找到目标库位: {parent_name}/{update_site}")
                        logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")
                        break

            if not parent_name or not target_location_uuid:
                logger.error(f"❌ [同步→Bioyond] 库位 {update_site} 没有在 WAREHOUSE_MAPPING 中配置")
                logger.debug(f"[同步→Bioyond] 可用仓库: {list(warehouse_mapping.keys())}")
                return False

            # 第2步：转换为 Bioyond 格式
            logger.info(f"[同步→Bioyond] 🔄 转换物料为 Bioyond 格式...")

            # 从配置中获取物料默认参数
            material_default_params = self.workstation.bioyond_config.get("material_default_parameters", {})
            material_type_params = self.workstation.bioyond_config.get("material_type_parameters", {})

            # 合并参数配置：物料名称参数 + typeId参数（转换为 type:<uuid> 格式）
            merged_params = material_default_params.copy()
            for type_id, params in material_type_params.items():
                merged_params[f"type:{type_id}"] = params

            bioyond_material = resource_plr_to_bioyond(
                [resource],
                type_mapping=self.workstation.bioyond_config["material_type_mappings"],
                warehouse_mapping=self.workstation.bioyond_config["warehouse_mapping"],
                material_params=merged_params
            )[0]

            logger.info(f"[同步→Bioyond] 🔧 准备覆盖locations字段，目标仓库: {parent_name}, 库位: {update_site}, UUID: {target_location_uuid[:8]}...")

            # 🔥 强制覆盖 locations 信息，使用正确的目标库位 UUID
            # resource_plr_to_bioyond 可能会生成错误的仓库信息，这里直接覆盖
            bioyond_material["locations"] = [{
                "id": target_location_uuid,
                "whid": "",
                "whName": parent_name,
                "x": ord(update_site[0]) - ord('A') + 1,  # A→1, B→2, ...
                "y": int(update_site[1:]),  # 01→1, 02→2, ...
                "z": 1,
                "quantity": 0
            }]
            logger.info(f"[同步→Bioyond] ✅ 已覆盖库位信息: {parent_name}/{update_site} (UUID: {target_location_uuid[:8]}...)")

            logger.debug(f"[同步→Bioyond] Bioyond 物料数据: {bioyond_material}")

            location_info = bioyond_material.get("locations")
            logger.debug(f"[同步→Bioyond] 库位信息: {location_info}, 类型: {type(location_info)}")

            # 第3步：根据是否已有 Bioyond ID 决定创建还是使用现有物料
            if material_bioyond_id:
                # 物料已存在,直接使用现有 ID
                material_id = material_bioyond_id
                logger.info(f"✅ [同步→Bioyond] 使用已有物料 ID: {material_id[:8]}...")
            else:
                # 物料不存在,调用 API 创建新物料
                logger.info(f"[同步→Bioyond] 📤 调用 Bioyond API 添加物料...")
                material_id = self.bioyond_api_client.add_material(bioyond_material)

                if not material_id:
                    logger.error(f"❌ [同步→Bioyond] 添加物料失败，API 返回空")
                    return False

                logger.info(f"✅ [同步→Bioyond] 物料添加成功，Bioyond ID: {material_id[:8]}...")

                # 保存新创建的物料 ID 到资源对象
                extra_info["material_bioyond_id"] = material_id
                setattr(resource, "unilabos_extra", extra_info)

            # 第4步：物料入库前先检查目标库位是否被占用
            if location_info:
                logger.info(f"[同步→Bioyond] 📥 准备入库到库位 {update_site}...")

                # 处理不同的 location_info 数据结构
                if isinstance(location_info, list) and len(location_info) > 0:
                    location_id = location_info[0]["id"]
                elif isinstance(location_info, dict):
                    location_id = location_info["id"]
                else:
                    logger.warning(f"⚠️ [同步→Bioyond] 无效的库位信息格式: {location_info}")
                    location_id = None

                if location_id:
                    # 查询目标库位是否已有物料
                    logger.info(f"[同步→Bioyond] 🔍 检查库位 {update_site} (UUID: {location_id[:8]}...) 是否被占用...")

                    # 查询所有物料，检查是否有物料在目标库位
                    try:
                        all_materials_type1 = self.bioyond_api_client.stock_material('{"typeMode": 1, "includeDetail": true}')
                        all_materials_type2 = self.bioyond_api_client.stock_material('{"typeMode": 2, "includeDetail": true}')
                        all_materials = (all_materials_type1 or []) + (all_materials_type2 or [])

                        # 检查是否有物料已经在目标库位
                        location_occupied = False
                        occupying_material = None

                        # 同时检查当前物料是否在其他位置（需要先出库）
                        current_material_location = None
                        current_location_uuid = None

                        for material in all_materials:
                            locations = material.get("locations", [])

                            # 检查目标库位占用情况
                            for loc in locations:
                                if loc.get("id") == location_id:
                                    location_occupied = True
                                    occupying_material = material
                                    logger.warning(f"⚠️ [同步→Bioyond] 库位 {update_site} 已被占用！")
                                    logger.warning(f"   占用物料: {material.get('name')} (ID: {material.get('id', '')[:8]}...)")
                                    logger.warning(f"   占用位置: code={loc.get('code')}, x={loc.get('x')}, y={loc.get('y')}")
                                    logger.warning(f"   🔍 详细信息: location_id={loc.get('id')[:8]}..., 目标UUID={location_id[:8]}...")
                                    logger.warning(f"   🔍 完整location数据: {loc}")
                                    break

                            # 检查当前物料是否在其他位置
                            if material.get("id") == material_id and locations:
                                current_material_location = locations[0]
                                current_location_uuid = current_material_location.get("id")
                                logger.info(f"📍 [同步→Bioyond] 物料当前位置: {current_material_location.get('whName')}/{current_material_location.get('code')} (UUID: {current_location_uuid[:8]}...)")

                            if location_occupied:
                                break

                        if location_occupied:
                            # 如果是同一个物料（ID相同），说明已经在目标位置了，跳过
                            if occupying_material and occupying_material.get("id") == material_id:
                                logger.info(f"✅ [同步→Bioyond] 物料 {resource.name} 已经在库位 {update_site}，跳过重复入库")
                                return True
                            else:
                                logger.error(f"❌ [同步→Bioyond] 库位 {update_site} 已被其他物料占用，拒绝入库")
                                return False

                        logger.info(f"✅ [同步→Bioyond] 库位 {update_site} 可用，准备入库...")

                    except Exception as e:
                        logger.warning(f"⚠️ [同步→Bioyond] 检查库位状态时发生异常: {e}，继续尝试入库...")

                    # 🔧 如果物料当前在其他位置，先出库再入库
                    if current_location_uuid and current_location_uuid != location_id:
                        logger.info(f"[同步→Bioyond] 🚚 物料需要移动，先从当前位置出库...")
                        logger.info(f"   当前位置 UUID: {current_location_uuid[:8]}...")
                        logger.info(f"   目标位置 UUID: {location_id[:8]}...")

                        try:
                            # 获取物料数量用于出库
                            material_quantity = current_material_location.get("totalNumber", 1)
                            logger.info(f"   出库数量: {material_quantity}")

                            # 调用出库 API
                            outbound_response = self.bioyond_api_client.material_outbound_by_id(
                                material_id,
                                current_location_uuid,
                                material_quantity
                            )
                            logger.info(f"✅ [同步→Bioyond] 物料从 {current_material_location.get('code')} 出库成功")
                        except Exception as e:
                            logger.error(f"❌ [同步→Bioyond] 物料出库失败: {e}")
                            return False

                    # 执行入库
                    logger.info(f"[同步→Bioyond] 📥 调用 Bioyond API 物料入库...")
                    response = self.bioyond_api_client.material_inbound(material_id, location_id)

                    # 注意：Bioyond API 成功时返回空字典 {}，所以不能用 if not response 判断
                    # 只要没有抛出异常，就认为成功（response 是 dict 类型，即使是 {} 也不是 None）
                    if response is not None:
                        logger.info(f"✅ [同步→Bioyond] 物料 {resource.name} 成功入库到 {update_site}")

                        # 入库成功后，重新查询验证物料实际入库位置
                        logger.info(f"[同步→Bioyond] 🔍 验证物料实际入库位置...")
                        try:
                            all_materials_type1 = self.bioyond_api_client.stock_material('{"typeMode": 1, "includeDetail": true}')
                            all_materials_type2 = self.bioyond_api_client.stock_material('{"typeMode": 2, "includeDetail": true}')
                            all_materials = (all_materials_type1 or []) + (all_materials_type2 or [])

                            for material in all_materials:
                                if material.get("id") == material_id:
                                    locations = material.get("locations", [])
                                    if locations:
                                        actual_loc = locations[0]
                                        logger.info(f"📍 [同步→Bioyond] 物料实际位置: code={actual_loc.get('code')}, "
                                                  f"warehouse={actual_loc.get('whName')}, "
                                                  f"x={actual_loc.get('x')}, y={actual_loc.get('y')}")

                                        # 验证 UUID 是否匹配
                                        if actual_loc.get("id") != location_id:
                                            logger.error(f"❌ [同步→Bioyond] UUID 不匹配！")
                                            logger.error(f"   预期 UUID: {location_id}")
                                            logger.error(f"   实际 UUID: {actual_loc.get('id')}")
                                            logger.error(f"   这说明配置文件中的 UUID 映射有误，请检查 config.py 中的 WAREHOUSE_MAPPING")
                                    break
                        except Exception as e:
                            logger.warning(f"⚠️ [同步→Bioyond] 验证入库位置时发生异常: {e}")
                    else:
                        logger.error(f"❌ [同步→Bioyond] 物料入库失败")
                        return False
                else:
                    logger.warning(f"⚠️ [同步→Bioyond] 无法获取库位 ID，跳过入库操作")
            else:
                logger.warning(f"⚠️ [同步→Bioyond] 物料没有库位信息，跳过入库操作")
            return True

        except Exception as e:
            logger.error(f"❌ [同步→Bioyond] 同步物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def handle_external_change(self, change_info: Dict[str, Any]) -> bool:
        """处理Bioyond系统的变更通知"""
        try:
            # 这里可以实现对Bioyond变更的处理逻辑
            logger.info(f"处理Bioyond变更通知: {change_info}")

            return True
        except Exception as e:
            logger.error(f"处理Bioyond变更通知失败: {e}")
            return False

    def _create_material_only(self, resource: Any) -> Optional[str]:
        """只创建物料到 Bioyond 系统（不入库）

        Transfer 阶段使用：只调用 add_material API 创建物料记录

        Args:
            resource: 要创建的资源对象

        Returns:
            str: 创建成功返回 Bioyond 物料 ID，失败返回 None
        """
        try:
            # 跳过仓库类型的资源
            resource_category = getattr(resource, "category", None)
            if resource_category == "warehouse":
                logger.debug(f"[创建物料] 跳过仓库类型资源: {resource.name}")
                return None

            logger.info(f"[创建物料] 开始创建物料: {resource.name}")

            # 检查是否已经有 Bioyond ID
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")

            if material_bioyond_id:
                logger.info(f"[创建物料] 物料 {resource.name} 已存在 (ID: {material_bioyond_id[:8]}...)，跳过创建")
                return material_bioyond_id

            # 转换为 Bioyond 格式
            # 从配置中获取物料默认参数
            material_default_params = self.workstation.bioyond_config.get("material_default_parameters", {})
            material_type_params = self.workstation.bioyond_config.get("material_type_parameters", {})

            # 合并参数配置：物料名称参数 + typeId参数（转换为 type:<uuid> 格式）
            merged_params = material_default_params.copy()
            for type_id, params in material_type_params.items():
                merged_params[f"type:{type_id}"] = params

            bioyond_material = resource_plr_to_bioyond(
                [resource],
                type_mapping=self.workstation.bioyond_config["material_type_mappings"],
                warehouse_mapping=self.workstation.bioyond_config["warehouse_mapping"],
                material_params=merged_params
            )[0]

            # ⚠️ 关键：创建物料时不设置 locations，让 Bioyond 系统暂不分配库位
            # locations 字段在后续的入库操作中才会指定
            bioyond_material.pop("locations", None)

            logger.info(f"[创建物料] 调用 Bioyond API 创建物料（不指定库位）...")
            material_id = self.bioyond_api_client.add_material(bioyond_material)

            if not material_id:
                logger.error(f"[创建物料] 创建物料失败，API 返回空")
                return None

            logger.info(f"✅ [创建物料] 物料创建成功，ID: {material_id[:8]}...")

            # 保存 Bioyond ID 到资源对象
            extra_info["material_bioyond_id"] = material_id
            setattr(resource, "unilabos_extra", extra_info)

            return material_id

        except Exception as e:
            logger.error(f"❌ [创建物料] 创建物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _inbound_material_only(self, resource: Any, material_id: str) -> bool:
        """只执行物料入库操作（物料已存在于 Bioyond 系统）

        Add 阶段使用：调用 material_inbound API 将物料入库到指定库位

        Args:
            resource: 要入库的资源对象
            material_id: Bioyond 物料 ID

        Returns:
            bool: 入库成功返回 True，失败返回 False
        """
        try:
            logger.info(f"[物料入库] 开始入库物料: {resource.name} (ID: {material_id[:8]}...)")

            # 获取目标库位信息
            extra_info = getattr(resource, "unilabos_extra", {})
            update_site = extra_info.get("update_resource_site")

            if not update_site:
                logger.warning(f"[物料入库] 物料 {resource.name} 没有指定目标库位，跳过入库")
                return True

            logger.info(f"[物料入库] 目标库位: {update_site}")

            # 获取仓库配置和目标库位 UUID
            warehouse_mapping = self.workstation.bioyond_config.get("warehouse_mapping", {})

            parent_name = None
            target_location_uuid = None

            # 查找目标库位的 UUID
            if resource.parent is not None:
                parent_name_candidate = resource.parent.name
                if parent_name_candidate in warehouse_mapping:
                    site_uuids = warehouse_mapping[parent_name_candidate].get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = parent_name_candidate
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[物料入库] 从父节点找到库位: {parent_name}/{update_site}")

            # 兜底：遍历所有仓库查找
            if not target_location_uuid:
                for warehouse_name, warehouse_info in warehouse_mapping.items():
                    site_uuids = warehouse_info.get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = warehouse_name
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[物料入库] 从所有仓库找到库位: {parent_name}/{update_site}")
                        break

            if not target_location_uuid:
                logger.error(f"❌ [物料入库] 库位 {update_site} 未在配置中找到")
                return False

            logger.info(f"[物料入库] 库位 UUID: {target_location_uuid[:8]}...")

            # 调用入库 API
            logger.info(f"[物料入库] 调用 Bioyond API 执行入库...")
            response = self.bioyond_api_client.material_inbound(material_id, target_location_uuid)

            if response:  # 空字典 {} 表示失败，非空字典表示成功
                logger.info(f"✅ [物料入库] 物料 {resource.name} 成功入库到 {update_site}")
                return True
            else:
                logger.error(f"❌ [物料入库] 物料入库失败，API返回空响应或失败")
                return False

        except Exception as e:
            logger.error(f"❌ [物料入库] 入库物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False


class BioyondWorkstation(WorkstationBase):
    """Bioyond工作站

    集成Bioyond物料管理的工作站实现
    """

    # 子类（如 sirna / peptide）覆写以指定默认 raw-call 日志目录。
    # 路径相对仓库根；为 None 时若 debug_log=True 仍会写入临时位置。
    _DEBUG_LOG_DEFAULT_DIR: Optional[str] = None

    def _create_bioyond_rpc(self, config: Dict[str, Any]) -> BioyondV1RPC:
        """创建 Bioyond RPC 客户端并应用调试包装。

        所有创建 ``BioyondV1RPC`` 的路径（饿汉初始化、Sirna 延迟初始化、
        以及未来的前端重新配置路径）都应通过该 helper，
        以确保 debug_log 包装与命名/日志策略保持一致。
        """
        rpc = BioyondV1RPC(config)
        debug_call_log.wrap_rpc_http(rpc)
        return rpc

    def _set_hardware_interface(self, rpc: BioyondV1RPC) -> BioyondV1RPC:
        """将已构造的 RPC 客户端设置到 ``self.hardware_interface``，并应用调试包装。"""
        debug_call_log.wrap_rpc_http(rpc)
        self.hardware_interface = rpc
        return rpc

    def _debug_log_resolved_dir(self) -> Path:
        """解析 ``debug_log_dir`` 为绝对路径。"""
        configured = (getattr(self, "bioyond_config", {}) or {}).get("debug_log_dir")
        default_dir = getattr(self, "_DEBUG_LOG_DEFAULT_DIR", None)
        candidate = configured or default_dir or "bioyond_debug_records"
        path = Path(candidate)
        if not path.is_absolute():
            # 本包是外部设备包，安装路径深度不固定（旧 monorepo 的 parents[4]
            # 在浅路径如 D:\LabDeviceBioyondPeptide 下会 IndexError）。相对
            # debug_log_dir 一律以 unilab 启动时的工作目录为基准。
            path = Path.cwd() / path
        return path

    def _ensure_debug_log_state(self) -> None:
        """从 ``self.bioyond_config`` 派生 ``_debug_log_enabled`` / ``_debug_log_dir``。

        每次进入 ``_debug_call_session`` 时都重新解析，以兼容前端在运行时
        修改 ``bioyond_config['debug_log']`` 或目录的场景；同时也容忍
        子类（如 Sirna 延迟初始化）在 ``__init__`` 早期未触发本方法。
        """
        cfg = getattr(self, "bioyond_config", {}) or {}
        self._debug_log_enabled = bool(cfg.get("debug_log"))
        self._debug_log_dir = self._debug_log_resolved_dir()

    @contextmanager
    def _debug_call_session(self, action_name: str):
        """在 action 体外加一层 debug 会话上下文。

        - ``debug_log`` 关闭时是空上下文，开销为 0。
        - ``debug_log`` 开启时进入 :func:`debug_call_log.session`，所有
          已被 ``wrap_rpc_http`` 包装过的 RPC 客户端都会捕获本次 action
          产生的 HTTP 调用并写入 Markdown 文件。

        子类（如 ``end_experiment``、``manual_unload`` 等）可以直接在
        action 体里以 ``with self._debug_call_session("action_name"):`` 包裹。
        """
        cfg = getattr(self, "bioyond_config", {}) or {}
        enabled = bool(cfg.get("debug_log"))
        if not enabled:
            yield None
            return
        out_dir = BioyondWorkstation._debug_log_resolved_dir(self)
        with debug_call_log.session(action_name, out_dir) as ctx:
            yield ctx

    def _publish_task_status(
        self,
        task_id: str,
        task_type: str,
        status: str,
        result: dict = None,
        progress: float = 0.0,
        task_code: str = None
    ):
        """发布任务状态事件"""
        try:
            if not getattr(self, "_ros_node", None):
                return

            event_data = {
                "task_id": task_id,
                "task_code": task_code,
                "task_type": task_type,
                "status": status,
                "progress": progress,
                "timestamp": datetime.now().isoformat()
            }
            if result:
                event_data["result"] = result

            topic = f"{self._ros_node.namespace}/events/task_status"

            if not hasattr(self, "_task_status_pub"):
                self._task_status_pub = self._ros_node.create_publisher(
                    String, topic, 10
                )

            self._task_status_pub.publish(
                convert_to_ros_msg(String, json.dumps(event_data, ensure_ascii=False))
            )
        except Exception as e:
            logger.error(f"发布任务状态事件失败: {e}")

    def __init__(
        self,
        bioyond_config: Optional[Dict[str, Any]] = None,
        deck: Optional[Any] = None,
        *args,
        **kwargs,
    ):
        # 初始化父类
        super().__init__(
            # 桌子
            deck=deck,
            *args,
            **kwargs,
        )

        # 检查 deck 是否为 None，防止 AttributeError
        if self.deck is None:
            logger.error("❌ Deck 配置为空，请检查配置文件中的 deck 参数")
            raise ValueError("Deck 配置不能为空，请在配置文件中添加正确的 deck 配置")

        # 初始化 warehouses 属性
        if not hasattr(self.deck, "warehouses") or self.deck.warehouses is None:
            self.deck.warehouses = {}

        # 仅当 warehouses 为空时尝试重新扫描（避免覆盖子类的修复）
        if not self.deck.warehouses:
            for resource in self.deck.children:
                # 兼容性增强: 只要是仓库类别或者是 WareHouse 实例均可
                is_warehouse = isinstance(resource, WareHouse) or getattr(resource, "category", "") == "warehouse"

                # 如果配置中有定义，也可以认定为 warehouse
                if not is_warehouse and "warehouse_mapping" in bioyond_config:
                    if resource.name in bioyond_config["warehouse_mapping"]:
                        is_warehouse = True

                if is_warehouse:
                    self.deck.warehouses[resource.name] = resource
                    # 确保 category 被正确设置，方便后续使用
                    if getattr(resource, "category", "") != "warehouse":
                        try:
                            resource.category = "warehouse"
                        except:
                            pass

        # 创建通信模块
        self._create_communication_module(bioyond_config)
        self.resource_synchronizer = BioyondResourceSynchronizer(self)
        if bioyond_config.get("sync_from_external_on_init", True):
            self.resource_synchronizer.sync_from_external(clear_stale=True)
        else:
            logger.info("跳过启动阶段 Bioyond 物料全量同步")

        # TODO: self._ros_node里面拿属性

        # 工作流加载
        self.is_running = False
        self.workflow_mappings = {}
        self.workflow_sequence = []
        self.pending_task_params = []

        if "workflow_mappings" in bioyond_config:
            self._set_workflow_mappings(bioyond_config["workflow_mappings"])

        # 准备 HTTP 报送接收服务配置（延迟到 post_init 启动）
        # 从 bioyond_config 中的 http_service_config 获取
        http_service_cfg = bioyond_config.get("http_service_config", {})
        self._http_service_config = {
            "host": http_service_cfg.get("http_service_host", "127.0.0.1"),
            "port": http_service_cfg.get("http_service_port", 8080)
        }
        self.http_service = None  # 将在 post_init 启动
        self.connection_monitor = None # 将在 post_init 启动

        logger.info(f"Bioyond工作站初始化完成")

    def __del__(self):
        """析构函数：清理资源，停止 HTTP 服务"""
        try:
            if hasattr(self, 'connection_monitor') and self.connection_monitor:
                self.connection_monitor.stop()
            if hasattr(self, 'http_service') and self.http_service is not None:
                logger.info("正在停止 HTTP 报送服务...")
                self.http_service.stop()
        except Exception as e:
            logger.error(f"停止 HTTP 服务时发生错误: {e}")

    def post_init(self, ros_node: ROS2WorkstationNode):
        self._ros_node = ros_node

        # 调试用连接监控会产生周期性的 ping/pong 心跳噪音，默认关闭。
        # try:
        #     self.connection_monitor = ConnectionMonitor(self)
        #     self.connection_monitor.start()
        # except Exception as e:
        #     logger.error(f"启动连接监控失败: {e}")

        # 启动 HTTP 报送接收服务（现在 device_id 已可用）
        # ⚠️ 检查子类是否已经自己管理 HTTP 服务
        if self.bioyond_config.get("_disable_auto_http_service"):
            logger.info("🔧 检测到 _disable_auto_http_service 标志，跳过自动启动 HTTP 服务")
            logger.info("   子类（BioyondCellWorkstation）已自行管理 HTTP 服务")
        elif hasattr(self, '_http_service_config'):
            try:
                self.http_service = WorkstationHTTPService(
                    workstation_instance=self,
                    host=self._http_service_config["host"],
                    port=self._http_service_config["port"]
                )
                self.http_service.start()
                logger.info(f"Bioyond工作站HTTP报送服务已启动: {self.http_service.service_url}")
            except Exception as e:
                logger.error(f"启动HTTP报送服务失败: {e}")
                import traceback
                traceback.print_exc()
                self.http_service = None

        # ⭐ 上传 deck（包括所有 warehouses 及其中的物料）
        # 注意：如果有从 Bioyond 同步的物料，它们已经被放置到 warehouse 中了
        # 所以只需要上传 deck，物料会作为 warehouse 的 children 一起上传
        logger.info("正在上传 deck（包括 warehouses 和物料）到云端...")
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })

        # 清理临时变量（物料已经在 deck 的 warehouse children 中，不需要单独上传）
        if hasattr(self, "_synced_resources"):
            logger.info(f"✅ {len(self._synced_resources)} 个从Bioyond同步的物料已包含在 deck 中")
            self._synced_resources = []

    def transfer_resource_to_another(self, resource: List[ResourceSlot], mount_resource: List[ResourceSlot], sites: List[str], mount_device_id: DeviceSlot):
        future = ROS2DeviceNode.run_async_func(self._ros_node.transfer_resource_to_another, True, **{
            "plr_resources": resource,
            "target_device_id": mount_device_id,
            "target_resources": mount_resource,
            "sites": sites,
        })
        return future

    def _create_communication_module(self, config: Optional[Dict[str, Any]] = None) -> None:
        """创建Bioyond通信模块"""
        # 直接使用传入的配置，不再使用默认值
        # 所有配置必须从 JSON 文件中提供
        if config:
            self.bioyond_config = config
        else:
            # 如果没有配置，使用空字典（会导致后续错误，但这是预期的）
            self.bioyond_config = {}
            print("警告: 未提供 bioyond_config，请确保在 JSON 配置文件中提供完整配置")

        self.hardware_interface = self._create_bioyond_rpc(self.bioyond_config)

    def resource_tree_add(self, resources: List[ResourcePLR]) -> None:
        """添加资源到资源树并更新ROS节点

        Args:
            resources (List[ResourcePLR]): 要添加的资源列表
        """
        logger.info(f"[resource_tree_add] 开始同步 {len(resources)} 个资源到 Bioyond 系统")
        for resource in resources:
            try:
                # 🔍 检查资源是否已有 Bioyond ID
                extra_info = getattr(resource, "unilabos_extra", {})
                material_bioyond_id = extra_info.get("material_bioyond_id")

                if material_bioyond_id:
                    # ⭐ 已有 Bioyond ID，说明 transfer 已经创建了物料
                    # 现在只需要执行入库操作
                    logger.info(f"✅ [resource_tree_add] 物料 {resource.name} 已有 Bioyond ID ({material_bioyond_id[:8]}...)，执行入库操作")
                    self.resource_synchronizer._inbound_material_only(resource, material_bioyond_id)
                else:
                    # ⚠️ 没有 Bioyond ID，说明是直接添加的物料（兜底逻辑）
                    # 需要先创建再入库
                    logger.info(f"⚠️ [resource_tree_add] 物料 {resource.name} 无 Bioyond ID，执行创建+入库操作")
                    self.resource_synchronizer.sync_to_external(resource)

            except Exception as e:
                logger.error(f"[resource_tree_add] 同步资源失败 {resource}: {e}")
                import traceback
                traceback.print_exc()

    def resource_tree_remove(self, resources: List[ResourcePLR]) -> None:
        """处理资源删除时的同步（出库操作）

        当 UniLab 前端删除物料时，需要将删除操作同步到 Bioyond 系统（出库）

        Args:
            resources: 要删除的资源列表
        """
        logger.info(f"[resource_tree_remove] 收到 {len(resources)} 个资源的移除请求（出库操作）")

        # ⭐ 关键优化：先找出所有的顶层容器（BottleCarrier），只对它们进行出库
        # 因为在 Bioyond 中，容器（如分装板 1105-12）是一个完整的物料
        # 里面的小瓶子是它的 detail 字段，不需要单独出库

        top_level_resources = []
        child_resource_names = set()

        # 第一步：识别所有子资源的名称
        for resource in resources:
            resource_category = getattr(resource, "category", None)
            if resource_category == "bottle_carrier":
                children = list(resource.children) if hasattr(resource, 'children') else []
                for child in children:
                    child_resource_names.add(child.name)

        # 第二步：筛选出顶层资源（不是任何容器的子资源）
        for resource in resources:
            resource_category = getattr(resource, "category", None)

            # 跳过仓库类型的资源
            if resource_category == "warehouse":
                logger.debug(f"[resource_tree_remove] 跳过仓库类型资源: {resource.name}")
                continue

            # 如果是容器，它就是顶层资源
            if resource_category == "bottle_carrier":
                top_level_resources.append(resource)
                logger.info(f"[resource_tree_remove] 识别到顶层容器资源: {resource.name}")
            # 如果不是任何容器的子资源，它也是顶层资源
            elif resource.name not in child_resource_names:
                top_level_resources.append(resource)
                logger.info(f"[resource_tree_remove] 识别到顶层独立资源: {resource.name}")
            else:
                logger.debug(f"[resource_tree_remove] 跳过子资源（将随容器一起出库）: {resource.name}")

        logger.info(f"[resource_tree_remove] 实际需要处理的顶层资源: {len(top_level_resources)} 个")

        # 第三步：对每个顶层资源执行出库操作
        for resource in top_level_resources:
            try:
                self._outbound_single_resource(resource)
            except Exception as e:
                logger.error(f"❌ [resource_tree_remove] 处理资源 {resource.name} 出库失败: {e}")
                import traceback
                traceback.print_exc()

        logger.info(f"[resource_tree_remove] 资源移除（出库）操作完成")

    def _outbound_single_resource(self, resource: ResourcePLR) -> bool:
        """对单个资源执行 Bioyond 出库操作

        Args:
            resource: 要出库的资源

        Returns:
            bool: 出库是否成功
        """
        try:
            logger.info(f"[resource_tree_remove] 🎯 开始处理资源出库: {resource.name}")

            # 获取资源的 Bioyond 信息
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")
            material_bioyond_name = extra_info.get("material_bioyond_name")  # ⭐ 原始 Bioyond 名称

            # ⭐ 优先使用保存的 Bioyond ID，避免重复查询
            if material_bioyond_id:
                logger.info(f"✅ [resource_tree_remove] 从资源中获取到 Bioyond ID: {material_bioyond_id[:8]}...")
                if material_bioyond_name and material_bioyond_name != resource.name:
                    logger.info(f"   原始 Bioyond 名称: {material_bioyond_name} (当前名称: {resource.name})")
            else:
                # 如果没有 Bioyond ID，尝试按名称查询
                logger.info(f"[resource_tree_remove] 资源 {resource.name} 没有保存 Bioyond ID，尝试查询...")

                # ⭐ 优先使用保存的原始 Bioyond 名称，如果没有则使用当前名称
                query_name = material_bioyond_name if material_bioyond_name else resource.name
                logger.info(f"[resource_tree_remove] 查询 Bioyond 系统中的物料: {query_name}")

                # 查询所有类型的物料：0=耗材, 1=样品, 2=试剂
                all_materials = []
                for type_mode in [0, 1, 2]:
                    query_params = json.dumps({
                        "typeMode": type_mode,
                        "filter": query_name,  # ⭐ 使用原始 Bioyond 名称查询
                        "includeDetail": True
                    })
                    materials = self.hardware_interface.stock_material(query_params)
                    if materials:
                        all_materials.extend(materials)

                # 精确匹配物料名称
                matched_material = None
                for mat in all_materials:
                    if mat.get("name") == query_name:
                        matched_material = mat
                        material_bioyond_id = mat.get("id")
                        logger.info(f"✅ [resource_tree_remove] 找到物料 {query_name} 的 Bioyond ID: {material_bioyond_id[:8]}...")
                        break

                if not matched_material:
                    logger.warning(f"⚠️ [resource_tree_remove] Bioyond 系统中未找到物料: {query_name}")
                    logger.info(f"[resource_tree_remove] 该物料可能尚未入库或已被删除，跳过出库操作")
                    return True

            # 获取物料当前所在的库位信息
            logger.info(f"[resource_tree_remove] 📍 查询物料的库位信息...")

            # 重新查询物料详情以获取最新的库位信息
            all_materials_type1 = self.hardware_interface.stock_material('{"typeMode": 1, "includeDetail": true}')
            all_materials_type2 = self.hardware_interface.stock_material('{"typeMode": 2, "includeDetail": true}')
            all_materials_type0 = self.hardware_interface.stock_material('{"typeMode": 0, "includeDetail": true}')
            all_materials = (all_materials_type0 or []) + (all_materials_type1 or []) + (all_materials_type2 or [])

            location_id = None
            current_quantity = 0

            for material in all_materials:
                if material.get("id") == material_bioyond_id:
                    locations = material.get("locations", [])
                    if locations:
                        # 取第一个库位
                        location = locations[0]
                        location_id = location.get("id")
                        current_quantity = location.get("quantity", 1)
                        logger.info(f"📍 [resource_tree_remove] 物料位于库位:")
                        logger.info(f"   - 库位代码: {location.get('code')}")
                        logger.info(f"   - 仓库名称: {location.get('whName')}")
                        logger.info(f"   - 数量: {current_quantity}")
                        logger.info(f"   - 库位ID: {location_id[:8]}...")
                        break
                    else:
                        logger.warning(f"⚠️ [resource_tree_remove] 物料没有库位信息，可能尚未入库")
                        return True

            if not location_id:
                logger.warning(f"⚠️ [resource_tree_remove] 无法获取物料的库位信息，跳过出库")
                return False

            # 调用 Bioyond 出库 API
            logger.info(f"[resource_tree_remove] 📤 调用 Bioyond API 出库物料...")
            logger.info(f"   UniLab 名称: {resource.name}")
            if material_bioyond_name and material_bioyond_name != resource.name:
                logger.info(f"   Bioyond 名称: {material_bioyond_name}")
            logger.info(f"   物料ID: {material_bioyond_id[:8]}...")
            logger.info(f"   库位ID: {location_id[:8]}...")
            logger.info(f"   出库数量: {current_quantity}")

            response = self.hardware_interface.material_outbound_by_id(
                material_id=material_bioyond_id,
                location_id=location_id,
                quantity=current_quantity
            )

            if response is not None:
                logger.info(f"✅ [resource_tree_remove] 物料成功从 Bioyond 系统出库")
                return True
            else:
                logger.error(f"❌ [resource_tree_remove] 物料出库失败，API 返回空")
                return False

        except Exception as e:
            logger.error(f"❌ [resource_tree_remove] 物料 {resource.name} 出库时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def resource_tree_transfer(self, old_parent: Optional[ResourcePLR], resource: ResourcePLR, new_parent: ResourcePLR) -> None:
        """处理资源在设备间迁移时的同步

        当资源从一个设备迁移到 BioyondWorkstation 时,只创建物料（不入库）
        入库操作由后续的 resource_tree_add 完成

        Args:
            old_parent: 资源的原父节点（可能为 None）
            resource: 要迁移的资源
            new_parent: 资源的新父节点
        """
        logger.info(f"[resource_tree_transfer] 资源迁移: {resource.name}")
        logger.info(f"  旧父节点: {old_parent.name if old_parent else 'None'}")
        logger.info(f"  新父节点: {new_parent.name}")

        try:
            # ⭐ Transfer 阶段：只创建物料到 Bioyond 系统，不执行入库
            logger.info(f"[resource_tree_transfer] 开始创建物料 {resource.name} 到 Bioyond 系统（不入库）")
            result = self.resource_synchronizer._create_material_only(resource)

            if result:
                logger.info(f"✅ [resource_tree_transfer] 物料 {resource.name} 创建成功，Bioyond ID: {result[:8]}...")
            else:
                logger.warning(f"⚠️ [resource_tree_transfer] 物料 {resource.name} 创建失败")

        except Exception as e:
            logger.error(f"❌ [resource_tree_transfer] 资源 {resource.name} 创建异常: {e}")
            import traceback
            traceback.print_exc()

    def resource_tree_update(self, resources: List[ResourcePLR]) -> None:
        """处理资源更新时的同步（位置移动、属性修改等）

        当 UniLab 前端更新物料信息时（如修改位置），需要将更新操作同步到 Bioyond 系统

        Args:
            resources: 要更新的资源列表
        """
        logger.info(f"[resource_tree_update] 开始同步 {len(resources)} 个资源更新到 Bioyond 系统")

        for resource in resources:
            try:
                logger.info(f"[resource_tree_update] 同步资源更新: {resource.name}")

                # 调用同步器的 sync_to_external 方法
                # 该方法会检查 unilabos_extra 中的 update_resource_site 字段
                # 如果存在，会执行位置移动操作
                result = self.resource_synchronizer.sync_to_external(resource)

                if result:
                    logger.info(f"✅ [resource_tree_update] 资源 {resource.name} 成功同步到 Bioyond 系统")
                else:
                    logger.warning(f"⚠️ [resource_tree_update] 资源 {resource.name} 同步到 Bioyond 系统失败")

            except Exception as e:
                logger.error(f"❌ [resource_tree_update] 同步资源 {resource.name} 时发生异常: {e}")
                import traceback
                traceback.print_exc()

        logger.info(f"[resource_tree_update] 资源更新同步完成")

    @property
    def bioyond_status(self) -> Dict[str, Any]:
        """获取 Bioyond 系统状态信息

        这个属性被 ROS 节点用来发布设备状态

        Returns:
            Dict[str, Any]: Bioyond 系统的状态信息
                - 连接成功时返回 {"connected": True}
                - 连接失败时返回 {"connected": False, "error": "错误信息"}
        """
        try:
            # 检查硬件接口是否存在
            if not self.hardware_interface:
                return {"connected": False, "error": "hardware_interface not initialized"}

            # 尝试获取调度器状态来验证连接
            scheduler_status = self.hardware_interface.scheduler_status()

            # 如果能成功获取状态，说明连接正常
            if scheduler_status:
                return {"connected": True}
            else:
                return {"connected": False, "error": "scheduler_status returned None"}

        except Exception as e:
            logger.warning(f"获取Bioyond状态失败: {e}")
            return {"connected": False, "error": str(e)}

    # ==================== 工作流合并与参数设置 API ====================

    def append_to_workflow_sequence(self, web_workflow_name: str) -> bool:
        # 检查是否为JSON格式的字符串
        actual_workflow_name = web_workflow_name
        if web_workflow_name.startswith('{') and web_workflow_name.endswith('}'):
            try:
                data = json.loads(web_workflow_name)
                actual_workflow_name = data.get("web_workflow_name", web_workflow_name)
                print(f"解析JSON格式工作流名称: {web_workflow_name} -> {actual_workflow_name}")
            except json.JSONDecodeError:
                print(f"JSON解析失败，使用原始字符串: {web_workflow_name}")

        workflow_id = self._get_workflow(actual_workflow_name)
        if workflow_id:
            # 兼容 BioyondReactionStation 中 workflow_sequence 被重写为 property 的情况
            if isinstance(self.workflow_sequence, list):
                self.workflow_sequence.append(workflow_id)
            elif hasattr(self, "_cached_workflow_sequence") and isinstance(self._cached_workflow_sequence, list):
                self._cached_workflow_sequence.append(workflow_id)
            else:
                print(f"❌ 无法添加工作流: workflow_sequence 类型错误 {type(self.workflow_sequence)}")
                return False

            print(f"添加工作流到执行顺序: {actual_workflow_name} -> {workflow_id}")
            return True
        return False

    def set_workflow_sequence(self, json_str: str) -> List[str]:
        try:
            data = json.loads(json_str)
            web_workflow_names = data.get("web_workflow_names", [])
        except:
            return []

        sequence = []
        for web_name in web_workflow_names:
            workflow_id = self._get_workflow(web_name)
            if workflow_id:
                sequence.append(workflow_id)

    def get_all_workflows(self) -> Dict[str, str]:
        return self.workflow_mappings.copy()

    def _get_workflow(self, web_workflow_name: str) -> str:
        if web_workflow_name not in self.workflow_mappings:
            print(f"未找到工作流映射配置: {web_workflow_name}")
            return ""
        workflow_id = self.workflow_mappings[web_workflow_name]
        print(f"获取工作流: {web_workflow_name} -> {workflow_id}")
        return workflow_id

    def _set_workflow_mappings(self, mappings: Dict[str, str]):
        self.workflow_mappings = mappings
        print(f"设置工作流映射配置: {mappings}")

    def process_web_workflows(self, json_str: str) -> Dict[str, str]:
        try:
            data = json.loads(json_str)
            web_workflow_list = data.get("web_workflow_list", [])
        except json.JSONDecodeError:
            print(f"无效的JSON字符串: {json_str}")
            return {}
        result = {}

        self.workflow_sequence = []
        for web_name in web_workflow_list:
            workflow_id = self._get_workflow(web_name)
            if workflow_id:
                result[web_name] = workflow_id
                self.workflow_sequence.append(workflow_id)
            else:
                print(f"无法获取工作流ID: {web_name}")
        print(f"工作流执行顺序: {self.workflow_sequence}")
        return result

    def clear_workflows(self):
        self.workflow_sequence = []
        print("清空工作流执行顺序")

    # ==================== 基础物料管理接口 ====================

    # ============ 工作站状态管理 ============
    def get_station_info(self) -> Dict[str, Any]:
        """获取工作站基础信息

        Returns:
            Dict[str, Any]: 工作站基础信息，包括设备ID、状态等
        """
        return {
            "device_id": getattr(self._ros_node, 'device_id', 'unknown'),
            "station_type": "BioyondWorkstation",
            "workflow_status": self.current_workflow_status.value if hasattr(self, 'current_workflow_status') else "unknown",
            "is_busy": getattr(self, 'is_busy', False),
            "deck_info": {
                "name": self.deck.name if self.deck and hasattr(self.deck, 'name') else "unknown",
                "children_count": len(self.deck.children) if self.deck and hasattr(self.deck, 'children') else 0
            } if self.deck else None,
            "hardware_interface": type(self.hardware_interface).__name__ if self.hardware_interface else None
        }

    def get_workstation_status(self) -> Dict[str, Any]:
        """获取工作站状态

        Returns:
            Dict[str, Any]: 工作站状态信息
        """
        try:
            # 获取基础工作站状态
            base_status = {
                "station_info": self.get_station_info(),
                "bioyond_status": self.bioyond_status
            }

            # 如果有接口，获取设备列表
            if self.hardware_interface:
                try:
                    devices = self.hardware_interface.device_list()
                    base_status["devices"] = devices
                except Exception as e:
                    logger.warning(f"获取设备列表失败: {e}")
                    base_status["devices"] = []

            return {
                "success": True,
                "data": base_status,
                "action": "get_workstation_status"
            }

        except Exception as e:
            error_msg = f"获取工作站状态失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "get_workstation_status"
            }

    def get_bioyond_status(self) -> Dict[str, Any]:
        """获取完整的 Bioyond 状态信息

        这个方法提供了比 bioyond_status 属性更详细的状态信息，
        包括错误处理和格式化的响应结构

        Returns:
            Dict[str, Any]: 格式化的 Bioyond 状态响应
        """
        try:
            status = self.bioyond_status
            return {
                "success": True,
                "data": status,
                "action": "get_bioyond_status"
            }

        except Exception as e:
            error_msg = f"获取 Bioyond 状态失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "get_bioyond_status"
            }

    def reset_workstation(self) -> Dict[str, Any]:
        """重置工作站

        重置工作站到初始状态

        Returns:
            Dict[str, Any]: 操作结果
        """
        try:
            logger.info("开始重置工作站")

            # 重置调度器
            if self.hardware_interface:
                self.hardware_interface.scheduler_reset()

            # 重新同步资源，并用同一次库存查询结果更新物料缓存
            if self.resource_synchronizer:
                self.resource_synchronizer.sync_from_external()

            logger.info("工作站重置完成")
            return {
                "success": True,
                "message": "工作站重置成功",
                "action": "reset_workstation"
            }

        except Exception as e:
            error_msg = f"重置工作站失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "reset_workstation"
            }

    # ==================== HTTP 报送处理方法 ====================

    def process_step_finish_report(self, report_request) -> Dict[str, Any]:
        """处理步骤完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含步骤完成信息

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            logger.info(f"[步骤完成报送] 订单: {data.get('orderCode')}, 步骤: {data.get('stepName')}")
            logger.info(f"  样品ID: {data.get('sampleId')}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")

            # TODO: 根据实际业务需求处理步骤完成逻辑
            # 例如：更新数据库、触发后续流程等

            # 发布任务状态事件 (running/progress update)
            self._publish_task_status(
                task_id=data.get('orderCode'), # 使用 OrderCode 作为关联 ID
                task_code=data.get('orderCode'),
                task_type="bioyond_step",
                status="running",
                progress=0.5, # 步骤完成视为任务进行中
                result={"step_name": data.get('stepName'), "step_id": data.get('stepId')}
            )

            # 更新物料信息
            # 步骤完成后，物料状态可能发生变化（如位置、用量等），触发同步
            logger.info(f"[步骤完成报送] 触发物料同步...")
            self.resource_synchronizer.sync_from_external()


            return {
                "processed": True,
                "step_id": data.get('stepId'),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理步骤完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def process_sample_finish_report(self, report_request) -> Dict[str, Any]:
        """处理通量完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含通量完成信息

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            status_names = {
                "0": "待生产", "2": "进样", "10": "开始",
                "20": "完成", "-2": "异常停止", "-3": "人工停止"
            }
            status_desc = status_names.get(str(data.get('status')), f"状态{data.get('status')}")

            logger.info(f"[通量完成报送] 订单: {data.get('orderCode')}, 样品: {data.get('sampleId')}")
            logger.info(f"  状态: {status_desc}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")

            # TODO: 根据实际业务需求处理通量完成逻辑

            # 发布任务状态事件
            self._publish_task_status(
                task_id=data.get('orderCode'),
                task_code=data.get('orderCode'),
                task_type="bioyond_sample",
                status="running",
                progress=0.7,
                result={"sample_id": data.get('sampleId'), "status": status_desc}
            )


            return {
                "processed": True,
                "sample_id": data.get('sampleId'),
                "status": data.get('status'),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理通量完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def process_order_finish_report(self, report_request, used_materials: List) -> Dict[str, Any]:
        """处理任务完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含任务完成信息
            used_materials: 物料使用记录列表

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            status_names = {"30": "完成", "-11": "异常停止", "-12": "人工停止"}
            status_desc = status_names.get(str(data.get('status')), f"状态{data.get('status')}")

            logger.info(f"[任务完成报送] 订单: {data.get('orderCode')} - {data.get('orderName')}")
            logger.info(f"  状态: {status_desc}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")
            logger.info(f"  使用物料数量: {len(used_materials)}")

            # 记录物料使用情况
            for material in used_materials:
                logger.debug(f"  物料: {material.materialId}, 用量: {material.usedQuantity}")

            # TODO: 根据实际业务需求处理任务完成逻辑
            # 例如：更新物料库存、生成报表等

            # 映射状态到事件状态
            event_status = "completed"
            if str(data.get('status')) in ["-11", "-12"]:
                event_status = "error"
            elif str(data.get('status')) == "30":
                event_status = "completed"
            else:
                event_status = "running" # 其他状态视为运行中（或根据实际定义）

            # 发布任务状态事件
            self._publish_task_status(
                task_id=data.get('orderCode'),
                task_code=data.get('orderCode'),
                task_type="bioyond_order",
                status=event_status,
                progress=1.0 if event_status in ["completed", "error"] else 0.9,
                result={"order_name": data.get('orderName'), "status": status_desc, "materials_count": len(used_materials)}
            )

            # 更新物料信息
            # 任务完成后，且状态为完成时，触发同步以更新最终物料状态
            if event_status == "completed":
                logger.info(f"[任务完成报送] 触发物料同步...")
                self.resource_synchronizer.sync_from_external()


            return {
                "processed": True,
                "order_code": data.get('orderCode'),
                "status": data.get('status'),
                "materials_count": len(used_materials),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理任务完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def _iter_resource_subtree(self, resource: ResourcePLR):
        """遍历资源树，包含起点自身。"""
        yield resource
        for child in list(getattr(resource, "children", []) or []):
            yield from self._iter_resource_subtree(child)

    @staticmethod
    def _material_publish_root(resource: Optional[ResourcePLR]) -> Optional[ResourcePLR]:
        if resource is None:
            return None
        root = resource
        parent = getattr(root, "parent", None)
        while parent is not None and getattr(parent, "parent", None) is not None:
            root = parent
            parent = getattr(root, "parent", None)
        return root

    @classmethod
    def _dedupe_material_publish_roots(cls, resources: Optional[List[ResourcePLR]]) -> List[ResourcePLR]:
        deduped: List[ResourcePLR] = []
        seen: set[int] = set()
        for resource in resources or []:
            root = cls._material_publish_root(resource)
            if root is None:
                continue
            marker = id(root)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(root)
        return deduped

    def _append_material_publish_root(
        self,
        publish_roots: Optional[List[ResourcePLR]],
        resource: Optional[ResourcePLR],
    ) -> None:
        if publish_roots is None or resource is None:
            return
        root = self._material_publish_root(resource)
        if root is not None:
            publish_roots.append(root)

    @staticmethod
    def _clean_bioyond_identity(value: Any) -> Optional[str]:
        cleaned = str(value or "").strip()
        return cleaned or None

    @classmethod
    def _value_field(cls, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    @classmethod
    def _legacy_bioyond_id_from_value(
        cls,
        value: Any,
        *,
        allow_payload_id: bool = False,
    ) -> Optional[str]:
        if value is None:
            return None

        material_id = cls._clean_bioyond_identity(cls._value_field(value, "material_bioyond_id"))
        if material_id:
            return material_id

        # 迁移兼容：旧资源树曾经把 Bioyond id 写到别名字段或嵌套原始载荷中。
        for key in ("bioyond_id", "bioyond_material_id"):
            material_id = cls._clean_bioyond_identity(cls._value_field(value, key))
            if material_id:
                return material_id

        if allow_payload_id:
            for key in ("id", "materialId", "material_id"):
                material_id = cls._clean_bioyond_identity(cls._value_field(value, key))
                if material_id:
                    return material_id

        nested = cls._value_field(value, "bioyond_material")
        if nested is not None:
            material_id = cls._legacy_bioyond_id_from_value(nested, allow_payload_id=False)
            if material_id:
                return material_id

            raw_payload = cls._value_field(nested, "raw_payload")
            material_id = cls._legacy_bioyond_id_from_value(raw_payload, allow_payload_id=True)
            if material_id:
                return material_id

        raw_payload = cls._value_field(value, "raw_payload")
        if raw_payload is not None:
            material_id = cls._legacy_bioyond_id_from_value(raw_payload, allow_payload_id=True)
            if material_id:
                return material_id

        return None

    def _resource_bioyond_id(self, resource: ResourcePLR) -> Optional[str]:
        extra_info = getattr(resource, "unilabos_extra", {}) or {}
        material_id = self._clean_bioyond_identity(extra_info.get("material_bioyond_id"))
        if material_id:
            return material_id
        return self._legacy_bioyond_id_from_value(extra_info) or self._legacy_bioyond_id_from_value(resource)

    def _has_bioyond_id_ancestor(self, resource: ResourcePLR, candidates: set[int]) -> bool:
        parent = getattr(resource, "parent", None)
        while parent is not None:
            if id(parent) in candidates:
                return True
            parent = getattr(parent, "parent", None)
        return False

    def _clear_external_sync_bioyond_materials(
        self,
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> int:
        """全量外部同步前清理旧 Bioyond 物料资源。

        只清理带 Bioyond material id 的资源；静态 deck/warehouse/slot 结构不会被移除。
        """
        deck = getattr(self, "deck", None)
        if deck is None:
            return 0

        candidates = [
            resource
            for resource in self._iter_resource_subtree(deck)
            if resource is not deck and self._resource_bioyond_id(resource)
        ]
        candidate_ids = {id(resource) for resource in candidates}
        roots = [
            resource
            for resource in candidates
            if not self._has_bioyond_id_ancestor(resource, candidate_ids)
        ]

        removed = 0
        for resource in roots:
            bioyond_id = self._resource_bioyond_id(resource)
            if self._remove_resource_subtree(resource, publish_roots=publish_roots):
                removed += 1
                logger.info(
                    f"[Bioyond全量同步] 清理旧Bioyond物料: "
                    f"id={bioyond_id} name={getattr(resource, 'name', None)}"
                )
        return removed

    def _sweep_absent_bioyond_materials(
        self,
        returned_ids: set,
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> int:
        """删除缺失清扫：删除 deck 上携带 bioyond_id 但不在 returned_ids 的根资源子树。

        复用 _clear_external_sync_bioyond_materials 的根级过滤；显式排除仓库与虚拟
        暂存节点（按身份/名称，不靠模糊判断）。不独立删除无 bioyond_id 的资源——它们
        只随祖先子树级联删除。returned_ids 为空集（真·空成功）时删除全部 bioyond_id 根。
        """
        deck = getattr(self, "deck", None)
        if deck is None:
            return 0
        wanted = {str(material_id).strip() for material_id in (returned_ids or set()) if str(material_id or "").strip()}

        candidates = [
            resource
            for resource in self._iter_resource_subtree(deck)
            if resource is not deck
            and self._resource_bioyond_id(resource)
            and not self._is_virtual_holding_node(resource)
            and getattr(resource, "category", "") != "warehouse"
        ]
        candidate_ids = {id(resource) for resource in candidates}
        roots = [
            resource
            for resource in candidates
            if not self._has_bioyond_id_ancestor(resource, candidate_ids)
            and self._resource_bioyond_id(resource) not in wanted
        ]
        removed = 0
        for resource in roots:
            bioyond_id = self._resource_bioyond_id(resource)
            if self._remove_resource_subtree(resource, publish_roots=publish_roots):
                removed += 1
                logger.info(
                    f"[Bioyond删除缺失清扫] 删除不在返回集合的旧物料: "
                    f"id={bioyond_id} name={getattr(resource, 'name', None)}"
                )
        return removed

    def _remove_bioyond_materials_from_deck(
        self,
        material_ids: List[str],
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> int:
        """按 Bioyond material id 从 deck 上移除物料实例，缓存记录不受影响。"""
        wanted = {str(material_id).strip() for material_id in material_ids if str(material_id or "").strip()}
        if not wanted:
            return 0
        deck = getattr(self, "deck", None)
        if deck is None:
            return 0

        candidates = [
            resource
            for resource in self._iter_resource_subtree(deck)
            if resource is not deck and self._resource_bioyond_id(resource) in wanted
        ]
        candidate_ids = {id(resource) for resource in candidates}
        roots = [
            resource
            for resource in candidates
            if not self._has_bioyond_id_ancestor(resource, candidate_ids)
        ]
        removed = 0
        for resource in roots:
            bioyond_id = self._resource_bioyond_id(resource)
            if self._remove_resource_subtree(resource, publish_roots=publish_roots):
                removed += 1
                logger.info(
                    f"[Bioyond deck] 按物料ID移除 deck 资源: id={bioyond_id} "
                    f"name={getattr(resource, 'name', None)}"
                )
        return removed

    def _delete_bioyond_materials_from_cache_and_deck(
        self,
        material_ids: List[str],
        *,
        publish_tree: bool = False,
        reason: str = "delete",
    ) -> Dict[str, Any]:
        """take-out 边界：从 durable cache 和 deck 同步删除订单相关物料。"""
        wanted = {str(material_id).strip() for material_id in material_ids if str(material_id or "").strip()}
        publish_roots: List[ResourcePLR] = []
        removed_from_deck = self._remove_bioyond_materials_from_deck(list(wanted), publish_roots=publish_roots)
        removed_from_cache = 0
        synchronizer = getattr(self, "resource_synchronizer", None)
        cache_getter = getattr(synchronizer, "_bioyond_material_cache", None)
        if callable(cache_getter):
            cache = cache_getter()
            for material_id in list(wanted):
                if material_id in cache:
                    del cache[material_id]
                    removed_from_cache += 1
        logger.info(
            f"[Bioyond take-out] 删除订单相关物料: ids={len(wanted)} "
            f"deck={removed_from_deck} cache={removed_from_cache}"
        )
        published = False
        if publish_tree and publish_roots:
            published = self._publish_material_tree_update(reason, resources=publish_roots)
        return {
            "material_ids": sorted(wanted),
            "removed_from_deck": removed_from_deck,
            "removed_from_cache": removed_from_cache,
            "published": published,
            "publish_roots_count": len(self._dedupe_material_publish_roots(publish_roots)),
        }

    def _material_identity_matches(self, resource: ResourcePLR, material_data: Dict[str, Any]) -> bool:
        extra_info = getattr(resource, "unilabos_extra", {}) or {}
        material_id = (
            material_data.get("id")
            or material_data.get("materialId")
            or material_data.get("material_id")
            or material_data.get("bioyond_id")
        )
        if material_id:
            return self._resource_bioyond_id(resource) == str(material_id).strip()
        material_name = material_data.get("name")
        if material_name and extra_info.get("material_bioyond_name") == material_name:
            return True
        material_code = material_data.get("barCode") or material_data.get("code")
        if material_code and getattr(resource, "code", None) == material_code:
            return True
        return False

    def _find_material_resource(self, material_data: Dict[str, Any]) -> Optional[ResourcePLR]:
        deck = getattr(self, "deck", None)
        if deck is None:
            return None
        for resource in self._iter_resource_subtree(deck):
            if self._material_identity_matches(resource, material_data):
                return resource
        return None

    def _unassign_resource_from_parent(self, resource: ResourcePLR) -> bool:
        parent = getattr(resource, "parent", None)
        if parent is None:
            return False
        try:
            parent.unassign_child_resource(resource)
            logger.info(
                f"[物料变更报送] 已从 {getattr(parent, 'name', None)} 移除 {resource.name}"
            )
            return True
        except Exception as exc:
            logger.warning(
                f"[物料变更报送] 从父节点移除 {resource.name} 失败，尝试清理 slots: {exc}"
            )
            sites = getattr(parent, "sites", None)
            if isinstance(sites, list):
                for idx, occupant in enumerate(sites):
                    if occupant is resource:
                        sites[idx] = None
                        break
            if resource in getattr(parent, "children", []):
                parent.children.remove(resource)
            resource.parent = None
            return True

    def _remove_resource_subtree(
        self,
        resource: ResourcePLR,
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> int:
        old_parent = getattr(resource, "parent", None)
        if self._unassign_resource_from_parent(resource):
            if publish_roots is not None:
                publish_roots.append(old_parent if old_parent is not None else resource)
            return 1
        return 0

    def _clear_stale_occupied_by_strings(self, material_data: Dict[str, Any]) -> int:
        deck = getattr(self, "deck", None)
        if deck is None:
            return 0
        candidates = {
            str(value)
            for value in (
                material_data.get("id"),
                material_data.get("name"),
                material_data.get("code"),
                material_data.get("barCode"),
            )
            if value
        }
        cleared = 0
        for resource in self._iter_resource_subtree(deck):
            sites = getattr(resource, "sites", None)
            if not isinstance(sites, list):
                continue
            for idx, occupant in enumerate(sites):
                if isinstance(occupant, str) and occupant in candidates:
                    sites[idx] = None
                    cleared += 1
                    logger.warning(
                        f"[物料变更报送] 清理 stale occupied_by 字符串: "
                        f"{getattr(resource, 'name', None)}[{idx}]={occupant!r}"
                    )
        return cleared

    def _reported_material_model(self, material_data: Dict[str, Any]) -> Optional[str]:
        type_name = material_data.get("typeName")
        type_id = (
            material_data.get("typeId")
            or material_data.get("materialTypeId")
            or material_data.get("typeUUID")
        )
        for resource_id, mapping in (self.bioyond_config.get("material_type_mappings") or {}).items():
            if not isinstance(mapping, list) or not mapping:
                continue
            if type_name and mapping[0] == type_name:
                return resource_id
            if type_id and len(mapping) > 1 and mapping[1] == type_id:
                return resource_id
        return None

    def _preserve_slot_occupant_for_report(self, incoming_model: Optional[str], occupant: ResourcePLR) -> bool:
        if incoming_model == "bioyond_peptide_96_well_synthesis_plate" and self._is_synthesis_base(occupant):
            return True
        if incoming_model == "bioyond_peptide_96_well_synthesis_plate_base" and self._is_synthesis_plate(occupant):
            return True
        return False

    def _first_location_key_from_material_data(self, material_data: Dict[str, Any]) -> Optional[tuple[Any, Any, Any, Any]]:
        locations = material_data.get("locations") or []
        if not isinstance(locations, list) or not locations:
            return None
        location = locations[0] or {}
        return (location.get("whName"), location.get("x"), location.get("y"), location.get("z"))

    def _find_material_resource_by_bioyond_id(self, material_id: str) -> Optional[ResourcePLR]:
        wanted = str(material_id or "").strip()
        if not wanted:
            return None
        deck = getattr(self, "deck", None)
        if deck is None:
            return None
        for resource in self._iter_resource_subtree(deck):
            if self._resource_bioyond_id(resource) == wanted:
                return resource
        return None

    # ----- 虚拟暂存节点 (limbo) 与批次 lookup -------------------------------

    @staticmethod
    def _virtual_holding_node_name() -> str:
        """虚拟暂存节点名称。优先复用 decks 模块常量，导入失败时回退到同值字面量。

        懒导入以规避 resources.decks -> _vendored 的潜在循环依赖。
        """
        try:
            from ..resources.decks import BIOYOND_VIRTUAL_HOLDING_NODE_NAME
            return BIOYOND_VIRTUAL_HOLDING_NODE_NAME
        except Exception:
            return "BioyondVirtualHolding"

    def _is_virtual_holding_node(self, resource: ResourcePLR) -> bool:
        return getattr(resource, "name", "") == self._virtual_holding_node_name()

    def _ensure_virtual_holding_node(self) -> Optional[ResourcePLR]:
        """返回 deck 上的虚拟暂存节点；缺失时尽量创建。

        - 先按名称在 deck 子节点中查找；
        - 未找到则调用 deck 自带的 _ensure_virtual_holding_node()（新版 deck）；
        - 老版序列化 deck 缺该方法时，构造一个等价的回退节点并挂到 deck 上。
        """
        deck = getattr(self, "deck", None)
        if deck is None:
            return None
        node_name = self._virtual_holding_node_name()
        for child in getattr(deck, "children", []) or []:
            if getattr(child, "name", "") == node_name:
                return child

        deck_ensure = getattr(deck, "_ensure_virtual_holding_node", None)
        if callable(deck_ensure):
            try:
                node = deck_ensure()
                if node is not None:
                    return node
            except Exception as exc:
                logger.warning(f"[虚拟暂存节点] deck._ensure_virtual_holding_node 失败，回退本地创建: {exc}")

        # 回退：构造一个等价节点。
        try:
            from pylabrobot.resources import Coordinate
            from ..resources.decks import (
                BIOYOND_VIRTUAL_HOLDING_NODE_CATEGORY,
                BIOYOND_VIRTUAL_HOLDING_NODE_MODEL,
            )
            category = BIOYOND_VIRTUAL_HOLDING_NODE_CATEGORY
            model = BIOYOND_VIRTUAL_HOLDING_NODE_MODEL
        except Exception:
            from pylabrobot.resources import Coordinate
            category = "virtual_holding"
            model = "BioyondVirtualHolding"
        try:
            node = ResourcePLR(
                name=node_name,
                size_x=1.0,
                size_y=1.0,
                size_z=1.0,
                category=category,
                model=model,
            )
            deck.assign_child_resource(node, location=Coordinate(0.0, 0.0, 0.0))
            logger.info(f"[虚拟暂存节点] 已在 deck 上回退创建虚拟暂存节点 {node_name}")
            return node
        except Exception as exc:
            logger.error(f"[虚拟暂存节点] 回退创建失败: {exc}")
            return None

    def _park_resource(
        self,
        resource: ResourcePLR,
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> bool:
        """把 resource 从当前父节点卸下并挂到虚拟暂存节点下，保留最后库位元数据。

        子树整体随父节点搬迁（offspring 仍然嵌套）。旧父节点追加进 publish_roots。
        """
        node = self._ensure_virtual_holding_node()
        if node is None:
            logger.warning(f"[暂存] 无虚拟暂存节点，无法 park {getattr(resource, 'name', None)}")
            return False
        if getattr(resource, "parent", None) is node:
            return False
        old_parent = getattr(resource, "parent", None)
        if old_parent is not None:
            self._unassign_resource_from_parent(resource)
        try:
            from pylabrobot.resources import Coordinate
            node.assign_child_resource(resource, location=Coordinate(0.0, 0.0, 0.0))
        except Exception:
            try:
                node.assign_child_resource(resource)
            except Exception as exc:
                logger.error(f"[暂存] 挂载 {getattr(resource, 'name', None)} 到虚拟暂存节点失败: {exc}")
                return False
        if publish_roots is not None:
            publish_roots.append(old_parent if old_parent is not None else node)
            publish_roots.append(node)
        logger.info(
            f"[暂存] 物料 {getattr(resource, 'name', None)} 已 park 到虚拟暂存节点 "
            f"{getattr(node, 'name', None)}（保留最后库位元数据）"
        )
        return True

    def _build_bioyond_lookup_map(self) -> Dict[str, ResourcePLR]:
        """walk deck 一次，构造 {bioyond_id: resource} 瞬态映射（批次内复用）。

        调用方在每次 create/move/park/delete 后用 _update_lookup_map 维护它。
        """
        lookup: Dict[str, ResourcePLR] = {}
        deck = getattr(self, "deck", None)
        if deck is None:
            return lookup
        for resource in self._iter_resource_subtree(deck):
            if resource is deck:
                continue
            bid = self._resource_bioyond_id(resource)
            if bid and bid not in lookup:
                lookup[bid] = resource
        return lookup

    @staticmethod
    def _update_lookup_map(
        lookup: Optional[Dict[str, ResourcePLR]],
        bioyond_id: Optional[str],
        resource: Optional[ResourcePLR],
    ) -> None:
        """批次 lookup 维护：resource=None 表示删除该 id；否则写入/更新。"""
        if lookup is None or not bioyond_id:
            return
        key = str(bioyond_id).strip()
        if not key:
            return
        if resource is None:
            lookup.pop(key, None)
        else:
            lookup[key] = resource

    def _is_combined_counterpart(self, occupant: ResourcePLR, incoming_model: Optional[str]) -> bool:
        """占位策略谓词：occupant 是否是合法的组合/底座-子物料配对方，应予保留。

        泛化自 _preserve_slot_occupant_for_report：合成板/底座互为合法配对方。
        """
        if incoming_model == "bioyond_peptide_96_well_synthesis_plate" and self._is_synthesis_base(occupant):
            return True
        if incoming_model == "bioyond_peptide_96_well_synthesis_plate_base" and self._is_synthesis_plate(occupant):
            return True
        return False

    def _resolve_target_slot_occupant(
        self,
        warehouse: ResourcePLR,
        idx: int,
        incoming_model: Optional[str],
        incoming_resource: Optional[ResourcePLR] = None,
        publish_roots: Optional[List[ResourcePLR]] = None,
        on_conflict: str = "park",
    ) -> str:
        """共享占位策略：决定目标库位现有占位物的去留。

        返回 "free"（本就空闲/即将入住物本身）、"preserve"（合法配对方，保留）、
        "park"/"delete"（真冲突/陈旧重复，按 on_conflict 处理）。
        """
        try:
            current = warehouse[idx]
        except Exception:
            current = None
        if current is None or current is incoming_resource:
            return "free"
        if isinstance(current, (ResourceHolder, str)):
            # ResourceHolder 是静态槽位结构，字符串是 stale occupied_by。
            if isinstance(current, str):
                sites = getattr(warehouse, "sites", None)
                if isinstance(sites, list) and idx < len(sites):
                    sites[idx] = None
            return "free"
        if self._is_combined_counterpart(current, incoming_model):
            logger.debug(
                f"[占位策略] 保留 {getattr(warehouse, 'name', None)}[{idx}] 的合法组合配对方 "
                f"{getattr(current, 'name', None)}"
            )
            return "preserve"
        # 真冲突 / 陈旧重复：按 on_conflict 处理。
        if on_conflict == "delete":
            self._remove_resource_subtree(current, publish_roots=publish_roots)
            logger.warning(
                f"[占位策略] 删除目标库位旧占位物: {getattr(warehouse, 'name', None)}[{idx}] "
                f"-> {getattr(current, 'name', None)}"
            )
            return "delete"
        self._park_resource(current, publish_roots=publish_roots)
        logger.warning(
            f"[占位策略] park 目标库位旧占位物: {getattr(warehouse, 'name', None)}[{idx}] "
            f"-> {getattr(current, 'name', None)}"
        )
        return "park"

    def _assign_child_to_parent_slot(self, parent: ResourcePLR, child: ResourcePLR) -> bool:
        """把组合子物料挂到父物料的 0 号槽位（合成场景复用 _assign_plate_to_base_slot）。"""
        if self._is_synthesis_base(parent):
            return self._assign_plate_to_base_slot(parent, child)
        if getattr(child, "parent", None) is parent:
            return False
        try:
            current = parent[0] if hasattr(parent, "__getitem__") else None
        except Exception:
            current = None
        if current is child:
            return False
        if isinstance(current, str):
            sites = getattr(parent, "sites", None)
            if isinstance(sites, list) and sites:
                sites[0] = None
        elif current is not None and current is not child:
            self._unassign_resource_from_parent(current)
        if getattr(child, "parent", None) is not None:
            self._unassign_resource_from_parent(child)
        try:
            parent[0] = child
        except Exception as exc:
            logger.warning(
                f"[组合物料] 挂载 {getattr(child, 'name', None)} 到父物料 "
                f"{getattr(parent, 'name', None)} 槽位0失败: {exc}"
            )
            return False
        logger.info(
            f"[组合物料] {getattr(child, 'name', None)} 挂载到父物料 "
            f"{getattr(parent, 'name', None)} 的槽位0"
        )
        return True

    def _move_resource_to_warehouse_slot(
        self,
        resource: ResourcePLR,
        warehouse: ResourcePLR,
        idx: int,
    ) -> bool:
        """把已存在的 resource 搬到目标仓库槽位（先卸下当前父节点）。"""
        try:
            if warehouse[idx] is resource:
                return True
        except Exception:
            pass
        if getattr(resource, "parent", None) is not None:
            self._unassign_resource_from_parent(resource)
        try:
            warehouse[idx] = resource
        except Exception as exc:
            logger.error(
                f"[物料移动] 放置 {getattr(resource, 'name', None)} 到 "
                f"{getattr(warehouse, 'name', None)}[{idx}] 失败: {exc}"
            )
            return False
        return True


    def _clear_reported_location_occupants(
        self,
        material_data: Dict[str, Any],
        publish_roots: Optional[List[ResourcePLR]] = None,
    ) -> int:
        combined_parent_id = str(material_data.get("combinedMaterialId") or "").strip()
        locations = material_data.get("locations") or []
        if not locations:
            if combined_parent_id:
                logger.debug(
                    f"[物料变更报送] 物料 {material_data.get('id')} 是无 locations[] 的组合子物料，"
                    f"combinedMaterialId={combined_parent_id}，跳过自身库位清理"
                )
            return 0
        if combined_parent_id:
            parent = self._find_material_resource_by_bioyond_id(combined_parent_id)
            if parent is not None:
                child_location_key = self._first_location_key_from_material_data(material_data)
                parent_location_key = self._first_location_key_from_resource(parent)
                if child_location_key == parent_location_key:
                    logger.debug(
                        f"[物料变更报送] 物料 {material_data.get('id')} 是同库位组合子物料，"
                        f"combinedMaterialId={combined_parent_id}，跳过自身库位清理"
                    )
                    return 0
        incoming_model = self._reported_material_model(material_data)
        cleared = 0
        for location in locations:
            warehouse, idx, slot_key = self._bioyond_slot_from_location(location)
            if warehouse is None or idx is None:
                continue
            current = warehouse[idx]
            if current is None or isinstance(current, (ResourceHolder, str)):
                continue
            if self._preserve_slot_occupant_for_report(incoming_model, current):
                logger.debug(
                    f"[物料变更报送] 保留 {warehouse.name}[{idx}]"
                    f"{f'({slot_key})' if slot_key else ''} 的固相板/底座组合占位"
                )
                continue
            if not self._unassign_resource_from_parent(current):
                sites = getattr(warehouse, "sites", None)
                if isinstance(sites, list):
                    sites[idx] = None
            if publish_roots is not None:
                publish_roots.append(warehouse)
            cleared += 1
            logger.warning(
                f"[物料变更报送] 清理目标库位旧物料: {warehouse.name}[{idx}]"
                f"{f'({slot_key})' if slot_key else ''} -> {getattr(current, 'name', None)}"
            )
        return cleared

    def _bioyond_slot_from_location(self, location: Dict[str, Any]) -> tuple[Optional[ResourcePLR], Optional[int], Optional[str]]:
        deck = getattr(self, "deck", None)
        wh_name = location.get("whName")
        if wh_name == "堆栈1":
            x_val = location.get("x", 1)
            if 1 <= x_val <= 4:
                wh_name = "堆栈1左"
            elif 5 <= x_val <= 8:
                wh_name = "堆栈1右"
            else:
                return None, None, None
        elif wh_name == "站内Tip盒堆栈":
            y_val = location.get("y", 1)
            if y_val == 1:
                wh_name = "站内Tip盒堆栈(右)"
            elif y_val in [2, 3]:
                wh_name = "站内Tip盒堆栈(左)"
                location = dict(location)
                location["y"] = y_val - 1
            else:
                return None, None, None
        if deck is None or not hasattr(deck, "warehouses") or wh_name not in deck.warehouses:
            return None, None, None

        warehouse = deck.warehouses[wh_name]
        x = location.get("x", 1)
        y = location.get("y", 1)
        z = location.get("z", 1)

        bioyond_axis = getattr(warehouse, "bioyond_axis", "xy_row_col")
        bioyond_key_axis = getattr(warehouse, "bioyond_key_axis", "row_col")
        if bioyond_axis == "xy_col_row" and bioyond_key_axis != "col_row":
            x, y = y, x

        if wh_name == "堆栈1右":
            y = y - 4

        if wh_name in ["站内试剂存放堆栈", "测量小瓶仓库(测密度)"]:
            col_idx = x - 1
            row_idx = y - 1
            layer_idx = z - 1
            idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_y + col_idx
        else:
            row_idx = x - 1
            col_idx = y - 1
            layer_idx = z - 1
            ordering_layout = getattr(warehouse, "ordering_layout", "col-major")
            if ordering_layout == "row-major":
                idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_x + col_idx
            else:
                idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + col_idx * warehouse.num_items_y + row_idx

        if not (0 <= idx < getattr(warehouse, "capacity", 0)):
            return warehouse, None, None

        slot_key = None
        ordering = getattr(warehouse, "_ordering", {})
        sites = getattr(warehouse, "sites", [])
        if isinstance(ordering, dict) and idx < len(sites):
            site_at_idx = sites[idx]
            slot_key = next((key for key, site in ordering.items() if site is site_at_idx), None)
        return warehouse, idx, slot_key

    def _first_location_key_from_resource(self, resource: ResourcePLR) -> Optional[tuple[Any, Any, Any, Any]]:
        extra_info = getattr(resource, "unilabos_extra", {}) or {}
        locations = extra_info.get("bioyond_material_locations")
        if not isinstance(locations, list) or not locations:
            return None
        location = locations[0] or {}
        return (location.get("whName"), location.get("x"), location.get("y"), location.get("z"))

    def _is_synthesis_plate(self, resource: ResourcePLR) -> bool:
        return getattr(resource, "model", None) == "bioyond_peptide_96_well_synthesis_plate"

    def _is_synthesis_base(self, resource: ResourcePLR) -> bool:
        return getattr(resource, "model", None) == "bioyond_peptide_96_well_synthesis_plate_base"

    def _place_base_in_reported_slot(self, base: ResourcePLR) -> bool:
        extra_info = getattr(base, "unilabos_extra", {}) or {}
        locations = extra_info.get("bioyond_material_locations")
        if not isinstance(locations, list) or not locations:
            return False
        warehouse, idx, slot_key = self._bioyond_slot_from_location(locations[0])
        if warehouse is None or idx is None:
            return False
        current = warehouse[idx]
        if current is base:
            return True
        if current is not None:
            if isinstance(current, str):
                logger.warning(
                    f"[物料变更报送] base 放置前清理 stale occupied_by: "
                    f"{warehouse.name}[{idx}]={current!r}"
                )
                warehouse.sites[idx] = None
            else:
                self._unassign_resource_from_parent(current)
        warehouse[idx] = base
        logger.info(
            f"[物料变更报送] 96孔固相合成板底座 {base.name} 放置到 "
            f"{warehouse.name}[{idx}]{f'({slot_key})' if slot_key else ''}"
        )
        return True

    def _assign_plate_to_base_slot(self, base: ResourcePLR, plate: ResourcePLR) -> bool:
        if getattr(plate, "parent", None) is base:
            return False
        current = base[0] if hasattr(base, "__getitem__") else None
        if current is plate:
            return False
        if isinstance(current, str):
            base.sites[0] = None
        elif current is not None and current is not plate:
            self._unassign_resource_from_parent(current)
        if getattr(plate, "parent", None) is not None:
            self._unassign_resource_from_parent(plate)
        base[0] = plate
        logger.info(
            f"[物料变更报送] 96孔固相合成板 {plate.name} 挂载到底座 {base.name} 的槽位 A1"
        )
        return True

    def _reconcile_synthesis_plate_bases(self, extra_resources: Optional[List[ResourcePLR]] = None) -> int:
        deck = getattr(self, "deck", None)
        if deck is None:
            return 0
        resources = list(self._iter_resource_subtree(deck))
        for resource in extra_resources or []:
            if resource not in resources:
                resources.append(resource)
                for child in list(getattr(resource, "children", []) or []):
                    if child not in resources:
                        resources.append(child)
        bases = [resource for resource in resources if self._is_synthesis_base(resource)]
        plates = [resource for resource in resources if self._is_synthesis_plate(resource)]
        changes = 0
        for base in bases:
            base_location = self._first_location_key_from_resource(base)
            if base_location is None:
                continue
            for plate in plates:
                if self._first_location_key_from_resource(plate) != base_location:
                    continue
                if self._place_base_in_reported_slot(base):
                    if self._assign_plate_to_base_slot(base, plate):
                        changes += 1
        return changes

    def _annotate_reported_material(self, resource: ResourcePLR, material_data: Dict[str, Any]) -> None:
        extra_info = dict(getattr(resource, "unilabos_extra", {}) or {})
        material_code = (
            material_data.get("code")
            or material_data.get("materialCode")
            or material_data.get("barCode")
            or ""
        )
        type_code = str(material_code or "").split("-", 1)[0] if material_code else ""
        material_id = material_data.get("id") or material_data.get("materialId")
        extra_info["material_bioyond_id"] = material_id
        extra_info["material_bioyond_name"] = material_data.get("name")
        extra_info["material_bioyond_type"] = material_data.get("typeName")
        extra_info["material_bioyond_code"] = material_code
        extra_info["bioyond_material_type_code"] = type_code
        locations = copy.deepcopy(material_data.get("locations") or [])
        details = copy.deepcopy(material_data.get("detail") or material_data.get("details") or [])
        extra_info["bioyond_material_locations"] = locations
        extra_info["bioyond_material_details"] = details
        extra_info["bioyond_combinedmaterialId"] = material_data.get("combinedMaterialId")
        synchronizer = getattr(self, "resource_synchronizer", None)
        cache_getter = getattr(synchronizer, "_bioyond_material_cache", None)
        if callable(cache_getter) and material_data.get("id"):
            record = cache_getter().get(str(material_data.get("id")))
            if isinstance(record, dict):
                extra_info["bioyond_material"] = copy.deepcopy(record)
                extra_info["bioyond_resolved_type_name"] = record.get("resolved_type_name")
                if record.get("resolved_type_name"):
                    extra_info["material_bioyond_type"] = record.get("resolved_type_name")
        resource.unilabos_extra = extra_info

    def _publish_material_tree_update(
        self,
        reason: str,
        resources: Optional[List[ResourcePLR]] = None,
    ) -> bool:
        ros_node = getattr(self, "_ros_node", None)
        deck = getattr(self, "deck", None)
        if ros_node is None or deck is None or not hasattr(ros_node, "update_resource"):
            logger.warning(f"[物料变更报送] 跳过资源树发布: ros_node/deck 未就绪 ({reason})")
            return False
        if resources is None:
            resources = getattr(self, "_last_material_sync_publish_roots", None) or [deck]
        publish_roots = self._dedupe_material_publish_roots(resources)
        if not publish_roots:
            logger.info(f"[物料变更报送] 无资源树节点需要发布: {reason}")
            return False
        ROS2DeviceNode.run_async_func(ros_node.update_resource, True, **{"resources": publish_roots})
        names = [getattr(resource, "name", None) for resource in publish_roots]
        logger.info(f"[物料变更报送] 已发布更新后的资源父节点: {reason}, roots={names}")
        return True

    def process_material_change_report(
        self,
        report_data: Dict[str, Any],
        *,
        on_empty_locations: str = "park",
        source: str = "material_change",
        source_scope: Optional[str] = None,
        lookup: Optional[Dict[str, ResourcePLR]] = None,
    ) -> Dict[str, Any]:
        """统一的"移动优先" upsert 原语（所有同步路径共用）。

        规范流程：(1) 按 source 权威 resolve/缓存 → (2) lookup 既有实例 →
        (3) 更新 deck（move/create/park/delete）→ (4) publish。

        Args:
            report_data: Bioyond 物料载荷（首个位置参数，HTTP 委托以单参调用，
                因此其余参数全部 keyword-only 带默认值，保持向后兼容）。
            on_empty_locations: 空 locations 且无 combinedMaterialId 时的策略：
                "park"（默认，挂虚拟暂存节点）或 "delete"（硬删子树）。
            source: 来源枚举（material_change/stock_material/order_materials/material_info）。
            source_scope: 来源作用域（如 order sync 的 order_id）。
            lookup: 批次瞬态 {bioyond_id: resource} 映射；提供时用它查找并在
                create/move/park/delete 后维护，缺省时回退 deck walk。

        Returns:
            Dict，至少含 success/action/message。action ∈
            {"delete","park","move","add","noop"}；API 失败时 success=False。
        """
        try:
            # ---------- (1) resolve/cache by source authority ----------
            synchronizer = getattr(self, "resource_synchronizer", None)
            norm_source = (
                synchronizer._normalize_source(source)
                if synchronizer is not None and hasattr(synchronizer, "_normalize_source")
                else source
            )

            # 归一化别名（materialId→id 等），不丢弃原始 key、不臆造 locations。
            if synchronizer is not None and hasattr(synchronizer, "normalize_material_aliases"):
                report_data = synchronizer.normalize_material_aliases(report_data)
            else:
                report_data = dict(report_data)

            material_id = synchronizer._material_bioyond_id(report_data) if synchronizer else (
                report_data.get("id") or report_data.get("materialId") or report_data.get("material_id")
            )
            material_id = str(material_id or "").strip()
            if material_id and not report_data.get("id"):
                report_data = dict(report_data)
                report_data["id"] = material_id

            # 稀疏载荷（尤其实时推送）回退 material-info(id)，取权威完整状态（含 locations）。
            need_info = (
                norm_source == "material_change"
                and "locations" not in report_data
                and material_id
            )
            if need_info and synchronizer is not None and hasattr(synchronizer, "material_info_result"):
                info_result = synchronizer.material_info_result(material_id)
                if not info_result.get("ok"):
                    msg = info_result.get("message") or "material-info 查询失败"
                    logger.error(f"[物料变更报送] material-info 失败，跳过 deck 变更: id={material_id} {msg}")
                    return {
                        "processed": False,
                        "success": False,
                        "action": "noop",
                        "material_id": material_id,
                        "message": msg,
                        "timestamp": datetime.now().isoformat(),
                    }
                info = info_result.get("data") or {}
                if info:
                    merged = dict(report_data)
                    merged.update(info)
                    merged["id"] = material_id
                    report_data = merged

            # 兜底走旧的 payload 解析（补类型/编码元数据，不复用旧库位）。
            if synchronizer is not None and hasattr(synchronizer, "_resolve_material_payloads"):
                resolved = synchronizer._resolve_material_payloads([report_data], source=norm_source)
                if resolved:
                    report_data = resolved[0]

            material_name = report_data.get("name")
            material_type = report_data.get("typeName")
            locations = report_data.get("locations") or []
            combined_parent_id = str(report_data.get("combinedMaterialId") or "").strip()
            logger.info(
                f"[物料变更报送] 物料: id={material_id} name={material_name} "
                f"type={material_type} locations={len(locations)} source={norm_source}"
            )

            # 源感知缓存更新（API 失败已在上面短路，不会到这里改缓存/deck）。
            material_cache_updated = False
            try:
                if synchronizer is not None and hasattr(synchronizer, "_update_material_cache_from_stock"):
                    synchronizer._update_material_cache_from_stock(
                        [report_data], source=norm_source, source_scope=source_scope
                    )
                    material_cache_updated = True
            except Exception as exc:
                logger.warning(f"[物料变更报送] 更新物料缓存失败: {exc}")

            # ---------- (2) lookup existing instance ----------
            if lookup is not None and material_id:
                existing_resource = lookup.get(material_id)
            else:
                existing_resource = self._find_material_resource_by_bioyond_id(material_id) if material_id else None
                if existing_resource is None:
                    existing_resource = self._find_material_resource(report_data)

            cleared_stale = self._clear_stale_occupied_by_strings(report_data)
            publish_roots: List[ResourcePLR] = []

            # ---------- (3) update deck ----------
            # (3a) 空 locations + 无 combinedMaterialId → park 或 delete
            if not locations and not combined_parent_id:
                action = "noop"
                if existing_resource is not None:
                    if on_empty_locations == "delete":
                        self._remove_resource_subtree(existing_resource, publish_roots=publish_roots)
                        action = "delete"
                    else:
                        # park 前先刷新最后库位元数据再搬迁。
                        self._annotate_reported_material(existing_resource, report_data)
                        self._park_resource(existing_resource, publish_roots=publish_roots)
                        action = "park"
                    self._update_lookup_map(
                        lookup, material_id, None if action == "delete" else existing_resource
                    )
                published = self._publish_material_tree_update(
                    f"{action}:{material_id or material_name}",
                    resources=publish_roots,
                )
                return {
                    "processed": True,
                    "success": True,
                    "action": action,
                    "material_id": material_id,
                    "material_name": material_name,
                    "cleared_stale_slots": cleared_stale,
                    "material_cache_updated": material_cache_updated,
                    "published": published,
                    "message": f"{action} 完成",
                    "timestamp": datetime.now().isoformat(),
                }

            # (3b) 有归宿：先尝试组合父挂载，否则按 locations[] 计算仓库槽位。
            incoming_model = self._reported_material_model(report_data)
            cleared_occupied = 0
            action = None

            # combined：父无差异位置 → 挂父槽位0
            parent_resource = None
            if combined_parent_id:
                if lookup is not None:
                    parent_resource = lookup.get(combined_parent_id)
                if parent_resource is None:
                    parent_resource = self._find_material_resource_by_bioyond_id(combined_parent_id)

            target_warehouse = None
            target_idx = None
            mount_to_parent = False
            if parent_resource is not None:
                child_location_key = self._first_location_key_from_material_data(report_data)
                parent_location_key = self._first_location_key_from_resource(parent_resource)
                if child_location_key is None or child_location_key == parent_location_key:
                    mount_to_parent = True
            if not mount_to_parent and locations:
                target_warehouse, target_idx, _slot_key = self._bioyond_slot_from_location(locations[0])

            if existing_resource is not None:
                # ---- MOVE 既有实例（保持对象身份/内容）----
                if mount_to_parent and parent_resource is not None:
                    self._append_material_publish_root(publish_roots, existing_resource)
                    self._assign_child_to_parent_slot(parent_resource, existing_resource)
                    self._append_material_publish_root(publish_roots, parent_resource)
                    action = "move"
                elif target_warehouse is not None and target_idx is not None:
                    self._resolve_target_slot_occupant(
                        target_warehouse, target_idx, incoming_model,
                        incoming_resource=existing_resource,
                        publish_roots=publish_roots, on_conflict="park",
                    )
                    self._append_material_publish_root(publish_roots, existing_resource)
                    self._move_resource_to_warehouse_slot(existing_resource, target_warehouse, target_idx)
                    publish_roots.append(target_warehouse)
                    action = "move"
                else:
                    # 无法解析目标库位：保留实例，仅刷新元数据。
                    action = "noop"
                self._annotate_reported_material(existing_resource, report_data)
                self._update_lookup_map(lookup, material_id, existing_resource)
                placed_resources = [existing_resource]
            else:
                # ---- CREATE：deck 上没有该实例，走 graphio 转换 ----
                cleared_occupied = self._clear_reported_location_occupants(
                    report_data, publish_roots=publish_roots
                )
                lookup_fn = lookup if lookup is not None else self._find_material_resource_by_bioyond_id
                placed_resources = resource_bioyond_to_plr(
                    [report_data],
                    type_mapping=self.bioyond_config["material_type_mappings"],
                    deck=self.deck,
                    lookup=lookup_fn,
                )
                for resource in placed_resources:
                    self._annotate_reported_material(resource, report_data)
                    self._append_material_publish_root(publish_roots, resource)
                    rid = self._resource_bioyond_id(resource)
                    self._update_lookup_map(lookup, rid, resource)
                action = "add"

            reconciled = self._reconcile_synthesis_plate_bases(placed_resources)
            if reconciled:
                for resource in placed_resources:
                    self._append_material_publish_root(publish_roots, resource)

            published = self._publish_material_tree_update(
                f"{action}:{material_id or material_name}",
                resources=publish_roots,
            )
            logger.info(
                f"[物料变更报送] {action} 完成: id={material_id} name={material_name} "
                f"resources={len(placed_resources)} reconciled={reconciled} published={published}"
            )
            return {
                "processed": True,
                "success": True,
                "action": action,
                "material_id": material_id,
                "material_name": material_name,
                "material_type": material_type,
                "locations_count": len(locations),
                "resources_count": len(placed_resources),
                "cleared_stale_slots": cleared_stale,
                "cleared_occupied_slots": cleared_occupied,
                "material_cache_updated": material_cache_updated,
                "base_plate_reconciled": reconciled,
                "published": published,
                "message": f"{action} 完成",
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f"处理物料变更报送失败: {e}")
            logger.debug(traceback.format_exc())
            return {"processed": False, "success": False, "action": "error", "error": str(e), "message": str(e)}


    def handle_external_error(self, error_data: Dict[str, Any]) -> Dict[str, Any]:
        """处理错误处理报送

        Args:
            error_data: 错误数据（可能是奔曜格式或标准格式）

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            # 检查是否为奔曜格式
            if 'task' in error_data and 'code' in error_data:
                # 奔曜格式
                logger.error(f"[错误处理报送-奔曜] 任务: {error_data.get('task')}")
                logger.error(f"  错误代码: {error_data.get('code')}")
                error_message_parts = [
                    str(error_data.get(key)).strip()
                    for key in (
                        "message",
                        "errMessage",
                        "errInnerMessage",
                        "errInnerMessage2",
                        "errInnerMessage3",
                        "optionMessage",
                    )
                    if error_data.get(key) not in (None, "")
                ]
                error_message = "\n".join(error_message_parts) if error_message_parts else "无"
                logger.error(f"  错误信息: {error_message}")
                error_type = "bioyond_error"
            else:
                # 标准格式
                logger.error(f"[错误处理报送] 工作站: {error_data.get('workstation_id')}")
                logger.error(f"  错误类型: {error_data.get('error_type')}")
                logger.error(f"  错误信息: {error_data.get('error_message')}")
                error_type = error_data.get('error_type', 'unknown')

            # TODO: 根据实际业务需求处理错误
            # 例如：记录日志、发送告警、触发恢复流程等

            return {
                "handled": True,
                "error_type": error_type,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理错误报送失败: {e}")
            return {"handled": False, "error": str(e)}

    # ==================== 文件加载与其他功能 ====================

    def load_bioyond_data_from_file(self, file_path: str) -> bool:
        """从文件加载Bioyond数据（用于测试）"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                bioyond_data = json.load(f)

            logger.info(f"从文件加载Bioyond数据: {file_path}")

            # 转换为UniLab格式
            unilab_resources = resource_bioyond_to_plr(
                bioyond_data,
                type_mapping=self.bioyond_config["material_type_mappings"],
                deck=self.deck
            )

            logger.info(f"成功加载 {len(unilab_resources)} 个资源")
            return True

        except Exception as e:
            logger.error(f"从文件加载Bioyond数据失败: {e}")
            return False


# 使用示例
def create_bioyond_workstation_example():
    """创建Bioyond工作站示例"""

    # 配置参数
    device_id = "bioyond_workstation_001"

    # 子资源配置
    children = {
        "plate_1": {
            "name": "plate_1",
            "type": "plate",
            "position": {"x": 100, "y": 100, "z": 0},
            "config": {
                "size_x": 127.76,
                "size_y": 85.48,
                "size_z": 14.35,
                "model": "Generic 96 Well Plate"
            }
        }
    }

    # Bioyond配置
    bioyond_config = {
        "base_url": "http://bioyond.example.com/api",
        "api_key": "your_api_key_here",
        "sync_interval": 60,  # 60秒同步一次
        "timeout": 30
    }

    # Deck配置
    deck_config = {
        "size_x": 1000.0,
        "size_y": 1000.0,
        "size_z": 100.0,
        "model": "BioyondDeck"
    }

    # 创建工作站
    workstation = BioyondWorkstation(
        station_resource=deck_config,
        bioyond_config=bioyond_config,
        deck_config=deck_config,
    )

    return workstation


if __name__ == "__main__":
    pass
