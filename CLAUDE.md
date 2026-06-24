# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **external device package** for [Uni-Lab-OS](https://github.com/uni-lab) — the `bioyond_peptide_station` driver for the Bioyond peptide-synthesis workstation. It is *not* a standalone app: it is loaded into a running `unilabos` install via `--devices`, and `unilabos` itself is a runtime-only dependency (the `dev` extra) that requires a full ROS2/conda environment. The package was migrated out of the unilabos monorepo to live independently.

The README is in Chinese and authoritative for setup; this file captures the non-obvious wiring.

## Commands

All commands assume the `unilab` conda env (Python 3.11, ROS2) is active: `mamba activate unilab`.

```bash
pip install -e .                       # install this package into the unilab env

# Registry validation — same command CI runs (.github/workflows/check_registry.yml).
# Validates @device/@action/@resource metadata via AST scan; no network/LIMS needed.
unilab --check_mode --devices ./bioyond_peptide_station --external_devices_only

pytest tests/                          # offline unit/contract tests
pytest tests/test_peptide_station_contracts.py::test_filter_step_parameters_preserves_zero_and_skips_unknown  # single test

# Launch with an example graph (needs reachable LIMS + credentials)
unilab --devices ./bioyond_peptide_station --external_devices_only \
       --ak xxx --sk xxx --upload_registry --addr test --disable_browser \
       -g ./examples/peptide_station_graph.with_bioyond_devices.json
```

### Test layering (important)
- `tests/` — CI-safe. Offline contract/unit tests. Many gate runtime-dependent assertions behind `pytest.importorskip("rclpy")` / skip when `unilabos`/ROS is missing, so they still run (AST-only) without a full env. The package is designed so registry decorators **fall back to stubs** when `unilabos.registry.decorators` can't be imported (see top of `peptide_station.py`), which is what lets contract tests inspect `_action_registry_meta` offline.
- `scripts/` — **not** pytest; live LIMS diagnostics and monorepo-fixture-dependent checks. `test_peptide_station_live.py` is a CLI printing raw action returns for manual inspection (LIMS status codes documented in `tests/README.md`).

## Architecture

Three layers, all driven by unilabos registry decorators (`@device`, `@action`, `@resource`, `NodeType.MANUAL_CONFIRM`):

1. **`peptide_station.py` — `BioyondPeptideStation(BioyondWorkstation)`** (~3300 lines). The workstation device class. Implements the Day1–Day4 peptide workflows (synthesis/quantitation/cyclization/acylation), LIMS submission, order/report polling, material sync, reset scheduling, and human-in-the-loop steps. Several `@action`s use `node_type=NodeType.MANUAL_CONFIRM` (operator confirmation checkpoints, e.g. `unload_materials`, `wait_for_order_finish`). Config is merged from `config_path`/`config`/`bioyond_config`/`kwargs`; required keys: `api_key`, `api_host`, `warehouse_mapping`.

2. **`bioyond_device_proxy.py`** — 18 thin `@device` proxy nodes (robot, LCMS, Tecan/G3/IDOT liquid handlers, CEM synthesizer, plate sealer, centrifuge, plate reader…). Each `@action` just calls `self._execute_operation(<Chinese op name>, params)` against the LIMS `device-list` operation snapshot. These represent physical sub-devices; they share `BioyondDeviceProxyBase` which loads the same station config JSON.

3. **`resources/`** — `@resource` labware factories. `peptide_materials.py` defines 49 PLR labware subclasses + `DEFAULT_PEPTIDE_MATERIAL_TYPE_MAPPINGS` (imported by the station). `decks.py` defines `BIOYOND_PeptideStation_Deck`; `warehouses.py` defines `BioyondWareHouse`.
   - **Gotcha:** `resources/__init__.py` *eagerly imports* the Deck and WareHouse classes. unilabos resolves PLR subclasses by walking already-imported `__subclasses__()`; AST scanning registers entries but never imports the module. Without the eager import, startup fails with `Deck 配置不能为空`. Don't remove those imports.

### `_vendored/` — forked Bioyond kernel
`station.py` (`BioyondWorkstation` base + `BioyondResourceSynchronizer`), `bioyond_rpc.py` (`BioyondV1RPC` HTTP client), `debug_call_log.py`, and `graphio_bioyond.py` (the two Bioyond resource-conversion functions extracted from unilabos `graphio`). These were modified by the peptide work but the published/`dev` `unilabos` still ships the old versions, so they travel with the package. **Everything else** (logger, `WorkstationBase`, registry decorators, etc.) is imported from the installed `unilabos`. When a vendored module diverges further from upstream, it stays here; do not assume `unilabos` has the same code.
   - These vendored files contain `Path(__file__).resolve().parents[4|5]` repo-root hacks left over from the monorepo layout — fragile, but tolerated because debug-log/path logic falls back gracefully.

## Config & secrets

- Runtime config schema: see `examples/peptide_station_config.example.json` — `api_host`, `api_key`, `warehouse_mapping` (name→`{uuid, site_uuids}`), `material_type_mappings` (labware id → `[显示名, type_uuid]`), `http_service_config` (where LIMS pushes callbacks), optional `debug_log`/`debug_log_dir`.
- `.gitignore` blocks `examples/*.json` **except** `*.example.json`, and explicitly ignores `peptide_station_graph.with_bioyond_devices.json`. Never commit live configs/graphs (they hold real hosts/keys/UUIDs). `unilabos_data/` (runtime artifacts, logs) is also gitignored.
