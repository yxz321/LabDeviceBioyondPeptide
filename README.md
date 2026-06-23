# LabDeviceBioyondPeptide

Uni-Lab-OS 外部设备包：**Bioyond 多肽合成工作站** (`bioyond_peptide_station`)。

从 Uni-Lab-OS 主仓库迁移而来，作为独立的外部设备包通过 `--devices` 加载，运行时依赖已安装的 `unilabos`。

## 包含内容

- `BioyondPeptideStation` 多肽工作站设备类（Day1–Day4 合成/定量/环化/酰化工作流、LIMS 提交、复位调度、订单/报告、错误处理、下料等）。
- 18 个 Bioyond 设备代理（机械臂、LCMS、Tecan/G3/IDOT 移液、CEM 合成仪、离心机、热封膜机、酶标仪等）。
- 多肽专用耗材资源（`peptide_materials.py`，49 个 `@resource` labware）与 `BIOYOND_PeptideStation_Deck`。

## 目录结构

```
bioyond_peptide_station/
├── __init__.py                  # 导出 BioyondPeptideStation / fetch_workflow_list / load_peptide_config
├── peptide_station.py           # 多肽工作站设备类
├── bioyond_device_proxy.py      # Bioyond 设备代理
├── _vendored/                   # 从 unilabos fork 的 Bioyond 共享内核（被多肽工作改过，故随包携带）
│   ├── station.py               # BioyondWorkstation 基类
│   ├── bioyond_rpc.py           # BioyondV1RPC HTTP 客户端
│   ├── debug_call_log.py
│   └── graphio_bioyond.py       # 仅 resource_bioyond_to_plr / resource_plr_to_bioyond（从 graphio 抽出）
└── resources/                   # 多肽耗材、deck、warehouse 工厂
examples/                        # 脱敏示例图与配置（无密钥/实盘数据）
tests/                           # 自包含单元/契约测试（CI 可跑）
scripts/                         # 实盘/依赖 monorepo 夹具的诊断脚本（非 CI）
```

> **为什么有 `_vendored/`**：多肽工作改动了 `BioyondWorkstation`、`bioyond_rpc`、`debug_call_log` 以及 `graphio` 的两个 Bioyond 函数，而已发布/`dev` 的 `unilabos` 仍是旧版本。为保证本包独立可运行，这些被改动的共享模块随包携带，其余未改动的 `unilabos` 内核仍从安装环境导入。

## 快速开始

```bash
# 1. 准备 unilabos 环境（需 ROS2 完整环境）
mamba create -n unilab python=3.11.14 -c conda-forge -y
mamba activate unilab
mamba install uni-lab::unilabos -c uni-lab -c robostack-staging -c conda-forge -y

# 2. 安装本包
pip install -e .

# 3. 验证注册表（CI 同款命令）
unilab --check_mode --devices ./bioyond_peptide_station --external_devices_only

# 4. 运行测试
pytest tests/

# 5. 带示例图启动
unilab --devices ../LabDeviceBioyondPeptide/bioyond_peptide_station --external_devices_only \
       --ak ba43b4b9-53f3-4400-9fde-70c4a76bfe1f --sk ebbfc986-8f5f-4d97-9779-07e0b5f50c02 --upload_registry --addr test --disable_browser \
       -g ../LabDeviceBioyondPeptide/examples/peptide_station_graph.with_bioyond_devices.json
unilab -g test/experiments/comprehensive_protocol/comprehensive_station.json --ak ba43b4b9-53f3-4400-9fde-70c4a76bfe1f --sk ebbfc986-8f5f-4d97-9779-07e0b5f50c02 --upload_registry --addr test --disable_browser
```

> 依赖自动安装：unilabos 启动时检测 `--devices` 目录下的 `requirements.txt`，缺失包通过 `uv`（优先）或 `pip` 自动安装。

## License

设备驱动代码遵循 DP Technology Proprietary License，请勿外传。
