# Peptide Station Tests

This directory contains offline contract tests plus manual live LIMS print checks for
`BioyondPeptideStation`.

## Offline Contract Tests

These tests do not require network access or a real LIMS host:

```bash
conda activate unilab
python -m pytest \
  unilabos/devices/workstation/bioyond_studio/peptide_station/tests/test_peptide_station_contracts.py
```

## Live LIMS Print Checks

All live LIMS print checks are grouped in:

```text
test_peptide_station_live.py
```

Run them directly with Python. They are not pytest tests; they print live action
returns for manual inspection.

```bash
conda activate unilab
python \
  unilabos/devices/workstation/bioyond_studio/peptide_station/tests/test_peptide_station_live.py \
  --case order-list --status 80
```

By default, the live tests use:

```text
temp_benyao/peptide/peptide_station_config.example.json
```

Override the config file:

```bash
conda activate unilab
python \
  unilabos/devices/workstation/bioyond_studio/peptide_station/tests/test_peptide_station_live.py \
  temp_benyao/peptide/peptide_station_config.http.json \
  --case order-list --status 80
```

Print report files for a specific order id:

```bash
conda activate unilab
python \
  unilabos/devices/workstation/bioyond_studio/peptide_station/tests/test_peptide_station_live.py \
  --case order-report-files \
  --order-id 3a217fd0-6b70-e721-1ea3-4a68b833b785
```

Print report files for the first order found by status:

```bash
conda activate unilab
python \
  unilabos/devices/workstation/bioyond_studio/peptide_station/tests/test_peptide_station_live.py \
  --case order-report-files --status 80
```

Status note:

- `order-list` status: `"80"` success, `"90"` failure, `"60"` running, `"100"` taken out.
- `/report/sample_finish` `Status`: `"0"` pending, `"2"` injection, `"10"` started, `"20"` completed, `"-2"` abnormal stop, `"-3"` manual stop.
- `/report/order_finish` `status`: `"30"` completed, `"-11"` abnormal stop, `"-12"` manual stop.

These checks depend on reachable LIMS services and current server data, so they
print raw action returns for manual inspection instead of asserting business
correctness.
