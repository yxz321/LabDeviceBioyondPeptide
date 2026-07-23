#!/usr/bin/env python3
"""Small CLI for live Uni-Lab notebook lab_record operations.

The script deliberately avoids printing AK/SK, derived auth secrets, or headers.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import requests

ENV_URLS = {
    "prod": "https://leap-lab.bohrium.com/api/v1",
    "online": "https://leap-lab.bohrium.com/api/v1",
    "线上": "https://leap-lab.bohrium.com/api/v1",
    "test": "https://leap-lab.test.bohrium.com/api/v1",
    "uat": "https://leap-lab.uat.bohrium.com/api/v1",
}


LAB_RECORD_STATUS_EDITING = "editing"


def _launch_value(command: str | None, option: str) -> str | None:
    if not command:
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for idx, part in enumerate(parts):
        if part == option and idx + 1 < len(parts):
            return parts[idx + 1]
        if part.startswith(f"{option}="):
            return part.split("=", 1)[1]
    return None


def _notebook_id(args: argparse.Namespace) -> str:
    if args.notebook_id:
        return args.notebook_id
    if args.notebook_url:
        match = re.search(r"/experiment-record/([0-9a-fA-F-]{36})(?:[/?#]|$)", args.notebook_url)
        if match:
            return match.group(1)
        match = re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", args.notebook_url)
        if match:
            return match.group(0)
    raise RuntimeError("missing notebook id; pass --notebook-id or --notebook-url")


def _web_origin_from_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _base_url(args: argparse.Namespace) -> str:
    explicit = args.base_url or _launch_value(args.launch_command, "--addr")
    if explicit:
        return explicit.rstrip("/")
    if args.env:
        return ENV_URLS[args.env].rstrip("/")
    raise RuntimeError("missing Lab API URL; pass --base-url, --env, or a launch command with --addr")


def _auth_secret(args: argparse.Namespace) -> str | None:
    launch_command = getattr(args, "launch_command", None)
    ak = getattr(args, "ak", None) or _launch_value(launch_command, "--ak") or os.environ.get("UNILAB_AK")
    sk = getattr(args, "sk", None) or _launch_value(launch_command, "--sk") or os.environ.get("UNILAB_SK")
    if ak and sk:
        return base64.b64encode(f"{ak}:{sk}".encode("utf-8")).decode("utf-8")
    return None


def _guess_content_type(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


class LabNotebookClient:
    def __init__(self, base_url: str, auth_secret: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_secret = auth_secret

    def lab_headers(self) -> Dict[str, str]:
        if not self.auth_secret:
            raise RuntimeError("missing Lab auth; pass --ak/--sk or a launch command with --ak and --sk")
        return {"Authorization": f"Lab {self.auth_secret}"}

    def storage_put(
        self,
        filename: str,
        data_bytes: bytes,
        scene: str,
        content_type: str,
        sub_path: str = "",
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        params = {
            "scene": scene,
            "filename": filename,
            "content_type": content_type,
        }
        if sub_path:
            params["sub_path"] = sub_path
        token_resp = requests.get(
            f"{self.base_url}/lab/storage/token",
            params=params,
            headers=self.lab_headers(),
            timeout=timeout,
        )
        token_resp.raise_for_status()
        body = token_resp.json()
        if body.get("code") != 0 or "data" not in body:
            raise RuntimeError(f"failed to get storage token: {body}")

        data = body["data"]
        put_resp = requests.put(
            data["url"],
            data=data_bytes,
            headers={"Content-Type": data.get("content_type") or content_type},
            timeout=timeout,
        )
        put_resp.raise_for_status()
        return data

    def upload_to_oss(
        self,
        file_path: str,
        scene: str = "file",
        content_type: Optional[str] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"file to upload does not exist: {path}")
        raw = path.read_bytes()
        ctype = content_type or _guess_content_type(path.name)
        data = self.storage_put(path.name, raw, scene, ctype, timeout=timeout)
        return {
            "url": data.get("public_url") or "",
            "path": data.get("path", ""),
            "name": path.name,
            "size": len(raw),
            "mimeType": ctype,
        }

    def upload_lab_record_to_oss(
        self,
        uuid: str,
        lab_record: List[Dict[str, Any]],
        timeout: float = 60.0,
    ) -> str:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        filename = f"record-{ts}.json"
        raw = json.dumps(lab_record, ensure_ascii=False).encode("utf-8")
        data = self.storage_put(
            filename,
            raw,
            "record",
            "application/json",
            sub_path=str(uuid),
            timeout=timeout,
        )
        public_url = data.get("public_url") or ""
        if not public_url:
            raise RuntimeError("storage token did not return public_url for scene=record")
        return public_url

    def get_notebook_detail(self, uuid: str, timeout: float = 30.0) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.base_url}/lab/notebook/detail",
            params={"uuid": uuid},
            headers=self.lab_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"failed to get notebook detail: {body}")
        return body.get("data", {}) or {}

    def resolve_lab_record(self, existing: Any, timeout: float = 30.0) -> List[Dict[str, Any]]:
        if isinstance(existing, list):
            return list(existing)
        if not isinstance(existing, str):
            return []
        value = existing.strip()
        if not value:
            return []

        for _ in range(2):
            if not value or value[0] not in '"[{':
                break
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                break
            if isinstance(parsed, list):
                return parsed
            if not isinstance(parsed, str):
                break
            value = parsed.strip()

        if re.match(r"^https?://", value, re.IGNORECASE):
            resp = requests.get(value, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        return []

    def save_lab_record(
        self,
        uuid: str,
        lab_record: List[Dict[str, Any]],
        timeout: float = 30.0,
    ) -> str:
        record_url = self.upload_lab_record_to_oss(uuid, lab_record, timeout=timeout)
        payload = {
            "uuid": uuid,
            "lab_record": record_url,
            "lab_record_status": LAB_RECORD_STATUS_EDITING,
        }
        resp = requests.patch(
            f"{self.base_url}/lab/notebook/lab-record",
            headers={**self.lab_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"failed to save lab_record: {body}")
        return record_url

    def create_draft_notebook(
        self,
        lab_uuid: str,
        name: str,
        workflow_uuid: str = "",
        project_uuid: str = "",
        lab_number: str = "",
        lab_record: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "lab_uuid": lab_uuid,
            "name": name,
            "status": "draft",
        }
        if workflow_uuid:
            payload["workflow_uuid"] = workflow_uuid
        if project_uuid:
            payload["project_uuid"] = project_uuid
        if lab_number:
            payload["lab_number"] = lab_number
        if lab_record is not None:
            payload["lab_record"] = lab_record

        resp = requests.post(
            f"{self.base_url}/lab/notebook",
            headers={**self.lab_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"failed to create notebook: {body}")
        return body.get("data", {}) or {}

    def append_blocks_to_notebook(self, uuid: str, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        detail = self.get_notebook_detail(uuid)
        status = detail.get("lab_record_status")
        if status != LAB_RECORD_STATUS_EDITING:
            raise RuntimeError(
                f"notebook is not writable: lab_record_status={status} "
                f"(expected editing); uuid={uuid}"
            )
        base = self.resolve_lab_record(detail.get("lab_record"))
        new_record = base + list(blocks)
        record_url = self.save_lab_record(uuid, new_record)
        return {"appended": len(blocks), "total": len(new_record), "lab_record_url": record_url}


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "p", "children": [{"text": str(text)}]}


def build_file_node(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "file",
        "url": meta.get("url", ""),
        "path": meta.get("path", ""),
        "name": meta.get("name", ""),
        "size": meta.get("size", 0),
        "mimeType": meta.get("mimeType", "application/octet-stream"),
        "uploadStatus": "done",
        "children": [{"text": ""}],
    }


def _client(args: argparse.Namespace) -> LabNotebookClient:
    return LabNotebookClient(base_url=_base_url(args), auth_secret=_auth_secret(args))



def _walk(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or []:
            yield from _walk(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _block_text(block: Dict[str, Any]) -> str:
    parts: List[str] = []
    for node in _walk(block):
        text = node.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _safe_name(name: str, url: str, notebook_id: str, idx: int, fallback_ext: str = "") -> str:
    candidate = name or os.path.basename(unquote(urlparse(url).path))
    if not candidate:
        candidate = f"notebook-{notebook_id}-attachment-{idx}{fallback_ext}"
    elif fallback_ext and "." not in Path(candidate).name:
        candidate = f"{candidate}{fallback_ext}"
    candidate = re.sub(r"[\\/:*?\"<>|]+", "_", candidate).strip()
    return candidate or f"attachment-{idx}{fallback_ext}"


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _output_target(out_path: Path, filename: str, multiple: bool) -> Path:
    out_path = out_path.expanduser().resolve()
    if multiple or out_path.exists() and out_path.is_dir() or not out_path.suffix:
        out_path.mkdir(parents=True, exist_ok=True)
        return _unique_path(out_path / filename)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return _unique_path(out_path)


def _node_file_url(node: Dict[str, Any]) -> str:
    return str(node.get("url") or node.get("href") or node.get("src") or "")


def _node_file_name(node: Dict[str, Any]) -> str:
    return str(node.get("name") or node.get("filename") or node.get("fileName") or node.get("title") or "")


def _node_file_haystack(node: Dict[str, Any]) -> str:
    values = [
        _node_file_url(node),
        _node_file_name(node),
        str(node.get("path") or ""),
        str(node.get("mimeType") or node.get("mime_type") or ""),
        str(node.get("type") or ""),
    ]
    return " ".join(values).lower()


def _normalize_ext(ext: str | None) -> str:
    if not ext:
        return ""
    ext = ext.strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return ext


def _file_matches(node: Dict[str, Any], ext: str, name_contains: str, all_files: bool) -> bool:
    url = _node_file_url(node)
    if not url.startswith("http"):
        return False
    haystack = _node_file_haystack(node)
    if all_files:
        return True
    if ext and ext in haystack:
        return True
    if name_contains and name_contains.lower() in haystack:
        return True
    return False


def append_text(args: argparse.Namespace) -> int:
    client = _client(args)
    notebook_id = _notebook_id(args)
    result = client.append_blocks_to_notebook(notebook_id, [text_block(args.text)])
    print("SUCCESS")
    print(f"base_url={client.base_url}")
    print("client=.agents/skills/unilab-notebook-external/scripts/notebook_ops.py:LabNotebookClient.append_blocks_to_notebook")
    print(f"appended={result.get('appended', 'unknown')}")
    print(f"total={result.get('total', 'unknown')}")
    print(f"has_lab_record_url={bool(result.get('lab_record_url'))}")
    return 0


def upload_file(args: argparse.Namespace) -> int:
    client = _client(args)
    notebook_id = _notebook_id(args)
    file_path = Path(args.file).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"file to upload does not exist: {file_path}")

    meta = client.upload_to_oss(
        str(file_path),
        scene=args.scene,
        content_type=args.content_type,
        timeout=args.timeout,
    )
    result = client.append_blocks_to_notebook(notebook_id, [build_file_node(meta)])
    print("SUCCESS")
    print(f"base_url={client.base_url}")
    print("client=.agents/skills/unilab-notebook-external/scripts/notebook_ops.py:upload_to_oss+append_blocks_to_notebook")
    print(f"uploaded_file={file_path}")
    print(f"bytes={file_path.stat().st_size}")
    print(f"scene={args.scene}")
    print(f"has_public_url={bool(meta.get('url'))}")
    print(f"appended={result.get('appended', 'unknown')}")
    print(f"total={result.get('total', 'unknown')}")
    return 0


def remove_exact_text(args: argparse.Namespace) -> int:
    client = _client(args)
    notebook_id = _notebook_id(args)
    detail = client.get_notebook_detail(notebook_id)
    status = detail.get("lab_record_status")
    if status != "editing":
        raise RuntimeError(f"notebook is not writable: lab_record_status={status} (expected editing)")

    record = client.resolve_lab_record(detail.get("lab_record"))
    before = len(record)
    removed = 0
    new_record: List[Dict[str, Any]] = []
    for block in record:
        if removed == 0 and isinstance(block, dict) and block.get("type") == "p" and _block_text(block) == args.text:
            removed += 1
            continue
        new_record.append(block)

    if removed:
        client.save_lab_record(notebook_id, new_record)

    print("SUCCESS" if removed else "NOT_FOUND")
    print(f"base_url={client.base_url}")
    print("client=.agents/skills/unilab-notebook-external/scripts/notebook_ops.py:LabNotebookClient")
    print(f"removed={removed}")
    print(f"before={before}")
    print(f"after={len(new_record)}")
    return 0 if removed else 2


def download_files(args: argparse.Namespace) -> int:
    client = _client(args)
    notebook_id = _notebook_id(args)
    out_arg = getattr(args, "out", None) or getattr(args, "out_dir", None)
    if not out_arg:
        raise RuntimeError("missing output path; pass --out or --out-dir")
    out_path = Path(out_arg)
    ext = _normalize_ext(args.ext)
    name_contains = args.name_contains or ""
    all_files = bool(args.all)

    detail = client.get_notebook_detail(notebook_id)
    record = client.resolve_lab_record(detail.get("lab_record"))
    matches: List[Dict[str, str]] = []
    for node in _walk(record):
        if _file_matches(node, ext, name_contains, all_files):
            matches.append({"url": _node_file_url(node), "name": _node_file_name(node)})

    print(f"base_url={client.base_url}")
    print(f"notebook_id={notebook_id}")
    print(f"file_candidates={len(matches)}")
    if ext:
        print(f"filter_ext={ext}")
    if name_contains:
        print(f"filter_name_contains={name_contains}")
    if not matches:
        print(f"record_blocks={len(record)}")
        return 2
    if len(matches) > 1 and out_path.suffix and not out_path.exists():
        raise RuntimeError(
            "multiple files matched but --out looks like a file path; pass a directory path instead"
        )

    downloaded = []
    for idx, item in enumerate(matches, 1):
        filename = _safe_name(item.get("name", ""), item["url"], notebook_id, idx, fallback_ext=ext)
        target = _output_target(out_path, filename, multiple=len(matches) > 1)
        resp = requests.get(item["url"], timeout=args.timeout)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        downloaded.append((target, len(resp.content)))

    print(f"downloaded={len(downloaded)}")
    for target, size in downloaded:
        print(f"file={target}")
        print(f"bytes={size}")
    return 0


def download_xlsx(args: argparse.Namespace) -> int:
    args.ext = ".xlsx"
    args.name_contains = args.name_contains or ""
    args.all = False
    args.out = getattr(args, "out", None) or getattr(args, "out_dir", None)
    return download_files(args)


def _initial_lab_record(args: argparse.Namespace) -> Optional[Any]:
    if args.initial_text and args.lab_record_json:
        raise RuntimeError("pass only one of --initial-text or --lab-record-json")
    if args.initial_text:
        return [text_block(args.initial_text)]
    if args.lab_record_json:
        try:
            return json.loads(args.lab_record_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid --lab-record-json: {exc}") from exc
    return None


def create_notebook(args: argparse.Namespace) -> int:
    client = _client(args)
    result = client.create_draft_notebook(
        lab_uuid=args.lab_uuid,
        name=args.name,
        workflow_uuid=args.workflow_uuid or "",
        project_uuid=args.project_uuid or "",
        lab_number=args.lab_number or "",
        lab_record=_initial_lab_record(args),
        timeout=args.timeout,
    )
    notebook_id = str(result.get("uuid") or "")
    if not notebook_id:
        raise RuntimeError(f"create notebook response did not include data.uuid: {result}")

    origin = _web_origin_from_base_url(client.base_url)
    notebook_url = f"{origin}/laboratory/{args.lab_uuid}/experiment-record/{notebook_id}" if origin else ""

    print("SUCCESS")
    print(f"base_url={client.base_url}")
    print("client=.agents/skills/unilab-notebook-external/scripts/notebook_ops.py:LabNotebookClient.create_draft_notebook")
    print(f"notebook_id={notebook_id}")
    print(f"lab_number={result.get('lab_number') or ''}")
    if notebook_url:
        print(f"notebook_url={notebook_url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Uni-Lab notebook lab_record operations")
    parser.add_argument("--base-url", default=None, help="Remote Lab API base URL; can also be parsed from --launch-command --addr")
    parser.add_argument("--env", choices=sorted(ENV_URLS), default=None, help="Known Lab environment URL")
    parser.add_argument("--ak", default=None, help="Uni-Lab access key; not printed")
    parser.add_argument("--sk", default=None, help="Uni-Lab secret key; not printed")
    parser.add_argument("--launch-command", default=None, help="Copied unilab launch command; --ak/--sk/--addr are parsed and not printed")
    parser.add_argument("--timeout", type=float, default=60.0)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-notebook")
    create.add_argument("--lab-uuid", required=True, help="Target laboratory UUID")
    create.add_argument("--name", default="未命名", help="Notebook display name")
    create.add_argument("--workflow-uuid", default=None, help="Optional workflow UUID to bind to the draft")
    create.add_argument("--project-uuid", default=None, help="Optional project UUID")
    create.add_argument("--lab-number", default=None, help="Optional explicit experiment number; normally omit")
    create.add_argument("--initial-text", default=None, help="Optional first paragraph for the rich-text lab_record")
    create.add_argument("--lab-record-json", default=None, help="Optional raw lab_record JSON, usually a Slate block list")
    create.set_defaults(func=create_notebook)

    append = sub.add_parser("append-text")
    append.add_argument("--notebook-id", default=None)
    append.add_argument("--notebook-url", default=None)
    append.add_argument("--text", required=True)
    append.set_defaults(func=append_text)

    upload = sub.add_parser("upload-file")
    upload.add_argument("--notebook-id", default=None)
    upload.add_argument("--notebook-url", default=None)
    upload.add_argument("--file", required=True, help="Local file path to upload and append as a notebook file node")
    upload.add_argument("--scene", default="file", help="Lab storage scene; default is file")
    upload.add_argument("--content-type", default=None, help="Optional MIME type override")
    upload.set_defaults(func=upload_file)

    remove = sub.add_parser("remove-exact-text")
    remove.add_argument("--notebook-id", default=None)
    remove.add_argument("--notebook-url", default=None)
    remove.add_argument("--text", required=True)
    remove.set_defaults(func=remove_exact_text)

    download = sub.add_parser("download-files")
    download.add_argument("--notebook-id", default=None)
    download.add_argument("--notebook-url", default=None)
    download.add_argument("--out", required=True, help="Destination file or directory. Use a directory when multiple files may match.")
    download.add_argument("--ext", default=None, help="File extension to match, such as .zip, zip, .xlsx, or pdf")
    download.add_argument("--name-contains", default=None, help="Case-insensitive substring to match in file name, URL, path, or MIME type")
    download.add_argument("--all", action="store_true", help="Download all HTTP file/link nodes found in the notebook record")
    download.set_defaults(func=download_files)

    download_xlsx_parser = sub.add_parser("download-xlsx")
    download_xlsx_parser.add_argument("--notebook-id", default=None)
    download_xlsx_parser.add_argument("--notebook-url", default=None)
    download_xlsx_parser.add_argument("--out", default=None, help="Destination file or directory")
    download_xlsx_parser.add_argument("--out-dir", default=None, help="Backward-compatible destination directory")
    download_xlsx_parser.add_argument("--name-contains", default=None)
    download_xlsx_parser.set_defaults(func=download_xlsx)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print("FAILURE")
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
