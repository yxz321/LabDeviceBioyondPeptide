"""Per-action raw call/response log for Bioyond stations.

When a debug session is active, ``wrap_rpc_http`` replaces a ``BioyondV1RPC``
instance's ``post`` / ``get`` methods with closures that perform the HTTP
transport themselves, capture the request/response details, and append a record
to the active session before returning exactly what ``BaseRequest`` would have
returned. Outside of an active session the wrapped method delegates to the
original (unwrapped) implementation, leaving non-debug behavior intact.

The session writes a Markdown file under ``out_dir`` mirroring the format of
``bioyond_debug_records/2026-04-30_160316_day3_samplefile_only_raw_calls.md``
minus the "Raw Payload Argument" section.

This module has no dependency on ``BioyondV1RPC`` itself; the only contract is
that the wrapped instance descends from ``BaseRequest`` (i.e. has a logger
returned by ``self.get_logger()``).
"""

from __future__ import annotations

import contextvars
import copy
import inspect
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional

import requests

__all__ = [
    "CallRecord",
    "CallLogContext",
    "session",
    "wrap_rpc_http",
    "active_session",
]


_DEFAULT_TIMEOUT_GET = 30
_DEFAULT_TIMEOUT_POST = 120


@dataclass
class CallRecord:
    """One captured HTTP call inside a debug session."""

    index: int
    method: str
    url: str
    path: str
    source: str
    transport: str
    http_status: Optional[int]
    request_body: Any
    response_body: Any
    error: Optional[str] = None


@dataclass
class CallLogContext:
    """State for a single ``session()`` block.

    A session lazily creates its file on the first appended record. Actions
    that abort before any RPC produce no file.
    """

    action: str
    out_dir: Path
    started_at: datetime
    calls: List[CallRecord] = field(default_factory=list)
    file_path: Optional[Path] = None

    def append(self, record: CallRecord) -> None:
        record.index = len(self.calls) + 1
        self.calls.append(record)
        self._write_file()

    # -- file I/O -------------------------------------------------------------

    def _resolve_file_path(self) -> Path:
        if self.file_path is not None:
            return self.file_path
        timestamp = self.started_at.strftime("%Y-%m-%d_%H%M%S")
        slug = _slugify_action(self.action)
        candidate = self.out_dir / f"{timestamp}_{slug}_raw_calls.md"
        suffix = 2
        while candidate.exists():
            candidate = (
                self.out_dir
                / f"{timestamp}_{slug}_raw_calls_{suffix:02d}.md"
            )
            suffix += 1
        self.file_path = candidate
        return self.file_path

    def _write_file(self) -> None:
        path = self._resolve_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(self), encoding="utf-8")


_active_session: contextvars.ContextVar[Optional[CallLogContext]] = (
    contextvars.ContextVar("_active_session", default=None)
)


def active_session() -> Optional[CallLogContext]:
    """Return the currently active :class:`CallLogContext`, if any."""
    return _active_session.get()


@contextmanager
def session(action: str, out_dir: Path) -> Iterator[CallLogContext]:
    """Open a per-action debug session.

    On entry, sets the module-level ``_active_session`` ContextVar so any
    ``wrap_rpc_http``'d clients on the same thread/task record their calls.
    On exit, the previous active session (if any) is restored.
    """
    ctx = CallLogContext(
        action=str(action),
        out_dir=Path(out_dir),
        started_at=datetime.now(),
    )
    token = _active_session.set(ctx)
    try:
        yield ctx
    finally:
        _active_session.reset(token)


