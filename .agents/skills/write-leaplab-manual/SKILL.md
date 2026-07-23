---
name: write-leaplab-manual
description: Write or update LeapLab operator manuals for Uni-Lab/Bioyond peptide workflows by fetching live workflow graphs through the LeapLab backend API, benchmarking workflow nodes against Python action code, and drafting workflow-level and action-node manual sections. Use when Codex needs to document LeapLab workflows, explain workflow execution order, describe @action nodes, reconcile manual text with handles/goal_default/TypedDict Args metadata, or update reader-friendly Chinese instructions for BioyondPeptideStation workflows.
---

# LeapLab Manual Writer

## Overview

Use this skill to write accurate, reader-friendly manuals for LeapLab workflows. Ground every manual update in two sources:

1. Live LeapLab workflow graph data.
2. The Python action implementation and registry metadata.

Prefer operator-facing language over implementation jargon. The workflow section should help a user run the workflow; the action-node section should explain one node’s inputs, outputs, defaults, upstream/downstream behavior, and operational caveats in more detail.

## Workflow

1. Collect identifiers and auth.
2. Fetch the live workflow graph by exact workflow name.
3. Topologically order nodes from graph edges.
4. Benchmark each node against Python code.
5. Update the workflow-level manual section.
6. Update detailed action-node sections for complex or reusable nodes.
7. Verify every documented parameter and data flow against both graph data and code.

## Fetch Workflow From LeapLab

Ask the user for missing values:

- `ak` and `sk` for Lab auth.
- `lab_uuid`, or a LeapLab laboratory URL containing `/laboratory/<lab_uuid>`.
- Exact `workflow_name`.
- Optional `base_url`; default to `https://leap-lab.bohrium.com/api`.

Construct auth:

```text
Authorization: Lab <base64("<ak>:<sk>")>
```

Use owner workflow list for lab-local workflow names:

```text
GET /v1/lab/workflow/owner/list?lab_uuid=<lab_uuid>&page=1&page_size=100
```

Match `name` exactly. If no exact match appears, show nearby workflow names and ask the user to choose. If more than one exact match appears, ask which UUID to use.

Fetch the selected workflow detail:

```text
GET /v1/lab/workflow/template/detail/<workflow_uuid>
```

Save large JSON responses under `temp/` while working. Do not include AK, SK, or derived auth secrets in summaries.

## Interpret The Workflow Graph

Use `nodes`, `edges`, node `param`, and node `handles`.

Build the execution order from edges instead of trusting the raw node array. A node row should be documented from:

- `name`: action/node name shown in the workflow.
- `type`: `ILab`, `manual_confirm`, group, etc.
- `device_name`: usually `bioyond_peptide_station` for this package.
- `param`: configured default/internal values in this workflow instance.
- Input handles: important upstream values and required markers.
- Output handles: values generated for downstream nodes.
- Edges: which outputs feed which later inputs.

For a workflow-level manual, do not list every automatic edge in prose. Highlight only the operator-visible sequence and the important automatic tail, such as report retrieval or Notebook attachment.

## Benchmark Against Code

For Bioyond peptide workflows, inspect:

- `bioyond_peptide_station/peptide_station.py`
- `developer_docs/bioyond_peptide_station_action_args.md`

For each node action, read the `@action` decorator, method signature, docstring `Args:`, body, and related parameter classes.

Check these code surfaces:

- `description`: concise action purpose.
- `node_type=NodeType.MANUAL_CONFIRM`: whether the user must act.
- `goal_default`: default values shown/configured in the node.
- `placeholder_keys`: fields supplied by manual confirmation UI/runtime.
- `ActionInputHandle`: upstream inputs, labels, data types, required markers, data keys.
- `ActionOutputHandle`: values produced for downstream nodes.
- `TypedDict` parameter classes and `Field(title=..., description=..., default=...)`.
- Return dictionary keys.
- Error paths, retries, timeouts, and special fallback behavior.

Use the developer docs for naming rules:

- `order_id` is internal; display it as `<order_id>`, with `*` when required.
- `order_code` displays as `实验编号`.
- Required markers belong in the display name, not the Python variable name.
- Prefer Chinese user-facing labels from `Field(title=...)`, handle labels, and `Args:` blocks.
- Avoid endpoint names and low-level RPC details unless they directly affect operator behavior.

Use display names, not implementation keys, in operator-facing text:

