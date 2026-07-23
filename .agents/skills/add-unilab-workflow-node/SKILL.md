---
name: add-unilab-workflow-node
description: Add or update Uni-Lab-OS workflow/action nodes in Python device packages. Use when editing @device/@action registry decorators, adding exposed workflow nodes, action handles, manual-confirm nodes, parameter classes, goal_default/function defaults, always_free behavior, or Chinese user-facing Args/Field metadata for Uni-Lab/Bioyond device workflows.
---

# Add Uni-Lab Workflow Node

## 快速流程

新增或修改工作流节点时，先读仓库里的 `AGENTS.md`、`developer_docs/` 或同类本地约定。若本仓库是 `BioyondPeptideStation`，必须遵守 `developer_docs/bioyond_peptide_station_action_args.md`。

按这个顺序做：

1. 确认节点属于哪个 `@device` 类，以及方法是否必须直接声明在该类体内。
2. 设计 Python 方法名、参数变量名、返回值、handles、默认值和中文展示文本。
3. 写 `@action(...)` 元数据：`description`、`goal_default`、`handles`、`node_type`、`placeholder_keys`、`always_free` 等。
4. 写函数签名和参数类。用函数默认值定义顶层 required；用 `TypedDict` / `total=False` 定义参数类内部 required。
5. 写 docstring `Args:`，每个暴露输入都要有 `variable[显示名]` 和用户能懂的描述。
6. 验证：至少编译 Python 文件；能跑时执行 `unilab --check_mode --devices ... --external_devices_only` 和相关测试。

## 装饰器原则

常用导入：

```python
from typing import Any, Dict, List, Optional, TypedDict
from typing_extensions import Annotated

from pydantic import Field
from unilabos.registry.decorators import (
    action,
    device,
    not_action,
    ActionInputHandle,
    ActionOutputHandle,
    DataSource,
    NodeType,
)
```

`@device` 放在用户实际看到的设备/工作站类上。AST 扫描通常只可靠发现这个类体内直接声明的方法；继承方法、helper 方法、组合对象方法需要在被装饰类里加薄 wrapper。

```python
@action(auto_prefix=True, description="提交多肽合成实验")
def submit_day1_experiment(self, *args, **kwargs):
    return self._submit_day1_experiment_impl(*args, **kwargs)
```

普通工作流节点使用 `@action(...)`。只有需要更多注册元数据时才显式添加字段，例如 `handles`、`goal_default`、`node_type`、`placeholder_keys`、`always_free`、`feedback_interval`。公共 helper 不应暴露时加 `@not_action`。

`description` 写节点做什么，面向操作员或流程作者，不写 HTTP endpoint、RPC 名称、内部 payload、调试实现。

## 命名约定

Python 变量和方法使用 `snake_case`。方法名用动词开头，例如 `submit_experiment`、`wait_for_order_finish`、`prepare_unload_materials`。

同一个概念在函数参数、goal key、handle key、`data_key`、参数类字段、docstring、`Field(title=...)` 中尽量统一。只有确实为了展示而不同的节点可以例外。

常用名称：

- `order_id`: 内部实验 ID，通常由上游节点传入；展示名用 `<order_id>`，必填时 `<order_id>*`。
- `order_ids`: 内部实验 ID 列表；展示名用 `<order_ids>`。
- `order_code`: 用户可读实验编号；展示名统一用 `实验编号`。
- `sample_excel_relative_path`: 内部样品表路径；展示名用 `<sample_excel_relative_path>`。
- `resultTable`: 表格结果；内部传递时展示名可用 `<resultTable>`，给人看的表格用业务名，例如 `装载确认表`。

避免 `orderID`、`orderId`、`订单ID`、`实验ID`、`orderCode` 作为新接口或展示名。

## 参数展示文本

每个暴露输入都要在 docstring 的 `Args:` 中写成：

```python
Args:
    sample_excel_pattern[样品excel名称*]: 选择要提交的excel文件，如果上游已传入<sample_excel_relative_path>，可不填。
    order_code[实验编号]: 用于人工核对或查找的实验编号。
    order_id[<order_id>*]: 奔曜内部实验ID，通常由上游节点传入。
```

规则：

- `*` 加在显示名末尾，表示用户/流程视角的必填；不要加在变量名上。
- 内部解析或上游传入的值，显示名用 `<variable_name>`；必填则 `<variable_name>*`。
- 描述写用户行为和业务含义，不写“调用 /api/...”“默认勾选”“payload 字段”这类开发实现。
- 上游传入字段的推荐句式：`奔曜内部实验ID，通常由上游节点传入。`
- `display_values` 等纯展示节点可以有自己的展示语义，但仍要清楚。

