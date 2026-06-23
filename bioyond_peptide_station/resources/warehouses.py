from pylabrobot.resources import Coordinate
from pylabrobot.resources.carrier import ResourceHolder, create_homogeneous_resources

from unilabos.resources.warehouse import WareHouse, warehouse_factory


class BioyondWareHouse(WareHouse):
    """Bioyond 仓库，额外保存服务端 x/y 坐标和库位标签语义。"""

    def __init__(self, *args, bioyond_axis: str = "xy_row_col", bioyond_key_axis: str = "row_col", **kwargs):
        super().__init__(*args, **kwargs)
        self.bioyond_axis = bioyond_axis
        self.bioyond_key_axis = bioyond_key_axis

    def serialize(self) -> dict:
        data = super().serialize()
        data["bioyond_axis"] = self.bioyond_axis
        data["bioyond_key_axis"] = self.bioyond_key_axis
        return data


def bioyond_warehouse_numeric_stack(
    name: str,
    rows: int = 10,
    columns: int = 17,
    bioyond_axis: str = "xy_row_col",
    bioyond_key_axis: str = "row_col",
    frontend_y_flip: bool = False,
) -> WareHouse:
    """创建 Bioyond 数字库位堆栈，库位名使用服务端返回的 行-列 格式。

    bioyond_axis: 仓库级别的 Bioyond 坐标轴约定，供 graphio 的坐标映射使用。
        - "xy_row_col" (default): Bioyond x→row, y→col (reaction/peptide 历史约定).
        - "xy_col_row": Bioyond x→col, y→row (Sirna live API 实测约定).
    bioyond_key_axis: 库位标签生成约定。
        - "row_col" (default): 视觉行列和标签行列一致，例如 10 行 x 17 列 → 1-1..10-17。
        - "col_row": 视觉行列转置，但标签仍保持 Bioyond row-col，例如
          17 行 x 10 列 → 1-1..10-17。
    未设置时 graphio 回退到默认 "xy_row_col"，其他调用方保持原行为。
    """
    num_items_x = columns
    num_items_y = rows
    num_items_z = 1
    dx = 10.0
    dy = 10.0
    dz = 10.0
    item_dx = 147.0
    item_dy = 106.0
    item_dz = 130.0
    resource_size_x = 127.0
    resource_size_y = 86.0
    resource_size_z = 25.0
    size_y = dy + item_dy * num_items_y
    locations = []
    for row in range(num_items_y):
        display_y = dy + row * item_dy
        y = size_y - display_y - resource_size_y if frontend_y_flip else display_y
        for col in range(num_items_x):
            locations.append(Coordinate(dx + col * item_dx, y, dz))
    holders = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=locations,
        resource_size_x=resource_size_x,
        resource_size_y=resource_size_y,
        resource_size_z=resource_size_z,
        name_prefix=name,
    )
    if bioyond_key_axis == "row_col":
        keys = [
            f"{row + 1}-{col + 1}"
            for row in range(num_items_y)
            for col in range(num_items_x)
        ]
    elif bioyond_key_axis == "col_row":
        keys = [
            f"{col + 1}-{row + 1}"
            for row in range(num_items_y)
            for col in range(num_items_x)
        ]
    else:
        raise ValueError(f"未知 Bioyond 库位标签约定: {bioyond_key_axis!r}")
    warehouse = BioyondWareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=size_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x=num_items_x,
        num_items_y=num_items_y,
        num_items_z=num_items_z,
        ordering_layout="row-major",
        sites={key: holder for key, holder in zip(keys, holders.values())},
        category="warehouse",
        bioyond_axis=bioyond_axis,
        bioyond_key_axis=bioyond_key_axis,
    )
    return warehouse


