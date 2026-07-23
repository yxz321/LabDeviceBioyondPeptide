---
name: unilab-notebook-external
description: Operate Uni-Lab live notebooks from an external agent without requiring a Uni-Lab or peptide-package install. Use when Codex needs to create a draft notebook and obtain its UUID, append a small text paragraph, upload/attach a local file through Lab OSS then append a file node, remove an exact text paragraph, inspect notebook lab_record structure, or fetch/download notebook attachments of any requested file type such as .xlsx, .zip, .pdf, or all files while resolving notebook URL/UUID, Lab API environment URL, AK, and SK without exposing secrets.
---

# Uni-Lab Notebook External

Use this skill for live Uni-Lab notebook operations from environments that may not have `unilabos` or `bioyond_peptide_station` installed.
The bundled script uses only Python plus `requests` and calls the Lab backend directly.

Notebook writes are whole-`lab_record` updates:

1. Read notebook detail.
2. Resolve existing `lab_record` JSON or record URL.
3. Upload the new full record JSON to OSS with `scene=record`.
4. PATCH `/lab/notebook/lab-record` with the new record URL.

Treat every write as production data.

## Safety Rules

- Always ask the user to provide the experiment-record browser URL and the copied lab launch command before live notebook operations, unless they already provided equivalent values in the current turn.
- For notebook creation, ask for the target `lab_uuid`, notebook name, and copied lab launch command. If the user wants to bind the draft to a workflow or project, also ask for `workflow_uuid` or `project_uuid`.
- Prefer the remote Lab API parsed from `--addr` in the launch command. Otherwise use an explicit known environment URL from `--env` or `--base-url`.
- Do not print AK, SK, base64 auth secrets, `Authorization` headers, or process/env values that may contain credentials.
- For create/write operations, make the smallest possible change and report counts or returned IDs.
- For remove operations, match exact text unless the user explicitly asks for broader deletion.
- Do not edit repository source files while performing notebook operations.

## Ask the User

When the notebook URL or Lab credentials are missing, ask in Chinese:

```text
请提供希望操作的实验记录本链接：新建或进入实验记录本后，直接复制浏览器地址即可。

请提供实验室启动命令以便解析密钥（ak/sk）和环境地址：点击 LeapLab 网页左下角头像，进入「实验室详情」，复制启动实验室命令即可。
```

When creating a notebook, ask in Chinese:

```text
请提供要在哪个实验室中新建记录本的 lab_uuid，以及记录本名称。

请提供实验室启动命令以便解析密钥（ak/sk）和环境地址：点击 LeapLab 网页左下角头像，进入「实验室详情」，复制启动实验室命令即可。
```

Expected inputs look like:

```text
https://leap-lab.bohrium.com/laboratory/47ca706e-fe5d-4bce-a39c-3352e3f2b949/experiment-record/565d8f26-4376-4108-a618-8db9ad64e041
```

```text
unilab -g test/experiments/comprehensive_protocol/comprehensive_station.json --ak <ak> --sk <sk> --upload_registry --addr https://leap-lab.bohrium.com/api/v1 --disable_browser
```

Resolve:

- Notebook UUID from `/experiment-record/<uuid>` in the browser URL.
- Lab UUID from `/laboratory/<uuid>` in the browser URL, or from the user when creating a new notebook.
- `ak`, `sk`, and API URL from `--ak`, `--sk`, and `--addr` in the launch command.

Known Lab API URLs:

- 线上: `https://leap-lab.bohrium.com/api/v1`
- test: `https://leap-lab.test.bohrium.com/api/v1`
- uat: `https://leap-lab.uat.bohrium.com/api/v1`

## Raw API Flow

The external script implements these calls:

- Auth header: `Authorization: Lab <base64(ak:sk)>`
- Create draft notebook: `POST {base_url}/lab/notebook`
- Read detail: `GET {base_url}/lab/notebook/detail?uuid=<notebook_uuid>`
- Get OSS token: `GET {base_url}/lab/storage/token`
- Upload bytes: signed `PUT` to token response `data.url`
- Save record: `PATCH {base_url}/lab/notebook/lab-record`

For notebook creation:

1. Use `POST {base_url}/lab/notebook` with JSON `{"lab_uuid":"<lab_uuid>","name":"<name>","status":"draft"}`.
2. Optional fields: `workflow_uuid`, `project_uuid`, `lab_number`, and `lab_record`.
3. The returned notebook UUID is `data.uuid`; the auto-generated experiment number, when present, is `data.lab_number`.
4. The browser URL is `{web_origin}/laboratory/<lab_uuid>/experiment-record/<uuid>`, where `web_origin` is the API host without `/api/v1`.

