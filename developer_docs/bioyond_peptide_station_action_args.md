# BioyondPeptideStation Action Argument Text

This is the source of truth for user-facing argument names and descriptions on
`BioyondPeptideStation` exposed actions.

## Scope

Apply these rules whenever editing:

- `BioyondPeptideStation` methods decorated with `@action`.
- `@action` metadata such as `goal_default`, placeholder keys, and handles.
- Manual-confirm argument/goal fields.
- Parameter classes such as `PeptideDay2RequiredParams` and optional parameter
  classes.
- `Args:` docstrings used by Uni-Lab-OS registry scanning.

Most actions should use one unified parameter surface: method argument name,
action goal key, handle key, parameter-class field name, display name, and
description should all describe the same concept in the same way. Exceptions are
allowed only for actions that intentionally display different names, such as
`display_values`.

## Required Args Format

Every input argument on an exposed `BioyondPeptideStation` action must have an
`Args:` entry with:

- the Python variable name;
- a display name in square brackets, using Chinese user-facing wording;
- a clear user-facing description.

Use this shape:

```python
Args:
    order_code[实验编号*]: 用于人工核对或查找的实验编号。
    order_name[实验名称]: 给本次实验显示的名称；未填写时由系统生成。
```

Mark required fields by adding `*` to the display name, not to the Python
variable name.

## Parameter Order

Order parameters manually by operator importance, not alphabetically and not by
internal implementation convenience.

Use this ranking:

1. Human-readable required parameters.
2. Human-readable optional parameters.
3. Internally resolved required parameters.
4. Internally resolved optional parameters.

Within each group, put workflow-critical fields first. For example, IDs and
business inputs should appear before tuning flags, timeouts, cached payloads, or
system-filled paths.

When it is safe to change the code surface, keep the method signature, action
goal defaults, handles, parameter classes, and docstring in the same order. If
compatibility requires keeping an old signature order, the `Args:` block should
still be sorted for users by importance.

Input handle labels should carry the same required marker as the corresponding
`Args:` or parameter-class display name. For example, an input handle that feeds
`order_id[<order_id>*]` should use `label="<order_id>*"`. Output handle labels
generally describe values produced by the node and do not automatically inherit
the input required marker.

## Internally Resolved Parameters

Parameters that are normally produced by another action or by internal runtime
state should use the raw variable name as the display name, wrapped in angle
brackets. Required internally resolved fields still use `*`.

Example:

```python
Args:
    required_params[必填参数*]: 填写本次实验必须提供的样品、板位、方法等参数。
    optional_params[可选参数]: 设置实验名称、复位、同步等可选行为；未填写时使用默认值。
    order_id[<order_id>*]: 奔曜内部实验ID，通常由上游节点传入。
    sample_excel_relative_path[<sample_excel_relative_path>*]: 上传样品表后由系统返回的内部文件路径，通常由上一步自动传入。
```

## Parameter Class Inner Fields

Parameter classes need the same variable-name, display-name, description, and
ordering discipline as top-level action arguments.

For `TypedDict` fields using `Annotated[..., Field(...)]`, put the display name
in `Field(title=...)` and the user-facing explanation in
`Field(description=...)`. Required `TypedDict` classes should mark every inner
field that is required with `*` in the title. Optional `TypedDict(total=False)`
classes should not use `*` unless compatibility forces a business-required field
to remain there.

Concrete example:

```python
class PeptideDay2RequiredParams(TypedDict):
    sample_excel_pattern: Annotated[
        str,
        Field(
            title="样品excel名称*",
            description="选择要提交的excel文件；如果已传入<sample_excel_pattern>，可填写空字符串。",
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
    parameter_overrides: Annotated[
        List[Dict[str, Any]],
        Field(
            default_factory=list,
            title="参数覆盖",
            description="仅在需要临时覆盖奔曜工作流参数时填写；未填写时使用工作流默认参数。",
        ),
    ]
```

The enclosing action docstring should describe the class arguments, not repeat
every inner field:

```python
Args:
    required_params[必填参数*]: 填写本次实验必须提供的样品excel名称等参数。
    optional_params[可选参数]: 设置实验名称、创建后同步物料、参数覆盖等可选行为。
    sample_excel_relative_path[<sample_excel_relative_path>*]: 上传样品表后由系统返回的内部文件路径，通常由上一步自动传入。
```

If the active registry scanner cannot read `Field(title=...)`, mirror the title
at the start of the description as a temporary compatibility measure, for
example `description="[样品excel名称*] 选择要提交的excel文件..."`. Do not leave
the inner field without a display name.

## User-Facing Descriptions

Descriptions must explain what the operator or workflow author is choosing.
Avoid developer jargon, HTTP endpoint names, RPC details, payload shape, and
implementation-only wording.

Use operator-facing language like:

```python
Args:
    reset_location[库位复位]: 清空仓库库位，删除所有物料。
    clear_stale_after_reset[同步时清理陈旧物料]: 复位后删除所有奔曜端不存在的陈旧物料。
```

Instead of implementation-facing language like:

```python
Args:
    reset_location[库位复位]: 调用 /api/lims/storage/reset-location，默认勾选。
    clear_stale_after_reset[同步时清理陈旧缓存]: 清理本次库存快照外的旧缓存。
```

Good reset wording:

```python
Args:
    reset_location[库位复位]: 清空仓库库位，删除所有物料。
    reset_scheduler[调度器复位]: 清空调度器当前状态，准备重新下发任务。
    reset_order_status[实验状态复位]: 将奔曜端实验状态恢复到可重新调度的初始状态。
    reset_devices[仪器复位]: 让仪器执行复位动作；仅在现场确认需要时启用。
    sync_materials_after_reset[复位后同步物料]: 复位完成后同步奔曜库存到本地资源树。
    clear_stale_after_reset[同步时清理陈旧物料]: 复位后删除所有奔曜端不存在的陈旧物料。
    publish_tree_after_reset[同步后发布资源树]: 同步成功后刷新前端资源树。
```

## Standard Order Identifiers

Use these terms everywhere unless an action intentionally presents a different
concept:

- `order_id`: treat as internally resolved; display required fields as `<order_id>*`, for example `order_id[<order_id>*]`.
- `order_code`: display as `实验编号`.

Do not use `orderID`, `orderId`, `订单ID`, `实验ID`, `order UUID`, or `orderCode`
as user-facing labels. Keep Python variables snake_case.

Recommended descriptions:

```python
Args:
    order_code[实验编号]: 用于人工核对或查找的实验编号。
    order_id[<order_id>*]: 奔曜内部实验ID，通常由上游节点传入。
```

## Consistency Checklist

Before finishing an action-argument cleanup:

- Every exposed input argument has `name[显示名]` in `Args:`.
- Required fields are marked with `*` in the display name.
- Required input handle labels are also marked with `*`.
- Internal resolved fields use `<variable_name>` display names.
- Parameter order follows required/optional and human/internal importance.
- Parameter-class inner fields use `Field(title=..., description=...)`, with
  required inner fields marked by `*` in the title.
- `order_id` uses `<order_id>*` when required, and `order_code` uses `实验编号`.
- Method arguments, `goal_default`, handles, and parameter classes agree on
  variable names, display names, and descriptions.
- Descriptions are user-facing and avoid endpoint names or implementation
  details.