## 参数排序

手动按重要性排序，不按字母，也不按实现便利性。

排序规则：

1. 人能理解的必填参数。
2. 人能理解的可选参数。
3. 内部解析的必填参数。
4. 内部解析的可选参数。

同一组内，业务关键字段优先，例如实验编号、样品表、方法文件、确认选择，再放 timeout、轮询间隔、缓存 payload、调试开关。

能安全改接口时，函数签名、`goal_default`、handles、参数类字段、docstring `Args:` 都保持同一顺序。若兼容性要求保留旧签名，至少把 `Args:` 和展示字段排成用户友好的顺序。

## Required 与默认值

顶层 action 参数是否进入 JSON Schema 的 `required`，主要由函数签名决定：

```python
def submit_experiment(
    self,
    required_params: PeptideDay2RequiredParams,        # required
    optional_params: Optional[PeptideDay2OptionalParams] = None,  # optional
    sample_excel_relative_path: str = "",             # optional at runtime
):
    ...
```

没有 Python 默认值就是机器 required；有 `= None`、`= ""`、`= 0` 等默认值就是机器 optional。`goal_default` 不定义 required。

下游区别：

- schema/UI/校验器可用 `required` 判断必填。
- Python workflow 的位置参数映射会把 required 字段排在前面。
- 设备运行时最终按 `function(**function_args)` 调用；缺少无默认参数时由 Python 抛错，有默认参数时使用函数默认值。

默认值分三层：

- 函数默认值：运行时真正的兜底值。
- `@action(goal_default=...)`：注册表/UI/上游构造 payload 用的默认值；不会在设备运行时自动注入。
- `Field(default=...)` / `Field(default_factory=...)`：参数类内部 schema/UI 默认值，不替代函数调用默认值。

可变对象在函数签名中用 `None`，在函数体内归一化；`goal_default` 可以用 JSON-safe 的 `[]` / `{}`：

```python
@action(goal_default={"order_ids": [], "timeout_seconds": 3600})
def wait_orders(self, order_ids: Optional[List[str]] = None, timeout_seconds: int = 3600):
    order_ids = list(order_ids or [])
```

如果某个参数业务上需要用户确认但系统可以默认填写，可以在显示名保留 `*`，但要意识到它未必是机器 required。

## 参数类分组

输入很多时，用参数类把人填写的业务参数分组，避免 action 签名过长。推荐结构：

```python
class PeptideDay2RequiredParams(TypedDict):
    sample_excel_pattern: Annotated[
        str,
        Field(
            title="样品excel名称*",
            description="选择要提交的excel文件，如果上游已传入<sample_excel_relative_path>，可不填。",
        ),
    ]


class PeptideDay2OptionalParams(TypedDict, total=False):
    order_name: Annotated[
        str,
        Field(
            title="实验名称",
            description="给本次实验显示的名称；未填写时由系统生成。",
        ),
    ]
    auto_register_materials: Annotated[
        bool,
        Field(
            default=True,
            title="创建后同步物料",
            description="创建实验后同步本次实验用到的物料到本地资源树。",
        ),
    ]
```

`TypedDict` 默认 `total=True`，内部字段都是机器 required；`TypedDict(total=False)` 内部字段是 optional。`Field(title=...)` 放显示名，`Field(description=...)` 放用户说明，内部 required 字段的 title 加 `*`。

外层 action docstring 描述参数类本身，不要在 `Args:` 里重复所有内部字段：

```python
Args:
    required_params[必填参数*]: 填写本次实验必须提供的样品excel名称等参数。
    optional_params[可选参数]: 设置实验名称、创建后同步物料、参数覆盖等可选行为。
    sample_excel_relative_path[<sample_excel_relative_path>*]: 上传样品表后返回的内部文件路径，通常由上游节点传入。
```

## Handles

handles 定义工作流连线端口，不等同于 required。一个字段可以 required 但没有 handle，也可以有 handle 但有函数默认值。

写 handle 时保持 `key`、`data_key` 与变量名一致，`label` 与显示名一致，`data_type` 稳定且能被上下游复用。Input handle 的 `label` 也要继承必填标记：如果对应 `Args:` 或 `Field(title=...)` 显示名带 `*`，输入端口 label 也必须带 `*`。输出 handle 一般表示本节点产物，不因为输入必填而自动加 `*`。