- In `运行前准备`, `节点和参数说明`, and action-node detail tables, name parameters by their UI/display label.
- Display labels come from handle labels, `Args:` bracket labels, and `Field(title=...)`.
- Do not expose raw Python keys as parameter names, such as `file_name_filter`, `required_params.sample_excel_pattern`, `resultTable`, `files`, `file_zip`, `reply_choice`, `poll_mode`, or `deterministic_resolve`.
- If a key is the only available clue, translate it to a Chinese display label before writing the manual, for example `file_zip` -> `报告 ZIP 文件`.
- Keep code values only when they are actual selectable values shown to the operator or needed for troubleshooting. Prefer display choices such as `重试当前步骤` over internal enum values such as `retry`.
- Action/node names such as `get_order_list` may stay as code in the `动作节点` column and operation sequence. This rule is about parameter and output names.

## Workflow-Level Manual Style

Use this structure for each workflow section:

1. Workflow screenshot or diagram if already available.
2. A short callout beginning with `适用场景：`.
3. `运行前准备`.
4. `操作顺序（人工确认节点）`.
5. `节点和参数说明`.

Write the callout as one paragraph:

```text
适用场景：在实验记录本界面选择工作流 <workflow_name> 并开始实验后，系统会...，人工确认...，最后...
```

`运行前准备` should be a short checklist of operator prerequisites. Include only items the operator can act on, such as:

- Sample Excel exists and the submit node uses the correct sample Excel name.
- Method file name is correct.
- Workstation cleanup/reset preconditions are satisfied.
- The workflow is launched from the correct experiment context when later nodes write results back.

`操作顺序（人工确认节点）` should focus on manual checkpoints and user decisions. Collapse automatic nodes into nearby steps. Example shape:

1. Run workflow and complete `reset_manual` confirmation.
2. Open the `confirm_cem_info` link and verify CEM details.
3. Use `start_experiment` loading table to load materials and continue.
4. Wait for completion, then use `unload_materials` unloading table.
5. Let automatic report nodes write results back.

`节点和参数说明` should be a table with these columns:

```text
动作节点 | 说明 | 重要内部参数 | 重要传入参数 | 重要传出参数
```

Keep this workflow table concise:

- Use `必填：无` when the node has no human-set required internal parameters.
- Put timeouts, confirmers, booleans, and retry options under `可选`.
- Use `无` for cells that genuinely have no meaningful value for an operator.
- For outputs, list only values useful downstream or useful for the reader to understand the flow.
- Use code formatting for internal names such as `<order_id>`, `<sample_excel_relative_path>`, `order_info`, and action names.

### Workflow Table Cell Structure

The `重要内部参数`, `重要传入参数`, and `重要传出参数` cells must be structured like the existing `实验_提交Day1` manual. Do not compress parameters, defaults, and operator context into one long paragraph.

Use this shape inside each parameter cell:

```text
必填： 无

可选：
- 参数展示名：默认值。简短说明。
- 参数展示名：默认值。简短说明。
```

Rules for these cells:

- Start with `必填： 无` or `必填：` followed by a short list. Do not write `必填：无。可选：...` in one paragraph.
- Put `可选：`, `运行前提：`, `输出：`, or `运行结果：` on its own line when it introduces multiple items.
- Use bullet lists for two or more parameters, outputs, or caveats.
- Each bullet should begin with the Chinese display label first, then the default/code value, then the human reason it matters.
- Keep code keys only where they help the operator or match visible handles, such as `<token>`, `<order_id>`, `retry`, or `skip`.
- Split long explanations into separate bullets. A table cell should be scannable on mobile.
- Prefer `下游传出： 无` plus `运行结果：` bullets when an action returns useful status but has no downstream edge.

Parameter names in these bullets must be display names, not implementation keys. Examples:

```text
Good:
- 样品excel筛选关键字：默认 *.xlsx。
- 报告文件列表：后续写入 Notebook 的附件来源。
- 处理方式：默认重试当前步骤。

Bad:
- file_name_filter：默认 *.xlsx。
- files：后续写入 Notebook 的附件来源。
- reply_choice：默认 retry。
```

Good XML pattern for a table cell:

```xml
<td vertical-align="top">
  <p>必填： 无</p>
  <p>可选：</p>
  <ul>
    <li>等待超时时间：默认 <code>100000</code> 秒。</li>
    <li>定时检查间隔：默认 <code>0.5</code> 秒。</li>
  </ul>
</td>
```