def bioyond_warehouse_live_grid(
    name: str,
    rows: int,
    columns: int,
    slot_keys: list[str] | None = None,
    bioyond_axis: str = "xy_col_row",
    bioyond_key_axis: str = "row_col",
    frontend_y_flip: bool = False,
) -> WareHouse:
    """创建 Bioyond 实测库位网格，按服务端 code 保存位点标签。

    默认用于 Peptide live API 返回的坐标：x 是视觉列，y 是视觉行。
    当服务端 code 重复时，为保持 PLR ordering 唯一性，会给后续重复项追加 ``#N``。
    """
    num_items_x = columns
    num_items_y = rows
    num_items_z = 1
    dx = 10.0
    dy = 10.0
    dz = 10.0
    item_dx = 147.0
    item_dy = 106.0
    item_dz = 130.0
    resource_size_x = 127.0
    resource_size_y = 86.0
    resource_size_z = 25.0
    size_y = dy + item_dy * num_items_y
    locations = []
    for row in range(num_items_y):
        display_y = dy + row * item_dy
        y = size_y - display_y - resource_size_y if frontend_y_flip else display_y
        for col in range(num_items_x):
            locations.append(Coordinate(dx + col * item_dx, y, dz))
    holders = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=locations,
        resource_size_x=resource_size_x,
        resource_size_y=resource_size_y,
        resource_size_z=resource_size_z,
        name_prefix=name,
    )
    keys = slot_keys or [str(index + 1) for index in range(num_items_x * num_items_y)]
    if len(keys) != len(holders):
        raise ValueError(f"{name} 库位数量不匹配: keys={len(keys)}, holders={len(holders)}")

    seen: dict[str, int] = {}
    unique_keys: list[str] = []
    for key in keys:
        count = seen.get(key, 0) + 1
        seen[key] = count
        unique_keys.append(key if count == 1 else f"{key}#{count}")

    return BioyondWareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=size_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x=num_items_x,
        num_items_y=num_items_y,
        num_items_z=num_items_z,
        ordering_layout="row-major",
        sites={key: holder for key, holder in zip(unique_keys, holders.values())},
        category="warehouse",
        bioyond_axis=bioyond_axis,
        bioyond_key_axis=bioyond_key_axis,
    )


# ================ 小核酸工作站相关堆栈 ================

def bioyond_warehouse_sirna_g3_liquid_handler(name: str = "G3移液站") -> WareHouse:
    """创建小核酸 G3 移液站库位堆栈：显示为 14 行 x 1 列，标签保持 1-1..1-14。"""
    return bioyond_warehouse_numeric_stack(
        name, rows=14, columns=1, bioyond_axis="xy_col_row", bioyond_key_axis="col_row"
    )


def bioyond_warehouse_sirna_automation_stack(name: str = "自动化堆栈") -> WareHouse:
    """创建小核酸自动化堆栈：显示为 17 行 x 10 列，标签保持 1-1..10-17。"""
    return bioyond_warehouse_numeric_stack(
        name, rows=17, columns=10, bioyond_axis="xy_col_row", bioyond_key_axis="col_row"
    )


def bioyond_warehouse_sirna_centrifuge_balance_plate_stack(name: str = "离心机配平板堆栈") -> WareHouse:
    """创建小核酸离心机配平板堆栈：显示为 1 行 x 2 列，标签保持 1-1、2-1。"""
    return bioyond_warehouse_numeric_stack(
        name, rows=1, columns=2, bioyond_axis="xy_col_row", bioyond_key_axis="col_row"
    )


# ================ 反应站相关堆栈 ================

def bioyond_warehouse_1x4x4(name: str) -> WareHouse:
    """创建BioYond 4x4x1仓库 (左侧堆栈: A01～D04)

    使用行优先排序，前端展示为:
    A01 | A02 | A03 | A04
    B01 | B02 | B03 | B04
    C01 | C02 | C03 | C04
    D01 | D02 | D03 | D04
    """
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列
        num_items_y=4,  # 4行
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=0,  # 从01开始: A01, A02, A03, A04
        layout="row-major",  # ⭐ 改为行优先排序
    )

def bioyond_warehouse_1x4x4_right(name: str) -> WareHouse:
    """创建BioYond 4x4x1仓库 (右侧堆栈: A05～D08)"""
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=4,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=4,  # 从05开始: A05, A06, A07, A08
        layout="row-major",  # ⭐ 改为行优先排序
    )

def bioyond_warehouse_density_vial(name: str) -> WareHouse:
    """创建测量小瓶仓库(测密度) - 竖向排列2列3行
    布局（从下到上，从左到右）：
    | A03 | B03 |  ← 顶部
    | A02 | B02 |  ← 中部
    | A01 | B01 |  ← 底部
    """
    return warehouse_factory(
        name=name,
        num_items_x=2,  # 2列（A, B）
        num_items_y=3,  # 3行（01-03，从下到上）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=40.0,  # 列间距（A到B的横向距离）
        item_dy=40.0,  # 行间距（01到02到03的竖向距离）
        item_dz=50.0,
        # ⭐ 竖向warehouse：槽位尺寸也是竖向的（小瓶已经是正方形，无需调整）
        resource_size_x=30.0,
        resource_size_y=30.0,
        resource_size_z=12.0,
        category="warehouse",
        col_offset=0,
        layout="vertical-col-major",  # ⭐ 竖向warehouse专用布局
    )

