# 仓库结构

| 用途 | 位置 |
| --- | --- |
| 研究叙事和实验协议 | [overview.md](../overview.md)、[六种方法对照](comparison.md)、[单通道问题验证](prefix-reload.md) |
| 轨迹回放和 KV 语料 | [superload/dataset/prefix.py](../superload/dataset/prefix.py)、[superload/dataset/corpus.py](../superload/dataset/corpus.py) |
| 运行基准 | [superload/cli.py](../superload/cli.py)、[superload/benchmark/](../superload/benchmark/) |
| 正式复跑、HBM 校验与审计 | [experiments/](../experiments/)、[复现与审计说明](reproduction.md) |
| 回载实现 | [SuperLoad 流水](../superload/schemes/superload.py)、[Bidaw 排序](../superload/schemes/bidaw.py)、[Libra 流水](../superload/schemes/libra.py)、[文件读与 H2D](../superload/schemes/local.py) |
| AscendCL 与主机缓冲 | [superload/hardware/device.py](../superload/hardware/device.py) |
| 校验、汇总与出图 | [report/comparison_data.py](../report/comparison_data.py)、[report/comparison.py](../report/comparison.py)、[report/build.py](../report/build.py)、[report/static/](../report/static/) |
| 发布数据与 Pages | [问题验证清单](../report/prefix-results-release.json)、[方法对照清单](../report/comparison-results-release.json)、[Pages 工作流](../.github/workflows/pages.yml) |
| 测试 | [tests/](../tests/) |

`data/` 存放轨迹和 KV 语料，`runs/` 存放回放计划、日志与原始测量，`build/` 存放本地 Release 缓存和生成的网页。三类目录均由任务生成，以 GitHub Release 作为公开原始数据入口。 [构建与发布](../README.md#构建与发布)
