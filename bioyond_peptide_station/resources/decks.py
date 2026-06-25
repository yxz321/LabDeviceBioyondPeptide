from pylabrobot.resources import Deck, Coordinate, Resource

from unilabos.registry.decorators import resource
from bioyond_peptide_station.resources.warehouses import (
    bioyond_warehouse_numeric_stack,  # 多肽站自动化堆栈
    bioyond_warehouse_live_grid,
)


# 虚拟暂存节点（limbo）：parked/limbo 物料的统一持有节点。
# 通过精确身份（名称/类别）识别，stale/delete 清扫与仓库/Bioyond 导出逻辑据此排除它。
BIOYOND_VIRTUAL_HOLDING_NODE_NAME = "BioyondVirtualHolding"
BIOYOND_VIRTUAL_HOLDING_NODE_MODEL = "BioyondVirtualHolding"
BIOYOND_VIRTUAL_HOLDING_NODE_CATEGORY = "virtual_holding"


@resource(
    id="BIOYOND_PeptideStation_Deck",
    category=["deck"],
    description="BIOYOND 多肽工作站 Deck",
    icon="preparation_station.webp",
)
class BIOYOND_PeptideStation_Deck(Deck):
    WAREHOUSE_BIOYOND_AXIS = dict.fromkeys(
        [
            "自动化堆栈",
            "低温冰箱仓库",
            "Tecan移液站库",
            "G3移液站库",
            "IDOT移液站库",
            "G3缓冲库",
            "盖板缓冲库",
            "配平板缓冲库",
            "IDOT缓冲库",
            "固相合成板底座缓冲位",
            "离心机库位",
            "热封膜机位",
        ],
        "xy_col_row",
    )
    WAREHOUSE_BIOYOND_KEY_AXIS = dict.fromkeys(WAREHOUSE_BIOYOND_AXIS, "col_row")

    def __init__(
        self,
        name: str = "PeptideStation_Deck",
        size_x: float = 2700.0,
        size_y: float = 2000.0,
        size_z: float = 1500.0,
        category: str = "deck",
        setup: bool = False
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z)
        if setup:
            self.setup()

    @classmethod
    def deserialize(cls, data: dict, allow_marshal: bool = False):
        if data.get("children") and data.get("setup") is True:
            data = data.copy()
            data["setup"] = False
            # 已有序列化子资源，跳过 setup 避免重复创建
            result = super(BIOYOND_PeptideStation_Deck, cls).deserialize(data, allow_marshal=allow_marshal)
        else:
            result = super(BIOYOND_PeptideStation_Deck, cls).deserialize(data, allow_marshal=allow_marshal)
        result._ensure_peptide_warehouse_metadata()
        result._ensure_virtual_holding_node()
        return result

    def _ensure_peptide_warehouse_metadata(self) -> None:
        for child in getattr(self, "children", []):
            name = getattr(child, "name", "")
            if name == BIOYOND_VIRTUAL_HOLDING_NODE_NAME:
                # 虚拟暂存节点不是仓库，跳过 axis 元数据标注。
                continue
            axis = self.WAREHOUSE_BIOYOND_AXIS.get(name)
            if axis and not hasattr(child, "bioyond_axis"):
                child.bioyond_axis = axis
            key_axis = self.WAREHOUSE_BIOYOND_KEY_AXIS.get(name)
            if key_axis and not hasattr(child, "bioyond_key_axis"):
                child.bioyond_key_axis = key_axis

    def _ensure_virtual_holding_node(self) -> Resource:
        """确保 deck 上存在唯一的虚拟暂存节点（limbo），用于持有 parked/limbo 物料。

        通过精确名称识别，幂等：已存在则直接返回。该节点不会被加入
        self.warehouses / self.warehouse_locations，也不会被仓库迭代/元数据逻辑当作仓库。
        """
        for child in getattr(self, "children", []):
            if getattr(child, "name", "") == BIOYOND_VIRTUAL_HOLDING_NODE_NAME:
                return child

        node = Resource(
            name=BIOYOND_VIRTUAL_HOLDING_NODE_NAME,
            size_x=1.0,
            size_y=1.0,
            size_z=1.0,
            category=BIOYOND_VIRTUAL_HOLDING_NODE_CATEGORY,
            model=BIOYOND_VIRTUAL_HOLDING_NODE_MODEL,
        )
        # 放在不与任何仓库碰撞的角落坐标。
        self.assign_child_resource(node, location=Coordinate(0.0, 0.0, 0.0))
        return node

    def _frontend_y_flipped_coordinate(self, display_x: float, display_y: float, child) -> Coordinate:
        """把期望显示坐标转换为兼容前端 y 轴翻转的存储坐标。"""
        return Coordinate(display_x, self.get_size_y() - display_y - child.get_size_y(), 0.0)

    def setup(self) -> None:
        # 多肽工作站仓库配置
        # 基于 2026-05-09 live API probe 发现的实际仓库拓扑 (12个仓库)
        # 数据来源: Bioyond 现场仓库发现结果。
        self.warehouses = {
            # 主自动化堆栈 - live API: code 10-17 -> x=17, y=10，显示为 17 行×10 列
            "自动化堆栈": bioyond_warehouse_numeric_stack(
                "自动化堆栈",
                rows=17,
                columns=10,
                bioyond_axis="xy_col_row",
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),

            # 低温存储
            "低温冰箱仓库": bioyond_warehouse_live_grid(
                "低温冰箱仓库",
                rows=3,
                columns=2,
                slot_keys=["1", "2", "3", "4", "5", "6"],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),

            # 移液站库位
            "Tecan移液站库": bioyond_warehouse_live_grid(
                "Tecan移液站库",
                rows=18,
                columns=1,
                slot_keys=[str(index) for index in range(1, 19)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "G3移液站库": bioyond_warehouse_live_grid(
                "G3移液站库",
                rows=18,
                columns=1,
                slot_keys=["1", "2", "3", "4", "垃圾桶", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18"],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "IDOT移液站库": bioyond_warehouse_live_grid(
                "IDOT移液站库",
                rows=12,
                columns=1,
                slot_keys=[f"0009-{index:04d}" for index in range(1, 13)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),

            # 缓冲库位
            "G3缓冲库": bioyond_warehouse_live_grid(
                "G3缓冲库",
                rows=5,
                columns=1,
                slot_keys=[str(index) for index in range(1, 6)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "盖板缓冲库": bioyond_warehouse_live_grid(
                "盖板缓冲库",
                rows=7,
                columns=1,
                slot_keys=[str(index) for index in range(1, 8)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "配平板缓冲库": bioyond_warehouse_live_grid(
                "配平板缓冲库",
                rows=3,
                columns=1,
                slot_keys=[str(index) for index in range(1, 4)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "IDOT缓冲库": bioyond_warehouse_live_grid(
                "IDOT缓冲库",
                rows=2,
                columns=1,
                slot_keys=["1", "1"],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "固相合成板底座缓冲位": bioyond_warehouse_live_grid(
                "固相合成板底座缓冲位",
                rows=4,
                columns=1,
                slot_keys=[f"0015-{index:04d}" for index in range(1, 5)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),

            # 设备库位
            "离心机库位": bioyond_warehouse_live_grid(
                "离心机库位",
                rows=4,
                columns=1,
                slot_keys=[f"0017-{index:04d}" for index in range(1, 5)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
            "热封膜机位": bioyond_warehouse_live_grid(
                "热封膜机位",
                rows=2,
                columns=1,
                slot_keys=[f"0016-{index:04d}" for index in range(1, 3)],
                bioyond_key_axis="col_row",
                frontend_y_flip=True,
            ),
        }

        # 仓库显示布局：紧凑排列；存储 y 坐标按前端兼容翻转预先反向。
        display_layout = {
            "自动化堆栈": (0.0, 0.0),
            "Tecan移液站库": (1520.0, 0.0),
            "G3移液站库": (1710.0, 0.0),
            "IDOT移液站库": (1900.0, 0.0),
            "G3缓冲库": (2090.0, 0.0),
            "盖板缓冲库": (2090.0, 580.0),
            "低温冰箱仓库": (2280.0, 0.0),
            "配平板缓冲库": (2280.0, 370.0),
            "IDOT缓冲库": (2470.0, 370.0),
            "固相合成板底座缓冲位": (2280.0, 740.0),
            "离心机库位": (2470.0, 740.0),
            "热封膜机位": (2280.0, 1210.0),
        }
        self.warehouse_locations = {
            name: self._frontend_y_flipped_coordinate(x, y, self.warehouses[name])
            for name, (x, y) in display_layout.items()
        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])

        # 仓库分配完成后，挂载唯一的虚拟暂存节点（limbo）。
        self._ensure_virtual_holding_node()
