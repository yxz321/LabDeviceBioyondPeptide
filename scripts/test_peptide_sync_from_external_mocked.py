from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bioyond_peptide_station._vendored import station as station_module
from bioyond_peptide_station.resources import peptide_materials


REPO_ROOT = Path(__file__).resolve().parents[1]
STOCK_MATERIAL_LOG = REPO_ROOT / "temp_benyao/peptide/_logs/stock_material_occupied_locations_2026-06-05_type_modes_0_1_2.json"
AUTO_WAREHOUSE = "自动化堆栈"
BASE_WAREHOUSE = "固相合成板底座缓冲位"


class _FakeBioyondHardware:
    def __init__(self, rows_by_type_mode: dict[int, list[dict[str, Any]]]) -> None:
        self.rows_by_type_mode = rows_by_type_mode
        self.material_cache: dict[str, str] = {}
        self.bioyond_material_cache: dict[str, dict[str, Any]] = {}
        self.material_info_by_id: dict[str, dict[str, Any]] = {}
        self.material_info_calls: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def stock_material(self, json_str: str) -> list[dict[str, Any]]:
        query = json.loads(json_str)
        self.calls.append(query)
        assert query["includeDetail"] is True
        return copy.deepcopy(self.rows_by_type_mode[query["typeMode"]])

    def material_info(self, material_id: str) -> dict[str, Any]:
        self.material_info_calls.append(material_id)
        return copy.deepcopy(self.material_info_by_id.get(material_id, {}))


class _FakeWorkstation:
    def __init__(self, hardware: _FakeBioyondHardware, material_type_mappings: dict[str, list[str]]) -> None:
        self.hardware_interface = hardware
        self.bioyond_config = {
            "sync_interval": 600,
            "material_type_mappings": material_type_mappings,
        }
        self.deck = object()


def _stock_material_row(
    material_id: str,
    type_name: str,
    *,
    name: str,
    wh_name: str,
    x: int,
    y: int,
    z: int = 1,
) -> dict[str, Any]:
    type_code = _type_code_for_display_name(type_name) or "UNKNOWN"
    row = {
        "id": material_id,
        "typeName": type_name,
        "code": f"{type_code}-{material_id}",
        "barCode": None,
        "name": name,
        "quantity": 1.0,
        "lockQuantity": 0.0,
        "unit": "个",
        "status": 2,
        "isUse": True,
        "parameters": None,
        "locations": [{
            "id": f"{material_id}:location",
            "whid": "wh-base",
            "whName": wh_name,
            "code": f"{x}-{y}",
            "x": x,
            "y": y,
            "z": z,
            "quantity": 1.0,
        }],
        "detail": [],
    }
    type_id = _type_id_for_display_name(type_name)
    if type_id:
        row["typeId"] = type_id
    return row


def _walk(resource: Any):
    yield resource
    for child in getattr(resource, "children", []) or []:
        yield from _walk(child)


def _bioyond_id(resource: Any) -> Any:
    extra = getattr(resource, "unilabos_extra", None)
    if isinstance(extra, dict):
        if extra.get("material_bioyond_id") or extra.get("bioyond_id") or extra.get("bioyond_material_id"):
            return extra.get("material_bioyond_id") or extra.get("bioyond_id") or extra.get("bioyond_material_id")
        nested = extra.get("bioyond_material")
        if isinstance(nested, dict):
            if nested.get("bioyond_id") or nested.get("id") or nested.get("bioyond_material_id"):
                return nested.get("bioyond_id") or nested.get("id") or nested.get("bioyond_material_id")
            raw_payload = nested.get("raw_payload")
            if isinstance(raw_payload, dict):
                return raw_payload.get("id") or raw_payload.get("materialId")
    return None


def _resource_by_bioyond_id(deck: Any, material_id: str) -> Any | None:
    matches = [resource for resource in _walk(deck) if _bioyond_id(resource) == material_id]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _display_name_for_type_code(type_code: str) -> str:
    cls = peptide_materials.get_material_class_by_type_code(type_code)
    assert cls is not None, f"fixture type code {type_code} must have a peptide resource class"
    display_name = getattr(cls, "bioyond_material_type_name", None)
    if not display_name:
        display_name = peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS[cls.resource_id][0]
    assert display_name
    return display_name


