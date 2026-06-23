"""Peptide Station Material Resource Definitions."""

from __future__ import annotations

from collections import OrderedDict

try:
    from pylabrobot.resources import Container, Plate, TipRack
except Exception:  # pragma: no cover - 允许无 pylabrobot 的轻量动作测试导入
    class _FallbackResource:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Container(_FallbackResource):  # type: ignore[no-redef]
        pass

    class Plate(_FallbackResource):  # type: ignore[no-redef]
        pass

    class TipRack(_FallbackResource):  # type: ignore[no-redef]
        pass

try:
    from unilabos.registry.decorators import resource
except Exception:  # pragma: no cover - 允许无完整 registry 依赖时导入常量
    def resource(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator


def _ensure_itemized_ordering(kwargs: dict) -> None:
    if kwargs.get("ordering") is None and kwargs.get("ordered_items") is None:
        kwargs["ordering"] = OrderedDict()


class _PeptideTipRack(TipRack):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 64.0)
        kwargs.setdefault("with_tips", True)
        _ensure_itemized_ordering(kwargs)
        super().__init__(*args, **kwargs)


class _PeptidePlate(Plate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 14.35)
        kwargs.setdefault("plate_type", "skirted")
        _ensure_itemized_ordering(kwargs)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_1000ul_tip_rack",
    category=["labware", "tip_rack"],
    description="1000uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_1000ul_TipRack(_PeptideTipRack):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_1000ul_tip_rack")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_200ul_tip_rack",
    category=["labware", "tip_rack"],
    description="200uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_200ul_TipRack(_PeptideTipRack):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_200ul_tip_rack")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_50ul_tip_rack",
    category=["labware", "tip_rack"],
    description="50uL tip rack for Bioyond peptide station",
)
class BioyondPeptide_50ul_TipRack(_PeptideTipRack):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_50ul_tip_rack")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_deep_well_plate",
    category=["labware", "plate"],
    description="96 well deep well plate for Bioyond peptide station",
)
class BioyondPeptide_96WellDeepWellPlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_96_well_deep_well_plate")
        kwargs.setdefault("size_z", 44.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_synthesis_plate",
    category=["labware", "plate"],
    description="96 well solid-phase synthesis plate for Bioyond peptide station",
)
class BioyondPeptide_96WellSynthesisPlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_96_well_synthesis_plate")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_synthesis_plate_base",
    category=["labware", "adapter"],
    description="96 well solid-phase synthesis plate base for Bioyond peptide station",
)
class BioyondPeptide_96WellSynthesisPlateBase(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_96_well_synthesis_plate_base")
        kwargs.setdefault("size_z", 20.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_96_well_balance_plate",
    category=["labware", "plate"],
    description="96 well balance plate for Bioyond peptide station",
)
class BioyondPeptide_96WellBalancePlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_96_well_balance_plate")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_384_well_plate",
    category=["labware", "plate"],
    description="384 well plate for Bioyond peptide station",
)
class BioyondPeptide_384WellPlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_384_well_plate")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_384_lcms_plate",
    category=["labware", "plate"],
    description="384 well LCMS plate for Bioyond peptide station",
)
class BioyondPeptide_384LCMSPlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_384_lcms_plate")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_384_balance_plate",
    category=["labware", "plate"],
    description="384 well balance plate for Bioyond peptide station",
)
class BioyondPeptide_384BalancePlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_384_balance_plate")
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_cover_plate",
    category=["labware", "cover"],
    description="Cover plate for Bioyond peptide station",
)
class BioyondPeptide_CoverPlate(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_cover_plate")
        kwargs.setdefault("size_z", 8.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_sealing_base",
    category=["labware", "adapter"],
    description="Sealing base for Bioyond peptide station",
)
class BioyondPeptide_SealingBase(_PeptidePlate):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "bioyond_peptide_sealing_base")
        kwargs.setdefault("size_z", 20.0)
        super().__init__(*args, **kwargs)


@resource(
    id="bioyond_peptide_reagent_trough",
    category=["labware", "trough"],
    description="Reagent trough for Bioyond peptide station",
)
class BioyondPeptide_ReagentTrough(Container):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("size_x", 127.76)
        kwargs.setdefault("size_y", 85.48)
        kwargs.setdefault("size_z", 44.0)
        kwargs.setdefault("max_volume", 300000.0)
        kwargs.setdefault("model", "bioyond_peptide_reagent_trough")
        super().__init__(*args, **kwargs)


DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS = {
    "bioyond_peptide_1000ul_tip_rack": ["1000μL枪头盒", "3a1890bb-736e-cfdd-3213-eb314e8a60f9"],
    "bioyond_peptide_200ul_tip_rack": ["200μL枪头盒", "3a1890bb-36d1-964a-18bd-0bf0f2877a7b"],
    "bioyond_peptide_50ul_tip_rack": ["50μL枪头盒", "3a1890bc-5fae-361c-cc09-e6f2f6dcd71d"],
    "bioyond_peptide_96_well_deep_well_plate": ["96孔深孔板", "3a1890bc-1fa8-fe39-9faa-12279ed4569b"],
    "bioyond_peptide_96_well_synthesis_plate": ["96孔固相合成板", "3a1871cb-99f3-f01d-23e2-08dbbd0045b5"],
    "bioyond_peptide_96_well_synthesis_plate_base": ["96孔固相合成板底座", "3a1b997e-241b-64f0-80d1-47bca08799d1"],
    "bioyond_peptide_96_well_balance_plate": ["96孔配平板", "3a187661-2378-1e20-fa5c-a27d49fdc15d"],
    "bioyond_peptide_384_well_plate": ["384孔酶标板", "3a1890bf-2148-ed20-92bd-d85869947d9a"],
    "bioyond_peptide_384_lcms_plate": ["384孔LCMS板", "3a1e6a8b-cb61-74da-a089-8e6f197f80f0"],
    "bioyond_peptide_384_balance_plate": ["384孔配平板", "3a18be7e-47cc-888c-fc68-055753286826"],
    "bioyond_peptide_cover_plate": ["防挥发盖板", "3a19d5a6-b0e2-b486-e5eb-bcabc632f4de"],
    "bioyond_peptide_sealing_base": ["封膜底座", "3a1d1d7b-e33b-6975-165d-c56cba5ed345"],
    "bioyond_peptide_reagent_trough": ["12道试剂槽", "3a18b431-ac58-ca2e-9680-2a4f5880ea45"],
}


MATERIAL_TYPE_CODE_TO_CLASS = {
    "0001": BioyondPeptide_96WellSynthesisPlate,
    "0002": BioyondPeptide_96WellBalancePlate,
    "0008": BioyondPeptide_200ul_TipRack,
    "0009": BioyondPeptide_1000ul_TipRack,
    "0011": BioyondPeptide_96WellDeepWellPlate,
    "0012": BioyondPeptide_50ul_TipRack,
    "0016": BioyondPeptide_384WellPlate,
    "0018": BioyondPeptide_384WellPlate,
    "0024": BioyondPeptide_ReagentTrough,
    "0026": BioyondPeptide_384BalancePlate,
    "0035": BioyondPeptide_CoverPlate,
    "0039": BioyondPeptide_96WellSynthesisPlateBase,
    "0041": BioyondPeptide_SealingBase,
    "0049": BioyondPeptide_384LCMSPlate,
}


def get_material_class_by_type_code(type_code: str):
    """Return a peptide material class by Bioyond material type code."""
    return MATERIAL_TYPE_CODE_TO_CLASS.get(type_code)