For direct runnable creation instead of a draft, omit `status:"draft"` and include `workflow_uuid`, `name`, and non-empty `node_params`. Each `node_params` group contains `datas`, and each item needs `node_uuid` plus non-empty `param`. This may trigger approval or scheduling, so prefer draft creation unless the user explicitly asks to submit/run.

For file attachments:

1. Get an OSS token with `scene=file`, `filename`, and `content_type`.
2. PUT the local file bytes to the signed URL.
3. Build a Slate file node with `type=file`, `url=public_url`, `path`, `name`, `size`, `mimeType`, and `uploadStatus=done`.
4. Append that file node through the whole-record save flow.

For record saves:

1. Serialize the full Slate block list as JSON.
2. Get an OSS token with `scene=record` and `sub_path=<notebook_uuid>`.
3. PUT the record JSON to the signed URL.
4. PATCH the notebook with `lab_record=<public_url>` and `lab_record_status=editing`.

## Script

Use `scripts/notebook_ops.py` for repeatable operations. It does not import `unilabos` or the peptide package.

Create a draft notebook and get its UUID:

```bash
python .agents/skills/unilab-notebook-external/scripts/notebook_ops.py \
  --launch-command '<copied unilab command>' \
  create-notebook \
  --lab-uuid '<lab UUID>' \
  --name '记录本名称'
```

```bash
python .agents/skills/unilab-notebook-external/scripts/notebook_ops.py \
  --launch-command '<copied unilab command>' \
  append-text \
  --notebook-url '<experiment-record browser URL>' \
  --text "small paragraph"
```

```bash
python .agents/skills/unilab-notebook-external/scripts/notebook_ops.py \
  --launch-command '<copied unilab command>' \
  upload-file \
  --notebook-url '<experiment-record browser URL>' \
  --file /path/to/local/file.zip
```

```bash
python .agents/skills/unilab-notebook-external/scripts/notebook_ops.py \
  --launch-command '<copied unilab command>' \
  remove-exact-text \
  --notebook-url '<experiment-record browser URL>' \
  --text "paragraph to remove"
```

```bash
python .agents/skills/unilab-notebook-external/scripts/notebook_ops.py \
  --launch-command '<copied unilab command>' \
  download-files \
  --notebook-url '<experiment-record browser URL>' \
  --ext .zip \
  --out /output/path
```

Credential options, in order:

- Pass `--notebook-url` and `--launch-command`; the script parses notebook UUID, `--addr`, `--ak`, and `--sk`.
- For `create-notebook`, pass `--lab-uuid` and `--launch-command`; the script parses `--addr`, `--ak`, and `--sk`, then prints `notebook_id` and `notebook_url`.
- Or pass `--notebook-id`, `--base-url`, `--ak`, and `--sk` directly.
- Set `UNILAB_AK` and `UNILAB_SK`, plus `--base-url` or `--env`.

For user requests like "create a notebook", "new experiment record", or "get a fresh notebook UUID", use `create-notebook`:

- Required: `--lab-uuid` and usually `--name`.
- Optional: `--workflow-uuid` to pre-bind the draft to a workflow.
- Optional: `--project-uuid` to place it under a project.
- Optional: `--initial-text` for a first paragraph, or `--lab-record-json` for a raw Slate block list.
- Report the printed `notebook_id` as the notebook UUID.

For user requests like "download .zip to xxx" or "download the PDF to this folder", use `download-files`:

- `--ext .zip` / `--ext .pdf` / `--ext .xlsx` for extension-based matching.
- `--name-contains <text>` when the user identifies a file by name fragment.
- `--all` only when the user asks to download every file/link attachment.
- `--out <path>` accepts either a destination directory or a single file path. Use a directory when more than one file may match.
- Keep `download-xlsx` only as a backward-compatible shortcut for `download-files --ext .xlsx`.

For user requests like "add this file to the notebook", "attach this ZIP", or "upload a report file", use `upload-file`. Override `--content-type` only when MIME guessing is wrong.

## Reporting

Report only non-secret details:

- Operation success/failure.
- Remote base URL and script/client path used.
- For create: notebook UUID (`notebook_id`), notebook browser URL, and lab number if returned.
- For append: appended count and total block count.
- For uploads: local file path, byte size, scene, whether a public URL was returned, appended count, and total block count.
- For remove: removed count, before count, after count.
- For downloads: filter used, candidate count, downloaded count, absolute file path(s), and byte sizes.
- Exact non-secret error messages if the operation fails.