def _type_id_for_display_name(type_name: str) -> str | None:
    for display_name, type_id in peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS.values():
        if display_name == type_name:
            return type_id
    return None


def _type_code_for_display_name(type_name: str) -> str | None:
    for type_code, resource_cls in peptide_materials.MATERIAL_TYPE_CODE_TO_CLASS.items():
        display_name = getattr(resource_cls, "bioyond_material_type_name", None)
        if not display_name:
            mapping = peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS.get(resource_cls.resource_id)
            display_name = mapping[0] if mapping else None
        if display_name == type_name:
            return str(type_code)
    return None


def _stock_rows_from_logged_occupied_locations() -> dict[int, list[dict[str, Any]]]:
    logged = json.loads(STOCK_MATERIAL_LOG.read_text(encoding="utf-8"))
    rows_by_type_mode: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: []}
    wanted_type_codes = {0: "0007", 1: "0039", 2: "0021"}

    for location in logged["occupied_locations"]:
        type_mode = int(location["typeMode"])
        type_code = location["materialCode"].split("-", 1)[0]
        if wanted_type_codes.get(type_mode) != type_code or rows_by_type_mode[type_mode]:
            continue

        rows_by_type_mode[type_mode].append({
            "id": location["materialId"],
            "typeName": _display_name_for_type_code(type_code),
            "code": location["materialCode"],
            "barCode": None,
            "name": location["materialName"],
            "quantity": location["materialQuantity"],
            "lockQuantity": location["materialLockQuantity"],
            "unit": location["materialUnit"],
            "status": location["materialStatus"],
            "isUse": location["materialIsUse"],
            "locations": [{
                "id": f"{location['materialId']}:location",
                "whid": None,
                "whName": location["whName"],
                "code": location["whCode"],
                "x": location["x"],
                "y": location["y"],
                "z": location["z"],
                "quantity": location["locationQuantity"],
            }],
            "detail": [],
            "_queriedTypeMode": type_mode,
        })

    assert all(rows_by_type_mode.values())
    return rows_by_type_mode


def _material_type_mappings_for(rows_by_type_mode: dict[int, list[dict[str, Any]]]) -> dict[str, list[str]]:
    needed_display_names = {
        row["typeName"]
        for rows in rows_by_type_mode.values()
        for row in rows
    }
    return {
        resource_id: mapping
        for resource_id, mapping in peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS.items()
        if mapping[0] in needed_display_names
    }


@pytest.mark.parametrize("plate_first", [False, True])
def test_graphio_attaches_combined_synthesis_plate_at_same_parent_location(plate_first: bool) -> None:
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_synthesis_plate_base" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    base_row = _stock_material_row(
        "graphio-base",
        "96孔固相合成板底座",
        name="转换固相底座",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=1,
    )
    plate_row = _stock_material_row(
        "graphio-plate",
        "96孔固相合成板",
        name="转换固相板",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=1,
    )
    plate_row["combinedMaterialId"] = "graphio-base"
    rows = [plate_row, base_row] if plate_first else [base_row, plate_row]
    deck = BIOYOND_PeptideStation_Deck(setup=True)

    resources = resource_bioyond_to_plr(
        rows,
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=deck,
    )

    base = _resource_by_bioyond_id(deck, "graphio-base")
    plate = _resource_by_bioyond_id(deck, "graphio-plate")
    assert len(resources) == 2
    assert base is not None
    assert plate is not None
    assert all(getattr(resource, "parent", None) is not None for resource in resources)
    assert deck.warehouses[BASE_WAREHOUSE]["0015-0001"] is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base


