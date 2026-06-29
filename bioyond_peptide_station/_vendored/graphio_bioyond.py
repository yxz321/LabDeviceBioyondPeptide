"""Bioyond resource-conversion functions carved from ``unilabos/resources/graphio.py``.

Only ``resource_bioyond_to_plr`` and ``resource_plr_to_bioyond`` were modified by the
peptide work, so they are vendored here. Every other graphio helper is unchanged
upstream and imported from ``unilabos.resources.graphio``.
"""
import importlib
import inspect
import json
import os.path
import traceback
import copy
from typing import Union, Any, Dict, List, Tuple
import uuid
import networkx as nx
from pylabrobot.resources import ResourceHolder
from unilabos_msgs.msg import Resource

from unilabos.config.config import BasicConfig
from unilabos.resources.container import RegularContainer
from unilabos.resources.itemized_carrier import ItemizedCarrier, BottleCarrier
from unilabos.ros.msgs.message_converter import convert_to_ros_msg
from unilabos.resources.resource_tracker import (
    ResourceDictInstance,
    ResourceTreeSet,
)
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

try:
    from pylabrobot.resources.resource import Resource as ResourcePLR
except ImportError:
    pass
from typing import get_origin

# Unchanged graphio helper re-used by the carved functions below.
from unilabos.resources.graphio import initialize_resource


