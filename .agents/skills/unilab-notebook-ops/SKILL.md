---
name: unilab-notebook-ops
description: Developer guide for writing Uni-Lab runtime Python code that operates live notebook lab_record content with bioyond_peptide_station.notebook_client. Use when Codex should implement or modify runtime scripts, device actions, workflow glue, diagnostics, or tests that append text, upload/attach files, remove exact text, or download notebook attachments while relying on the active unilab environment, HTTPConfig, and BasicConfig instead of manually handling Lab API URL or AK/SK.
---

# Uni-Lab Notebook Ops

Use this skill to write developer-facing Python code that runs inside the active Uni-Lab runtime or `unilab` conda environment.
The runtime path should use `bioyond_peptide_station.notebook_client` instead of reimplementing Lab HTTP calls.

For standalone external agents without `unilabos` or the peptide package, use `$unilab-notebook-external`.

## Developer Posture

- Treat this as a coding helper, not a user-facing operation guide.
- Prefer adding small functions or scripts that call `bioyond_peptide_station.notebook_client`.
- Let the runtime provide Lab API URL and auth:
  - `HTTPConfig.remote_addr`
  - `BasicConfig.auth_secret()`
- Do not ask for AK/SK unless runtime auth is missing and the task explicitly wants a fallback.
- Only ask the caller for a notebook URL or notebook UUID when the target is missing.
- Do not print AK, SK, auth secrets, or authorization headers in logs, exceptions, or diagnostics.
- Keep notebook writes minimal: append specific blocks or remove exact target blocks.
- Remember the helper saves by whole-record read/modify/upload/PATCH; avoid concurrent write assumptions.

## Notebook Target

Accept either a notebook UUID or browser URL. Use this parser in runtime scripts:

```python
import re

def resolve_notebook_id(value: str) -> str:
    match = re.search(r"/experiment-record/([0-9a-fA-F-]{36})(?:[/?#]|$)", value)
    if match:
        return match.group(1)
    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        value,
    )
    if match:
        return match.group(0)
    raise ValueError("missing notebook uuid")
```

If asking a human for the target, ask:

```text
请提供希望操作的实验记录本链接或 uuid：新建或进入实验记录本后，直接复制浏览器地址即可。
```

## Runtime Helper API

Import:

```python
from bioyond_peptide_station import notebook_client as nbc
```

Core helpers:

- `nbc.default_client()` / `nbc.NotebookClient()`
- `nbc.text_block(text)`
- `client.upload_to_oss(file_path, scene="file", content_type=None)`
- `nbc.build_file_node(meta)`
- `client.append_blocks_to_notebook(uuid, blocks)`
- `client.get_notebook_detail(uuid)`
- `client.resolve_lab_record(existing)`
- `client.save_lab_record(uuid, lab_record)`

Do not pass `base_url` or `auth_secret` in normal runtime code. The default client gets those from Uni-Lab config at call time.

## Append Text

Use for smoke tests, status notes, or generated textual summaries:

```python
from bioyond_peptide_station import notebook_client as nbc

def append_text_to_notebook(notebook_id: str, text: str) -> dict:
    client = nbc.default_client()
    return client.append_blocks_to_notebook(notebook_id, [nbc.text_block(text)])
```

Report `result["appended"]` and `result["total"]` if available.

## Attach A File

Use this for "write/add/upload a file to the notebook" from runtime code:

```python
from bioyond_peptide_station import notebook_client as nbc

def attach_file_to_notebook(notebook_id: str, file_path: str) -> dict:
    client = nbc.default_client()
    meta = client.upload_to_oss(file_path, scene="file")
    return client.append_blocks_to_notebook(notebook_id, [nbc.build_file_node(meta)])
```

Flow:

1. `upload_to_oss(..., scene="file")` gets a Lab storage token and PUTs bytes to OSS.
2. `build_file_node(meta)` creates the Slate file node.
3. `append_blocks_to_notebook()` reads the current record, appends the node, uploads the full record JSON with `scene=record`, and PATCHes the notebook.