@pytest.mark.parametrize("plate_first", [False, True])
def test_graphio_attaches_combined_synthesis_plate_without_child_location(plate_first: bool) -> None:
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_synthesis_plate_base" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    base_row = _stock_material_row(
        "graphio-empty-base",
        "96孔固相合成板底座",
        name="无子位置固相底座",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=1,
    )
    plate_row = _stock_material_row(
        "graphio-empty-plate",
        "96孔固相合成板",
        name="无位置固相板",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=2,
    )
    plate_row["combinedMaterialId"] = "graphio-empty-base"
    plate_row["locations"] = []
    rows = [plate_row, base_row] if plate_first else [base_row, plate_row]
    deck = BIOYOND_PeptideStation_Deck(setup=True)

    resources = resource_bioyond_to_plr(
        rows,
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=deck,
    )

    base = _resource_by_bioyond_id(deck, "graphio-empty-base")
    plate = _resource_by_bioyond_id(deck, "graphio-empty-plate")
    assert len(resources) == 2
    assert base is not None
    assert plate is not None
    assert deck.warehouses[BASE_WAREHOUSE]["0015-0001"] is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base


def test_graphio_places_combined_synthesis_plate_at_distinct_child_location() -> None:
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_synthesis_plate_base" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    base_row = _stock_material_row(
        "graphio-different-base",
        "96孔固相合成板底座",
        name="异位固相底座",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=1,
    )
    plate_row = _stock_material_row(
        "graphio-different-plate",
        "96孔固相合成板",
        name="异位固相板",
        wh_name=BASE_WAREHOUSE,
        x=1,
        y=2,
    )
    plate_row["combinedMaterialId"] = "graphio-different-base"
    deck = BIOYOND_PeptideStation_Deck(setup=True)

    resources = resource_bioyond_to_plr(
        [base_row, plate_row],
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=deck,
    )

    base = _resource_by_bioyond_id(deck, "graphio-different-base")
    plate = _resource_by_bioyond_id(deck, "graphio-different-plate")
    assert len(resources) == 2
    assert base is not None
    assert plate is not None
    assert deck.warehouses[BASE_WAREHOUSE]["0015-0001"] is base
    assert deck.warehouses[BASE_WAREHOUSE]["0015-0002"] is plate
    assert getattr(plate, "parent", None) is deck.warehouses[BASE_WAREHOUSE]


def test_sync_from_external_uses_logged_stock_material_rows_without_network(monkeypatch: Any) -> None:
    rows_by_type_mode = _stock_rows_from_logged_occupied_locations()
    material_type_mappings = _material_type_mappings_for(rows_by_type_mode)
    hardware = _FakeBioyondHardware(rows_by_type_mode)
    workstation = _FakeWorkstation(hardware, material_type_mappings)
    synchronizer = station_module.BioyondResourceSynchronizer(workstation)
    converter_call: dict[str, Any] = {}

    def fake_resource_bioyond_to_plr(
        bioyond_materials: list[dict[str, Any]],
        type_mapping: dict[str, list[str]],
        deck: Any,
    ) -> list[dict[str, Any]]:
        reverse_mapping = {value[0]: resource_id for resource_id, value in type_mapping.items()}
        converter_call["rows"] = copy.deepcopy(bioyond_materials)
        converter_call["type_mapping"] = copy.deepcopy(type_mapping)
        converter_call["deck"] = deck
        converter_call["resolved_resource_ids"] = [
            reverse_mapping[row["typeName"]]
            for row in bioyond_materials
        ]
        return [{"id": row["id"], "resource_id": reverse_mapping[row["typeName"]]} for row in bioyond_materials]

    monkeypatch.setattr(station_module, "resource_bioyond_to_plr", fake_resource_bioyond_to_plr)

    assert synchronizer.sync_from_external() is True

    assert hardware.calls == [
        {"typeMode": 0, "includeDetail": True},
        {"typeMode": 1, "includeDetail": True},
        {"typeMode": 2, "includeDetail": True},
    ]
    assert set(hardware.material_cache) == {
        row["name"]
        for rows in rows_by_type_mode.values()
        for row in rows
    }
    assert converter_call["deck"] is workstation.deck
    assert len(converter_call["rows"]) == 3
    assert converter_call["resolved_resource_ids"] == [
        "bioyond_peptide_96_well_collection_plate",
        "bioyond_peptide_96_well_synthesis_plate_base",
        "bioyond_peptide_reagent_0021_300ml_ether_trough_1",
    ]


