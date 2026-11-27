# 仓库结构与清理记录

SuperLoad 的当前实验包括 Agent 前缀回载的队首阻塞验证，同一负载上的五种方法对照，以及 Direct I/O 分块与锁页补测。

## 当前文件

| 任务 | 文件 |
| --- | --- |
| 理解研究问题与复现 | [overview.md](../overview.md)、[README.md](../README.md)、[方法对照](comparison.md)、[问题验证](prefix-reload.md) |
| 生成回放计划 | [superload/cli.py](../superload/cli.py)、[superload/dataset/prefix.py](../superload/dataset/prefix.py) |
| 生成和读取 KV 语料 | [superload/dataset/geometry.py](../superload/dataset/geometry.py)、[superload/dataset/corpus.py](../superload/dataset/corpus.py)、[superload/schemes/local.py](../superload/schemes/local.py) |
| 测量与 HBM 校验 | [superload/benchmark/prefix.py](../superload/benchmark/prefix.py)、[superload/benchmark/comparison.py](../superload/benchmark/comparison.py)、[superload/hardware/device.py](../superload/hardware/device.py) |
| 运行论文机制对照 | [superload/schemes/bidaw.py](../superload/schemes/bidaw.py)、[superload/schemes/libra.py](../superload/schemes/libra.py)。Strata I/O 布局在 `dataset/corpus.py`，固定分块轮转在 `benchmark/comparison.py`。 |
| Direct I/O 分块与锁页补测 | [实验说明与复现](direct-io-ablation.md)、[汇总代码](../report/direct_ablation.py) |
| 选择发布批次 | [report/prefix-results-release.json](../report/prefix-results-release.json)、[report/comparison-results-release.json](../report/comparison-results-release.json)、[report/direct-ablation-results-release.json](../report/direct-ablation-results-release.json) |
| 生成图表和网页 | [report/prefix.py](../report/prefix.py)、[report/comparison.py](../report/comparison.py)、[report/overview.py](../report/overview.py)、[report/build.py](../report/build.py)、[report/static/index.html](../report/static/index.html)、[report/static/style.css](../report/static/style.css) |
| 测试与发布 | [tests/](../tests/)、[.github/workflows/pages.yml](../.github/workflows/pages.yml) |

## 已退役的文件

| 路径 | 清理范围 |
| --- | --- |
| `docs/` | `experiment.md`、`mixed-kv.md`、`roce.md` |
| `native/` | `CMakeLists.txt`、`roce_bridge.cpp` |
| `scripts/` | `run_matrix.py` 与整个 `mixed_kv/`，包括 `mixed_load.py`、`joint_schedule.py`、`strata_yield.py` 和配套汇总脚本 |
| `superload/benchmark/` | `metrics.py`、`replay.py`、`runner.py` |
| `superload/dataset/`、`superload/hardware/`、`superload/schemes/` | `workloads.py`、`verbs.py`、`roce.py`。当前 KV 几何尺寸定义在 `dataset/geometry.py`。 |
| `report/` | `mixed.py`、`mixed-results.csv`、`results-release.json`、`static/report.js`，以及 `results/joint-four-baselines/` 的 Markdown、CSV 和压缩包 |
| `tests/` | 旧接口测试 `test_bidaw.py`、`test_dataset.py`、`test_joint_schedule.py`、`test_replay.py` |
| Pages 生成目录 | `assets/report.js`、`assets/summary.json`、`assets/mixed-results.csv`，以及旧的单类负载、混跑、消融和 RoCE 图 |

可用 `git diff --name-status --diff-filter=D 1f44841 a909999` 核对删除的 34 个跟踪文件。2026-11-26 的旧数据附件继续保存在独立的历史 Release。[历史 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-ascend-910b2-2026-11-26)

## 生成目录与发布

`data/` 存放轨迹和 KV 语料，`runs/` 存放回放计划、原始测量及环境记录，`build/` 存放本地结果缓存和生成的网页。发布流程见 [README](../README.md#构建与发布)。Pages 由当前三份 Release 清单生成，通过独立入口提供 SVG、PDF、汇总 JSON 和原始 ZIP。