def resource_bioyond_to_plr(bioyond_materials: list[dict], type_mapping: Dict[str, Tuple[str, str]] = {}, deck: Any = None, lookup: Any = None) -> list[dict]:
    """
    将 bioyond 物料格式转换为 ulab 物料格式

    Args:
        bioyond_materials: bioyond 系统的物料查询结果列表
        type_mapping: 物料类型映射字典，格式 {model: (显示名称, UUID)} 或 {显示名称: (model, UUID)}
        location_id_mapping: 库位 ID 到名称的映射字典，格式 {location_id: location_name}
        lookup: 可选的组合物料父物料外部解析器，用于在本批次之外（例如已在 deck 上、
            但不在当前转换批次内）查找父物料。可为 dict（``{bioyond_id: resource}``）或
            callable（``lookup(bioyond_id) -> Optional[resource]``）。仅作为本批次
            ``resources_by_bioyond_id`` 查找失败时的后备；为 ``None`` 时行为与旧版完全一致。

    Returns:
        pylabrobot 格式的物料列表
    """
    plr_materials = []

    def _resolve_parent(bid):
        if not bid:
            return None
        if callable(lookup):
            return lookup(bid)
        if lookup:
            return lookup.get(bid)
        return None

    # 创建反向映射: {显示名称: (model, UUID)} -> 用于从 Bioyond typeName 查找 model
    # 如果 type_mapping 的 key 已经是显示名称,则直接使用;否则创建反向映射
    reverse_type_mapping = {}
    for key, value in type_mapping.items():
        # value 可能是 tuple 或 list: (显示名称, UUID) 或 [显示名称, UUID]
        display_name = value[0] if isinstance(value, (tuple, list)) and len(value) >= 1 else None
        if display_name:
            # 反向映射: {显示名称: (原始key作为model, UUID)}
            resource_uuid = value[1] if len(value) >= 2 else ""
            # 如果已存在该显示名称,跳过(保留第一个遇到的映射)
            if display_name not in reverse_type_mapping:
                reverse_type_mapping[display_name] = (key, resource_uuid)

    type_id_mapping = {}
    type_code_mapping = {}
    for key, value in type_mapping.items():
        if isinstance(value, (tuple, list)) and len(value) >= 2 and value[1]:
            type_id_mapping[str(value[1])] = (key, value[1])

    try:
        from unilabos.registry.registry import lab_registry

        for resource_id, value in type_mapping.items():
            resource_config = lab_registry.resource_type_registry.get(resource_id, {})
            class_config = resource_config.get("class")
            resource_cls = None
            if inspect.isclass(class_config):
                resource_cls = class_config
            elif isinstance(class_config, dict) and "module" in class_config:
                module_name, class_name = class_config["module"].split(":", 1)
                try:
                    module = importlib.import_module(module_name)
                    resource_cls = getattr(module, class_name, None)
                except Exception:
                    resource_cls = None
            if resource_cls is None:
                continue

            discovered_codes = set()
            type_code = getattr(resource_cls, "bioyond_material_type_code", None)
            if type_code:
                discovered_codes.add(str(type_code))

            resource_module = inspect.getmodule(resource_cls)
            code_to_class = getattr(resource_module, "MATERIAL_TYPE_CODE_TO_CLASS", {})
            if isinstance(code_to_class, dict):
                for mapped_code, mapped_cls in code_to_class.items():
                    if mapped_cls is resource_cls or getattr(mapped_cls, "resource_id", None) == resource_id:
                        discovered_codes.add(str(mapped_code))

            type_uuid = value[1] if isinstance(value, (tuple, list)) and len(value) >= 2 else ""
            for discovered_code in discovered_codes:
                type_code_mapping[discovered_code] = (resource_id, type_uuid)
    except Exception as exc:
        logger.debug(f"[反向映射表] 构建 Bioyond type code 映射失败: {exc}")

    try:
        from bioyond_peptide_station.resources import peptide_materials

        for mapped_code, mapped_cls in getattr(peptide_materials, "MATERIAL_TYPE_CODE_TO_CLASS", {}).items():
            resource_id = getattr(mapped_cls, "resource_id", None)
            if not resource_id or resource_id not in type_mapping:
                continue
            value = type_mapping.get(resource_id) or []
            type_uuid = value[1] if isinstance(value, (tuple, list)) and len(value) >= 2 else ""
            type_code_mapping[str(mapped_code)] = (resource_id, type_uuid)
    except Exception as exc:
        logger.debug(f"[反向映射表] 加载 peptide type code 映射失败: {exc}")

    logger.debug(f"[反向映射表] 共 {len(reverse_type_mapping)} 个条目: {list(reverse_type_mapping.keys())}")

    def _clean_mapping_key(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _material_type_code(material):
        code = _clean_mapping_key(
            material.get("typeCode")
            or material.get("materialTypeCode")
            or material.get("code")
            or material.get("materialCode")
        )
        if not code:
            return None
        return code.split("-", 1)[0]

    def _resolve_material_type(material):
        type_code = _material_type_code(material)
        if type_code and type_code in type_code_mapping:
            return type_code_mapping[type_code], "code", type_code

        if not type_code:
            return ("RegularContainer", ""), "regular_container_missing_code", None

        return ("RegularContainer", ""), "regular_container_unmapped_code", type_code


    def _iter_resource_names(resource):
        yield getattr(resource, "name", None)
        for child in getattr(resource, "children", []) or []:
            yield from _iter_resource_names(child)

    existing_names = {
        name for name in (_iter_resource_names(deck) if deck is not None else []) if name
    }

    def _is_synthesis_plate(resource):
        return getattr(resource, "model", None) == "bioyond_peptide_96_well_synthesis_plate"

    def _is_synthesis_base(resource):
        return getattr(resource, "model", None) == "bioyond_peptide_96_well_synthesis_plate_base"

    def _is_empty_itemized_plate(resource):
        model = str(getattr(resource, "model", "") or "")
        category = getattr(resource, "category", None)
        categories = category if isinstance(category, (list, tuple, set)) else [category]
        is_plate = "plate" in categories or model.startswith("bioyond_peptide_96_well_") or "384" in model
        return (
            is_plate
            and hasattr(resource, "get_item")
            and len(getattr(resource, "children", []) or []) == 0
        )

    def _clear_site_if_current(parent, child):
        sites = getattr(parent, "sites", None)
        if isinstance(sites, list):
            for site_idx, occupant in enumerate(sites):
                if occupant is child:
                    sites[site_idx] = None
                    break
        if child in getattr(parent, "children", []):
            parent.children.remove(child)
        if getattr(child, "parent", None) is parent:
            child.parent = None

    def _value_field(value, key):
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _legacy_bioyond_id_from_value(value, allow_payload_id=False):
        if value is None:
            return None
        material_id = _clean_mapping_key(_value_field(value, "material_bioyond_id"))
        if material_id:
            return material_id
        # 迁移兼容：旧资源树可能只有别名字段或嵌套原始载荷。
        for key in ("bioyond_id", "bioyond_material_id"):
            material_id = _clean_mapping_key(_value_field(value, key))
            if material_id:
                return material_id
        if allow_payload_id:
            for key in ("id", "materialId", "material_id"):
                material_id = _clean_mapping_key(_value_field(value, key))
                if material_id:
                    return material_id
        nested = _value_field(value, "bioyond_material")
        if nested is not None:
            material_id = _legacy_bioyond_id_from_value(nested, allow_payload_id=False)
            if material_id:
                return material_id
            raw_payload = _value_field(nested, "raw_payload")
            material_id = _legacy_bioyond_id_from_value(raw_payload, allow_payload_id=True)
            if material_id:
                return material_id
        raw_payload = _value_field(value, "raw_payload")
        if raw_payload is not None:
            material_id = _legacy_bioyond_id_from_value(raw_payload, allow_payload_id=True)
            if material_id:
                return material_id
        return None

    def _resource_bioyond_id(resource):
        extra_info = getattr(resource, "unilabos_extra", {}) or {}
        material_id = _clean_mapping_key(extra_info.get("material_bioyond_id"))
        if material_id:
            return material_id
        return _legacy_bioyond_id_from_value(extra_info) or _legacy_bioyond_id_from_value(resource)

    def _combined_parent_id(material):
        return _clean_mapping_key(
            material.get("combinedMaterialId")
            or material.get("combined_material_id")
            or material.get("combined_materialId")
        )

    def _first_location_key_from_locations(locations):
        if not isinstance(locations, list) or not locations:
            return None
        location = locations[0] or {}
        return (
            location.get("whName"),
            location.get("x"),
            location.get("y"),
            location.get("z"),
        )

    def _first_location_key_from_resource(resource):
        extra_info = getattr(resource, "unilabos_extra", {}) or {}
        return _first_location_key_from_locations(extra_info.get("bioyond_material_locations"))

    def _iter_resource_subtree(resource):
        yield resource
        for child in getattr(resource, "children", []) or []:
            yield from _iter_resource_subtree(child)

    resources_by_bioyond_id = {}
    if deck is not None:
        for resource in _iter_resource_subtree(deck):
            material_id = _resource_bioyond_id(resource)
            if material_id:
                resources_by_bioyond_id[material_id] = resource

    def _assign_child_to_parent_slot(parent, child):
        if getattr(child, "parent", None) is parent:
            return True
        if not hasattr(parent, "__setitem__"):
            logger.warning(
                f"[组合物料] 父物料 {getattr(parent, 'name', None)} 没有可挂载槽位，"
                f"跳过子物料 {getattr(child, 'name', None)}"
            )
            return False
        try:
            current = parent[0]
        except Exception as exc:
            logger.warning(
                f"[组合物料] 读取父物料 {getattr(parent, 'name', None)} 槽位失败，"
                f"跳过子物料 {getattr(child, 'name', None)}: {exc}"
            )
            return False
        if current is child:
            return True
        if isinstance(current, str):
            parent.sites[0] = None
        elif current is not None and current is not child:
            try:
                parent.unassign_child_resource(current)
            except Exception:
                _clear_site_if_current(parent, current)
        old_parent = getattr(child, "parent", None)
        if old_parent is not None:
            try:
                old_parent.unassign_child_resource(child)
            except Exception:
                _clear_site_if_current(old_parent, child)
        try:
            parent[0] = child
        except Exception as exc:
            logger.warning(
                f"[组合物料] 挂载子物料 {getattr(child, 'name', None)} 到 "
                f"{getattr(parent, 'name', None)} 失败: {exc}"
            )
            return False
        return getattr(child, "parent", None) is parent

    pending_combined_children = []

    def _try_attach_combined_children():
        attached_count = 0
        remaining = []
        for parent_id, child, child_location_key, placed_by_location in pending_combined_children:
            parent = resources_by_bioyond_id.get(parent_id)
            if parent is None:
                remaining.append((parent_id, child, child_location_key, placed_by_location))
                continue
            parent_location_key = _first_location_key_from_resource(parent)
            if child_location_key is not None and child_location_key != parent_location_key:
                logger.debug(
                    f"[组合物料] 子物料 {getattr(child, 'name', None)} "
                    f"combinedMaterialId={parent_id} 但自身位置 {child_location_key} "
                    f"不同于父物料位置 {parent_location_key}，按自身 locations[] 放置"
                )
                continue
            if _assign_child_to_parent_slot(parent, child):
                attached_count += 1
                logger.info(
                    f"[组合物料] 子物料 {getattr(child, 'name', None)} "
                    f"按 combinedMaterialId={parent_id} 挂载到父物料 {getattr(parent, 'name', None)}"
                )
            else:
                remaining.append((parent_id, child, child_location_key, placed_by_location))
        pending_combined_children[:] = remaining
        return attached_count

    def _assign_plate_to_synthesis_base(base, plate):
        if getattr(plate, "parent", None) is base:
            return True
        current = base[0] if hasattr(base, "__getitem__") else None
        if current is plate:
            return True
        if isinstance(current, str):
            base.sites[0] = None
        elif current is not None and current is not plate:
            try:
                base.unassign_child_resource(current)
            except Exception:
                _clear_site_if_current(base, current)
        parent = getattr(plate, "parent", None)
        if parent is not None:
            try:
                parent.unassign_child_resource(plate)
            except Exception:
                _clear_site_if_current(parent, plate)
        base[0] = plate
        return getattr(plate, "parent", None) is base

    def _place_synthesis_base_plate_combo(warehouse, idx, current_resource, incoming_resource, wh_name, slot_key):
        if _is_synthesis_base(current_resource) and _is_synthesis_plate(incoming_resource):
            assigned = _assign_plate_to_synthesis_base(current_resource, incoming_resource)
            if assigned:
                logger.info(
                    f"✅ 96孔固相合成板 {incoming_resource.name} 挂载到 "
                    f"{current_resource.name} 的槽位 A1，外部库位保持为底座 "
                    f"{wh_name}[{idx}]{f'({slot_key})' if slot_key else ''}"
                )
            return assigned

        if _is_synthesis_plate(current_resource) and _is_synthesis_base(incoming_resource):
            try:
                warehouse.unassign_child_resource(current_resource)
            except Exception:
                _clear_site_if_current(warehouse, current_resource)
            warehouse[idx] = incoming_resource
            assigned = _assign_plate_to_synthesis_base(incoming_resource, current_resource)
            if assigned:
                logger.info(
                    f"✅ 96孔固相合成板底座 {incoming_resource.name} 占用 "
                    f"{wh_name}[{idx}]{f'({slot_key})' if slot_key else ''}，"
                    f"并挂载原固相合成板 {current_resource.name}"
                )
            return assigned

        return False

    # 用于跟踪同名物料的计数器
    name_counter = {}

    def _material_display_name(material):
        return _clean_mapping_key(
            material.get("name")
            or material.get("materialName")
            or material.get("material_name")
        ) or ""

    def _material_code(material):
        return _clean_mapping_key(
            material.get("code")
            or material.get("materialCode")
            or material.get("material_code")
            or material.get("barCode")
        ) or ""

    def _internal_material_name(material):
        base_name = _material_display_name(material)
        code = _material_code(material)
        if not code:
            return base_name
        suffix = code.split("-", 1)[1] if "-" in code else code
        suffix = suffix.strip()
        return f"{base_name}-{suffix}" if suffix and base_name else (base_name or suffix)

    def _resolved_type_name(class_name, material):
        if class_name == "RegularContainer":
            return "RegularContainer"
        raw_type_name = _clean_mapping_key(material.get("typeName") or material.get("materialTypeName")) or ""
        try:
            from bioyond_peptide_station.resources import peptide_materials

            resource_cls = peptide_materials.get_material_class_by_type_code(_material_type_code(material))
            if resource_cls is not None:
                return (
                    getattr(resource_cls, "bioyond_material_type_name", None)
                    or raw_type_name
                    or class_name
                )
        except Exception:
            pass
        return raw_type_name or class_name

    for material in bioyond_materials:
        # 类型锚定使用 code/materialCode 前缀；缺码或未知码时退回 RegularContainer，
        # 不使用显示名或物料名猜测成具体类型。
        type_info, type_source, type_value = _resolve_material_type(material)
        className = type_info[0]
        fallback_warning = ""
        if className == "RegularContainer":
            fallback_warning = (
                "missing_material_code_fallback_regular_container"
                if type_source == "regular_container_missing_code"
                else f"unmapped_material_type_code_fallback_regular_container:{type_value}"
            )
            logger.warning(
                "[物料类型映射] Bioyond 物料退回 RegularContainer: "
                f"id={material.get('id') or material.get('materialId')!r}, "
                f"name={_material_display_name(material)!r}, "
                f"typeName={material.get('typeName') or material.get('materialTypeName')!r}, "
                f"code={material.get('code') or material.get('materialCode')!r}, "
                f"reason={fallback_warning}"
            )

        # 内部资源名必须唯一，使用 Bioyond 显示名 + 物料编码尾缀；展示仍用原始名。
        base_name = _internal_material_name(material)
        next_index = name_counter.get(base_name, 1)
        unique_name = base_name if next_index == 1 else f"{base_name}_{next_index}"
        while unique_name in existing_names:
            next_index += 1
            unique_name = f"{base_name}_{next_index}"
        name_counter[base_name] = next_index + 1
        existing_names.add(unique_name)

        if className == "RegularContainer":
            plr_material_result = RegularContainer(name=unique_name)
        else:
            plr_material_result = initialize_resource(
                {"name": unique_name, "class": className}, resource_type=ResourcePLR
            )

        # initialize_resource 可能返回列表或单个对象
        if isinstance(plr_material_result, list):
            if len(plr_material_result) == 0:
                logger.warning(f"物料 {_material_display_name(material)} 初始化失败，跳过")
                continue
            plr_material = plr_material_result[0]
        else:
            plr_material = plr_material_result

        # 确保 plr_material 是 ResourcePLR 实例
        if not isinstance(plr_material, ResourcePLR):
            logger.warning(f"物料 {unique_name} 不是有效的 ResourcePLR 实例，类型: {type(plr_material)}")
            continue

        material_code = _material_code(material)
        material_id = material.get("id") or material.get("materialId")
        raw_name = _material_display_name(material)
        type_code = _material_type_code(material) or ""
        resolved_type_name = _resolved_type_name(className, material)
        locations = copy.deepcopy(material.get("locations") or [])
        details = copy.deepcopy(material.get("detail") or material.get("details") or [])
        plr_material.code = material.get("barCode") or material_code
        plr_material.unilabos_uuid = str(uuid.uuid4())

        # ⭐ 保存 Bioyond 原始信息到 unilabos_extra（用于出库时查询）
        # 构造器可能已写入前端展示/位姿元数据，保留后再覆盖 Bioyond 同名字段。
        existing_extra = dict(getattr(plr_material, "unilabos_extra", {}) or {})
        bioyond_extra = {
            "material_bioyond_id": material_id,                  # Bioyond 物料 UUID
            "material_bioyond_name": raw_name,                   # Bioyond 原始名称（如 "MDA"）
            "material_bioyond_type": material.get("typeName") or material.get("materialTypeName"),
            "material_bioyond_code": material_code,
            "bioyond_material_type_code": type_code,
            "bioyond_resolved_type_name": resolved_type_name,
            "bioyond_material_locations": locations,
            "bioyond_regular_container_fallback": className == "RegularContainer",
            "bioyond_material_warnings": (
                list(material.get("_bioyond_material_warnings") or [])
                + ([fallback_warning] if fallback_warning and fallback_warning not in (material.get("_bioyond_material_warnings") or []) else [])
            ),
            "bioyond_material": {
                "bioyond_id": material_id,
                "raw_name": raw_name,
                "display_name": raw_name,
                "code": material_code,
                "material_code": material_code,
                "type_code": type_code,
                "type_id": material.get("typeId") or material.get("materialTypeId"),
                "raw_type_name": material.get("typeName") or material.get("materialTypeName"),
                "resolved_type_name": resolved_type_name,
                "resolved_resource_id": className,
                "locations": locations,
                "details": details,
                "combinedMaterialId": material.get("combinedMaterialId"),
                "parameters": copy.deepcopy(material.get("parameters") or material.get("materialParams")),
                "status": material.get("status"),
                "quantity": material.get("quantity"),
                "lockQuantity": material.get("lockQuantity"),
                "unit": material.get("unit"),
                "raw_payload": copy.deepcopy(material),
                "regular_container_fallback": className == "RegularContainer",
                "fallback_reason": (
                    material.get("_bioyond_regular_container_reason")
                    or ("missing_material_code" if type_source == "regular_container_missing_code" else "")
                    or (f"unmapped_material_type_code:{type_value}" if type_source == "regular_container_unmapped_code" else "")
                ),
            },
        }
        existing_extra.update(bioyond_extra)
        plr_material.unilabos_extra = existing_extra

        logger.debug(
            f"[转换物料] {raw_name} (ID:{material.get('id')}) → {unique_name} "
            f"(类型:{className}, 来源:{type_source}={type_value})"
        )

        # 处理子物料（detail）
        if material.get("detail") and len(material["detail"]) > 0:
            has_itemized_children = (
                hasattr(plr_material, "get_item")
                and hasattr(plr_material, "num_items")
                and len(getattr(plr_material, "children", []) or []) > 0
            )
            skip_physical_details = _is_empty_itemized_plate(plr_material)
            if skip_physical_details:
                logger.debug(
                    f"  └─ [子物料] {plr_material.name} 是无 well 子节点板，"
                    f"保留 {len(material['detail'])} 条 Bioyond detail 到 unilabos_extra"
                )
            elif not has_itemized_children:
                for bottle in reversed(plr_material.children):
                    plr_material.unassign_child_resource(bottle)
            child_ids = []

            # 确定detail物料的默认类型
            # 样品板的detail通常是样品瓶
            parent_type_name = str(material.get("typeName") or material.get("materialTypeName") or "")
            default_detail_type = "样品瓶" if "样品板" in parent_type_name else None

            for detail in [] if skip_physical_details else material["detail"]:
                number = (
                    (detail.get("z", 0) - 1) * plr_material.num_items_x * plr_material.num_items_y
                    + (detail.get("y", 0) - 1) * plr_material.num_items_y
                    + (detail.get("x", 0) - 1)
                )

                # 检查索引是否超出范围
                if has_itemized_children:
                    max_index = len(plr_material.children) - 1
                else:
                    max_index = plr_material.num_items_x * plr_material.num_items_y - 1
                if number < 0 or number > max_index:
                    logger.warning(
                        f"  └─ [子物料警告] {detail['name']} 的坐标 (x={detail.get('x')}, y={detail.get('y')}, z={detail.get('z')}) "
                        f"计算出索引 {number} 超出载架范围 [0-{max_index}] (布局: {plr_material.num_items_x}×{plr_material.num_items_y})，跳过"
                    )
                    continue

                # detail可能没有typeName，尝试从name推断，或使用默认类型
                typeName = detail.get("typeName")

                # 如果没有typeName，尝试根据父物料类型和位置推断
                if not typeName:
                    if "分装板" in parent_type_name:
                        # 分装板: 根据行(x)判断类型
                        # 第一行(x=1)是10%分装小瓶，第二行(x=2)是90%分装小瓶
                        x_pos = detail.get("x", 0)
                        y_pos = detail.get("y", 0)
                        # logger.debug(f"  └─ [推断类型] {detail['name']} 坐标(x={x_pos}, y={y_pos})")
                        if x_pos == 1:
                            typeName = "10%分装小瓶"
                        elif x_pos == 2:
                            typeName = "90%分装小瓶"
                        # logger.debug(f"  └─ [推断结果] {detail['name']} → {typeName}")
                    else:
                        typeName = default_detail_type

                if has_itemized_children:
                    target_items = plr_material[number]
                    target = target_items[0] if target_items else None
                    if target is not None:
                        if hasattr(target, "tracker"):
                            target.tracker.liquids = [
                                (detail["name"], float(detail.get("quantity", 0)) if detail.get("quantity") else 0)
                            ]
                        if detail.get("code"):
                            target.code = detail.get("code", "")
                        logger.debug(
                            f"  └─ [子物料] {detail['name']} → {plr_material.name}[{number}] "
                            f"(保留默认孔位/储液槽，类型:{typeName})"
                        )
                    continue

                if typeName and typeName in reverse_type_mapping:
                    bottle = plr_material[number] = initialize_resource(
                        {"name": f'{detail["name"]}_{number}', "class": reverse_type_mapping[typeName][0]}, resource_type=ResourcePLR
                    )
                    bottle.tracker.liquids = [
                        (detail["name"], float(detail.get("quantity", 0)) if detail.get("quantity") else 0)
                    ]
                    bottle.code = detail.get("code", "")
                    logger.debug(f"  └─ [子物料] {detail['name']} → {plr_material.name}[{number}] (类型:{typeName})")
                else:
                    logger.warning(f"  └─ [子物料警告] {detail['name']} 的类型 '{typeName}' 不在mapping中，跳过")
        else:
            # 只对有 capacity 属性的容器（液体容器）处理液体追踪
            if hasattr(plr_material, 'capacity'):
                liquid_target = None
                if hasattr(plr_material, "tracker"):
                    liquid_target = plr_material
                elif plr_material.capacity > 0:
                    candidate = plr_material[0]
                    if hasattr(candidate, "tracker"):
                        liquid_target = candidate

                if liquid_target is not None:
                    liquid_target.tracker.liquids = [
                        (raw_name, float(material.get("quantity", 0)) if material.get("quantity") else 0)
                    ]
                else:
                    logger.debug(
                        f"物料 {unique_name} 类型 {type(plr_material).__name__} 没有液体 tracker，跳过液体追踪"
                    )

        plr_materials.append(plr_material)
        material_bioyond_id = _resource_bioyond_id(plr_material)
        if material_bioyond_id:
            resources_by_bioyond_id[material_bioyond_id] = plr_material
        _try_attach_combined_children()

        combined_parent_id = _combined_parent_id(material)
        if combined_parent_id:
            child_location_key = _first_location_key_from_locations(locations)
            # 先查本批次（含预扫 deck）的映射，未命中再退回外部 lookup，
            # 以便父物料已在 deck 上但不在当前批次时也能找到。
            parent = resources_by_bioyond_id.get(combined_parent_id)
            if parent is None:
                parent = _resolve_parent(combined_parent_id)
            if parent is not None:
                parent_location_key = _first_location_key_from_resource(parent)
                if child_location_key is None or child_location_key == parent_location_key:
                    if _assign_child_to_parent_slot(parent, plr_material):
                        logger.info(
                            f"[组合物料] 物料 {unique_name} 按 combinedMaterialId={combined_parent_id} "
                            f"挂载到父物料 {getattr(parent, 'name', None)}，跳过自身 locations[] 放置"
                        )
                        continue
                else:
                    logger.debug(
                        f"[组合物料] 物料 {unique_name} combinedMaterialId={combined_parent_id} "
                        f"但自身位置 {child_location_key} 不同于父物料位置 {parent_location_key}，"
                        "继续按自身 locations[] 放置"
                    )
            elif child_location_key is None:
                pending_combined_children.append((combined_parent_id, plr_material, child_location_key, False))
                _try_attach_combined_children()
                if getattr(plr_material, "parent", None) is None:
                    logger.warning(
                        f"[组合物料] 物料 {unique_name} 声明 combinedMaterialId={combined_parent_id} "
                        "且没有 locations[]，等待父物料出现后挂载"
                    )
                continue
            else:
                pending_combined_children.append((combined_parent_id, plr_material, child_location_key, True))
                _try_attach_combined_children()
                if getattr(plr_material, "parent", None) is not None:
                    continue

        if deck and hasattr(deck, "warehouses"):
            locations = material.get("locations", [])
            if not locations:
                logger.debug(f"[物料位置] {unique_name} 没有location信息，跳过warehouse放置")

            # ⭐ 预先检查：如果物料的任何location在竖向warehouse中，提前交换尺寸
            # 这样可以避免多个location时尺寸不一致的问题
            needs_size_swap = False
            for loc in locations:
                wh_name_check = loc.get("whName")
                if wh_name_check in ["站内试剂存放堆栈", "测量小瓶仓库(测密度)"]:
                    needs_size_swap = True
                    break

            if needs_size_swap and hasattr(plr_material, 'size_x') and hasattr(plr_material, 'size_y'):
                original_x = plr_material.size_x
                original_y = plr_material.size_y
                plr_material.size_x = original_y
                plr_material.size_y = original_x
                logger.debug(f"   物料 {unique_name} 将放入竖向warehouse，预先交换尺寸: {original_x}×{original_y} → {plr_material.size_x}×{plr_material.size_y}")

            for loc in locations:
                wh_name = loc.get("whName")
                logger.debug(f"[物料位置] {unique_name} 尝试放置到 warehouse: {wh_name} (Bioyond坐标: x={loc.get('x')}, y={loc.get('y')}, z={loc.get('z')})")

                # 特殊处理: Bioyond的"堆栈1"需要映射到"堆栈1左"或"堆栈1右"
                # 根据列号(x)判断: 1-4映射到左侧, 5-8映射到右侧
                if wh_name == "堆栈1":
                    x_val = loc.get("x", 1)
                    if 1 <= x_val <= 4:
                        wh_name = "堆栈1左"
                    elif 5 <= x_val <= 8:
                        wh_name = "堆栈1右"
                    else:
                        logger.warning(f"物料 {raw_name} 的列号 x={x_val} 超出范围，无法映射到堆栈1左或堆栈1右")
                        continue

                # 特殊处理: Bioyond的"站内Tip盒堆栈"也需要进行拆分映射
                if wh_name == "站内Tip盒堆栈":
                    y_val = loc.get("y", 1)
                    if y_val == 1:
                        wh_name = "站内Tip盒堆栈(右)"
                    elif y_val in [2, 3]:
                        wh_name = "站内Tip盒堆栈(左)"
                        loc = dict(loc)
                        loc["y"] = y_val - 1  # 左侧仓库 Bioyond y=2 对应第1列

                if hasattr(deck, "warehouses") and wh_name in deck.warehouses:
                    warehouse = deck.warehouses[wh_name]
                    logger.debug(f"[Warehouse匹配] 找到warehouse: {wh_name} (容量: {warehouse.capacity}, 行×列: {warehouse.num_items_x}×{warehouse.num_items_y})")

                    # Bioyond坐标映射:
                    # - 历史 row_col 仓库中 x/y 直接按行/列参与索引。
                    # - Sirna 的库位标签为 col-row，stock-material 返回 x=标签第二段、y=标签第一段。
                    #   因此 x=13,y=4 应落到 key=4-13，而不是交换后落到 3-5。
                    x = loc.get("x", 1)
                    y = loc.get("y", 1)
                    z = loc.get("z", 1)  # 层号 (1-based, 通常为1)

                    # 仓库级别的轴约定覆盖。
                    # 对旧的 row-col 视觉标签，bioyond_axis="xy_col_row" 需要交换 x/y。
                    # 对 Sirna 的 col-row 库位标签，原始 x/y 已能直接索引到 code 对应位置，不再交换。
                    bioyond_axis = getattr(warehouse, "bioyond_axis", "xy_row_col")
                    bioyond_key_axis = getattr(warehouse, "bioyond_key_axis", "row_col")
                    if bioyond_axis == "xy_col_row" and bioyond_key_axis != "col_row":
                        x, y = y, x

                    # 如果是右侧堆栈，需要调整列号 (5→1, 6→2, 7→3, 8→4)
                    if wh_name == "堆栈1右":
                        y = y - 4  # 将5-8映射到1-4

                    # 特殊处理竖向warehouse（站内试剂存放堆栈、测量小瓶仓库）
                    # 这些warehouse使用 vertical-col-major 布局
                    if wh_name in ["站内试剂存放堆栈", "测量小瓶仓库(测密度)"]:
                        # vertical-col-major 布局的坐标映射：
                        # - Bioyond的x(1=A,2=B)对应warehouse的列(col, x方向)
                        # - Bioyond的y(1=01,2=02,3=03)对应warehouse的行(row, y方向)，从下到上
                        # vertical-col-major 中: row=0 对应底部，row=n-1 对应顶部
                        # Bioyond y=1(01) 对应底部 → row=0, y=2(02) 对应中间 → row=1
                        # 索引计算: idx = row * num_cols + col
                        col_idx = x - 1  # Bioyond的x(A,B) → col索引(0,1)
                        row_idx = y - 1  # Bioyond的y(01,02,03) → row索引(0,1,2)
                        layer_idx = z - 1

                        idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_y + col_idx
                        logger.debug(f"🔍 竖向warehouse {wh_name}: Bioyond(x={x},y={y},z={z}) → warehouse(col={col_idx},row={row_idx},layer={layer_idx}) → idx={idx}, capacity={warehouse.capacity}")

                    # 普通横向warehouse的处理
                    else:
                        # 多行warehouse: 根据 layout 使用不同的索引计算
                        row_idx = x - 1  # x表示行: 转为0-based
                        col_idx = y - 1  # y表示列: 转为0-based
                        layer_idx = z - 1  # 转为0-based

                        # 检查 warehouse 的排序方式属性
                        ordering_layout = getattr(warehouse, 'ordering_layout', 'col-major')
                        logger.debug(f"🔍 Warehouse {wh_name} layout检测: hasattr={hasattr(warehouse, 'ordering_layout')}, ordering_layout值='{ordering_layout}', warehouse类型={type(warehouse).__name__}")

                        if ordering_layout == "row-major":
                            # 行优先: A01,A02,A03,A04, B01,B02,B03,B04 (所有Bioyond堆栈)
                            # 索引计算: idx = (row) * num_cols + (col) + (layer) * (rows * cols)
                            idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_x + col_idx
                            logger.debug(f"行优先warehouse {wh_name}: x={x}(行),y={y}(列) → row={row_idx},col={col_idx} → idx={idx}")
                        else:
                            # 列优先 (后备): A01,B01,C01,D01, A02,B02,C02,D02
                            # 索引计算: idx = (col) * num_rows + (row) + (layer) * (rows * cols)
                            idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + col_idx * warehouse.num_items_y + row_idx
                            logger.debug(f"列优先warehouse {wh_name}: x={x}(行),y={y}(列) → row={row_idx},col={col_idx} → idx={idx}")

                    if 0 <= idx < warehouse.capacity:
                        slot_key = None
                        ordering = getattr(warehouse, "_ordering", {})
                        sites = getattr(warehouse, "sites", [])
                        if isinstance(ordering, dict) and idx < len(sites):
                            site_at_idx = sites[idx]
                            slot_key = next(
                                (key for key, site in ordering.items() if site is site_at_idx),
                                None,
                            )

                        current_resource = warehouse[idx]
                        if current_resource is None or isinstance(current_resource, (ResourceHolder, str)):
                            if isinstance(current_resource, str):
                                logger.warning(
                                    f"⚠️ 物料 {unique_name} 覆盖 {wh_name}[{idx}]"
                                    f"{f'({slot_key})' if slot_key else ''} 的旧占位 occupied_by={current_resource!r}"
                                )
                                warehouse.sites[idx] = None
                            # 物料尺寸已在放入warehouse前根据需要进行了交换
                            warehouse[idx] = plr_material
                            logger.debug(
                                f"✅ 物料 {unique_name} 放置到 {wh_name}[{idx}]"
                                f"{f'({slot_key})' if slot_key else ''} "
                                f"(Bioyond坐标: x={loc.get('x')}, y={loc.get('y')})"
                            )
                        else:
                            if _place_synthesis_base_plate_combo(
                                warehouse,
                                idx,
                                current_resource,
                                plr_material,
                                wh_name,
                                slot_key,
                            ):
                                continue
                            parent = getattr(current_resource, "parent", None)
                            current_repr = repr(current_resource)
                            current_len = len(current_resource) if isinstance(current_resource, str) else None
                            logger.warning(
                                f"⚠️ 物料 {unique_name} 跳过放置到 {wh_name}[{idx}]"
                                f"{f'({slot_key})' if slot_key else ''}：目标库位已有 "
                                f"{type(current_resource).__name__}"
                                f"(value={current_repr}, len={current_len})"
                                f"(name={getattr(current_resource, 'name', None)}, "
                                f"parent={getattr(parent, 'name', None)}, "
                                f"uuid={getattr(current_resource, 'unilabos_uuid', None)})"
                            )
                    else:
                        logger.warning(f"❌ 物料 {unique_name} 的索引 {idx} 超出仓库 {wh_name} 容量 {warehouse.capacity}")
                else:
                    if wh_name:
                        logger.warning(f"❌ 物料 {unique_name} 的warehouse '{wh_name}' 在deck中不存在。可用warehouses: {list(deck.warehouses.keys()) if hasattr(deck, 'warehouses') else '无'}")

    _try_attach_combined_children()
    for parent_id, child, _child_location_key, placed_by_location in pending_combined_children:
        if placed_by_location:
            logger.warning(
                f"[组合物料] 子物料 {getattr(child, 'name', None)} 的父物料 "
                f"combinedMaterialId={parent_id} 未找到，已保留自身 locations[] 放置结果"
            )
        else:
            logger.warning(
                f"[组合物料] 子物料 {getattr(child, 'name', None)} 的父物料 "
                f"combinedMaterialId={parent_id} 未找到，且子物料没有 locations[]，未放置到 deck"
            )

    return plr_materials


def resource_plr_to_bioyond(plr_resources: list[ResourcePLR], type_mapping: dict = {}, warehouse_mapping: dict = {}, material_params: dict = {}) -> list[dict]:
    """
    将 PyLabRobot 资源转换为 Bioyond 格式

    Args:
        plr_resources: PyLabRobot 资源列表
        type_mapping: 物料类型映射字典
        warehouse_mapping: 仓库映射字典
        material_params: 物料默认参数字典 (格式: {物料名称: {参数字典}})

    Returns:
        Bioyond 格式的物料列表
    """
    bioyond_materials = []

    # 定义不需要发送 details 的载架类型
    # 说明：这些载架上自带试剂瓶或烧杯，作为整体物料上传即可，不需要在 details 中重复上传子物料
    CARRIERS_WITHOUT_DETAILS = {
        "BIOYOND_PolymerStation_1BottleCarrier",  # 聚合站-单试剂瓶载架
        "BIOYOND_PolymerStation_1FlaskCarrier",   # 聚合站-单烧杯载架
    }

    for resource in plr_resources:
        if isinstance(resource, BottleCarrier) and resource.capacity > 1:
            # 获取 BottleCarrier 的类型映射
            type_info = type_mapping.get(resource.model)
            if not type_info:
                logger.error(f"❌ [PLR→Bioyond] BottleCarrier 资源 '{resource.name}' 的 model '{resource.model}' 不在 type_mapping 中")
                logger.debug(f"[PLR→Bioyond] 可用的 type_mapping 键: {list(type_mapping.keys())}")
                raise ValueError(f"资源 model '{resource.model}' 未在 MATERIAL_TYPE_MAPPINGS 中配置")

            material = {
                "typeId": type_info[1],
                "code": "",
                "barCode": "",
                "name": resource.name,
                "unit": "个",
                "quantity": 1,
                "details": [],
                "Parameters": "{}"  # API 实际要求的字段（必需）
            }

            # 如果是自带试剂瓶的载架类型，不处理子物料（details留空）
            if resource.model in CARRIERS_WITHOUT_DETAILS:
                logger.info(f"[PLR→Bioyond] 载架 '{resource.name}' (model: {resource.model}) 自带试剂瓶，不添加 details")
            else:
                # 处理其他载架类型的子物料
                for bottle in resource.children:
                    if isinstance(resource, ItemizedCarrier):
                        # ⭐ 优化：直接使用 get_child_identifier 获取真实的子物料坐标
                        # 这个方法会遍历 resource.children 找到 bottle 对象的实际位置
                        site = resource.get_child_identifier(bottle)

                        # 🔧 如果 get_child_identifier 失败或返回无效坐标 (0,0)
                        # 这通常发生在子物料名称使用纯数字后缀时（如 "BTDA_0", "BTDA_4"）
                        if not site or (site.get("x") == 0 and site.get("y") == 0):
                            # 方法1: 尝试从名称中提取标识符并解析
                            bottle_identifier = None
                            if "_" in bottle.name:
                                bottle_identifier = bottle.name.split("_")[-1]

                            # 只有非纯数字标识符才尝试解析（如 "A1", "B2"）
                            if bottle_identifier and not bottle_identifier.isdigit():
                                try:
                                    x_idx, y_idx, z_idx = resource._parse_identifier_to_indices(bottle_identifier, 0)
                                    site = {"x": x_idx, "y": y_idx, "z": z_idx, "identifier": bottle_identifier}
                                    logger.debug(f"  🔧 [坐标修正-方法1] 从名称 {bottle.name} 解析标识符 {bottle_identifier} → ({x_idx}, {y_idx})")
                                except Exception as e:
                                    logger.warning(f"  ⚠️ [坐标解析] 标识符 {bottle_identifier} 解析失败: {e}")

                            # 方法2: 如果方法1失败，使用线性索引反推坐标
                            if not site or (site.get("x") == 0 and site.get("y") == 0):
                                # 找到bottle在children中的索引位置
                                try:
                                    # 遍历所有槽位找到bottle的实际位置
                                    for idx in range(resource.num_items_x * resource.num_items_y):
                                        if resource[idx] is bottle:
                                            # 根据载架布局计算行列坐标
                                            # ItemizedCarrier 默认是列优先布局 (A1,B1,C1,D1, A2,B2,C2,D2...)
                                            col_idx = idx // resource.num_items_y  # 列索引 (0-based)
                                            row_idx = idx % resource.num_items_y   # 行索引 (0-based)
                                            site = {"x": col_idx, "y": row_idx, "z": 0, "identifier": str(idx)}
                                            logger.debug(f"  🔧 [坐标修正-方法2] {bottle.name} 在索引 {idx} → 列={col_idx}, 行={row_idx}")
                                            break
                                except Exception as e:
                                    logger.error(f"  ❌ [坐标计算失败] {bottle.name}: {e}")
                                    # 最后的兜底：使用 (0,0)
                                    site = {"x": 0, "y": 0, "z": 0, "identifier": ""}
                    else:
                        site = {"x": bottle.location.x - 1, "y": bottle.location.y - 1, "identifier": ""}

                    # 获取子物料的类型映射
                    bottle_type_info = type_mapping.get(bottle.model)
                    if not bottle_type_info:
                        logger.error(f"❌ [PLR→Bioyond] 子物料 '{bottle.name}' 的 model '{bottle.model}' 不在 type_mapping 中")
                        raise ValueError(f"子物料 model '{bottle.model}' 未在 MATERIAL_TYPE_MAPPINGS 中配置")

                    # ⚠️ 坐标系转换说明:
                    # _parse_identifier_to_indices 返回: x=列索引, y=行索引 (0-based)
                    # Bioyond 系统要求: x=行号, y=列号 (1-based)
                    # 因此需要交换 x 和 y!
                    bioyond_x = site["y"] + 1  # 行索引 → Bioyond的x (行号)
                    bioyond_y = site["x"] + 1  # 列索引 → Bioyond的y (列号)

                    # 🐛 调试日志
                    logger.debug(f"🔍 [PLR→Bioyond] detail转换: {bottle.name} → PLR(x={site['x']},y={site['y']},id={site.get('identifier','?')}) → Bioyond(x={bioyond_x},y={bioyond_y})")

                    # 🔥 提取物料名称：从 tracker.liquids 中获取第一个液体的名称（去除PLR系统添加的后缀）
                    # tracker.liquids 格式: [(物料名称, 数量, 单位), ...]
                    material_name = bottle_type_info[0]  # 默认使用类型名称（如"样品瓶"）
                    if hasattr(bottle, "tracker") and bottle.tracker.liquids:
                        # 如果有液体，使用液体的名称
                        first_liquid_name = bottle.tracker.liquids[0][0]
                        # 去除PLR系统为了唯一性添加的后缀（如 "_0", "_1" 等）
                        if "_" in first_liquid_name and first_liquid_name.split("_")[-1].isdigit():
                            material_name = "_".join(first_liquid_name.split("_")[:-1])
                        else:
                            material_name = first_liquid_name
                        logger.debug(f"  💧 [物料名称] {bottle.name} 液体: {first_liquid_name} → 转换为: {material_name}")
                    else:
                        logger.debug(f"  📭 [物料名称] {bottle.name} 无液体，使用类型名: {material_name}")

                    detail_item = {
                        "typeId": bottle_type_info[1],
                        "code": bottle.code if hasattr(bottle, "code") else "",
                        "name": material_name,  # 使用物料名称（如"9090"），而不是类型名称（"样品瓶"）
                        "quantity": sum(qty for _, qty, *_ in bottle.tracker.liquids) if hasattr(bottle, "tracker") else 0,
                        "x": bioyond_x,
                        "y": bioyond_y,
                        "z": 1,
                        "unit": "微升",
                        "Parameters": "{}"  # API 实际要求的字段（必需）
                    }
                    material["details"].append(detail_item)
        else:
            # 单个瓶子(非载架)类型的资源
            bottle = resource[0] if hasattr(resource, "capacity") and resource.capacity > 0 else resource

            # 根据 resource.model 从 type_mapping 获取正确的 typeId
            type_info = type_mapping.get(resource.model)
            if type_info:
                type_id = type_info[1]
            else:
                # 如果找不到映射，记录警告并使用默认值
                logger.warning(f"[PLR→Bioyond] 资源 {resource.name} 的 model '{resource.model}' 不在 type_mapping 中，使用默认烧杯类型")
                type_id = "3a14196b-24f2-ca49-9081-0cab8021bf1a"  # 默认使用烧杯类型

            # 🔥 提取物料名称：优先使用液体名称，否则使用资源名称
            material_name = resource.name if hasattr(resource, "name") else ""
            if hasattr(bottle, "tracker") and bottle.tracker.liquids:
                # 如果有液体，使用液体的名称
                first_liquid_name = bottle.tracker.liquids[0][0]
                # 去除PLR系统为了唯一性添加的后缀（如 "_0", "_1" 等）
                if "_" in first_liquid_name and first_liquid_name.split("_")[-1].isdigit():
                    material_name = "_".join(first_liquid_name.split("_")[:-1])
                else:
                    material_name = first_liquid_name
                logger.debug(f"  💧 [单瓶物料] {resource.name} 液体: {first_liquid_name} → 转换为: {material_name}")
            else:
                logger.debug(f"  📭 [单瓶物料] {resource.name} 无液体，使用资源名: {material_name}")

            # 🎯 处理物料默认参数和单位
            # 优先级: typeId参数 > 物料名称参数 > 默认值
            default_unit = "个"  # 默认单位
            material_parameters = {}

            # 1️⃣ 首先检查是否有 typeId 对应的参数配置（从 material_params 中获取，key 格式为 "type:<typeId>"）
            type_params_key = f"type:{type_id}"
            if type_params_key in material_params:
                params_config = material_params[type_params_key].copy()

                # 提取 unit 字段（如果有）
                if "unit" in params_config:
                    default_unit = params_config.pop("unit")  # 从参数中移除，放到外层

                # 剩余的字段放入 Parameters
                material_parameters = params_config
                logger.debug(f"  🔧 [物料参数-按typeId] 为 typeId={type_id[:8]}... 应用配置: unit={default_unit}, parameters={material_parameters}")
            # 2️⃣ 其次检查是否有该物料名称的默认参数配置
            elif material_name in material_params:
                params_config = material_params[material_name].copy()

                # 提取 unit 字段（如果有）
                if "unit" in params_config:
                    default_unit = params_config.pop("unit")  # 从参数中移除，放到外层

                # 剩余的字段放入 Parameters
                material_parameters = params_config
                logger.debug(f"  🔧 [物料参数-按名称] 为 {material_name} 应用配置: unit={default_unit}, parameters={material_parameters}")

            # 转换为 JSON 字符串
            parameters_json = json.dumps(material_parameters) if material_parameters else "{}"

            material = {
                "typeId": type_id,
                "code": "",
                "barCode": "",
                "name": material_name,  # 使用物料名称而不是资源名称
                "unit": default_unit,  # 使用配置的单位或默认单位
                "quantity": sum(qty for _, qty, *_ in bottle.tracker.liquids) if hasattr(bottle, "tracker") else 0,
                "Parameters": parameters_json  # API 实际要求的字段（必需）
            }

        # ⭐ 处理 locations 信息
        # 优先级: update_resource_site (位置更新请求) > 当前 parent 位置
        extra_info = getattr(resource, "unilabos_extra", {})
        update_site = extra_info.get("update_resource_site")

        if update_site:
            # 情况1: 有明确的位置更新请求 (如从 A02 移动到 A03)
            # 需要从 warehouse_mapping 中查找目标库位的 UUID
            logger.debug(f"🔄 [PLR→Bioyond] 检测到位置更新请求: {resource.name} → {update_site}")

            # 遍历所有仓库查找目标库位
            target_warehouse_name = None
            target_location_uuid = None

            for warehouse_name, warehouse_info in warehouse_mapping.items():
                site_uuids = warehouse_info.get("site_uuids", {})
                if update_site in site_uuids:
                    target_warehouse_name = warehouse_name
                    target_location_uuid = site_uuids[update_site]
                    break

            if target_warehouse_name and target_location_uuid:
                # 从库位代码解析坐标 (如 "A03" -> x=1, y=3)
                # A=1, B=2, C=3, D=4...
                # 01=1, 02=2, 03=3...
                try:
                    row_letter = update_site[0]  # 'A', 'B', 'C', 'D'
                    col_number = int(update_site[1:])  # '01', '02', '03'...
                    bioyond_x = ord(row_letter) - ord('A') + 1  # A→1, B→2, C→3, D→4
                    bioyond_y = col_number  # 01→1, 02→2, 03→3

                    material["locations"] = [
                        {
                            "id": target_location_uuid,
                            "whid": warehouse_mapping[target_warehouse_name].get("uuid", ""),
                            "whName": target_warehouse_name,
                            "x": bioyond_x,
                            "y": bioyond_y,
                            "z": 1,
                            "quantity": 0
                        }
                    ]
                    logger.debug(f"✅ [PLR→Bioyond] 位置更新: {resource.name} → {target_warehouse_name}/{update_site} (x={bioyond_x}, y={bioyond_y})")
                except Exception as e:
                    logger.error(f"❌ [PLR→Bioyond] 解析库位代码失败: {update_site}, 错误: {e}")
            else:
                logger.warning(f"⚠️ [PLR→Bioyond] 未找到库位 {update_site} 的配置")

        elif resource.parent is not None and isinstance(resource.parent, ItemizedCarrier):
            # 情况2: 使用当前 parent 位置
            site_in_parent = resource.parent.get_child_identifier(resource)

            # ⚠️ 坐标系转换说明:
            # get_child_identifier 返回: x_idx=列索引, y_idx=行索引 (0-based)
            # Bioyond 系统要求: x=行号, y=列号 (1-based)
            # 因此需要交换 x 和 y!
            bioyond_x = site_in_parent["y"] + 1  # 行索引 → Bioyond的x (行号)
            bioyond_y = site_in_parent["x"] + 1  # 列索引 → Bioyond的y (列号)

            material["locations"] = [
                {
                    "id": warehouse_mapping[resource.parent.name]["site_uuids"][site_in_parent["identifier"]],
                    "whid": warehouse_mapping[resource.parent.name]["uuid"],
                    "whName": resource.parent.name,
                    "x": bioyond_x,
                    "y": bioyond_y,
                    "z": 1,
                    "quantity": 0
                }
            ]
            logger.debug(f"🔄 [PLR→Bioyond] 坐标转换: {resource.name} 在 {resource.parent.name}[{site_in_parent['identifier']}] → UniLab(列={site_in_parent['x']},行={site_in_parent['y']}) → Bioyond(x={bioyond_x},y={bioyond_y})")

        bioyond_materials.append(material)
    return bioyond_materials
