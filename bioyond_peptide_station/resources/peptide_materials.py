"""Peptide Station Material Resource Definitions."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

try:
    from pylabrobot.resources import (
        Container,
        Coordinate,
        Plate,
        ResourceHolder,
        Tip,
        TipRack,
        TipSpot,
        Well,
        create_ordered_items_2d,
    )
    from pylabrobot.resources.well import CrossSectionType, WellBottomType
    from unilabos.resources.itemized_carrier import ItemizedCarrier
except Exception:  # pragma: no cover
    class _FallbackResource:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.children = []
            self.ordered_items = kwargs.get("ordered_items")
            self.ordering = kwargs.get("ordering")
            self.model = kwargs.get("model")

    class Container(_FallbackResource):  # type: ignore[no-redef]
        pass

    class Plate(_FallbackResource):  # type: ignore[no-redef]
        pass

    class TipRack(_FallbackResource):  # type: ignore[no-redef]
        pass

    class Well(_FallbackResource):  # type: ignore[no-redef]
        pass

    class Tip(_FallbackResource):  # type: ignore[no-redef]
        pass

    class TipSpot(_FallbackResource):  # type: ignore[no-redef]
        pass

    class ResourceHolder(_FallbackResource):  # type: ignore[no-redef]
        pass

    class ItemizedCarrier(_FallbackResource):  # type: ignore[no-redef]
        pass

    class Coordinate:  # type: ignore[no-redef]
        def __init__(self, x: float = 0, y: float = 0, z: float = 0):
            self.x = x
            self.y = y
            self.z = z

    class _FallbackEnumValue:
        def __init__(self, value: str):
            self.value = value

    class CrossSectionType:  # type: ignore[no-redef]
        CIRCLE = _FallbackEnumValue("circle")
        RECTANGLE = _FallbackEnumValue("rectangle")

    class WellBottomType:  # type: ignore[no-redef]
        FLAT = _FallbackEnumValue("flat")
        V = _FallbackEnumValue("v")

    def create_ordered_items_2d(klass, num_items_x, num_items_y, dx, dy, dz, item_dx, item_dy, **kwargs):  # type: ignore[no-redef]
        items = OrderedDict()
        for x in range(num_items_x):
            for y in range(num_items_y):
                label = f"{chr(ord('A') + y)}{x + 1}"
                items[label] = klass(label, **kwargs)
        return items

try:
    from unilabos.registry.decorators import resource
except Exception:  # pragma: no cover
    def resource(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator


FRONTEND_POSE_EXTRA = "unilabos_frontend_pose_extra"
SERIALIZED_RESOURCE_KWARGS = {
    "barcode",
    "children",
    "location",
    "parent_name",
    "preferred_pickup_location",
    "rotation",
    "type",
}


def _pop_constructor_extras(kwargs: dict[str, Any]) -> dict[str, Any]:
    for key in SERIALIZED_RESOURCE_KWARGS:
        kwargs.pop(key, None)
    extra = kwargs.pop("unilabos_extra", None)
    return dict(extra) if isinstance(extra, dict) else {}


def _merge_metadata(resource_instance: Any, extra: dict[str, Any]) -> None:
    material_data = {}
    for attr, key in (
        ("bioyond_material_type_code", "material_type_code"),
        ("bioyond_material_type_id", "material_type_id"),
        ("bioyond_material_type_name", "material_type_name"),
        ("bioyond_type_mode", "typeMode"),
        ("bioyond_unit", "unit"),
    ):
        value = getattr(resource_instance, attr, None)
        if value is not None:
            material_data[key] = value

    if material_data:
        bioyond_extra = dict(extra.get("bioyond_material_type", {}))
        for key, value in material_data.items():
            bioyond_extra.setdefault(key, value)
        extra["bioyond_material_type"] = bioyond_extra

    visual_metadata = getattr(resource_instance, "visual_metadata", None)
    if isinstance(visual_metadata, dict) and visual_metadata:
        pose_extra = dict(visual_metadata)
        existing_pose_extra = extra.get(FRONTEND_POSE_EXTRA, {})
        if isinstance(existing_pose_extra, dict):
            pose_extra.update(existing_pose_extra)
        extra[FRONTEND_POSE_EXTRA] = pose_extra

    if extra:
        resource_instance.unilabos_extra = extra


def _set_default_ordered_items(kwargs: dict[str, Any], builder: Callable[[], dict[str, Any]]) -> None:
    if kwargs.get("ordered_items") is not None or kwargs.get("ordering") is not None:
        return
    kwargs["ordered_items"] = builder()


def _make_tip(max_volume_ul: float, total_tip_length: float, fitting_depth: float, has_filter: bool = False) -> Tip:
    return Tip(
        has_filter=has_filter,
        maximal_volume=max_volume_ul,
        total_tip_length=total_tip_length,
        fitting_depth=fitting_depth,
    )


def _make_96_well_plate_ordered_items(deep: bool = False) -> dict[str, Well]:
    # 暂时不生成 96 个 well：完整 well 几何使资源树序列化体积过大，拖垮前后端通信。
    # 返回空 dict，使 96 孔板序列化为零子节点的空板（PLR 接受空 ordered_items）。
    # Bioyond detail/details 行不再映射成物理 well 内容。
    # 如需恢复 96 个孔，取消下方注释并删除 `return {}`。
    return {}
    # if deep:
    #     return create_ordered_items_2d(
    #         Well,
    #         num_items_x=12,
    #         num_items_y=8,
    #         dx=10.9,
    #         dy=8.25,
    #         dz=2.0,
    #         item_dx=9.0,
    #         item_dy=9.0,
    #         size_x=8.2,
    #         size_y=8.2,
    #         size_z=42.0,
    #         max_volume=2200,
    #         cross_section_type=CrossSectionType.RECTANGLE,
    #         bottom_type=WellBottomType.V,
    #     )
    # return create_ordered_items_2d(
    #     Well,
    #     num_items_x=12,
    #     num_items_y=8,
    #     dx=10.98,
    #     dy=7.84,
    #     dz=1.7,
    #     item_dx=9.0,
    #     item_dy=9.0,
    #     size_x=6.8,
    #     size_y=6.8,
    #     size_z=10.5,
    #     max_volume=350,
    #     cross_section_type=CrossSectionType.CIRCLE,
    #     bottom_type=WellBottomType.FLAT,
    # )


def _make_384_well_plate_ordered_items() -> dict[str, Well]:
    # 暂时不生成 384 个 well：完整 well 几何使资源树序列化体积过大，拖垮前后端通信。
    # 返回空 dict，使 384 孔板序列化为零子节点的空板（PLR 接受空 ordered_items）。
    # 如需恢复 384 个孔，取消下方注释并删除 `return {}`。
    return {}
    # return create_ordered_items_2d(
    #     Well,
    #     num_items_x=24,
    #     num_items_y=16,
    #     dx=10.43,
    #     dy=7.29,
    #     dz=1.7,
    #     item_dx=4.5,
    #     item_dy=4.5,
    #     size_x=3.4,
    #     size_y=3.4,
    #     size_z=10.0,
    #     max_volume=120,
    #     cross_section_type=CrossSectionType.CIRCLE,
    #     bottom_type=WellBottomType.FLAT,
    # )


def _make_12_lane_trough_ordered_items() -> dict[str, Well]:
    return create_ordered_items_2d(
        Well,
        num_items_x=12,
        num_items_y=1,
        dx=10.28,
        dy=7.14,
        dz=3.55,
        item_dx=9.0,
        item_dy=9.0,
        size_x=8.2,
        size_y=71.2,
        size_z=26.85,
        max_volume=15000,
        cross_section_type=CrossSectionType.RECTANGLE,
        bottom_type=WellBottomType.V,
    )


def _make_single_reservoir_trough_ordered_items() -> dict[str, Well]:
    return create_ordered_items_2d(
        Well,
        num_items_x=1,
        num_items_y=1,
        dx=10.28,
        dy=7.14,
        dz=3.55,
        item_dx=0,
        item_dy=0,
        size_x=107.2,
        size_y=71.2,
        size_z=26.85,
        max_volume=180000,
        cross_section_type=CrossSectionType.RECTANGLE,
        bottom_type=WellBottomType.V,
    )


def _make_tip_spots(
    max_volume_ul: float,
    total_tip_length: float,
    fitting_depth: float,
    dx: float,
    dy: float,
) -> dict[str, TipSpot]:
    return create_ordered_items_2d(
        TipSpot,
        num_items_x=12,
        num_items_y=8,
        dx=dx,
        dy=dy,
        dz=2.0,
        item_dx=9.0,
        item_dy=9.0,
        size_x=7.0,
        size_y=7.0,
        size_z=0,
        make_tip=lambda: _make_tip(max_volume_ul, total_tip_length, fitting_depth),
    )


def _make_one_slot_holder_sites() -> dict[str, ResourceHolder]:
    holder = ResourceHolder(
        name="slot",
        size_x=127.76,
        size_y=85.48,
        size_z=5.0,
        model="bioyond_peptide_material_top_slot",
        child_location=Coordinate(0, 0, 5.0),
    )
    holder.location = Coordinate(0, 0, 20.0)
    return OrderedDict({"A1": holder})


class _PeptideTipRack(TipRack):
    resource_id: str | None = None
    tip_max_volume_ul = 200.0
    tip_total_length = 52.0
    tip_fitting_depth = 5.0
    tip_dx = 8.24
    tip_dy = 6.81
    visual_metadata: dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        extra = _pop_constructor_extras(kwargs)
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 67.0)
        kwargs.setdefault("category", "tip_rack")
        # 暂时不生成 96 个 tip spot / tip：完整 tip 几何使资源树序列化体积过大，
        # 拖垮前后端通信。改为传入空 ordering，使 tip rack 序列化为零子节点的空架
        # （PLR 要求 ordered_items / ordering 至少给一个，故不能两者都省）。
        # 如需恢复带 tip 的行为，删除下方 ordering 默认并取消 _set_default_ordered_items 注释，
        # 同时将 with_tips 设回 True。
        kwargs.setdefault("with_tips", False)
        kwargs.setdefault("ordering", OrderedDict())
        if self.resource_id is not None:
            kwargs.setdefault("model", self.resource_id)
        # _set_default_ordered_items(
        #     kwargs,
        #     lambda: _make_tip_spots(
        #         self.tip_max_volume_ul,
        #         self.tip_total_length,
        #         self.tip_fitting_depth,
        #         self.tip_dx,
        #         self.tip_dy,
        #     ),
        # )
        super().__init__(*args, **kwargs)
        _merge_metadata(self, extra)


class _PeptidePlate(Plate):
    resource_id: str | None = None
    ordered_items_builder: Callable[[], dict[str, Well]] | None = None
    visual_metadata: dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        extra = _pop_constructor_extras(kwargs)
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 14.35)
        kwargs.setdefault("category", "plate")
        kwargs.setdefault("plate_type", "skirted")
        if self.resource_id is not None:
            kwargs.setdefault("model", self.resource_id)
        if self.ordered_items_builder is not None:
            _set_default_ordered_items(kwargs, self.ordered_items_builder)
        super().__init__(*args, **kwargs)
        _merge_metadata(self, extra)


class _PeptideReagentTrough(_PeptidePlate):
    ordered_items_builder = staticmethod(_make_single_reservoir_trough_ordered_items)
    bioyond_type_mode = 2
    bioyond_unit = "mL"
    visual_metadata = {
        "body_color": "#E6F0F2",
        "child_color": "#F6FAFB",
        "texture": "transparent trough",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 31.4)
        kwargs.setdefault("category", "trough")
        kwargs.setdefault("plate_type", "trough")
        super().__init__(*args, **kwargs)


class _Peptide12LaneReagentTrough(_PeptideReagentTrough):
    ordered_items_builder = staticmethod(_make_12_lane_trough_ordered_items)


class _PeptidePassiveLabware(Container):
    resource_id: str | None = None
    visual_metadata: dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        extra = _pop_constructor_extras(kwargs)
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 20.0)
        kwargs.setdefault("category", "plate_adapter")
        if self.resource_id is not None:
            kwargs.setdefault("model", self.resource_id)
        super().__init__(*args, **kwargs)
        _merge_metadata(self, extra)


class _PeptideOneSlotMaterialHolder(ItemizedCarrier):
    resource_id: str | None = None
    visual_metadata: dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        extra = _pop_constructor_extras(kwargs)
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 20.0)
        kwargs.setdefault("category", "deck")
        kwargs.setdefault("num_items_x", 1)
        kwargs.setdefault("num_items_y", 1)
        kwargs.setdefault("num_items_z", 1)
        if self.resource_id is not None:
            kwargs.setdefault("model", self.resource_id)
        kwargs.setdefault("sites", _make_one_slot_holder_sites())
        super().__init__(*args, **kwargs)
        _merge_metadata(self, extra)


@resource(
    id="bioyond_peptide_1000ul_tip_rack",
    category=["labware", "tip_rack"],
    description="1000uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_1000ul_TipRack(_PeptideTipRack):
    resource_id = "bioyond_peptide_1000ul_tip_rack"
    bioyond_material_type_code = "0009"
    tip_max_volume_ul = 1000.0
    tip_total_length = 55.0
    tip_fitting_depth = 8.0
    tip_dx = 10.52
    tip_dy = 7.43
    visual_metadata = {
        "body_color": "#D7DCE2",
        "child_color": "#2F80ED",
        "texture": "opaque rack, translucent tips",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 98.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_200ul_tip_rack",
    category=["labware", "tip_rack"],
    description="200uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_200ul_TipRack(_PeptideTipRack):
    resource_id = "bioyond_peptide_200ul_tip_rack"
    bioyond_material_type_code = "0008"
    tip_max_volume_ul = 200.0
    tip_total_length = 52.0
    tip_fitting_depth = 5.0
    tip_dx = 8.24
    tip_dy = 6.81
    visual_metadata = {
        "body_color": "#D7DCE2",
        "child_color": "#F2C94C",
        "texture": "opaque rack, translucent tips",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 67.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_50ul_tip_rack",
    category=["labware", "tip_rack"],
    description="50uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_50ul_TipRack(_PeptideTipRack):
    resource_id = "bioyond_peptide_50ul_tip_rack"
    bioyond_material_type_code = "0012"
    tip_max_volume_ul = 50.0
    tip_total_length = 50.0
    tip_fitting_depth = 5.0
    tip_dx = 8.49
    tip_dy = 7.06
    visual_metadata = {
        "body_color": "#D7DCE2",
        "child_color": "#F4F7F8",
        "texture": "opaque rack, translucent tips",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 60.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_deep_well_plate",
    category=["labware", "plate"],
    description="96 well deep well plate for Bioyond peptide station",
)
class BioyondPeptide_96WellDeepWellPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_deep_well_plate"
    bioyond_material_type_code = "0011"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=True))
    visual_metadata = {
        "body_color": "#DDE7EA",
        "child_color": "#EEF5F6",
        "texture": "translucent polypropylene",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 44.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_synthesis_plate",
    category=["labware", "plate"],
    description="96 well solid-phase synthesis plate for Bioyond peptide station",
)
class BioyondPeptide_96WellSynthesisPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_synthesis_plate"
    bioyond_material_type_code = "0001"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=True))
    visual_metadata = {
        "body_color": "#F2F0E8",
        "child_color": "#FAF8F0",
        "texture": "matte solid-phase plate",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 44.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_collection_plate",
    category=["labware", "plate"],
    description="96 well collection plate for Bioyond peptide station",
)
class BioyondPeptide_96WellCollectionPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_collection_plate"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=True))
    bioyond_material_type_code = "0007"
    bioyond_material_type_id = "3a1890ba-d19b-f260-f3c5-a34e6d416766"
    bioyond_material_type_name = "96孔收集板"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#DDE7EA",
        "child_color": "#EEF5F6",
        "texture": "translucent polypropylene",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 44.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_synthesis_plate_base",
    category=["deck"],
    description="Slot-bearing 96 well solid-phase synthesis plate base for Bioyond peptide station",
)
class BioyondPeptide_96WellSynthesisPlateBase(_PeptideOneSlotMaterialHolder):
    resource_id = "bioyond_peptide_96_well_synthesis_plate_base"
    bioyond_material_type_code = "0039"
    visual_metadata = {
        "body_color": "#555B61",
        "texture": "opaque adapter base",
    }


@resource(
    id="bioyond_peptide_96_well_balance_plate",
    category=["labware", "plate"],
    description="96 well balance plate for Bioyond peptide station",
)
class BioyondPeptide_96WellBalancePlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_balance_plate"
    bioyond_material_type_code = "0002"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=False))
    visual_metadata = {
        "body_color": "#D8E3EA",
        "child_color": "#E8F0F4",
        "texture": "opaque matte balance plate",
    }


@resource(
    id="bioyond_peptide_96_well_assay_plate",
    category=["labware", "plate"],
    description="96 well assay plate for Bioyond peptide station",
)
class BioyondPeptide_96WellAssayPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_assay_plate"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=False))
    bioyond_material_type_code = "0010"
    bioyond_material_type_id = "3a1890bb-d9be-d0c4-0b63-8333dc9b4cac"
    bioyond_material_type_name = "96孔酶标板"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EAF2F4",
        "child_color": "#F6FAFB",
        "texture": "transparent assay plate",
    }


@resource(
    id="bioyond_peptide_384_well_plate",
    category=["labware", "plate"],
    description="384 well plate for Bioyond peptide station",
)
class BioyondPeptide_384WellPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_384_well_plate"
    bioyond_material_type_code = "0018"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    visual_metadata = {
        "body_color": "#EAF2F4",
        "child_color": "#F6FAFB",
        "texture": "transparent assay plate",
    }


@resource(
    id="bioyond_peptide_idot_384_peptide_plate",
    category=["labware", "plate"],
    description="IDOT 384 well peptide plate for Bioyond peptide station",
)
class BioyondPeptide_IDOT384PeptidePlate(_PeptidePlate):
    resource_id = "bioyond_peptide_idot_384_peptide_plate"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    bioyond_material_type_code = "0016"
    bioyond_material_type_id = "3a1890be-4fa6-71ec-ed4b-10fcf7224fbd"
    bioyond_material_type_name = "IDOT384孔板（多肽）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EAF2F4",
        "child_color": "#F6FAFB",
        "texture": "transparent IDOT peptide plate",
    }


@resource(
    id="bioyond_peptide_idot_384_carboxylic_acid_plate",
    category=["labware", "plate"],
    description="IDOT 384 well carboxylic acid plate for Bioyond peptide station",
)
class BioyondPeptide_IDOT384CarboxylicAcidPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_idot_384_carboxylic_acid_plate"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    bioyond_material_type_code = "0017"
    bioyond_material_type_id = "3a1890be-bf83-a28a-0666-26f70cb86451"
    bioyond_material_type_name = "IDOT384孔板（羧酸）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EEF1E8",
        "child_color": "#FAFBF4",
        "texture": "transparent IDOT carboxylic acid plate",
    }


@resource(
    id="bioyond_peptide_384_carboxylic_acid_plate",
    category=["labware", "plate"],
    description="384 well carboxylic acid plate for Bioyond peptide station",
)
class BioyondPeptide_384CarboxylicAcidPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_384_carboxylic_acid_plate"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    bioyond_material_type_code = "0019"
    bioyond_material_type_id = "3a1890bf-7b7b-153f-d75a-bd6653844a3e"
    bioyond_material_type_name = "384孔羧酸板"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EEF1E8",
        "child_color": "#FAFBF4",
        "texture": "transparent carboxylic acid plate",
    }


@resource(
    id="bioyond_peptide_384_lcms_plate",
    category=["labware", "plate"],
    description="384 well LCMS plate for Bioyond peptide station",
)
class BioyondPeptide_384LCMSPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_384_lcms_plate"
    bioyond_material_type_code = "0049"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    visual_metadata = {
        "body_color": "#EAF2F4",
        "child_color": "#F6FAFB",
        "texture": "transparent low-bind LCMS plate",
    }


@resource(
    id="bioyond_peptide_384_balance_plate",
    category=["labware", "plate"],
    description="384 well balance plate for Bioyond peptide station",
)
class BioyondPeptide_384BalancePlate(_PeptidePlate):
    resource_id = "bioyond_peptide_384_balance_plate"
    bioyond_material_type_code = "0026"
    ordered_items_builder = staticmethod(_make_384_well_plate_ordered_items)
    visual_metadata = {
        "body_color": "#D8E3EA",
        "child_color": "#E8F0F4",
        "texture": "opaque matte balance plate",
    }


@resource(
    id="bioyond_peptide_96_well_carboxylic_acid_plate",
    category=["labware", "plate"],
    description="96 well carboxylic acid plate for Bioyond peptide station",
)
class BioyondPeptide_96WellCarboxylicAcidPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_carboxylic_acid_plate"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=False))
    bioyond_material_type_code = "0032"
    bioyond_material_type_id = "3a19bb8d-a380-cea7-921e-14a2e7f1ea95"
    bioyond_material_type_name = "96孔羧酸板"
    bioyond_type_mode = 2
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EEF1E8",
        "child_color": "#FAFBF4",
        "texture": "opaque matte carboxylic acid plate",
    }


@resource(
    id="bioyond_peptide_96_well_standard_curve_assay_plate",
    category=["labware", "plate"],
    description="96 well standard curve assay plate for Bioyond peptide station",
)
class BioyondPeptide_96WellStandardCurveAssayPlate(_PeptidePlate):
    resource_id = "bioyond_peptide_96_well_standard_curve_assay_plate"
    ordered_items_builder = staticmethod(lambda: _make_96_well_plate_ordered_items(deep=False))
    bioyond_material_type_code = "0042"
    bioyond_material_type_id = "3a1e0e84-26df-9b27-b2a7-b1ac22053f49"
    bioyond_material_type_name = "96孔酶标板（标曲）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#EAF2F4",
        "child_color": "#F6FAFB",
        "texture": "transparent standard curve assay plate",
    }


@resource(
    id="bioyond_peptide_cover_plate",
    category=["labware", "cover"],
    description="Cover plate for Bioyond peptide station",
)
class BioyondPeptide_CoverPlate(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_cover_plate"
    bioyond_material_type_code = "0035"
    visual_metadata = {
        "body_color": "#B7791F",
        "texture": "translucent cover",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 8.0)
        kwargs.setdefault("category", "plate_adapter")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_sealing_base",
    category=["labware", "adapter"],
    description="Sealing base for Bioyond peptide station",
)
class BioyondPeptide_SealingBase(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_sealing_base"
    bioyond_material_type_code = "0041"
    visual_metadata = {
        "body_color": "#555B61",
        "texture": "opaque adapter base",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 20.0)
        kwargs.setdefault("category", "plate_adapter")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_synthesizer_press_cover",
    category=["labware", "cover"],
    description="Peptide synthesizer press cover for Bioyond peptide station",
)
class BioyondPeptide_SynthesizerPressCover(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_synthesizer_press_cover"
    bioyond_material_type_code = "0025"
    bioyond_material_type_id = "3a18b44d-7ea0-6654-3a94-16526c6fd6e0"
    bioyond_material_type_name = "多肽合成仪压盖"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#7B8794",
        "texture": "opaque press cover",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 18.0)
        kwargs.setdefault("category", "plate_adapter")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_reused_1000ul_tip_rack",
    category=["labware", "tip_rack"],
    description="Reusable 1mL tip rack for Bioyond peptide station",
)
class BioyondPeptide_Reused1000ulTipRack(BioyondPeptide_1000ul_TipRack):
    resource_id = "bioyond_peptide_reused_1000ul_tip_rack"
    bioyond_material_type_code = "0027"
    bioyond_material_type_id = "3a18c9ed-79be-9bd4-1f39-9d0128c3a78a"
    bioyond_material_type_name = "1mL枪头盒（复用）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#CAD2D9",
        "child_color": "#2F80ED",
        "texture": "reusable opaque rack, translucent tips",
    }


@resource(
    id="bioyond_peptide_reused_200ul_tip_rack",
    category=["labware", "tip_rack"],
    description="Reusable 200uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_Reused200ulTipRack(BioyondPeptide_200ul_TipRack):
    resource_id = "bioyond_peptide_reused_200ul_tip_rack"
    bioyond_material_type_code = "0028"
    bioyond_material_type_id = "3a18d7fe-63a5-0bd6-984b-5c01812ac87a"
    bioyond_material_type_name = "200μL枪头盒（复用）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#CAD2D9",
        "child_color": "#F2C94C",
        "texture": "reusable opaque rack, translucent tips",
    }


@resource(
    id="bioyond_peptide_middle_cover_plate",
    category=["labware", "cover"],
    description="Middle cover plate for Bioyond peptide station",
)
class BioyondPeptide_MiddleCoverPlate(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_middle_cover_plate"
    bioyond_material_type_code = "0033"
    bioyond_material_type_id = "3a19d5a6-1055-7ed7-00b5-8a37cc7d8209"
    bioyond_material_type_name = "中层盖板"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#A8732A",
        "texture": "translucent middle cover",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 8.0)
        kwargs.setdefault("category", "plate_adapter")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_upper_cover_plate",
    category=["labware", "cover"],
    description="Upper cover plate for Bioyond peptide station",
)
class BioyondPeptide_UpperCoverPlate(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_upper_cover_plate"
    bioyond_material_type_code = "0034"
    bioyond_material_type_id = "3a19d5a6-5a57-0a45-35ca-67ded9a213cc"
    bioyond_material_type_name = "上层盖板"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#8B5E20",
        "texture": "translucent upper cover",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_z", 8.0)
        kwargs.setdefault("category", "plate_adapter")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_200ul_g3_tip_rack",
    category=["labware", "tip_rack"],
    description="200uL G3 tip rack for Bioyond peptide station",
)
class BioyondPeptide_200ulG3TipRack(BioyondPeptide_200ul_TipRack):
    resource_id = "bioyond_peptide_200ul_g3_tip_rack"
    bioyond_material_type_code = "0036"
    bioyond_material_type_id = "3a19e41c-f001-104b-3b70-7fb1ec626fc6"
    bioyond_material_type_name = "200μL枪头盒（G3）"
    bioyond_type_mode = 0
    bioyond_unit = "个"
    visual_metadata = {
        "body_color": "#BCC7D1",
        "child_color": "#F2C94C",
        "texture": "G3 opaque rack, translucent tips",
    }


@resource(
    id="bioyond_peptide_empty_material",
    category=["labware", "container"],
    description="Empty material marker for Bioyond peptide station",
)
class BioyondPeptide_EmptyMaterial(_PeptidePassiveLabware):
    resource_id = "bioyond_peptide_empty_material"
    bioyond_material_type_code = "0037"
    bioyond_material_type_id = "3a1a1db1-9644-24a6-b6ad-123b6279464d"
    bioyond_material_type_name = "无"
    bioyond_type_mode = 0
    bioyond_unit = "pcs"
    visual_metadata = {
        "body_color": "#E4E7EB",
        "texture": "empty material marker",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_x", 20.0)
        kwargs.setdefault("size_y", 20.0)
        kwargs.setdefault("size_z", 2.0)
        kwargs.setdefault("category", "material_marker")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_reagent_trough",
    category=["labware", "trough"],
    description="Reagent trough for Bioyond peptide station",
)
class BioyondPeptide_ReagentTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_trough"
    bioyond_material_type_code = "0024"
    bioyond_material_type_id = "3a18b431-ac58-ca2e-9680-2a4f5880ea45"
    bioyond_material_type_name = "12道试剂槽"
    bioyond_type_mode = 0
    bioyond_unit = "个"


@resource(
    id="bioyond_peptide_reagent_0003_reduction_cleavage_trough",
    category=["labware", "trough"],
    description="Reduction cleavage reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0003_ReductionCleavageTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0003_reduction_cleavage_trough"
    bioyond_material_type_code = "0003"
    bioyond_material_type_id = "3a1890b7-aa75-56b1-13ec-5d170f9855ea"
    bioyond_material_type_name = "还原裂解液"


@resource(
    id="bioyond_peptide_reagent_0004_ether_trough",
    category=["labware", "trough"],
    description="Ether reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0004_EtherTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0004_ether_trough"
    bioyond_material_type_code = "0004"
    bioyond_material_type_id = "3a1890b7-fcc4-5399-eb9d-ef41f08a0add"
    bioyond_material_type_name = "乙醚"


@resource(
    id="bioyond_peptide_reagent_0005_acetonitrile_trough",
    category=["labware", "trough"],
    description="Acetonitrile reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0005_AcetonitrileTrough(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0005_acetonitrile_trough"
    bioyond_material_type_code = "0005"
    bioyond_material_type_id = "3a1890b9-070c-d403-4092-21d941aba19b"
    bioyond_material_type_name = "乙腈板"
    bioyond_type_mode = 0


@resource(
    id="bioyond_peptide_reagent_0006_ellman_trough",
    category=["labware", "trough"],
    description="Ellman reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0006_EllmanTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0006_ellman_trough"
    bioyond_material_type_code = "0006"
    bioyond_material_type_id = "3a1890b9-4f75-92dd-86d2-b7378729b8a3"
    bioyond_material_type_name = "Ellman"


@resource(
    id="bioyond_peptide_reagent_0013_nh4hco3_buffer_1_trough",
    category=["labware", "trough"],
    description="NH4HCO3 buffer 1 reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0013_NH4HCO3Buffer1Trough(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0013_nh4hco3_buffer_1_trough"
    bioyond_material_type_code = "0013"
    bioyond_material_type_id = "3a1890bd-1634-908d-b73a-191aa55e0ec9"
    bioyond_material_type_name = "NH4HCO3缓冲液板1"


@resource(
    id="bioyond_peptide_reagent_0014_coupling_reagent_trough",
    category=["labware", "trough"],
    description="Coupling reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0014_CouplingReagentTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0014_coupling_reagent_trough"
    bioyond_material_type_code = "0014"
    bioyond_material_type_id = "3a1890bd-5ddb-c39f-d095-c7962e0f1f43"
    bioyond_material_type_name = "连接试剂"


@resource(
    id="bioyond_peptide_reagent_0015_mercaptoethanol_trough",
    category=["labware", "trough"],
    description="Mercaptoethanol reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0015_MercaptoethanolTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0015_mercaptoethanol_trough"
    bioyond_material_type_code = "0015"
    bioyond_material_type_id = "3a1890bd-d799-3414-bf9c-40e0dcaa8ddd"
    bioyond_material_type_name = "2-巯基乙醇"


@resource(
    id="bioyond_peptide_reagent_0020_300ml_cleavage_trough",
    category=["labware", "trough"],
    description="300mL cleavage reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0020_300mLCleavageTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0020_300ml_cleavage_trough"
    bioyond_material_type_code = "0020"
    bioyond_material_type_id = "3a189b09-ca6b-5116-1718-5ce2da0fa1fa"
    bioyond_material_type_name = "300mL裂解液试剂槽"


@resource(
    id="bioyond_peptide_reagent_0021_300ml_ether_trough_1",
    category=["labware", "trough"],
    description="300mL ether reagent trough 1 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0021_300mLEtherTrough1(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0021_300ml_ether_trough_1"
    bioyond_material_type_code = "0021"
    bioyond_material_type_id = "3a189b18-d5fc-94c0-df55-6b314bf5bff8"
    bioyond_material_type_name = "300mL乙醚试剂槽1"


@resource(
    id="bioyond_peptide_reagent_0023_acetonitrile_water_trough_1",
    category=["labware", "trough"],
    description="Acetonitrile water reagent trough 1 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0023_AcetonitrileWaterTrough1(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0023_acetonitrile_water_trough_1"
    bioyond_material_type_code = "0023"
    bioyond_material_type_id = "3a18a11a-aeab-b690-ed0e-925598069f8c"
    bioyond_material_type_name = "乙腈/水板1"


@resource(
    id="bioyond_peptide_reagent_0029_mercaptoethanol_solution_trough",
    category=["labware", "trough"],
    description="Mercaptoethanol solution reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0029_MercaptoethanolSolutionTrough(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0029_mercaptoethanol_solution_trough"
    bioyond_material_type_code = "0029"
    bioyond_material_type_id = "3a18d8d9-c4f1-93bd-f2c1-8bfc2e7ed783"
    bioyond_material_type_name = "2-巯基乙醇溶液板"


@resource(
    id="bioyond_peptide_reagent_0030_carboxylic_acid_trough",
    category=["labware", "trough"],
    description="Carboxylic acid reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0030_CarboxylicAcidTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0030_carboxylic_acid_trough"
    bioyond_material_type_code = "0030"
    bioyond_material_type_id = "3a18edc3-0bb6-c70e-fb2c-6e1dce3713fb"
    bioyond_material_type_name = "羧酸"


@resource(
    id="bioyond_peptide_reagent_0038_300ml_dmso_trough",
    category=["labware", "trough"],
    description="300mL DMSO reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0038_300mLDMSOTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0038_300ml_dmso_trough"
    bioyond_material_type_code = "0038"
    bioyond_material_type_id = "3a1aacf7-6bbd-c6eb-d8a2-623b3a441514"
    bioyond_material_type_name = "300mLDMSO试剂槽"


@resource(
    id="bioyond_peptide_reagent_0040_300ml_tris_lcms_trough",
    category=["labware", "trough"],
    description="300mL Tris LCMS reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0040_300mLTrisLCMSTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0040_300ml_tris_lcms_trough"
    bioyond_material_type_code = "0040"
    bioyond_material_type_id = "3a1c8c10-a6ce-95a0-38d3-077199369a73"
    bioyond_material_type_name = "300mLTris试剂槽（LCMS）"


@resource(
    id="bioyond_peptide_reagent_0043_acetonitrile_water_trough_2",
    category=["labware", "trough"],
    description="Acetonitrile water reagent trough 2 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0043_AcetonitrileWaterTrough2(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0043_acetonitrile_water_trough_2"
    bioyond_material_type_code = "0043"
    bioyond_material_type_id = "3a1e5afe-3c35-5fc1-8432-9ad91cc1682f"
    bioyond_material_type_name = "乙腈/水板2"


@resource(
    id="bioyond_peptide_reagent_0044_nh4hco3_buffer_2_trough",
    category=["labware", "trough"],
    description="NH4HCO3 buffer 2 reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0044_NH4HCO3Buffer2Trough(_Peptide12LaneReagentTrough):
    resource_id = "bioyond_peptide_reagent_0044_nh4hco3_buffer_2_trough"
    bioyond_material_type_code = "0044"
    bioyond_material_type_id = "3a1e5aff-c5bb-0f74-1751-8ea1c3ad8fdd"
    bioyond_material_type_name = "NH4HCO3缓冲液板2"


@resource(
    id="bioyond_peptide_reagent_0045_300ml_ether_trough_2",
    category=["labware", "trough"],
    description="300mL ether reagent trough 2 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0045_300mLEtherTrough2(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0045_300ml_ether_trough_2"
    bioyond_material_type_code = "0045"
    bioyond_material_type_id = "3a1e5b00-f51d-83bb-580d-f86a780b1583"
    bioyond_material_type_name = "300mL乙醚试剂槽2"


@resource(
    id="bioyond_peptide_reagent_0046_300ml_tris_assay_trough",
    category=["labware", "trough"],
    description="300mL Tris assay reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0046_300mLTrisAssayTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0046_300ml_tris_assay_trough"
    bioyond_material_type_code = "0046"
    bioyond_material_type_id = "3a1e6a8b-0364-6125-5bcf-0ebe51056951"
    bioyond_material_type_name = "300mLTris试剂槽（酶标）"


@resource(
    id="bioyond_peptide_reagent_0047_protein_buffer_trough",
    category=["labware", "trough"],
    description="Protein buffer reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0047_ProteinBufferTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0047_protein_buffer_trough"
    bioyond_material_type_code = "0047"
    bioyond_material_type_id = "3a1e6a8b-4c0d-1906-52b7-784e796b0a68"
    bioyond_material_type_name = "蛋白缓冲液试剂槽"


@resource(
    id="bioyond_peptide_reagent_0048_fluorescent_substrate_trough",
    category=["labware", "trough"],
    description="Fluorescent substrate reagent trough for Bioyond peptide station",
)
class BioyondPeptide_Reagent0048_FluorescentSubstrateTrough(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0048_fluorescent_substrate_trough"
    bioyond_material_type_code = "0048"
    bioyond_material_type_id = "3a1e6a8b-8276-9ac3-f665-9501ed397e1b"
    bioyond_material_type_name = "荧光酶底物试剂槽"


@resource(
    id="bioyond_peptide_reagent_0050_300ml_ether_trough_3",
    category=["labware", "trough"],
    description="300mL ether reagent trough 3 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0050_300mLEtherTrough3(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0050_300ml_ether_trough_3"
    bioyond_material_type_code = "0050"
    bioyond_material_type_id = "3a1e6a8e-3ae6-5399-22bd-2ae381405c8e"
    bioyond_material_type_name = "300mL乙醚试剂槽3"


@resource(
    id="bioyond_peptide_reagent_0051_300ml_ether_trough_4",
    category=["labware", "trough"],
    description="300mL ether reagent trough 4 for Bioyond peptide station",
)
class BioyondPeptide_Reagent0051_300mLEtherTrough4(_PeptideReagentTrough):
    resource_id = "bioyond_peptide_reagent_0051_300ml_ether_trough_4"
    bioyond_material_type_code = "0051"
    bioyond_material_type_id = "3a1e6a8e-71be-17f6-70e5-98b33e5ec695"
    bioyond_material_type_name = "300mL乙醚试剂槽4"


DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS = {
    "bioyond_peptide_1000ul_tip_rack": ["1000μL枪头盒", "3a1890bb-736e-cfdd-3213-eb314e8a60f9"],
    "bioyond_peptide_200ul_tip_rack": ["200μL枪头盒", "3a1890bb-36d1-964a-18bd-0bf0f2877a7b"],
    "bioyond_peptide_50ul_tip_rack": ["50μL枪头盒", "3a1890bc-5fae-361c-cc09-e6f2f6dcd71d"],
    "bioyond_peptide_96_well_deep_well_plate": ["96孔深孔板", "3a1890bc-1fa8-fe39-9faa-12279ed4569b"],
    "bioyond_peptide_96_well_synthesis_plate": ["96孔固相合成板", "3a1871cb-99f3-f01d-23e2-08dbbd0045b5"],
    "bioyond_peptide_96_well_collection_plate": ["96孔收集板", "3a1890ba-d19b-f260-f3c5-a34e6d416766"],
    "bioyond_peptide_96_well_synthesis_plate_base": ["96孔固相合成板底座", "3a1b997e-241b-64f0-80d1-47bca08799d1"],
    "bioyond_peptide_96_well_balance_plate": ["96孔配平板", "3a187661-2378-1e20-fa5c-a27d49fdc15d"],
    "bioyond_peptide_96_well_assay_plate": ["96孔酶标板", "3a1890bb-d9be-d0c4-0b63-8333dc9b4cac"],
    "bioyond_peptide_384_well_plate": ["384孔酶标板", "3a1890bf-2148-ed20-92bd-d85869947d9a"],
    "bioyond_peptide_idot_384_peptide_plate": ["IDOT384孔板（多肽）", "3a1890be-4fa6-71ec-ed4b-10fcf7224fbd"],
    "bioyond_peptide_idot_384_carboxylic_acid_plate": ["IDOT384孔板（羧酸）", "3a1890be-bf83-a28a-0666-26f70cb86451"],
    "bioyond_peptide_384_carboxylic_acid_plate": ["384孔羧酸板", "3a1890bf-7b7b-153f-d75a-bd6653844a3e"],
    "bioyond_peptide_384_lcms_plate": ["384孔LCMS板", "3a1e6a8b-cb61-74da-a089-8e6f197f80f0"],
    "bioyond_peptide_384_balance_plate": ["384孔配平板", "3a18be7e-47cc-888c-fc68-055753286826"],
    "bioyond_peptide_96_well_carboxylic_acid_plate": ["96孔羧酸板", "3a19bb8d-a380-cea7-921e-14a2e7f1ea95"],
    "bioyond_peptide_96_well_standard_curve_assay_plate": ["96孔酶标板（标曲）", "3a1e0e84-26df-9b27-b2a7-b1ac22053f49"],
    "bioyond_peptide_cover_plate": ["防挥发盖板", "3a19d5a6-b0e2-b486-e5eb-bcabc632f4de"],
    "bioyond_peptide_sealing_base": ["封膜底座", "3a1d1d7b-e33b-6975-165d-c56cba5ed345"],
    "bioyond_peptide_reagent_trough": ["12道试剂槽", "3a18b431-ac58-ca2e-9680-2a4f5880ea45"],
    "bioyond_peptide_reagent_0003_reduction_cleavage_trough": ["还原裂解液", "3a1890b7-aa75-56b1-13ec-5d170f9855ea"],
    "bioyond_peptide_reagent_0004_ether_trough": ["乙醚", "3a1890b7-fcc4-5399-eb9d-ef41f08a0add"],
    "bioyond_peptide_reagent_0005_acetonitrile_trough": ["乙腈板", "3a1890b9-070c-d403-4092-21d941aba19b"],
    "bioyond_peptide_reagent_0006_ellman_trough": ["Ellman", "3a1890b9-4f75-92dd-86d2-b7378729b8a3"],
    "bioyond_peptide_reagent_0013_nh4hco3_buffer_1_trough": ["NH4HCO3缓冲液板1", "3a1890bd-1634-908d-b73a-191aa55e0ec9"],
    "bioyond_peptide_reagent_0014_coupling_reagent_trough": ["连接试剂", "3a1890bd-5ddb-c39f-d095-c7962e0f1f43"],
    "bioyond_peptide_reagent_0015_mercaptoethanol_trough": ["2-巯基乙醇", "3a1890bd-d799-3414-bf9c-40e0dcaa8ddd"],
    "bioyond_peptide_reagent_0020_300ml_cleavage_trough": ["300mL裂解液试剂槽", "3a189b09-ca6b-5116-1718-5ce2da0fa1fa"],
    "bioyond_peptide_reagent_0021_300ml_ether_trough_1": ["300mL乙醚试剂槽1", "3a189b18-d5fc-94c0-df55-6b314bf5bff8"],
    "bioyond_peptide_reagent_0023_acetonitrile_water_trough_1": ["乙腈/水板1", "3a18a11a-aeab-b690-ed0e-925598069f8c"],
    "bioyond_peptide_synthesizer_press_cover": ["多肽合成仪压盖", "3a18b44d-7ea0-6654-3a94-16526c6fd6e0"],
    "bioyond_peptide_reused_1000ul_tip_rack": ["1mL枪头盒（复用）", "3a18c9ed-79be-9bd4-1f39-9d0128c3a78a"],
    "bioyond_peptide_reused_200ul_tip_rack": ["200μL枪头盒（复用）", "3a18d7fe-63a5-0bd6-984b-5c01812ac87a"],
    "bioyond_peptide_reagent_0029_mercaptoethanol_solution_trough": ["2-巯基乙醇溶液板", "3a18d8d9-c4f1-93bd-f2c1-8bfc2e7ed783"],
    "bioyond_peptide_reagent_0030_carboxylic_acid_trough": ["羧酸", "3a18edc3-0bb6-c70e-fb2c-6e1dce3713fb"],
    "bioyond_peptide_reagent_0038_300ml_dmso_trough": ["300mLDMSO试剂槽", "3a1aacf7-6bbd-c6eb-d8a2-623b3a441514"],
    "bioyond_peptide_middle_cover_plate": ["中层盖板", "3a19d5a6-1055-7ed7-00b5-8a37cc7d8209"],
    "bioyond_peptide_upper_cover_plate": ["上层盖板", "3a19d5a6-5a57-0a45-35ca-67ded9a213cc"],
    "bioyond_peptide_200ul_g3_tip_rack": ["200μL枪头盒（G3）", "3a19e41c-f001-104b-3b70-7fb1ec626fc6"],
    "bioyond_peptide_empty_material": ["无", "3a1a1db1-9644-24a6-b6ad-123b6279464d"],
    "bioyond_peptide_reagent_0040_300ml_tris_lcms_trough": ["300mLTris试剂槽（LCMS）", "3a1c8c10-a6ce-95a0-38d3-077199369a73"],
    "bioyond_peptide_reagent_0043_acetonitrile_water_trough_2": ["乙腈/水板2", "3a1e5afe-3c35-5fc1-8432-9ad91cc1682f"],
    "bioyond_peptide_reagent_0044_nh4hco3_buffer_2_trough": ["NH4HCO3缓冲液板2", "3a1e5aff-c5bb-0f74-1751-8ea1c3ad8fdd"],
    "bioyond_peptide_reagent_0045_300ml_ether_trough_2": ["300mL乙醚试剂槽2", "3a1e5b00-f51d-83bb-580d-f86a780b1583"],
    "bioyond_peptide_reagent_0046_300ml_tris_assay_trough": ["300mLTris试剂槽（酶标）", "3a1e6a8b-0364-6125-5bcf-0ebe51056951"],
    "bioyond_peptide_reagent_0047_protein_buffer_trough": ["蛋白缓冲液试剂槽", "3a1e6a8b-4c0d-1906-52b7-784e796b0a68"],
    "bioyond_peptide_reagent_0048_fluorescent_substrate_trough": ["荧光酶底物试剂槽", "3a1e6a8b-8276-9ac3-f665-9501ed397e1b"],
    "bioyond_peptide_reagent_0050_300ml_ether_trough_3": ["300mL乙醚试剂槽3", "3a1e6a8e-3ae6-5399-22bd-2ae381405c8e"],
    "bioyond_peptide_reagent_0051_300ml_ether_trough_4": ["300mL乙醚试剂槽4", "3a1e6a8e-71be-17f6-70e5-98b33e5ec695"],
}


MATERIAL_TYPE_CODE_TO_CLASS = {
    "0001": BioyondPeptide_96WellSynthesisPlate,
    "0002": BioyondPeptide_96WellBalancePlate,
    "0003": BioyondPeptide_Reagent0003_ReductionCleavageTrough,
    "0004": BioyondPeptide_Reagent0004_EtherTrough,
    "0005": BioyondPeptide_Reagent0005_AcetonitrileTrough,
    "0006": BioyondPeptide_Reagent0006_EllmanTrough,
    "0007": BioyondPeptide_96WellCollectionPlate,
    "0008": BioyondPeptide_200ul_TipRack,
    "0009": BioyondPeptide_1000ul_TipRack,
    "0010": BioyondPeptide_96WellAssayPlate,
    "0011": BioyondPeptide_96WellDeepWellPlate,
    "0012": BioyondPeptide_50ul_TipRack,
    "0013": BioyondPeptide_Reagent0013_NH4HCO3Buffer1Trough,
    "0014": BioyondPeptide_Reagent0014_CouplingReagentTrough,
    "0015": BioyondPeptide_Reagent0015_MercaptoethanolTrough,
    "0016": BioyondPeptide_IDOT384PeptidePlate,
    "0017": BioyondPeptide_IDOT384CarboxylicAcidPlate,
    "0018": BioyondPeptide_384WellPlate,
    "0019": BioyondPeptide_384CarboxylicAcidPlate,
    "0020": BioyondPeptide_Reagent0020_300mLCleavageTrough,
    "0021": BioyondPeptide_Reagent0021_300mLEtherTrough1,
    "0023": BioyondPeptide_Reagent0023_AcetonitrileWaterTrough1,
    "0024": BioyondPeptide_ReagentTrough,
    "0025": BioyondPeptide_SynthesizerPressCover,
    "0026": BioyondPeptide_384BalancePlate,
    "0027": BioyondPeptide_Reused1000ulTipRack,
    "0028": BioyondPeptide_Reused200ulTipRack,
    "0029": BioyondPeptide_Reagent0029_MercaptoethanolSolutionTrough,
    "0030": BioyondPeptide_Reagent0030_CarboxylicAcidTrough,
    "0032": BioyondPeptide_96WellCarboxylicAcidPlate,
    "0033": BioyondPeptide_MiddleCoverPlate,
    "0034": BioyondPeptide_UpperCoverPlate,
    "0035": BioyondPeptide_CoverPlate,
    "0036": BioyondPeptide_200ulG3TipRack,
    "0037": BioyondPeptide_EmptyMaterial,
    "0038": BioyondPeptide_Reagent0038_300mLDMSOTrough,
    "0039": BioyondPeptide_96WellSynthesisPlateBase,
    "0040": BioyondPeptide_Reagent0040_300mLTrisLCMSTrough,
    "0041": BioyondPeptide_SealingBase,
    "0042": BioyondPeptide_96WellStandardCurveAssayPlate,
    "0043": BioyondPeptide_Reagent0043_AcetonitrileWaterTrough2,
    "0044": BioyondPeptide_Reagent0044_NH4HCO3Buffer2Trough,
    "0045": BioyondPeptide_Reagent0045_300mLEtherTrough2,
    "0046": BioyondPeptide_Reagent0046_300mLTrisAssayTrough,
    "0047": BioyondPeptide_Reagent0047_ProteinBufferTrough,
    "0048": BioyondPeptide_Reagent0048_FluorescentSubstrateTrough,
    "0049": BioyondPeptide_384LCMSPlate,
    "0050": BioyondPeptide_Reagent0050_300mLEtherTrough3,
    "0051": BioyondPeptide_Reagent0051_300mLEtherTrough4,
}


def get_material_class_by_type_code(type_code: str):
    """Return a peptide material class by Bioyond material type code."""
    return MATERIAL_TYPE_CODE_TO_CLASS.get(type_code)
