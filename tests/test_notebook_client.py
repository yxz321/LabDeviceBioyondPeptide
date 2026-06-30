"""Offline tests for reusable notebook helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bioyond_peptide_station import notebook_client as nbc


class _Response:
    def __init__(self, body: Dict[str, Any] | List[Dict[str, Any]]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any] | List[Dict[str, Any]]:
        return self._body


def test_build_core_slate_nodes() -> None:
    assert nbc.text_block("hello") == {"type": "p", "children": [{"text": "hello"}]}

    table = nbc.build_table_node(["A", "B"], [[1, 2]], col_sizes=[120, 180])
    assert table["type"] == "table"
    assert table["colSizes"] == [120, 180]
    assert table["children"][0]["children"][0]["type"] == "th"
    assert table["children"][1]["children"][1]["children"][0]["children"][0]["text"] == "2"

    meta = {
        "url": "https://example.test/img.png",
        "path": "image/img.png",
        "name": "img.png",
        "size": 12,
        "mimeType": "image/png",
    }
    image = nbc.build_image_node(meta, width=320)
    assert image["type"] == "img"
    assert image["width"] == 320
    assert image["uploadStatus"] == "done"

    file_node = nbc.build_file_node({**meta, "name": "report.xlsx"})
    assert file_node["type"] == "file"
    assert file_node["name"] == "report.xlsx"


def test_resolve_lab_record_accepts_inline_and_json_string() -> None:
    client = nbc.NotebookClient(base_url="https://lab.test/api/v1", auth_secret="secret")

    inline = [nbc.text_block("existing")]
    assert client.resolve_lab_record(inline) == inline
    assert client.resolve_lab_record(json.dumps(inline)) == inline
    assert client.resolve_lab_record(json.dumps(json.dumps(inline))) == inline
    assert client.resolve_lab_record("") == []


def test_append_blocks_to_notebook_uploads_record_url_and_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: Dict[str, Any] = {"get": [], "put": [], "patch": []}
    client = nbc.NotebookClient(base_url="https://lab.test/api/v1", auth_secret="secret")

    def fake_get(url: str, **kwargs: Any) -> _Response:
        calls["get"].append((url, kwargs))
        if url.endswith("/lab/notebook/detail"):
            return _Response(
                {
                    "code": 0,
                    "data": {
                        "lab_record_status": "editing",
                        "lab_record": [nbc.text_block("old")],
                    },
                }
            )
        if url.endswith("/lab/storage/token"):
            return _Response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://oss.test/upload",
                        "public_url": "https://oss.test/record.json",
                        "path": "record/uuid/record.json",
                        "content_type": "application/json",
                    },
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    def fake_put(url: str, **kwargs: Any) -> _Response:
        calls["put"].append((url, kwargs))
        return _Response({})

    def fake_patch(url: str, **kwargs: Any) -> _Response:
        calls["patch"].append((url, kwargs))
        return _Response({"code": 0, "data": {}})

    monkeypatch.setattr(nbc.requests, "get", fake_get)
    monkeypatch.setattr(nbc.requests, "put", fake_put)
    monkeypatch.setattr(nbc.requests, "patch", fake_patch)

    result = client.append_blocks_to_notebook("notebook-1", [nbc.text_block("new")])

    assert result == {
        "appended": 1,
        "total": 2,
        "lab_record_url": "https://oss.test/record.json",
    }
    assert calls["get"][0][0] == "https://lab.test/api/v1/lab/notebook/detail"
    assert calls["get"][1][1]["params"]["scene"] == "record"
    assert calls["get"][1][1]["params"]["sub_path"] == "notebook-1"
    assert json.loads(calls["put"][0][1]["data"].decode("utf-8")) == [
        nbc.text_block("old"),
        nbc.text_block("new"),
    ]
    assert calls["patch"][0][0] == "https://lab.test/api/v1/lab/notebook/lab-record"
    assert calls["patch"][0][1]["json"] == {
        "uuid": "notebook-1",
        "lab_record": "https://oss.test/record.json",
        "lab_record_status": "editing",
    }


def test_append_blocks_requires_editing_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = nbc.NotebookClient(base_url="https://lab.test/api/v1", auth_secret="secret")

    def fake_get(url: str, **kwargs: Any) -> _Response:
        return _Response({"code": 0, "data": {"lab_record_status": "submitted"}})

    monkeypatch.setattr(nbc.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="not writable"):
        client.append_blocks_to_notebook("notebook-1", [nbc.text_block("new")])