Bad pattern:

```xml
<td vertical-align="top">
  <p>必填：无。可选：本图等待超时为 <code>100000</code> 秒，<code>poll_mode=true</code>，检查间隔 <code>0.5</code> 秒；默认自动跳过...</p>
</td>
```

Before updating a remote document, inspect or fetch the reference workflow section the user points to and match its rendered structure, not just its column names.

## Action-Node Detail Style

Use a separate action-node section when a node has structured parameters, important defaults, non-obvious upstream behavior, retries, or outputs reused by many workflows.

Start with a plain-language summary:

```text
根据样品Excel名称和CEM方法文件名称，提交 Day1 线肽合成实验，创建奔曜实验，获取实验ID并生成装载确认表。
```

Then add an input parameter table:

```text
参数展示名 | 参数名 | 说明 | 是否必填 | 是否上游传入
```

Then add an output parameter table:

```text
参数展示名 | 参数名 | 说明 | 来源（节点生成/上游传入） | 是否向下游传出
```

For input rows, derive from code in this order:

1. `TypedDict` fields inside required/optional parameter classes.
2. Method arguments in docstring `Args:`.
3. Input handles for upstream values.
4. `goal_default` for defaults configured in the workflow node.

For output rows, derive from:

1. Output handles and labels.
2. Return dictionary keys.
3. Downstream edge usage.

Fill table cells with operator meaning:

- `是否必填`: yes when the display label has `*`, the parameter is in a required `TypedDict`, or the method raises if missing.
- `是否上游传入`: yes for input handles and internal resolved values normally produced by earlier nodes.
- `来源`: `节点生成` for values created by this action; `上游传入` for passthrough values; `节点生成并向下游传出` when both are useful.
- `是否向下游传出`: yes only when an output handle or graph edge uses it downstream.

## What To Include For Common Nodes

For submit nodes such as `submit_experiment_day1`:

- Required sample file selector, e.g. `required_params.sample_excel_pattern`.
- Required method file when applicable, e.g. `required_params.cem_method_file_name`.
- Optional experiment name and material sync/override options only when relevant to the reader.
- Output `<order_id>`, loading table, sample path, and method file passthrough.

For manual confirmation nodes:

- State what the human verifies.
- Put timeout and assignee fields under optional internal parameters.
- Emphasize required upstream display tables or links.
- Avoid over-describing generic manual confirmation internals.

For wait nodes:

- State what event or status it waits for.
- Document timeout and polling behavior.
- Mention fallback lookup behavior only if it matters to the operator.
- List generated guide tables and status outputs.

For report and attachment nodes:

- State which reports/files are gathered.
- Document retry behavior for files such as report ZIPs.
- Mention which file types are inserted when boolean options control attachments.
- Explain where results are written in operator-facing terms.

## Writing Rules

- Write in Chinese for manual content unless the existing section uses another language.
- Use short, direct sentences. Prefer “确认…”, “打开…”, “按…完成…” over passive descriptions.
- Keep workflow sections reader-friendly; move parameter detail into action-node sections.
- Do not expose raw JSON, UUIDs, AK/SK, or derived auth in the manual unless the user explicitly wants an implementation appendix.
- Do not invent defaults. If a default differs between live graph and code, document the live graph value in the workflow section and note the code default only in the action detail when useful.
- Preserve existing terminology: “奔曜内部实验ID”, “实验编号”, “装载确认表”, “下料指引表”, “实验记录本（Notebook）”.
- Treat screenshots and diagrams as supporting context; verify text against API and code, not image labels.

## Final Check

Before finishing, verify:

- The workflow UUID came from exact-name matching.
- The node order comes from edges.
- Each documented node exists in the live workflow.
- Required/manual-confirm markers match handles, `Args:`, or parameter classes.
- All default values come from the live workflow `param` or the code, with no guessing.
- Workflow-level content is concise and action-node detail contains the deeper parameter tables.
- Workflow table parameter cells use structured `p` + `ul/li` blocks when listing multiple fields, and do not render as dense one-paragraph blobs.
- `运行前准备` and `节点和参数说明` use display names for parameters/outputs; no raw keys such as `file_name_filter`, `resultTable`, `files`, `file_zip`, `reply_choice`, or `deterministic_resolve` remain in operator-facing text.
