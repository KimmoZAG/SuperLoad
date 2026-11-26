# SuperLoad

Ascend 910B2 上的 KV Cache 存储加载基准。两种确定性负载分别模拟多轮对话的稠密前缀回载，以及长上下文解码的稀疏页面回载。基准在 NPU HBM 中校验字节，记录从请求到 KV 就绪的延迟、逻辑吞吐、文件读放大和设备侧传输字节。

[在线实验报告](https://kimmozag.github.io/SuperLoad/) · [实验方法与论文复现范围](docs/experiment.md) · [HIXL 双机说明](docs/hixl.md) · [原始测量](results/ascend-910b2/)

## 快速运行

在 Linux 下用 Python 3.10+。真实 HBM 测量需要 Ascend 驱动和 CANN。

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m superload build-corpus data/kv
python3 -m superload trace --profile sparse-decode --seed 42 --waves 52 --output runs/sparse-trace.json
python3 -m superload bench --corpus data/kv --profile interactive-prefix --scheme native \
  --device 0 --io buffered --warmup 4 --waves 48 --repeat 3 --verify last \
  --storage-label longhorn-ext4 --output runs/native.json
```

语料三种布局合计占用 1.125 GiB。默认是 BF16 等长字节、4 层、8 个 KV heads、128 head dim 和 128 tokens/页。可用 `--layers 32` 生成 32 层版本。每种布局使用相同种子和逻辑 KV 数据。直接 I/O 可用 `--io direct`，要求文件系统支持 `O_DIRECT`，缓冲与 I/O 范围满足对齐约束。

| 方案 | CLI | 实现 |
| --- | --- | --- |
| 原生加载 | `native` | layer-major 文件 → 主机 DRAM → 同步 AscendCL H2D |
| 昇腾 HIXL | `hixl` | 存储侧读文件到注册内存，客户端 HIXL READ 到已注册的 HBM |
| Strata | `strata` | page-first 读合并和同批前缀共享 |
| Bidaw | `bidaw` | DRAM/文件感知排序和基于回复长度的淘汰 |
| SolidAttention | `solid` | 分层 token-wise K/V 交织和稀疏选择历史预取 |

三篇论文均发表于 2026 年 CCF A 会议。这里复现的是其数据布局、调度和预取机制。完整模型、注意力内核及原论文 GPU 服务端结果需要原论文环境。HIXL 在单设备上已完成注册与建链探针，双机性能待测。

## 复跑实验矩阵

```bash
for f in data/kv/*.bin; do cat "$f" >/dev/null; done
python3 scripts/run_matrix.py --corpus data/kv --io buffered \
  --run-label buffered-primary --output runs/buffered
python3 scripts/run_matrix.py --corpus data/kv --io direct --waves 8 --warmup 1 --repeat 2 \
  --run-label direct-sensitivity --output runs/direct
python3 -m unittest discover -s tests -v
```

`--verify all` 会对每个批次做设备回读。正式计时用 `last`，每轮在计时结束后核查最后一批。完整原始 JSON 记录每个样本和批次计数。消融项 `strata-io`、`native-lru`、`solid-io` 在相应负载下与主方法配对。

## 目录

```text
superload/                负载生成、语料布局、回放与方案
native/                   CANN HIXL C++ 适配层
scripts/                  实验矩阵与 HIXL 单设备探针
tests/                    语料一致性和数据路径回归测试
docs/                     论文机制映射、指标与 HIXL 双机步骤
results/ascend-910b2/      实测原始数据，按 I/O 模式归档
report/                   报告模板、样式和静态构建脚本
.github/workflows/        Python 测试及 GitHub Pages 部署
```

运行 `python3 report/build.py` 生成 `build/site/index.html`。GitHub Actions 检查数据和测试，并在 `main` 推送时部署 Pages。