def bioyond_warehouse_reagent_storage(name: str) -> WareHouse:
    """创建BioYond站内试剂存放堆栈 - 竖向排列1列2行
    布局（竖向，从下到上）：
    | A02 |  ← 顶部
    | A01 |  ← 底部
    """
    return warehouse_factory(
        name=name,
        num_items_x=1,  # 1列
        num_items_y=2,  # 2行（01-02，从下到上）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=96.0,   # 列间距（这里只有1列，不重要）
        item_dy=137.0,  # 行间距（A01到A02的竖向距离）
        item_dz=120.0,
        # ⭐ 竖向warehouse：交换槽位尺寸，使槽位框也是竖向的
        resource_size_x=86.0,   # 原来的 resource_size_y
        resource_size_y=127.0,  # 原来的 resource_size_x
        resource_size_z=25.0,
        category="warehouse",
        layout="vertical-col-major",  # ⭐ 竖向warehouse专用布局
    )

def bioyond_warehouse_tipbox_storage_left(name: str) -> WareHouse:
    """创建BioYond站内Tip盒堆栈左侧部分（A02～B03），2列2行"""
    return warehouse_factory(
        name=name,
        num_items_x=2,  # 2列
        num_items_y=2,  # 2行（A-B）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        col_offset=1,   # 从02开始: A02, A03
        layout="row-major",
    )

def bioyond_warehouse_tipbox_storage_right(name: str) -> WareHouse:
    """创建BioYond站内Tip盒堆栈右侧部分（A01～B01），1列2行"""
    return warehouse_factory(
        name=name,
        num_items_x=1,  # 1列
        num_items_y=2,  # 2行（A-B）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        col_offset=0,   # 从01开始: A01
        layout="row-major",
    )

def bioyond_warehouse_liquid_preparation(name: str) -> WareHouse:
    """已弃用,创建BioYond移液站内10%分装液体准备仓库（A01～B04）"""
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列（01-04）
        num_items_y=2,  # 2行（A-B）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",
    )

# ================ 配液站相关堆栈 ================

def bioyond_warehouse_reagent_stack(name: str) -> WareHouse:
    """创建BioYond 试剂堆栈 2x4x1 (2行×4列: A01-A04, B01-B04)

    使用行优先排序，前端展示为:
    A01 | A02 | A03 | A04
    B01 | B02 | B03 | B04
    """
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列 (01-04)
        num_items_y=2,  # 2行 (A-B)
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=0,  # 从01开始
        layout="row-major",  # ⭐ 使用行优先排序: A01,A02,A03,A04, B01,B02,B03,B04
    )

 # 定义bioyond的堆栈

# =================== Other ===================

def bioyond_warehouse_1x4x2(name: str) -> WareHouse:
    """创建BioYond 4x2x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=4,
        num_items_z=2,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        removed_positions=None
    )

def bioyond_warehouse_1x2x2(name: str) -> WareHouse:
    """创建BioYond 1x2x2仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=2,
        num_items_z=2,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_10x1x1(name: str) -> WareHouse:
    """创建BioYond 10x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=10,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_1x3x3(name: str) -> WareHouse:
    """创建BioYond 1x3x3仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=3,
        num_items_z=3,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_2x1x3(name: str) -> WareHouse:
    """创建BioYond 2x1x3仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=3,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_3x3x1(name: str) -> WareHouse:
    """创建BioYond 3x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_5x1x1(name: str) -> WareHouse:
    """已弃用：创建BioYond 5x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=5,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_3x3x1_2(name: str) -> WareHouse:
    """已弃用：创建BioYond 3x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=3,
        num_items_z=1,
        dx=12.0,
        dy=12.0,
        dz=12.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_liquid_and_lid_handling(name: str) -> WareHouse:
    """创建BioYond开关盖加液模块台面"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=5,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        removed_positions=None
    )

def bioyond_warehouse_1x8x4(name: str) -> WareHouse:
    """创建BioYond 8x4x1反应站堆栈（A01～D08）"""
    return warehouse_factory(
        name=name,
        num_items_x=8,  # 8列（01-08）
        num_items_y=4,  # 4行（A-D）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
    )
