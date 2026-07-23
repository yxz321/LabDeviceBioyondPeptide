#!/usr/bin/env python3
"""Runtime Uni-Lab notebook operations using bioyond_peptide_station.notebook_client.

This script expects the active Python environment to have Uni-Lab config and
auth already initialized. It deliberately avoids printing credentials.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import unquote, urlparse

import requests


def _notebook_id(args: argparse.Namespace) -> str:
    if args.notebook_id:
        return args.notebook_id
    if args.notebook_url:
        match = re.search(r"/experiment-record/([0-9a-fA-F-]{36})(?:[/?#]|$)", args.notebook_url)
        if match:
            return match.group(1)
        match = re.search(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
            args.notebook_url,
        )
        if match:
            return match.group(0)
    raise RuntimeError("missing notebook id; pass --notebook-id or --notebook-url")


def _client() -> Any:
    from bioyond_peptide_station import notebook_client as nbc

    return nbc.default_client()


def _base_url(client: Any) -> str:
    try:
        return str(client.base_url)
    except Exception:
        return "unknown"


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
    from bioyond_peptide_station import notebook_client as nbc

    client = _client()
    notebook_id = _notebook_id(args)
    result = client.append_blocks_to_notebook(notebook_id, [nbc.text_block(args.text)])
    print("SUCCESS")
    print(f"base_url={_base_url(client)}")
    print("helper=bioyond_peptide_station.notebook_client.default_client().append_blocks_to_notebook")
    print(f"appended={result.get('appended', 'unknown')}")
    print(f"total={result.get('total', 'unknown')}")
    print(f"has_lab_record_url={bool(result.get('lab_record_url'))}")
    return 0


def upload_file(args: argparse.Namespace) -> int:
    from bioyond_peptide_station import notebook_client as nbc

    client = _client()
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
    result = client.append_blocks_to_notebook(notebook_id, [nbc.build_file_node(meta)])
    print("SUCCESS")
    print(f"base_url={_base_url(client)}")
    print("helper=bioyond_peptide_station.notebook_client.upload_to_oss+build_file_node+append_blocks_to_notebook")
    print(f"uploaded_file={file_path}")
    print(f"bytes={file_path.stat().st_size}")
    print(f"scene={args.scene}")
    print(f"has_public_url={bool(meta.get('url'))}")
    print(f"appended={result.get('appended', 'unknown')}")
    print(f"total={result.get('total', 'unknown')}")
    return 0


def remove_exact_text(args: argparse.Namespace) -> int:
    client = _client()
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
    print(f"base_url={_base_url(client)}")
    print("helper=bioyond_peptide_station.notebook_client.default_client()")
    print(f"removed={removed}")
    print(f"before={before}")
    print(f"after={len(new_record)}")
    return 0 if removed else 2


def download_files(args: argparse.Namespace) -> int:
    client = _client()
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

    print(f"base_url={_base_url(client)}")
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
        raise RuntimeError("multiple files matched but --out looks like a file path; pass a directory path instead")

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime Uni-Lab notebook lab_record operations")
    parser.add_argument("--timeout", type=float, default=60.0)
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append-text")
    append.add_argument("--notebook-id", default=None)
    append.add_argument("--notebook-url", default=None)
    append.add_argument("--text", required=True)
    append.set_defaults(func=append_text)

    upload = sub.add_parser("upload-file")
    upload.add_argument("--notebook-id", default=None)
    upload.add_argument("--notebook-url", default=None)
    upload.add_argument("--file", required=True)
    upload.add_argument("--scene", default="file")
    upload.add_argument("--content-type", default=None)
    upload.set_defaults(func=upload_file)

    remove = sub.add_parser("remove-exact-text")
    remove.add_argument("--notebook-id", default=None)
    remove.add_argument("--notebook-url", default=None)
    remove.add_argument("--text", required=True)
    remove.set_defaults(func=remove_exact_text)

    download = sub.add_parser("download-files")
    download.add_argument("--notebook-id", default=None)
    download.add_argument("--notebook-url", default=None)
    download.add_argument("--out", required=True)
    download.add_argument("--ext", default=None)
    download.add_argument("--name-contains", default=None)
    download.add_argument("--all", action="store_true")
    download.set_defaults(func=download_files)

    download_xlsx_parser = sub.add_parser("download-xlsx")
    download_xlsx_parser.add_argument("--notebook-id", default=None)
    download_xlsx_parser.add_argument("--notebook-url", default=None)
    download_xlsx_parser.add_argument("--out", default=None)
    download_xlsx_parser.add_argument("--out-dir", default=None)
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
