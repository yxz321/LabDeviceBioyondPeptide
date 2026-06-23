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

    def material_info(self, material_id: str) -> dict[str, Any]:
        self.material_info_calls.append(material_id)
        return self.material_info_by_id.get(material_id, {})

    def materials_by_order_id(self, json_str: str) -> list[dict[str, Any]]:
        payload = json.loads(json_str)
        return self.materials_by_order_id_by_order.get(str(payload.get("orderId") or ""), [])

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


def test_material_change_empty_locations_deletes_material_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SimpleNamespace,
) -> None:
    station = _fresh_station(monkeypatch, runtime)
    material_id = "bioyond-mat-delete"

    station.process_material_change_report(_material(material_id, "96孔收集板", name="收集板-3", x=1, y=1))
    result = station.process_material_change_report(
        _material(material_id, "96孔收集板", name="收集板-3", x=None, y=None)
    )

    assert result["processed"] is True
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
    result = station.process_material_change_report(delete_report)

    base = _resource_by_bioyond_id(station.deck, "bioyond-delete-combined-base")
    plate = _resource_by_bioyond_id(station.deck, "bioyond-delete-combined-plate")
    assert result["action"] == "delete"
    assert base is not None
    assert plate is None
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