```python
@action(
    always_free=True,
    goal_default={"order_id": ""},
    handles=[
        ActionInputHandle(
            key="order_id",
            data_type="bioyond_order_id",
            label="<order_id>*",
            data_key="order_id",
            data_source=DataSource.HANDLE,
            io_type="source",
        ),
        ActionOutputHandle(
            key="order_id",
            data_type="bioyond_order_id",
            label="<order_id>",
            data_key="order_id",
            data_source=DataSource.EXECUTOR,
        ),
    ],
)
def query_order(self, order_id: str = "") -> Dict[str, Any]:
    ...
```

输入 handle 用 `DataSource.HANDLE` 表示来自上游；输出 handle 用 `DataSource.EXECUTOR` 表示来自本节点返回值。返回资源树且需要展开时可用 `data_key="resource.@flatten"`；普通标量和对象保持 `data_key="<name>"`。

handle 顺序也按“人可理解 required -> 人可理解 optional -> 内部 required -> 内部 optional”和业务重要性排序。

## Manual Confirm

人工确认节点使用：

```python
@action(
    always_free=True,
    node_type=NodeType.MANUAL_CONFIRM,
    placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
    goal_default={
        "timeout_seconds": 3600,
        "assignee_user_ids": [],
    },
    feedback_interval=300,
    description="请现场确认装载物料后继续流程",
)
def confirm_load(
    self,
    resultTable: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 3600,
    assignee_user_ids: Optional[List[str]] = None,
):
    ...
```

人工确认通常要有 `timeout_seconds` 和 `assignee_user_ids`，并显式设置 `placeholder_keys`。确认节点应尽量只展示和回传确认数据，真正的硬件动作或资源修改放在普通 action 中。

## always_free

`always_free=True` 会绕过普通设备忙碌队列。默认不要随手加；只有这些场景通常合适：

- 人工确认节点。
- 状态查询、缓存同步、资源树发布等轻量元数据操作。
- 等待外部系统回调或轮询外部调度状态，且不应该阻塞设备动作队列。
- 代理节点本身不直接占用本地设备，只把指令交给外部调度系统。

涉及本地独占硬件、会改变设备状态、必须串行保护的动作，谨慎使用 `always_free=True`。

## 完整示例

```python
@action(
    always_free=True,
    goal_default={
        "optional_params": None,
        "sample_excel_relative_path": "",
    },
    description="提交 Day2 多肽实验",
    handles=[
        ActionInputHandle(
            key="sample_excel_relative_path",
            data_type="bioyond_sample_excel_path",
            label="<sample_excel_relative_path>*",
            data_key="sample_excel_relative_path",
            data_source=DataSource.HANDLE,
            io_type="source",
        ),
        ActionOutputHandle(
            key="order_id",
            data_type="bioyond_order_id",
            label="<order_id>",
            data_key="order_id",
            data_source=DataSource.EXECUTOR,
        ),
        ActionOutputHandle(
            key="order_code",
            data_type="bioyond_order_code",
            label="实验编号",
            data_key="order_code",
            data_source=DataSource.EXECUTOR,
        ),
    ],
)
def submit_day2_experiment(
    self,
    required_params: PeptideDay2RequiredParams,
    optional_params: Optional[PeptideDay2OptionalParams] = None,
    sample_excel_relative_path: str = "",
) -> Dict[str, Any]:
    """提交 Day2 多肽实验。

    Args:
        required_params[必填参数*]: 填写本次实验必须提供的样品excel名称等参数。
        optional_params[可选参数]: 设置实验名称、创建后同步物料、参数覆盖等可选行为。
        sample_excel_relative_path[<sample_excel_relative_path>*]: 上传样品表后返回的内部文件路径，通常由上游节点传入。
    """
    ...
```

## 验证清单

提交前检查：

- 新 action 直接声明在被 `@device` 装饰的类体内，或 wrapper 使用 `parent=True`/显式签名保留 schema。
- 每个暴露输入都有 `Args:` 条目，格式为 `变量名[显示名]: 用户说明`。
- 必填显示名加 `*`；内部字段用 `<variable_name>`。
- 必填 input handle 的 `label` 也加 `*`，例如 `<order_id>*`、`实验编号列表*`。
- 函数签名、`goal_default`、handles、参数类字段、docstring 顺序一致或有兼容性理由。
- 参数类内部字段有 `Field(title=..., description=...)`。
- `order_id`、`order_code` 等标准字段命名一致。
- `goal_default` 与函数默认值不冲突；运行时真正需要的默认值写在函数签名或函数体归一化中。
- handles 的 `key` / `data_key` / `data_type` 能和上下游匹配。
- `always_free=True` 有明确理由。
- `python -m py_compile <file>` 通过；能跑时执行 registry check 和相关 pytest。