def test_sync_from_external_reconciles_synthesis_plate_with_base_on_same_location() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck

    if "bioyond_peptide_96_well_synthesis_plate_base" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    rows_by_type_mode = {
        0: [
            _stock_material_row(
                "sync-base",
                "96孔固相合成板底座",
                name="同步固相底座",
                wh_name=BASE_WAREHOUSE,
                x=1,
                y=1,
            ),
            _stock_material_row(
                "sync-plate",
                "96孔固相合成板",
                name="同步固相板",
                wh_name=BASE_WAREHOUSE,
                x=1,
                y=1,
            ),
        ],
        1: [],
        2: [],
    }
    rows_by_type_mode[0][1]["combinedMaterialId"] = "sync-base"
    hardware = _FakeBioyondHardware(rows_by_type_mode)
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external() is True

    base = _resource_by_bioyond_id(workstation.deck, "sync-base")
    plate = _resource_by_bioyond_id(workstation.deck, "sync-plate")
    assert base is not None
    assert plate is not None
    assert getattr(base, "category", None) == "deck"
    assert workstation.deck.warehouses[BASE_WAREHOUSE]["0015-0001"] is base
    assert base[0] is plate
    assert getattr(plate, "parent", None) is base


def test_sync_from_external_resolves_by_code_and_falls_back_to_regular_container() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    type_id_mapping = _stock_material_row(
        "sync-typeid",
        "1000μL枪头盒",
        name="类型ID优先枪头盒",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    type_id_mapping["typeName"] = "无"
    type_id_mapping["code"] = "UNKNOWN-00001"
    code_fallback = _stock_material_row(
        "sync-code-fallback",
        "无",
        name="96孔收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=2,
    )
    code_fallback["code"] = "0007-00999"
    name_only = _stock_material_row(
        "sync-name-only",
        "",
        name="1000μL枪头盒",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=3,
    )
    name_only["code"] = "UNKNOWN-00002"

    rows_by_type_mode = {0: [type_id_mapping, code_fallback, name_only], 1: [], 2: []}
    hardware = _FakeBioyondHardware(rows_by_type_mode)
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external() is True

    type_id_only = _resource_by_bioyond_id(workstation.deck, "sync-typeid")
    collection = _resource_by_bioyond_id(workstation.deck, "sync-code-fallback")
    name_only = _resource_by_bioyond_id(workstation.deck, "sync-name-only")
    assert type_id_only is not None
    assert collection is not None
    assert name_only is not None
    assert type(type_id_only).__name__ == "RegularContainer"
    assert type(name_only).__name__ == "RegularContainer"
    assert getattr(collection, "model", None) == "bioyond_peptide_96_well_collection_plate"
    assert synchronizer.last_sync_result["fetched_count"] == 3
    assert synchronizer.last_sync_result["converted_count"] == 3
    assert synchronizer.last_sync_result["placed_count"] == 3
    assert synchronizer.last_sync_result["unresolved_count"] == 0
    assert hardware.bioyond_material_cache["sync-typeid"]["regular_container_fallback"] is True
    assert hardware.bioyond_material_cache["sync-name-only"]["regular_container_fallback"] is True


def test_sync_from_external_default_replaces_target_slot_without_pruning_cache() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    old_row = _stock_material_row(
        "codex-visible-old",
        "96孔收集板",
        name="Codex旧收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    new_row = _stock_material_row(
        "sync-authoritative-new",
        "无",
        name="96孔收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    new_row["code"] = "0007-01000"

    hardware = _FakeBioyondHardware({0: [new_row], 1: [], 2: []})
    hardware.bioyond_material_cache["codex-visible-old"] = {"bioyond_id": "codex-visible-old"}
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    old_resources = resource_bioyond_to_plr(
        [old_row],
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=workstation.deck,
    )
    assert old_resources
    assert _resource_by_bioyond_id(workstation.deck, "codex-visible-old") is not None

    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external() is True

    assert _resource_by_bioyond_id(workstation.deck, "codex-visible-old") is None
    new_resource = _resource_by_bioyond_id(workstation.deck, "sync-authoritative-new")
    assert new_resource is not None
    assert getattr(new_resource, "parent", None) is not None
    assert synchronizer.last_sync_result["removed_stale_count"] == 1
    assert synchronizer.last_sync_result["converted_count"] == 1
    assert synchronizer.last_sync_result["clear_stale"] is False
    assert "codex-visible-old" in hardware.bioyond_material_cache
    assert "sync-authoritative-new" in hardware.bioyond_material_cache


def test_sync_from_external_clear_stale_prunes_cache_and_deck() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    old_row = _stock_material_row(
        "codex-clear-old",
        "96孔收集板",
        name="清理旧收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    new_row = _stock_material_row(
        "codex-clear-new",
        "96孔收集板",
        name="清理新收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=2,
    )
    hardware = _FakeBioyondHardware({0: [new_row], 1: [], 2: []})
    hardware.bioyond_material_cache["codex-clear-old"] = {"bioyond_id": "codex-clear-old"}
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    resource_bioyond_to_plr(
        [old_row],
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=workstation.deck,
    )
    assert _resource_by_bioyond_id(workstation.deck, "codex-clear-old") is not None

    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external(clear_stale=True) is True

    assert _resource_by_bioyond_id(workstation.deck, "codex-clear-old") is None
    assert _resource_by_bioyond_id(workstation.deck, "codex-clear-new") is not None
    assert "codex-clear-old" not in hardware.bioyond_material_cache
    assert "codex-clear-new" in hardware.bioyond_material_cache
    assert synchronizer.last_sync_result["clear_stale"] is True


def test_sync_from_external_clear_stale_clears_same_slot_resource_without_id() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_synthesizer_press_cover" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    old_row = _stock_material_row(
        "press-cover-old",
        "多肽合成仪压盖",
        name="多肽合成仪压盖",
        wh_name="盖板缓冲库",
        x=1,
        y=1,
    )
    old_row["code"] = "0025-00023"
    new_row = _stock_material_row(
        "press-cover-new",
        "多肽合成仪压盖",
        name="多肽合成仪压盖",
        wh_name="盖板缓冲库",
        x=1,
        y=1,
    )
    new_row["code"] = "0025-00023"

    hardware = _FakeBioyondHardware({0: [new_row], 1: [], 2: []})
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    old_resources = resource_bioyond_to_plr(
        [old_row],
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=workstation.deck,
    )
    assert old_resources
    old_resource = workstation.deck.warehouses["盖板缓冲库"][0]
    assert old_resource is not None
    old_resource.unilabos_extra = {
        key: value
        for key, value in (getattr(old_resource, "unilabos_extra", {}) or {}).items()
        if key not in {"material_bioyond_id", "bioyond_material"}
    }

    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external(clear_stale=True) is True

    new_resource = _resource_by_bioyond_id(workstation.deck, "press-cover-new")
    assert new_resource is not None
    assert new_resource.parent is workstation.deck.warehouses["盖板缓冲库"]
    assert new_resource.name == "多肽合成仪压盖-00023"
    assert "_2" not in new_resource.name


def test_bioyond_slot_from_location_resolves_remapped_tip_stack() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation

    site = object()
    left_stack = SimpleNamespace(
        name="站内Tip盒堆栈(左)",
        capacity=4,
        num_items_x=2,
        num_items_y=2,
        ordering_layout="row-major",
        sites=[site, object(), object(), object()],
        _ordering={"A1": site},
    )
    station = object.__new__(BioyondPeptideStation)
    station.deck = SimpleNamespace(warehouses={"站内Tip盒堆栈(左)": left_stack})

    warehouse, idx, slot_key = station._bioyond_slot_from_location({
        "whName": "站内Tip盒堆栈",
        "x": 1,
        "y": 2,
        "z": 1,
    })

    assert warehouse is left_stack
    assert idx == 0
    assert slot_key == "A1"


def test_resource_bioyond_to_plr_writes_canonical_id_from_material_id() -> None:
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck
    from bioyond_peptide_station._vendored.graphio_bioyond import resource_bioyond_to_plr

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    row = _stock_material_row(
        "material-id-only",
        "96孔收集板",
        name="仅materialId收集板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    row["materialId"] = row.pop("id")
    deck = BIOYOND_PeptideStation_Deck(setup=True)

    resources = resource_bioyond_to_plr(
        [row],
        type_mapping=peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
        deck=deck,
    )

    assert resources
    assert resources[0].unilabos_extra["material_bioyond_id"] == "material-id-only"


def test_sync_from_external_accepts_null_type_name_with_detail_rows() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    row = _stock_material_row(
        "null-type-detail",
        "96孔收集板",
        name="空类型明细板",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    row["typeName"] = None
    row["detail"] = [
        {"name": "多肽", "typeName": None, "x": 1, "y": 1, "z": 1, "quantity": 1}
    ]
    hardware = _FakeBioyondHardware({0: [row], 1: [], 2: []})
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)

    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external(clear_stale=True) is True
    resource = _resource_by_bioyond_id(workstation.deck, "null-type-detail")
    assert resource is not None


def test_sync_from_external_uses_material_info_fallback_for_missing_type_and_code() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    missing = {
        "id": "sync-info-fallback",
        "typeName": None,
        "code": None,
        "name": "报送名称",
        "locations": [],
        "detail": [],
    }
    detailed = _stock_material_row(
        "sync-info-fallback",
        "96孔收集板",
        name="详情名称",
        wh_name=AUTO_WAREHOUSE,
        x=1,
        y=1,
    )
    hardware = _FakeBioyondHardware({0: [missing], 1: [], 2: []})
    hardware.material_info_by_id["sync-info-fallback"] = detailed
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external() is True

    resource = _resource_by_bioyond_id(workstation.deck, "sync-info-fallback")
    assert hardware.material_info_calls == ["sync-info-fallback"]
    assert resource is not None
    assert resource.unilabos_extra["material_bioyond_name"] == "详情名称"
    assert hardware.bioyond_material_cache["sync-info-fallback"]["raw_name"] == "详情名称"


def test_sync_from_external_missing_code_after_material_info_uses_regular_container() -> None:
    from bioyond_peptide_station.peptide_station import BioyondPeptideStation
    from unilabos.registry.registry import lab_registry
    from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck

    if "bioyond_peptide_96_well_collection_plate" not in lab_registry.resource_type_registry:
        lab_registry.setup()

    missing = {
        "id": "sync-regular-fallback",
        "typeName": None,
        "code": None,
        "name": "未知容器",
        "locations": [{
            "id": "sync-regular-fallback:location",
            "whName": AUTO_WAREHOUSE,
            "x": 1,
            "y": 1,
            "z": 1,
        }],
        "detail": [],
    }
    hardware = _FakeBioyondHardware({0: [missing], 1: [], 2: []})
    hardware.material_info_by_id["sync-regular-fallback"] = {
        **missing,
        "name": "详情未知容器",
        "typeName": "96孔收集板",
    }
    workstation = object.__new__(BioyondPeptideStation)
    workstation.hardware_interface = hardware
    workstation.bioyond_config = {
        "sync_interval": 600,
        "material_type_mappings": peptide_materials.DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS,
    }
    workstation.deck = BIOYOND_PeptideStation_Deck(setup=True)
    synchronizer = station_module.BioyondResourceSynchronizer(workstation)

    assert synchronizer.sync_from_external() is True

    resource = _resource_by_bioyond_id(workstation.deck, "sync-regular-fallback")
    record = hardware.bioyond_material_cache["sync-regular-fallback"]
    assert hardware.material_info_calls == ["sync-regular-fallback"]
    assert resource is not None
    assert type(resource).__name__ == "RegularContainer"
    assert resource.unilabos_extra["material_bioyond_name"] == "详情未知容器"
    assert resource.unilabos_extra["bioyond_regular_container_fallback"] is True
    assert record["regular_container_fallback"] is True
    assert "missing_material_code_fallback_regular_container" in record["warnings"]
