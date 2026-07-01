from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, Iterable

import pytest


AUTO_WAREHOUSE = "自动化堆栈"
BASE_WAREHOUSE = "固相合成板底座缓冲位"
G3_WAREHOUSE = "G3移液站库"


@pytest.fixture(scope="session", autouse=True)
def _peptide_registry_ready() -> None:
    from pathlib import Path

    from unilabos.registry.registry import lab_registry, build_registry

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        # Resources now live in this external package, so the registry must be
        # built with --devices pointing at it (instead of the built-in scan).
        package_dir = Path(__file__).resolve().parents[1] / "bioyond_peptide_station"
        build_registry(devices_dirs=[str(package_dir)])


@pytest.fixture()
def runtime() -> SimpleNamespace:
    pytest.importorskip("pylabrobot.resources")
    from bioyond_peptide_station._vendored import station as station_module
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station.resources.peptide_materials import DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS

    return SimpleNamespace(
        station_module=station_module,
        BioyondPeptideStation=BioyondPeptideStation,
        PeptideStationDeck=BIOYOND_PeptideStation_Deck,
        material_type_mappings=DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    )


class _FakeRosNode:
    def __init__(self) -> None:
        self.update_resource_calls: list[dict[str, Any]] = []

    def update_resource(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.update_resource_calls.append({"args": args, **kwargs})
        return {"ok": True}


class _FakeHardware:
    def __init__(self) -> None:
        self.material_cache: dict[str, str] = {}
        self.bioyond_material_cache: dict[str, dict[str, Any]] = {}
        self.material_info_calls: list[str] = []
        self.material_info_by_id: dict[str, dict[str, Any]] = {}
        self.materials_by_order_id_by_order: dict[str, list[dict[str, Any]]] = {}
        self.take_out_calls: list[tuple[str, list[str], list[str]]] = []
        # 全量同步（stock_material_result）返回的库存行，按 typeMode 索引。
        # 测试可直接覆盖 stock_material_rows（不区分 typeMode，所有 typeMode 共用同一批）。
        self.stock_material_rows: list[dict[str, Any]] = []
        self.stock_material_result_calls: list[str] = []

    def material_info(self, material_id: str) -> dict[str, Any]:
        self.material_info_calls.append(material_id)
        return self.material_info_by_id.get(material_id, {})

    def material_info_result(self, material_id: str) -> dict[str, Any]:
        self.material_info_calls.append(material_id)
        info = self.material_info_by_id.get(material_id, {})
        return {"ok": bool(info), "data": info, "message": "", "code": 1 if info else None}

    def stock_material_result(self, json_str: str) -> dict[str, Any]:
        # 仅 typeMode 0 返回行，避免重复（其余 typeMode 返回真·空成功）。
        self.stock_material_result_calls.append(json_str)
        payload = json.loads(json_str)
        rows = list(self.stock_material_rows) if payload.get("typeMode") == 0 else []
        return {"ok": True, "data": rows, "message": "", "code": 1}

    def materials_by_order_id(self, json_str: str) -> list[dict[str, Any]]:
        payload = json.loads(json_str)
        return self.materials_by_order_id_by_order.get(str(payload.get("orderId") or ""), [])

    def materials_by_order_id_result(self, json_str: str) -> dict[str, Any]:
        # result-aware 非折叠变体（同步路径现在用它）。
        payload = json.loads(json_str)
        rows = self.materials_by_order_id_by_order.get(str(payload.get("orderId") or ""), [])
        return {"ok": True, "data": list(rows), "message": "", "code": 1}

    def take_out(self, order_id: str, preintake_ids: list[str], material_ids: list[str]) -> dict[str, Any]:
        self.take_out_calls.append((order_id, list(preintake_ids), list(material_ids)))
        rows = self.materials_by_order_id_by_order.get(order_id)
        if rows is not None:
            self.materials_by_order_id_by_order[order_id] = [
                {**row, "locations": []}
                for row in rows
                if isinstance(row, dict)
            ]
        return {"code": 1, "message": "OK", "data": {}}


def _fresh_station(monkeypatch: pytest.MonkeyPatch, runtime: SimpleNamespace) -> Any:
    station = object.__new__(runtime.BioyondPeptideStation)
    hardware = _FakeHardware()
    station.deck = runtime.PeptideStationDeck(setup=True)
    station.bioyond_config = {
        "material_type_mappings": runtime.material_type_mappings,
        "warehouse_mapping": {},
    }
    station.hardware_interface = hardware
    station._ros_node = _FakeRosNode()
    station.resource_synchronizer = runtime.station_module.BioyondResourceSynchronizer(station)
    station._debug_call_session = lambda _name: nullcontext()

    def run_sync(
        func: Any,
        trace_error: bool = True,
        inner_trace_callback: Any = None,
        **kwargs: Any,
    ) -> Any:
        return func(**kwargs)

    monkeypatch.setattr(runtime.station_module.ROS2DeviceNode, "run_async_func", run_sync)
    return station


def _material(
    material_id: str,
    type_name: str,
    *,
    name: str | None = None,
    wh_name: str = AUTO_WAREHOUSE,
    wh_id: str = "wh-auto",
    x: int | None = 1,
    y: int | None = 1,
    z: int = 1,
) -> dict[str, Any]:
    type_code = _type_code_for_display_name(type_name) or "UNKNOWN"
    locations = [] if x is None or y is None else [{
        "id": f"{material_id}:location",
        "whid": wh_id,
        "whName": wh_name,
        "code": f"{x}-{y}",
        "x": x,
        "y": y,
        "z": z,
        "quantity": 1.0,
    }]
    return {
        "id": material_id,
        "typeName": type_name,
        "code": f"{type_code}-{material_id}",
        "barCode": None,
        "name": name or type_name,
        "quantity": 1.0,
        "lockQuantity": 0.0,
        "unit": "个",
        "status": 2,
        "isUse": True,
        "parameters": None,
        "locations": locations,
        "detail": [],
    }


def _type_code_for_display_name(type_name: str) -> str | None:
    from bioyond_peptide_station.resources import peptide_materials

    for type_code, resource_cls in peptide_materials.MATERIAL_TYPE_CODE_TO_CLASS.items():
        display_name = getattr(resource_cls, "bioyond_material_type_name", None)
        if not display_name:
            mapping = peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS.get(resource_cls.resource_id)
            display_name = mapping[0] if mapping else None
        if display_name == type_name:
            return str(type_code)
    return None


def _walk(resource: Any) -> Iterable[Any]:
    yield resource
    for child in getattr(resource, "children", []) or []:
        yield from _walk(child)


def _bioyond_id(resource: Any) -> Any:
    extra = getattr(resource, "unilabos_extra", None)
    if isinstance(extra, dict):
        for key in ("material_bioyond_id", "bioyond_id", "bioyond_material_id"):
            if extra.get(key):
                return extra[key]
        nested = extra.get("bioyond_material")
        if isinstance(nested, dict):
            for key in ("material_bioyond_id", "bioyond_id", "bioyond_material_id", "id"):
                if nested.get(key):
                    return nested[key]
            raw_payload = nested.get("raw_payload")
            if isinstance(raw_payload, dict):
                return raw_payload.get("id") or raw_payload.get("materialId")
    for attr in ("material_bioyond_id", "bioyond_id", "bioyond_material_id"):
        if getattr(resource, attr, None):
            return getattr(resource, attr)
    return None


def _resource_by_bioyond_id(deck: Any, material_id: str) -> Any | None:
    matches = [resource for resource in _walk(deck) if _bioyond_id(resource) == material_id]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _slot(station: Any, slot_key: str, warehouse_name: str = AUTO_WAREHOUSE) -> Any:
    return station.deck.warehouses[warehouse_name][slot_key]


def _assert_empty_slot(station: Any, slot_key: str, warehouse_name: str = AUTO_WAREHOUSE) -> None:
    slot = _slot(station, slot_key, warehouse_name)
    assert slot is None or type(slot).__name__ == "ResourceHolder"


def _set_stale_occupied_by(
    station: Any,
    slot_key: str,
    occupied_by: str,
    warehouse_name: str = AUTO_WAREHOUSE,
) -> None:
    warehouse = station.deck.warehouses[warehouse_name]
    idx = list(warehouse._ordering.keys()).index(slot_key)
    warehouse.sites[idx] = occupied_by


def _assert_published_deck(station: Any, expected_count: int = 1) -> None:
    calls = station._ros_node.update_resource_calls
    assert len(calls) == expected_count
    resources = calls[-1]["resources"]
    assert resources
    assert resources != [station.deck]
    assert station.deck not in resources


def test_96_well_plate_resources_do_not_create_well_children(runtime: SimpleNamespace) -> None:
    from bioyond_peptide_station.resources import peptide_materials

    plate_classes = [
        peptide_materials.BioyondPeptide_96WellDeepWellPlate,
        peptide_materials.BioyondPeptide_96WellSynthesisPlate,
        peptide_materials.BioyondPeptide_96WellCollectionPlate,
        peptide_materials.BioyondPeptide_96WellBalancePlate,
        peptide_materials.BioyondPeptide_96WellAssayPlate,
        peptide_materials.BioyondPeptide_96WellCarboxylicAcidPlate,
        peptide_materials.BioyondPeptide_96WellStandardCurveAssayPlate,
    ]

    for plate_cls in plate_classes:
        plate = plate_cls(name=plate_cls.resource_id)
        assert list(getattr(plate, "children", []) or []) == []


def test_material_change_detail_rows_are_metadata_for_empty_96_well_plate(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material = _material("details-96", "96孔收集板", name="明细收集板", x=1, y=1)
    material["detail"] = [
        {"name": "多肽A", "typeName": None, "x": 1, "y": 1, "z": 1, "quantity": 12.5, "code": "P-A"},
        {"name": "多肽B", "typeName": None, "x": 2, "y": 1, "z": 1, "quantity": 7.0, "code": "P-B"},
    ]

    station.process_material_change_report(material)

    resource = _resource_by_bioyond_id(station.deck, "details-96")
    assert resource is not None
    assert list(getattr(resource, "children", []) or []) == []
    extra = getattr(resource, "unilabos_extra", {}) or {}
    assert extra["bioyond_material_details"] == material["detail"]
    assert "bioyond_material_detail_contents" not in extra
    assert _slot(station, "1-1") is resource


def test_full_sync_detail_rows_are_metadata_for_empty_96_well_plate(runtime: SimpleNamespace) -> None:
    row = _material("full-sync-details-96", "96孔收集板", name="全量明细收集板", x=1, y=1)
    row["detail"] = [
        {"name": "全量多肽", "typeName": None, "x": 1, "y": 1, "z": 1, "quantity": 3.5, "code": "P-F"}
    ]

    resources = runtime.station_module.resource_bioyond_to_plr(
        [row],
        type_mapping=runtime.material_type_mappings,
    )

    assert len(resources) == 1
    resource = resources[0]
    assert list(getattr(resource, "children", []) or []) == []
    extra = getattr(resource, "unilabos_extra", {}) or {}
    assert "bioyond_material_details" not in extra
    assert "bioyond_material_detail_contents" not in extra
    assert "detail_contents" not in extra["bioyond_material"]
    assert extra["bioyond_material"]["details"] == row["detail"]


def test_material_change_add_registers_material_on_deck_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material = _material("bioyond-mat-add", "96孔收集板", name="收集板-1", x=1, y=1)

    result = station.process_material_change_report(material)

    assert result["processed"] is True
    resource = _resource_by_bioyond_id(station.deck, "bioyond-mat-add")
    assert resource is not None
    assert resource.name == "收集板-1-bioyond-mat-add"
    assert resource.unilabos_extra["material_bioyond_name"] == "收集板-1"
    assert resource.unilabos_extra["bioyond_material"]["raw_name"] == "收集板-1"
    assert resource.unilabos_extra["unilabos_frontend_pose_extra"]["body_color"] == "#DDE7EA"
    assert _slot(station, "1-1") is resource
    assert result["material_cache_updated"] is True
    assert station.hardware_interface.bioyond_material_cache["bioyond-mat-add"]["locations"]
    _assert_published_deck(station)


def test_material_change_writes_canonical_id_from_material_id(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material = _material("material-id-only-change", "96孔收集板", name="materialId收集板", x=1, y=1)
    material["materialId"] = material.pop("id")

    result = station.process_material_change_report(material)

    assert result["processed"] is True
    resource = _resource_by_bioyond_id(station.deck, "material-id-only-change")
    assert resource is not None
    assert resource.unilabos_extra["material_bioyond_id"] == "material-id-only-change"
    _assert_published_deck(station)


def test_material_change_move_replaces_old_location_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-move"

    station.process_material_change_report(_material(material_id, "96孔收集板", name="收集板-2", x=1, y=1))
    station.process_material_change_report(_material(material_id, "96孔收集板", name="收集板-2", x=1, y=2))

    resource = _resource_by_bioyond_id(station.deck, material_id)
    assert resource is not None
    _assert_empty_slot(station, "1-1")
    assert _slot(station, "2-1") is resource
    _assert_published_deck(station, expected_count=2)


def test_material_change_new_id_with_reused_name_does_not_remove_existing_material(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    shared_name = "收集板-重名"

    station.process_material_change_report(_material("bioyond-mat-name-1", "96孔收集板", name=shared_name, x=1, y=1))
    station.process_material_change_report(_material("bioyond-mat-name-2", "96孔收集板", name=shared_name, x=1, y=2))

    first = _resource_by_bioyond_id(station.deck, "bioyond-mat-name-1")
    second = _resource_by_bioyond_id(station.deck, "bioyond-mat-name-2")
    assert first is not None
    assert second is not None
    assert first is not second
    assert _slot(station, "1-1") is first
    assert _slot(station, "2-1") is second
    _assert_published_deck(station, expected_count=2)


def test_material_change_authoritatively_replaces_different_material_in_target_slot(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)

    station.process_material_change_report(
        _material("bioyond-mat-old-slot", "96孔收集板", name="旧收集板", x=1, y=1)
    )
    result = station.process_material_change_report(
        _material("bioyond-mat-new-slot", "96孔收集板", name="新收集板", x=1, y=1)
    )

    old = _resource_by_bioyond_id(station.deck, "bioyond-mat-old-slot")
    new = _resource_by_bioyond_id(station.deck, "bioyond-mat-new-slot")
    assert result["cleared_occupied_slots"] == 1
    assert old is None
    assert new is not None
    assert _slot(station, "1-1") is new
    _assert_published_deck(station, expected_count=2)


def test_material_change_empty_locations_parks_material_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-delete"

    station.process_material_change_report(_material(material_id, "96孔收集板", name="收集板-3", x=1, y=1))
    result = station.process_material_change_report(
        _material(material_id, "96孔收集板", name="收集板-3", x=None, y=None)
    )

    # 默认策略 PARK：物理槽位释放，但资源仍存在于虚拟暂存节点下。
    assert result["processed"] is True
    assert result["action"] == "park"
    resource = _resource_by_bioyond_id(station.deck, material_id)
    assert resource is not None, "park 不应删除资源，应改挂虚拟暂存节点"
    holding = next(
        child
        for child in station.deck.children
        if getattr(child, "name", "") == "BioyondVirtualHolding"
    )
    assert getattr(resource, "parent", None) is holding
    _assert_empty_slot(station, "1-1")
    cache_record = station.hardware_interface.bioyond_material_cache[material_id]
    assert cache_record["locations"] == []
    assert cache_record["raw_name"] == "收集板-3"
    _assert_published_deck(station, expected_count=2)


def test_material_change_empty_locations_with_delete_policy_deletes_material(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-delete-policy"

    station.process_material_change_report(_material(material_id, "96孔收集板", name="收集板-3", x=1, y=1))
    result = station.process_material_change_report(
        _material(material_id, "96孔收集板", name="收集板-3", x=None, y=None),
        on_empty_locations="delete",
    )

    # 显式 delete 策略：资源被硬删除（不再挂虚拟暂存节点）。
    assert result["processed"] is True
    assert result["action"] == "delete"
    assert _resource_by_bioyond_id(station.deck, material_id) is None
    _assert_empty_slot(station, "1-1")
    cache_record = station.hardware_interface.bioyond_material_cache[material_id]
    assert cache_record["locations"] == []
    assert cache_record["raw_name"] == "收集板-3"
    _assert_published_deck(station, expected_count=2)


def test_material_change_uses_material_info_when_code_and_type_missing(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-info"
    station.hardware_interface.material_info_by_id[material_id] = _material(
        material_id,
        "96孔收集板",
        name="详情收集板",
        x=1,
        y=1,
    )
    report = {
        "id": material_id,
        "name": "报送收集板",
        "typeName": None,
        "code": None,
        "locations": [{"whName": AUTO_WAREHOUSE, "x": 1, "y": 1, "z": 1}],
        "detail": [],
    }

    result = station.process_material_change_report(report)

    resource = _resource_by_bioyond_id(station.deck, material_id)
    assert result["processed"] is True
    assert station.hardware_interface.material_info_calls == [material_id]
    assert resource is not None
    assert resource.name == "详情收集板-bioyond-mat-info"
    assert resource.unilabos_extra["material_bioyond_name"] == "详情收集板"


def test_material_change_missing_code_after_material_info_uses_regular_container(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-regular"
    report = {
        "id": material_id,
        "name": "报送未知容器",
        "typeName": None,
        "code": None,
        "locations": [{"whName": AUTO_WAREHOUSE, "x": 1, "y": 1, "z": 1}],
        "detail": [],
    }
    station.hardware_interface.material_info_by_id[material_id] = {
        **report,
        "name": "详情未知容器",
        "typeName": "96孔收集板",
    }

    result = station.process_material_change_report(report)

    resource = _resource_by_bioyond_id(station.deck, material_id)
    record = station.hardware_interface.bioyond_material_cache[material_id]
    assert result["processed"] is True
    assert station.hardware_interface.material_info_calls == [material_id]
    assert resource is not None
    assert type(resource).__name__ == "RegularContainer"
    assert resource.unilabos_extra["material_bioyond_name"] == "详情未知容器"
    assert resource.unilabos_extra["bioyond_regular_container_fallback"] is True
    assert record["regular_container_fallback"] is True
    assert "missing_material_code_fallback_regular_container" in record["warnings"]


def test_take_out_syncs_order_materials_with_empty_locations_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-take-out"
    material = _material(material_id, "96孔收集板", name="下料收集板", x=1, y=1)
    station.process_material_change_report(material)
    assert _resource_by_bioyond_id(station.deck, material_id) is not None
    assert material_id in station.hardware_interface.bioyond_material_cache
    station.hardware_interface.materials_by_order_id_by_order["ORDER-TAKE"] = [material]

    result = station.take_out("ORDER-TAKE")

    assert result["success"] is True
    assert station.hardware_interface.take_out_calls == [("ORDER-TAKE", [], [])]
    assert _resource_by_bioyond_id(station.deck, material_id) is None
    cache_record = station.hardware_interface.bioyond_material_cache[material_id]
    assert cache_record["locations"] == []
    assert result["post_take_out_sync"]["material_count"] == 1
    assert result["post_take_out_sync"]["published_count"] == 1


def test_clear_and_delete_bioyond_materials_support_legacy_identity_shapes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_ids = [
        "legacy-extra-bioyond-id",
        "legacy-nested-bioyond-id",
        "legacy-nested-raw-payload-id",
        "legacy-attr-bioyond-id",
        "legacy-delete-raw-payload-id",
    ]

    for index, material_id in enumerate(material_ids, start=1):
        station.process_material_change_report(
            _material(material_id, "96孔收集板", name=f"旧收集板-{index}", x=1, y=index)
        )

    resources = {
        material_id: _resource_by_bioyond_id(station.deck, material_id)
        for material_id in material_ids
    }
    assert all(resource is not None for resource in resources.values())

    resources["legacy-extra-bioyond-id"].unilabos_extra = {"bioyond_id": "legacy-extra-bioyond-id"}
    resources["legacy-nested-bioyond-id"].unilabos_extra = {
        "bioyond_material": {"bioyond_id": "legacy-nested-bioyond-id"}
    }
    resources["legacy-nested-raw-payload-id"].unilabos_extra = {
        "bioyond_material": {"raw_payload": {"id": "legacy-nested-raw-payload-id"}}
    }
    resources["legacy-attr-bioyond-id"].unilabos_extra = {}
    resources["legacy-attr-bioyond-id"].bioyond_id = "legacy-attr-bioyond-id"
    resources["legacy-delete-raw-payload-id"].unilabos_extra = {
        "bioyond_material": {"raw_payload": {"id": "legacy-delete-raw-payload-id"}}
    }

    assert station._remove_bioyond_materials_from_deck(["legacy-delete-raw-payload-id"]) == 1
    assert _resource_by_bioyond_id(station.deck, "legacy-delete-raw-payload-id") is None

    assert station._clear_external_sync_bioyond_materials() == 4
    for material_id in material_ids[:-1]:
        assert _resource_by_bioyond_id(station.deck, material_id) is None
    assert station.deck is not None


def test_synthesis_plate_attaches_to_base_when_reported_at_same_location(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)

    station.process_material_change_report(
        _material(
            "bioyond-base",
            "96孔固相合成板底座",
            name="固相底座-1",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )
    station.process_material_change_report(
        _material(
            "bioyond-plate",
            "96孔固相合成板",
            name="固相板-1",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )

    base = _resource_by_bioyond_id(station.deck, "bioyond-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-plate")
    assert base is not None
    assert plate is not None
    assert getattr(base, "category", None) == "deck"
    assert _slot(station, "0015-0001", BASE_WAREHOUSE) is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base
    assert plate in base.children
    assert station._ros_node.update_resource_calls[-1]["resources"] == [
        station.deck.warehouses[BASE_WAREHOUSE]
    ]
    _assert_published_deck(station, expected_count=2)


def test_material_change_attaches_combined_plate_at_same_parent_location(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    station.process_material_change_report(
        _material(
            "bioyond-combined-base",
            "96孔固相合成板底座",
            name="组合固相底座",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )
    station.process_material_change_report(
        _material(
            "bioyond-protected-slot",
            "96孔收集板",
            name="不应被组合子物料清理",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=2,
        )
    )
    plate_report = _material(
        "bioyond-combined-plate",
        "96孔固相合成板",
        name="组合固相板",
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=1,
        y=1,
    )
    plate_report["combinedMaterialId"] = "bioyond-combined-base"

    result = station.process_material_change_report(plate_report)

    base = _resource_by_bioyond_id(station.deck, "bioyond-combined-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-combined-plate")
    protected = _resource_by_bioyond_id(station.deck, "bioyond-protected-slot")
    assert result["processed"] is True
    assert result["cleared_occupied_slots"] == 0
    assert base is not None
    assert plate is not None
    assert protected is not None
    assert _slot(station, "0015-0001", BASE_WAREHOUSE) is base
    assert _slot(station, "0015-0002", BASE_WAREHOUSE) is protected
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base
    _assert_published_deck(station, expected_count=3)


def test_material_change_attaches_combined_plate_without_child_location(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    station.process_material_change_report(
        _material(
            "bioyond-empty-combined-base",
            "96孔固相合成板底座",
            name="无子位置组合底座",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )
    plate_report = _material(
        "bioyond-empty-combined-plate",
        "96孔固相合成板",
        name="无位置组合固相板",
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=1,
        y=2,
    )
    plate_report["combinedMaterialId"] = "bioyond-empty-combined-base"
    plate_report["locations"] = []

    result = station.process_material_change_report(plate_report)

    base = _resource_by_bioyond_id(station.deck, "bioyond-empty-combined-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-empty-combined-plate")
    assert result["processed"] is True
    assert result["action"] == "add"
    assert base is not None
    assert plate is not None
    assert _slot(station, "0015-0001", BASE_WAREHOUSE) is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base
    _assert_published_deck(station, expected_count=2)


def test_material_change_empty_locations_delete_does_not_reuse_cached_combined_parent(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    station.process_material_change_report(
        _material(
            "bioyond-delete-combined-base",
            "96孔固相合成板底座",
            name="删除组合底座",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )
    plate_report = _material(
        "bioyond-delete-combined-plate",
        "96孔固相合成板",
        name="删除组合固相板",
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=1,
        y=1,
    )
    plate_report["combinedMaterialId"] = "bioyond-delete-combined-base"
    station.process_material_change_report(plate_report)

    delete_report = _material(
        "bioyond-delete-combined-plate",
        "96孔固相合成板",
        name="删除组合固相板",
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=None,
        y=None,
    )
    result = station.process_material_change_report(delete_report, on_empty_locations="delete")

    base = _resource_by_bioyond_id(station.deck, "bioyond-delete-combined-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-delete-combined-plate")
    assert result["action"] == "delete"
    assert base is not None
    assert plate is None
    # 删除组合子物料不应复用缓存的组合父槽位再嵌套——底座槽位 0 应为空。
    assert base[0] is None or type(base[0]).__name__ == "ResourceHolder"


def test_material_change_places_combined_plate_at_distinct_child_location(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    station.process_material_change_report(
        _material(
            "bioyond-different-combined-base",
            "96孔固相合成板底座",
            name="异位组合底座",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=1,
        )
    )
    station.process_material_change_report(
        _material(
            "bioyond-different-protected",
            "96孔收集板",
            name="会被异位子物料替换",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=2,
        )
    )
    plate_report = _material(
        "bioyond-different-combined-plate",
        "96孔固相合成板",
        name="异位组合固相板",
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=1,
        y=2,
    )
    plate_report["combinedMaterialId"] = "bioyond-different-combined-base"

    result = station.process_material_change_report(plate_report)

    base = _resource_by_bioyond_id(station.deck, "bioyond-different-combined-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-different-combined-plate")
    replaced = _resource_by_bioyond_id(station.deck, "bioyond-different-protected")
    assert result["processed"] is True
    assert result["cleared_occupied_slots"] == 1
    assert base is not None
    assert plate is not None
    assert replaced is None
    assert _slot(station, "0015-0001", BASE_WAREHOUSE) is base
    assert _slot(station, "0015-0002", BASE_WAREHOUSE) is plate
    assert getattr(plate, "parent", None) is station.deck.warehouses[BASE_WAREHOUSE]
    _assert_published_deck(station, expected_count=3)


def test_synthesis_base_claims_slot_when_plate_reported_first(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)

    station.process_material_change_report(
        _material(
            "bioyond-plate-first",
            "96孔固相合成板",
            name="固相板-先到",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=2,
        )
    )
    station.process_material_change_report(
        _material(
            "bioyond-base-second",
            "96孔固相合成板底座",
            name="固相底座-后到",
            wh_name=BASE_WAREHOUSE,
            wh_id="wh-base",
            x=1,
            y=2,
        )
    )

    base = _resource_by_bioyond_id(station.deck, "bioyond-base-second")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-plate-first")
    assert base is not None
    assert plate is not None
    assert getattr(base, "category", None) == "deck"
    assert _slot(station, "0015-0002", BASE_WAREHOUSE) is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base
    assert station.deck.warehouses[BASE_WAREHOUSE] in station._ros_node.update_resource_calls[-1]["resources"]
    _assert_published_deck(station, expected_count=2)


def test_material_change_overwrites_stale_string_occupied_by(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    _set_stale_occupied_by(station, "4", "stale-material-name", G3_WAREHOUSE)

    result = station.process_material_change_report(
        _material(
            "bioyond-mat-stale-slot",
            "300mL乙醚试剂槽1",
            name="乙醚槽-1",
            wh_name=G3_WAREHOUSE,
            wh_id="wh-g3",
            x=1,
            y=4,
        )
    )

    assert result["processed"] is True
    resource = _resource_by_bioyond_id(station.deck, "bioyond-mat-stale-slot")
    assert resource is not None
    assert _slot(station, "4", G3_WAREHOUSE) is resource
    _assert_published_deck(station)


# ===========================================================================
# Part B — 资源同步重构契约测试（plan "Verification" 节）
# ===========================================================================


def _virtual_holding_node(station: Any) -> Any:
    from bioyond_peptide_station.resources.decks import BIOYOND_VIRTUAL_HOLDING_NODE_NAME

    for child in station.deck.children:
        if getattr(child, "name", "") == BIOYOND_VIRTUAL_HOLDING_NODE_NAME:
            return child
    return None


def _combined_plate_report(material_id: str, name: str, base_id: str, *, x: int, y: int) -> dict[str, Any]:
    report = _material(
        material_id,
        "96孔固相合成板",
        name=name,
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=x,
        y=y,
    )
    report["combinedMaterialId"] = base_id
    return report


def _base_report(material_id: str, name: str, *, x: int, y: int) -> dict[str, Any]:
    return _material(
        material_id,
        "96孔固相合成板底座",
        name=name,
        wh_name=BASE_WAREHOUSE,
        wh_id="wh-base",
        x=x,
        y=y,
    )


# --- B1. Combined offspring nesting on a deck-resident base, order-independent ---


def test_combined_plate_nests_on_deck_resident_base_base_first(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 底座先到，成为 deck 常驻资源（不在后续 plate 的"当前批次"里）。
    station.process_material_change_report(_base_report("b1-base", "底座B1", x=1, y=1))

    plate_report = _combined_plate_report("b1-plate", "固相板B1", "b1-base", x=1, y=1)
    result = station.process_material_change_report(plate_report)

    base = _resource_by_bioyond_id(station.deck, "b1-base")
    plates = [r for r in _walk(station.deck) if _bioyond_id(r) == "b1-plate"]
    assert result["processed"] is True
    assert len(plates) == 1, "deck 常驻底座必须被找到，不能产生 _2 重复板"
    assert base[0] is plates[0]
    assert getattr(plates[0], "parent", None) is base
    # 没有 _2 后缀的重复名。
    assert not any(getattr(r, "name", "").endswith("_2") for r in _walk(station.deck))


def test_combined_plate_nests_on_deck_resident_base_plate_processed_when_base_present(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 同样的最终状态，但确认 base 已在 deck 上后再处理 plate（不同到达顺序的等价检查）。
    station.process_material_change_report(_base_report("b1b-base", "底座B1b", x=2, y=1))
    base = _resource_by_bioyond_id(station.deck, "b1b-base")
    assert base is not None

    result = station.process_material_change_report(
        _combined_plate_report("b1b-plate", "固相板B1b", "b1b-base", x=2, y=1)
    )

    plates = [r for r in _walk(station.deck) if _bioyond_id(r) == "b1b-plate"]
    assert result["processed"] is True
    assert len(plates) == 1
    assert base[0] is plates[0]
    assert getattr(plates[0], "parent", None) is base


# --- B2. Move-preserves-object ---


def test_move_preserves_resource_object_identity_and_contents(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "b2-move"
    station.process_material_change_report(_material(material_id, "96孔收集板", name="移动板", x=1, y=1))
    resource = _resource_by_bioyond_id(station.deck, material_id)
    assert resource is not None
    original_id = id(resource)
    # 放入哨兵内容（well/容器内容），验证 move 不会重建对象、内容保留。
    resource.unilabos_extra["__sentinel__"] = "keep-me"

    station.process_material_change_report(_material(material_id, "96孔收集板", name="移动板", x=1, y=2))

    moved = _resource_by_bioyond_id(station.deck, material_id)
    assert moved is not None
    assert id(moved) == original_id, "move 必须保留对象身份，不能重建"
    assert moved.unilabos_extra.get("__sentinel__") == "keep-me", "内容必须保留"
    _assert_empty_slot(station, "1-1")
    assert _slot(station, "2-1") is moved


# --- B3. Lookup-map freshness within one batch ---


def test_lookup_map_freshness_parent_moves_before_combined_child_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 父底座先建在 0015-0001。
    station.process_material_change_report(_base_report("b3-base", "底座B3", x=1, y=1))
    base = _resource_by_bioyond_id(station.deck, "b3-base")
    assert _slot(station, "0015-0001", BASE_WAREHOUSE) is base

    # 单批次：lookup 一次构建，期间父先移动到 0015-0002，子物料随后必须挂到父的新槽位。
    lookup = station._build_bioyond_lookup_map()

    # 行 1：父底座移动到 0015-0002（同批次内更新 lookup）。
    station.process_material_change_report(
        _base_report("b3-base", "底座B3", x=1, y=2),
        source="order_materials",
        lookup=lookup,
    )
    base_after = _resource_by_bioyond_id(station.deck, "b3-base")
    assert base_after is base, "父底座 move 应保持对象身份"
    assert _slot(station, "0015-0002", BASE_WAREHOUSE) is base

    # 行 2：组合子物料到达，combinedMaterialId 指向父；必须用 lookup 中父的新槽位嵌套。
    plate_report = _combined_plate_report("b3-plate", "固相板B3", "b3-base", x=1, y=2)
    station.process_material_change_report(
        plate_report,
        source="order_materials",
        lookup=lookup,
    )

    plates = [r for r in _walk(station.deck) if _bioyond_id(r) == "b3-plate"]
    assert len(plates) == 1
    assert base[0] is plates[0], "子物料必须嵌套在父的当前（移动后）槽位"
    assert getattr(plates[0], "parent", None) is base


# --- B4. Target-slot occupant preservation ---


def test_move_base_into_slot_preserves_legitimate_nested_plate(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 底座 + 嵌套的合成板（合法组合配对方）。
    station.process_material_change_report(_base_report("b4-base", "底座B4", x=1, y=1))
    station.process_material_change_report(
        _combined_plate_report("b4-plate", "固相板B4", "b4-base", x=1, y=1)
    )
    base = _resource_by_bioyond_id(station.deck, "b4-base")
    plate = _resource_by_bioyond_id(station.deck, "b4-plate")
    assert base[0] is plate

    # 底座原地重报（同库位 move）：不得把它合法嵌套的板 park/delete。
    station.process_material_change_report(_base_report("b4-base", "底座B4", x=1, y=1))

    base_after = _resource_by_bioyond_id(station.deck, "b4-base")
    plate_after = _resource_by_bioyond_id(station.deck, "b4-plate")
    assert base_after is base
    assert plate_after is plate, "合法嵌套板不应被 park/delete"
    assert base[0] is plate
    # 板没有被搬到虚拟暂存节点。
    holding = _virtual_holding_node(station)
    assert holding is None or plate not in getattr(holding, "children", [])


def test_move_into_slot_evicts_true_conflicting_stale_occupant(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 真冲突：1-1 上一个无关收集板，被同库位另一物料覆盖。
    station.process_material_change_report(
        _material("b4-stale", "96孔收集板", name="陈旧占位", x=1, y=1)
    )
    result = station.process_material_change_report(
        _material("b4-new", "96孔收集板", name="新占位", x=1, y=1)
    )

    stale = _resource_by_bioyond_id(station.deck, "b4-stale")
    new = _resource_by_bioyond_id(station.deck, "b4-new")
    assert result["cleared_occupied_slots"] == 1
    assert stale is None, "真冲突的陈旧占位应被驱逐"
    assert _slot(station, "1-1") is new


# --- B5. Park-on-empty vs delete-on-empty by policy ---


def test_empty_locations_park_vs_delete_by_policy(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # park（默认）
    station.process_material_change_report(_material("b5-park", "96孔收集板", name="park板", x=1, y=1))
    park_result = station.process_material_change_report(
        _material("b5-park", "96孔收集板", name="park板", x=None, y=None)
    )
    assert park_result["action"] == "park"
    parked = _resource_by_bioyond_id(station.deck, "b5-park")
    assert parked is not None
    assert getattr(parked, "parent", None) is _virtual_holding_node(station)

    # delete（显式策略）
    station.process_material_change_report(_material("b5-del", "96孔收集板", name="del板", x=2, y=1))
    del_result = station.process_material_change_report(
        _material("b5-del", "96孔收集板", name="del板", x=None, y=None),
        on_empty_locations="delete",
    )
    assert del_result["action"] == "delete"
    assert _resource_by_bioyond_id(station.deck, "b5-del") is None


# --- B6. Full-sync delete-absent sweep ---


def test_full_sync_delete_absent_sweep_only_roots_with_stale_bioyond_id(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    synchronizer = station.resource_synchronizer

    # 种子：A、B 携带 bioyond_id；陈旧根 D 携带 bioyond_id，且其下挂一个无 id 的子 E。
    station.process_material_change_report(_material("A", "96孔收集板", name="A板", x=1, y=1))
    station.process_material_change_report(_material("B", "96孔收集板", name="B板", x=2, y=1))
    station.process_material_change_report(_base_report("D", "陈旧底座D", x=1, y=2))

    base_d = _resource_by_bioyond_id(station.deck, "D")
    # E：无 bioyond_id 的子物料，挂在 D 的槽位 0（随 D 子树级联删除，不独立删除）。
    from pylabrobot.resources import Resource as _Res
    child_e = _Res(name="E-no-id", size_x=1, size_y=1, size_z=1)
    child_e.unilabos_extra = {}
    station._assign_child_to_parent_slot(base_d, child_e)
    assert getattr(child_e, "parent", None) is base_d

    # 全量返回只含 A（B、D 缺席）。
    station.hardware_interface.stock_material_rows = [
        _material("A", "96孔收集板", name="A板", x=1, y=1),
    ]

    ok = synchronizer.sync_from_external(clear_stale=True)
    assert ok is True

    # A 保留（在返回里），B/D 删除（缺席的携带 bioyond_id 的根），E 随 D 子树消失。
    assert _resource_by_bioyond_id(station.deck, "A") is not None
    assert _resource_by_bioyond_id(station.deck, "B") is None
    assert _resource_by_bioyond_id(station.deck, "D") is None
    assert not any(getattr(r, "name", "") == "E-no-id" for r in _walk(station.deck)), (
        "无 id 子物料应随祖先 D 子树级联删除"
    )
    # 仓库与虚拟暂存节点从不被扫除。
    assert station.deck.warehouses
    holding = _virtual_holding_node(station)
    assert holding is not None
    assert holding in station.deck.children


def test_full_sync_clear_stale_removes_idless_material_roots(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    synchronizer = station.resource_synchronizer

    station.process_material_change_report(_base_report("A-idless-sweep", "保留底座", x=1, y=1))
    kept = _resource_by_bioyond_id(station.deck, "A-idless-sweep")

    from pylabrobot.resources import Resource as _Res

    kept_child = _Res(name="kept-child-no-id", size_x=1, size_y=1, size_z=1)
    kept_child.unilabos_extra = {}
    station._assign_child_to_parent_slot(kept, kept_child)

    stale_root = _Res(name="stale-root-no-id", size_x=1, size_y=1, size_z=1)
    stale_root.unilabos_extra = {}
    assert station._move_resource_to_warehouse_slot(stale_root, station.deck.warehouses[AUTO_WAREHOUSE], 5)
    assert getattr(stale_root, "parent", None) is station.deck.warehouses[AUTO_WAREHOUSE]

    station.hardware_interface.stock_material_rows = [
        _base_report("A-idless-sweep", "保留底座", x=1, y=1),
    ]

    ok = synchronizer.sync_from_external(clear_stale=True)
    assert ok is True

    assert _resource_by_bioyond_id(station.deck, "A-idless-sweep") is not None
    assert any(getattr(r, "name", "") == "kept-child-no-id" for r in _walk(station.deck)), (
        "被保留物料根下的无 id 子资源不应被独立删除"
    )
    assert not any(getattr(r, "name", "") == "stale-root-no-id" for r in _walk(station.deck)), (
        "clear_stale=True 应删除无 Bioyond id 的旧物料根"
    )
    assert station.deck.warehouses
    assert _virtual_holding_node(station) is not None


def test_full_sync_does_not_independently_delete_children_lacking_bioyond_id(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    synchronizer = station.resource_synchronizer

    # A 在返回里（保留）；C 是 A 的无 id 子物料 → A 存活则 C 也必须存活。
    station.process_material_change_report(_base_report("A2", "底座A2", x=1, y=1))
    base_a = _resource_by_bioyond_id(station.deck, "A2")
    from pylabrobot.resources import Resource as _Res
    child_c = _Res(name="C-no-id", size_x=1, size_y=1, size_z=1)
    child_c.unilabos_extra = {}
    station._assign_child_to_parent_slot(base_a, child_c)

    station.hardware_interface.stock_material_rows = [
        _base_report("A2", "底座A2", x=1, y=1),
    ]
    ok = synchronizer.sync_from_external(clear_stale=True)
    assert ok is True

    assert _resource_by_bioyond_id(station.deck, "A2") is not None
    assert any(getattr(r, "name", "") == "C-no-id" for r in _walk(station.deck)), (
        "无 id 子物料不可被独立删除——只随祖先子树级联删除"
    )


# --- B7. Virtual-node serialization / deserialization / exclusion ---


def test_virtual_holding_node_survives_serialize_deserialize_and_is_excluded(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    from bioyond_peptide_station.resources.decks import (
        BIOYOND_PeptideStation_Deck,
        BIOYOND_VIRTUAL_HOLDING_NODE_NAME,
    )

    station = _fresh_station(monkeypatch, runtime)
    holding = _virtual_holding_node(station)
    assert holding is not None, "deck.setup() 必须创建虚拟暂存节点"

    # 序列化 → 反序列化，节点必须存活。
    serialized = station.deck.serialize()
    restored = BIOYOND_PeptideStation_Deck.deserialize(serialized)
    restored_holding = next(
        (c for c in restored.children if getattr(c, "name", "") == BIOYOND_VIRTUAL_HOLDING_NODE_NAME),
        None,
    )
    assert restored_holding is not None, "反序列化后虚拟暂存节点必须存在（或被重新 ensure）"

    # 缺失时 _ensure 会重建。
    for child in list(restored.children):
        if getattr(child, "name", "") == BIOYOND_VIRTUAL_HOLDING_NODE_NAME:
            restored.unassign_child_resource(child)
    assert restored._ensure_virtual_holding_node() is not None
    assert any(
        getattr(c, "name", "") == BIOYOND_VIRTUAL_HOLDING_NODE_NAME for c in restored.children
    )

    # park 一个物料到虚拟节点后，全量同步的删除缺失扫除不应碰它。
    station.process_material_change_report(_material("b7-mat", "96孔收集板", name="b7板", x=1, y=1))
    station.process_material_change_report(
        _material("b7-mat", "96孔收集板", name="b7板", x=None, y=None)
    )
    parked = _resource_by_bioyond_id(station.deck, "b7-mat")
    assert getattr(parked, "parent", None) is holding

    # 删除缺失扫除（返回集合为空）——按身份排除虚拟节点本身；虚拟节点不会被删。
    station._sweep_absent_bioyond_materials(set())
    assert _virtual_holding_node(station) is not None, "虚拟节点本体不应被扫除"

    # 虚拟节点不应出现在仓库映射里。
    assert BIOYOND_VIRTUAL_HOLDING_NODE_NAME not in (station.deck.warehouses or {})


# --- B8. Duplicate-plate regression ---


def test_resync_plate_with_new_stock_id_yields_single_plate(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    # 底座常驻 + 板挂上。
    station.process_material_change_report(_base_report("b8-base", "底座B8", x=1, y=1))
    station.process_material_change_report(
        _combined_plate_report("b8-plate-v1", "固相板B8", "b8-base", x=1, y=1)
    )
    plates_v1 = [r for r in _walk(station.deck) if _bioyond_id(r) in ("b8-plate-v1", "b8-plate-v2")]
    assert len(plates_v1) == 1

    # 同一物理板换了新 stock id（v2），重新报送 —— 必须只有一块板，旧的被驱逐/移走。
    station.process_material_change_report(
        _combined_plate_report("b8-plate-v2", "固相板B8", "b8-base", x=1, y=1)
    )
    plates_v2 = [r for r in _walk(station.deck) if _bioyond_id(r) in ("b8-plate-v1", "b8-plate-v2")]
    base = _resource_by_bioyond_id(station.deck, "b8-base")
    assert len(plates_v2) == 1, "重新同步后 deck 上只能有一块板，不能累积成两块"
    assert base[0] is plates_v2[0]
    assert not any(getattr(r, "name", "").endswith("_2") for r in _walk(station.deck))