Override `content_type` only if MIME guessing is wrong:

```python
meta = client.upload_to_oss(path, scene="file", content_type="application/zip")
```

## Remove Exact Text

Use exact matching for cleanup/smoke-test rollback:

```python
from typing import Any

from bioyond_peptide_station import notebook_client as nbc

def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or []:
            yield from _walk(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)

def _block_text(block: dict) -> str:
    return "".join(
        node["text"] for node in _walk(block)
        if isinstance(node, dict) and isinstance(node.get("text"), str)
    )

def remove_first_exact_text(notebook_id: str, target_text: str) -> dict:
    client = nbc.default_client()
    detail = client.get_notebook_detail(notebook_id)
    if detail.get("lab_record_status") != "editing":
        raise RuntimeError(f"notebook is not writable: {detail.get('lab_record_status')}")
    record = client.resolve_lab_record(detail.get("lab_record"))
    new_record = []
    removed = 0
    for block in record:
        if removed == 0 and block.get("type") == "p" and _block_text(block) == target_text:
            removed = 1
            continue
        new_record.append(block)
    if removed:
        client.save_lab_record(notebook_id, new_record)
    return {"removed": removed, "before": len(record), "after": len(new_record)}
```

## Download Attachments

Use this pattern when runtime code needs files already attached in a notebook:

```python
from pathlib import Path
from urllib.parse import unquote, urlparse
import os
import re
import requests

from bioyond_peptide_station import notebook_client as nbc

def _node_text(node: dict) -> str:
    return " ".join(str(node.get(k) or "") for k in (
        "url", "href", "src", "name", "filename", "fileName", "title", "path", "mimeType", "mime_type", "type"
    )).lower()

def download_notebook_files(notebook_id: str, out_dir: str, ext: str = "", name_contains: str = "") -> list[Path]:
    client = nbc.default_client()
    detail = client.get_notebook_detail(notebook_id)
    record = client.resolve_lab_record(detail.get("lab_record"))
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    ext = ext.lower()
    if ext and not ext.startswith("."):
        ext = "." + ext

    downloaded = []
    for node in _walk(record):
        if not isinstance(node, dict):
            continue
        url = str(node.get("url") or node.get("href") or node.get("src") or "")
        haystack = _node_text(node)
        if not url.startswith("http"):
            continue
        if ext and ext not in haystack:
            continue
        if name_contains and name_contains.lower() not in haystack:
            continue
        name = str(node.get("name") or node.get("filename") or os.path.basename(unquote(urlparse(url).path)) or "attachment")
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        target = out / name
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        downloaded.append(target)
    return downloaded
```

For requests like "download .zip to xxx", use `ext=".zip"` and the requested destination directory.

## Reference CLI

The bundled script is a reference implementation and quick diagnostic tool, not the primary interface for developer tasks:

```bash
mamba run -n unilab python .agents/skills/unilab-notebook-ops/scripts/notebook_ops_runtime.py --help
```

Supported subcommands:

- `append-text`
- `upload-file`
- `remove-exact-text`
- `download-files`
- `download-xlsx`

Use it to verify behavior before embedding similar code into an action or workflow script.

## Error Handling

Common non-secret errors:

- Missing helper import: package is not installed in this runtime; install this repo into `unilab` or use `$unilab-notebook-external`.
- Missing `HTTPConfig`: not running inside a Uni-Lab environment.
- Empty `BasicConfig.auth_secret()`: runtime was not launched with Lab credentials.
- `lab_record_status` not `editing`: do not write; report current status.
- Storage token lacks `public_url`: file/record cannot be represented in the notebook.

## Reporting

When reporting runtime script results, include:

- Helper path used.
- Notebook UUID.
- Resolved base URL if available.
- For writes: appended/removed counts and total blocks.
- For uploads/downloads: file paths and byte sizes.
- Exact non-secret error messages.
