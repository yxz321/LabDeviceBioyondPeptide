"""Live LIMS print checks for BioyondPeptideStation.

This file is a small CLI for manual diagnostics. It prints raw action returns
so the operator can judge the live LIMS response directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bioyond_peptide_station.peptide_station import (  # noqa: E402
    BioyondPeptideStation,
    _apply_default_peptide_material_type_mappings,
    load_peptide_config,
)


DEFAULT_CONFIG_PATH = REPO_ROOT / "temp_benyao/peptide/peptide_station_config.example.json"


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_lims_station(config: dict[str, Any]) -> BioyondPeptideStation:
    from bioyond_peptide_station._vendored.bioyond_rpc import BioyondV1RPC, SimpleLogger

    resolved_config = dict(config)
    _apply_default_peptide_material_type_mappings(resolved_config)
    missing = [key for key in BioyondPeptideStation._REQUIRED_CONFIG_KEYS if not resolved_config.get(key)]
    if missing:
        raise ValueError(f"BioyondPeptideStation 缺少必要配置: {', '.join(missing)}")

    station = object.__new__(BioyondPeptideStation)
    station.bioyond_config = resolved_config

    rpc = object.__new__(BioyondV1RPC)
    rpc.config = resolved_config
    rpc.api_key = resolved_config["api_key"]
    rpc.host = str(resolved_config["api_host"]).rstrip("/")
    rpc.location_mapping = {}
    warehouse_mapping = resolved_config.get("warehouse_mapping", {})
    for warehouse_config in warehouse_mapping.values():
        if isinstance(warehouse_config, dict) and "site_uuids" in warehouse_config:
            rpc.location_mapping.update(warehouse_config["site_uuids"])
    rpc._logger = SimpleLogger()
    rpc.material_cache = {}
    station.hardware_interface = rpc
    return station


def print_order_list(station: Any, status: str, page_count: int, sorting: str) -> None:
    # Status vocabularies:
    # - order-list status: "80" success, "90" failure, "60" running, "100" taken out.
    # - /report/sample_finish Status: "0" pending, "2" injection, "10" started,
    #   "20" completed, "-2" abnormal stop, "-3" manual stop.
    # - /report/order_finish status: "30" completed, "-11" abnormal stop,
    #   "-12" manual stop.
    result = station.get_order_list(status=status, page_count=page_count, sorting=sorting)
    _print_json(result)


def print_order_report_files(station: Any, order_id: str, status: str, sorting: str) -> None:
    if not order_id:
        # The status here belongs to order-list. It is not the /report/order_finish
        # status vocabulary, where "30" means completed.
        listing = station.get_order_list(status=status, page_count=1, sorting=sorting)
        _print_json({"selected_order_source": listing})
        items = listing.get("items") or []
        if not items:
            print(f"No order-list item found for status={status}; pass --order-id to test report files.")
            return
        order_id = str(items[0].get("id") or "")
        if not order_id:
            print("Selected order-list item has no id; pass --order-id to test report files.")
            return

    result = station.get_order_report_files(order_id)
    _print_json(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Peptide Station live LIMS print checks")
    parser.add_argument(
        "config_path",
        nargs="?",
        default=str(DEFAULT_CONFIG_PATH),
        help="JSON 配置文件路径",
    )
    parser.add_argument(
        "--case",
        choices=("order-list", "order-report-files", "all"),
        default="order-list",
        help="要运行的 live check",
    )
    parser.add_argument("--status", default="80", help="order-list status，例如 80/90/60/100")
    parser.add_argument("--order-id", default="", help="指定订单 GUID；为空时从 order-list 选择第一条")
    parser.add_argument("--page-count", type=int, default=5, help="order-list pageCount")
    parser.add_argument("--sorting", default="creationTime desc", help="order-list sorting")
    args = parser.parse_args()

    station = build_lims_station(load_peptide_config(args.config_path))

    if args.case in {"order-list", "all"}:
        print_order_list(station, status=args.status, page_count=args.page_count, sorting=args.sorting)
    if args.case in {"order-report-files", "all"}:
        print_order_report_files(station, order_id=args.order_id.strip(), status=args.status, sorting=args.sorting)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