def wrap_rpc_http(rpc: Any) -> None:
    """Idempotently wrap ``rpc.post`` / ``rpc.get``.

    When a session is active (``_active_session.get() is not None``), the
    wrapped methods perform the HTTP call themselves with ``requests`` and
    record the call before returning the same value ``BaseRequest`` would have
    returned. When no session is active, the wrapped methods delegate to the
    original implementation, preserving stock ``BaseRequest`` behavior.

    Calling this twice on the same instance is a no-op. The wrapper does not
    alter ``rpc.form_post`` (no Sirna action calls it as of plan 3).
    """
    if rpc is None:
        return
    if getattr(rpc, "_debug_call_log_wrapped", False):
        return

    rpc._orig_post = rpc.post
    rpc._orig_get = rpc.get

    def _wrapped_post(
        url: str,
        params: Any = None,
        files: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        ctx = _active_session.get()
        if ctx is None:
            kwargs = {}
            if params is not None:
                kwargs["params"] = params
            if files is not None:
                kwargs["files"] = files
            if headers is not None:
                kwargs["headers"] = headers
            return rpc._orig_post(url, **kwargs)
        effective_params = params if params is not None else {}
        effective_headers = (
            headers
            if headers is not None
            else {"Content-Type": "application/json"}
        )
        source = _detect_source(rpc)
        request_body = _redact(effective_params)
        record = CallRecord(
            index=0,
            method="POST",
            url=str(url),
            path=_url_path(url),
            source=source,
            transport=_pick_transport(effective_params),
            http_status=None,
            request_body=request_body,
            response_body=None,
            error=None,
        )
        return_value: Any = None
        try:
            response = requests.post(
                url,
                data=json.dumps(effective_params) if effective_params else None,
                headers=effective_headers,
                timeout=_DEFAULT_TIMEOUT_POST,
                files=files,
            )
        except Exception as exc:  # pragma: no cover - delegated to logger
            record.error = f"transport error: {exc}"
            try:
                rpc.get_logger().error(f"Request ERROR: {exc}")
            except Exception:
                pass
            ctx.append(record)
            return None

        record.http_status = response.status_code
        record.response_body, parse_error = _decode_response_body(response)
        try:
            rpc.get_logger().debug(
                f"Request >>> : {response.request.body} "
                f"{response.status_code} {response.text}"
            )
        except Exception:
            pass

        if response.status_code == 200:
            if parse_error is not None:
                record.error = f"json parse error: {parse_error}"
                return_value = None
            else:
                return_value = record.response_body
        else:
            record.error = f"HTTP {response.status_code}: {response.text}"
            try:
                rpc.get_logger().error(
                    f"Request ERROR: ('Request ERROR:', {response.text!r})"
                )
            except Exception:
                pass
            return_value = None

        ctx.append(record)
        return return_value

    def _wrapped_get(
        url: str,
        params: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        ctx = _active_session.get()
        if ctx is None:
            kwargs = {}
            if params is not None:
                kwargs["params"] = params
            if headers is not None:
                kwargs["headers"] = headers
            return rpc._orig_get(url, **kwargs)
        effective_params = params if params is not None else {}
        effective_headers = (
            headers
            if headers is not None
            else {"Content-Type": "application/json"}
        )
        source = _detect_source(rpc)
        request_body = _redact(effective_params)
        record = CallRecord(
            index=0,
            method="GET",
            url=str(url),
            path=_url_path(url),
            source=source,
            transport="params",
            http_status=None,
            request_body=request_body,
            response_body=None,
            error=None,
        )
        return_value: Any = None
        try:
            response = requests.get(
                url,
                params=effective_params,
                headers=effective_headers,
                timeout=_DEFAULT_TIMEOUT_GET,
            )
        except Exception as exc:  # pragma: no cover - delegated to logger
            record.error = f"transport error: {exc}"
            try:
                rpc.get_logger().error(f"Request ERROR: {exc}")
            except Exception:
                pass
            ctx.append(record)
            return None

        record.http_status = response.status_code
        record.response_body, parse_error = _decode_response_body(response)
        try:
            rpc.get_logger().debug(
                f"Request >>> : {effective_params} "
                f"{response.status_code} {response.text}"
            )
        except Exception:
            pass

        if response.status_code == 200:
            if parse_error is not None:
                record.error = f"json parse error: {parse_error}"
                return_value = None
            else:
                return_value = record.response_body

        ctx.append(record)
        return return_value

    rpc.post = _wrapped_post
    rpc.get = _wrapped_get
    rpc._debug_call_log_wrapped = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_URL_PATH_RE = re.compile(r"https?://[^/]+(/.*)?$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify_action(action: str) -> str:
    slug = _SLUG_RE.sub("_", str(action)).strip("_")
    return slug or "action"


def _url_path(url: Any) -> str:
    text = str(url or "")
    match = _URL_PATH_RE.match(text)
    if match and match.group(1):
        return match.group(1)
    if text.startswith("/"):
        return text
    return text


def _pick_transport(params: Any) -> str:
    if isinstance(params, dict) and "data" in params:
        return "data"
    return "params"


def _detect_source(rpc: Any) -> str:
    """Walk the call stack to find the outermost frame whose ``self`` is rpc."""
    try:
        stack = inspect.stack()
    except Exception:
        return ""
    candidate = ""
    try:
        for frame_info in stack:
            frame = frame_info.frame
            if frame.f_locals.get("self", None) is rpc:
                candidate = frame_info.function
        return candidate
    finally:
        del stack


def _redact(params: Any) -> Any:
    """Return a copy of ``params`` with ``apiKey`` redacted."""
    try:
        cloned = copy.deepcopy(params)
    except Exception:
        return params
    _redact_in_place(cloned)
    return cloned


def _redact_in_place(value: Any) -> None:
    if isinstance(value, dict):
        for key in list(value.keys()):
            if isinstance(key, str) and key.lower() == "apikey":
                value[key] = "<redacted>"
            else:
                _redact_in_place(value[key])
    elif isinstance(value, list):
        for item in value:
            _redact_in_place(item)


def _decode_response_body(response: Any) -> tuple[Any, Optional[str]]:
    """Best-effort response decoding used for both record + return value."""
    text = getattr(response, "text", "")
    try:
        return response.json(), None
    except Exception as exc:
        if text:
            return {"raw_text": text}, str(exc)
        return None, str(exc)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(ctx: CallLogContext) -> str:
    title = f"# {ctx.action} Raw Call/Response Log"
    parts: List[str] = [title, ""]
    parts.append("## LIMS Calls")
    parts.append("")
    parts.append("| # | Method | Path | Source | HTTP |")
    parts.append("|---|---|---|---|---|")
    for record in ctx.calls:
        anchor = _row_anchor(record)
        http = (
            f"`{record.http_status}`"
            if record.http_status is not None
            else "`-`"
        )
        parts.append(
            f"| [{record.index}](#{anchor}) | `{record.method}` | "
            f"`{record.path}` | `{record.source}` | {http} |"
        )
    parts.append("")

    for record in ctx.calls:
        parts.append(f"## {record.index} {record.method} {record.path}")
        parts.append("")
        parts.append(f"- Source: `{record.source}`")
        parts.append(f"- Transport: `{record.transport}`")
        if record.http_status is not None:
            parts.append(f"- HTTP status: `{record.http_status}`")
        else:
            parts.append("- HTTP status: `-`")
        if record.error:
            parts.append(f"- Error: {record.error}")
        parts.append("")
        parts.append("### Request Body")
        parts.append("")
        parts.append("```json")
        parts.append(_to_json_block(record.request_body))
        parts.append("```")
        parts.append("")
        parts.append("### Response Body")
        parts.append("")
        parts.append("```json")
        parts.append(_to_json_block(record.response_body))
        parts.append("```")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _row_anchor(record: CallRecord) -> str:
    """Build a GitHub-style anchor matching ``## N METHOD /path``."""
    raw = f"{record.index}-{record.method}-{record.path}"
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-")


def _to_json_block(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False, indent=2)
