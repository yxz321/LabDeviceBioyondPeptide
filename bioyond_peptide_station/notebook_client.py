"""Reusable helpers for writing Uni-Lab lab notebook records.

The helpers in this module intentionally know nothing about peptide-specific
results. They cover the shared notebook mechanics:

* upload binary assets through the Lab storage-token flow;
* read and save notebook ``lab_record`` content;
* build Slate-compatible paragraph, table, image, and file nodes.

The default module-level functions read Lab auth/base URL from ``unilabos`` at
call time, so importing this module remains possible in offline test
environments where ``unilabos`` is not installed.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import requests

_DEFAULT_COL_WIDTH = 160
_LAB_RECORD_STATUS_EDITING = "editing"


class _FallbackLogger:
    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None


def _get_logger() -> Any:
    try:
        from unilabos.utils.log import logger

        return logger
    except Exception:
        try:
            from unilabos.utils import logger

            return logger
        except Exception:
            return _FallbackLogger()


def _unilab_base_url() -> str:
    try:
        from unilabos.config.config import HTTPConfig
    except Exception as exc:
        raise RuntimeError(
            "notebook_client requires unilabos HTTPConfig for default Lab base URL; "
            "install/run inside the unilab environment or pass base_url to NotebookClient."
        ) from exc
    return (HTTPConfig.remote_addr or "").rstrip("/")


def _unilab_auth_secret() -> str:
    try:
        from unilabos.config.config import BasicConfig
    except Exception as exc:
        raise RuntimeError(
            "notebook_client requires unilabos BasicConfig for default Lab auth; "
            "install/run inside the unilab environment or pass auth_secret to NotebookClient."
        ) from exc
    secret = BasicConfig.auth_secret()
    if not secret:
        raise RuntimeError(
            "notebook_client is missing Lab auth: BasicConfig.auth_secret() is empty. "
            "Confirm edge was started with --ak/--sk or equivalent config."
        )
    return secret


def _guess_content_type(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


class NotebookClient:
    """Small client for Lab notebook ``lab_record`` reads/writes."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_secret: Optional[str] = None,
        logger: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._auth_secret = auth_secret
        self._logger = logger or _get_logger()

    @property
    def base_url(self) -> str:
        return self._base_url or _unilab_base_url()

    def lab_headers(self) -> Dict[str, str]:
        secret = self._auth_secret or _unilab_auth_secret()
        return {"Authorization": f"Lab {secret}"}

    def storage_put(
        self,
        filename: str,
        data_bytes: bytes,
        scene: str,
        content_type: str,
        sub_path: str = "",
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """GET a Lab storage token, PUT bytes to the signed URL, and return token data."""
        params: Dict[str, str] = {
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
        put_content_type = data.get("content_type") or content_type
        put_resp = requests.put(
            data["url"],
            data=data_bytes,
            headers={"Content-Type": put_content_type},
            timeout=timeout,
        )
        put_resp.raise_for_status()
        return data

    def upload_to_oss(
        self,
        file_path: str,
        scene: str = "image",
        content_type: Optional[str] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Upload a local file and return metadata suitable for image/file nodes."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"file to upload does not exist: {file_path}")

        filename = os.path.basename(file_path)
        ctype = content_type or _guess_content_type(filename)
        with open(file_path, "rb") as fh:
            raw = fh.read()

        data = self.storage_put(filename, raw, scene, ctype, timeout=timeout)
        public_url = data.get("public_url") or ""
        if not public_url:
            self._logger.warning(
                "[notebook_client] storage token did not return public_url "
                "scene=%s name=%s; rendered node may be empty",
                scene,
                filename,
            )
        self._logger.info(
            "[notebook_client] uploaded asset scene=%s name=%s url=%s",
            scene,
            filename,
            public_url,
        )
        return {
            "url": public_url,
            "path": data.get("path", ""),
            "name": filename,
            "size": len(raw),
            "mimeType": ctype,
        }

    def upload_lab_record_to_oss(
        self,
        uuid: str,
        lab_record: List[Dict[str, Any]],
        timeout: float = 60.0,
    ) -> str:
        """Upload the full Slate record JSON with ``scene=record`` and return public URL."""
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
            raise RuntimeError(
                "storage token did not return public_url for scene=record; "
                "cannot save lab_record as a URL"
            )
        self._logger.info(
            "[notebook_client] uploaded lab_record uuid=%s blocks=%s bytes=%s url=%s",
            uuid,
            len(lab_record),
            len(raw),
            public_url,
        )
        return public_url

    def get_notebook_detail(self, uuid: str, timeout: float = 30.0) -> Dict[str, Any]:
        """Fetch ``/lab/notebook/detail`` and return the response ``data`` object."""
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

    def save_lab_record(
        self,
        uuid: str,
        lab_record: List[Dict[str, Any]],
        timeout: float = 30.0,
    ) -> str:
        """Save record JSON by uploading it to OSS, then PATCHing its URL."""
        record_url = self.upload_lab_record_to_oss(uuid, lab_record, timeout=timeout)
        payload = {
            "uuid": uuid,
            "lab_record": record_url,
            "lab_record_status": _LAB_RECORD_STATUS_EDITING,
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
        self._logger.info(
            "[notebook_client] saved lab_record uuid=%s blocks=%s url=%s",
            uuid,
            len(lab_record),
            record_url,
        )
        return record_url

    def resolve_lab_record(self, existing: Any, timeout: float = 30.0) -> List[Dict[str, Any]]:
        """Resolve inline, JSON-string, or OSS-URL ``lab_record`` into Slate blocks."""
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
            try:
                resp = requests.get(value, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                self._logger.warning(
                    "[notebook_client] failed to fetch existing lab_record %s; "
                    "continuing from an empty document: %s",
                    value,
                    exc,
                )
                return []
            return data if isinstance(data, list) else []

        self._logger.warning(
            "[notebook_client] unrecognized lab_record shape; continuing empty: %s",
            value[:80],
        )
        return []

    def append_blocks_to_notebook(
        self,
        uuid: str,
        blocks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Append Slate blocks to an editing notebook and save the resulting record."""
        detail = self.get_notebook_detail(uuid)
        status = detail.get("lab_record_status")
        if status != _LAB_RECORD_STATUS_EDITING:
            raise RuntimeError(
                f"notebook is not writable: lab_record_status={status} "
                f"(expected editing); uuid={uuid}"
            )

        base = self.resolve_lab_record(detail.get("lab_record"))
        new_record = base + list(blocks)
        record_url = self.save_lab_record(uuid, new_record)
        return {
            "appended": len(blocks),
            "total": len(new_record),
            "lab_record_url": record_url,
        }


def text_block(text: str) -> Dict[str, Any]:
    """Build a paragraph node."""
    return {"type": "p", "children": [{"text": str(text)}]}


def _cell(tag: str, text: Any) -> Dict[str, Any]:
    return {"type": tag, "children": [{"type": "p", "children": [{"text": str(text)}]}]}


def build_table_node(
    header: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    col_sizes: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Build a native Slate table node."""
    ncol = len(header)
    if col_sizes is None:
        col_sizes = [_DEFAULT_COL_WIDTH] * ncol
    trs: List[Dict[str, Any]] = [
        {"type": "tr", "children": [_cell("th", h) for h in header]}
    ]
    for row in rows:
        trs.append({"type": "tr", "children": [_cell("td", c) for c in row]})
    return {"type": "table", "colSizes": list(col_sizes), "children": trs}


def build_image_node(meta: Dict[str, Any], width: int = 600) -> Dict[str, Any]:
    """Build an image node from ``upload_to_oss`` metadata."""
    return {
        "type": "img",
        "url": meta.get("url", ""),
        "path": meta.get("path", ""),
        "name": meta.get("name", ""),
        "size": meta.get("size", 0),
        "mimeType": meta.get("mimeType", "image/png"),
        "width": width,
        "uploadStatus": "done",
        "children": [{"text": ""}],
    }


def build_file_node(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Build a file attachment node from ``upload_to_oss`` metadata."""
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


def default_client() -> NotebookClient:
    return NotebookClient()


def upload_to_oss(
    file_path: str,
    scene: str = "image",
    content_type: Optional[str] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    return default_client().upload_to_oss(file_path, scene, content_type, timeout)


def upload_lab_record_to_oss(
    uuid: str,
    lab_record: List[Dict[str, Any]],
    timeout: float = 60.0,
) -> str:
    return default_client().upload_lab_record_to_oss(uuid, lab_record, timeout)


def get_notebook_detail(uuid: str, timeout: float = 30.0) -> Dict[str, Any]:
    return default_client().get_notebook_detail(uuid, timeout)


def save_lab_record(
    uuid: str,
    lab_record: List[Dict[str, Any]],
    timeout: float = 30.0,
) -> str:
    return default_client().save_lab_record(uuid, lab_record, timeout)


def resolve_lab_record(existing: Any, timeout: float = 30.0) -> List[Dict[str, Any]]:
    return default_client().resolve_lab_record(existing, timeout)


def append_blocks_to_notebook(
    uuid: str,
    blocks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return default_client().append_blocks_to_notebook(uuid, blocks)
