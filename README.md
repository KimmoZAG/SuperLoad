# SuperLoad

Ascend 910B2 上的 KV Cache 存储加载基准。两种确定性负载分别模拟多轮对话的稠密前缀回载，以及长上下文解码的稀疏页面回载。基准在 NPU HBM 中校验字节，记录从请求到 KV 就绪的延迟、逻辑吞吐、文件读放大和设备侧传输字节。

[在线实验报告](https://kimmozag.github.io/SuperLoad/) · [实验方法与论文复现范围](docs/experiment.md) · [RoCE 运行说明](docs/roce.md) · [原始测量](results/ascend-910b2/)

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
| RoCE RC 中转 | `roce` | 存储进程 → RC SEND/RECV → 接收端 DRAM → AscendCL H2D |
| Strata | `strata` | page-first 读合并和同批前缀共享 |
| Bidaw | `bidaw` | DRAM/文件感知排序和基于回复长度的淘汰 |
| SolidAttention | `solid` | 分层 token-wise K/V 交织和稀疏选择历史预取 |

论文方案复现了数据布局、调度和预取机制。完整模型、注意力内核及原论文 GPU 服务端结果需要原论文环境。RoCE 使用同节点双进程和同一端口，跨机条件下的网络性能待测。

## 复跑实验矩阵

```bash
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native -j 2
for f in data/kv/*.bin; do cat "$f" >/dev/null; done
python3 scripts/run_matrix.py --corpus data/kv --roce --io buffered --waves 32 \
  --run-label buffered-primary-roce --output runs/buffered
python3 scripts/run_matrix.py --corpus data/kv --roce --io direct --waves 8 --warmup 1 --repeat 2 \
  --run-label direct-sensitivity-roce --output runs/direct
python3 -m unittest discover -s tests -v
```

`--verify all` 会对每个批次做设备回读。正式计时用 `last`，每轮在计时结束后核查最后一批。完整原始 JSON 记录每个样本和批次计数。消融项 `strata-io`、`native-lru`、`solid-io` 在相应负载下与主方法配对。

## 目录

```text
superload/
  cli.py                  命令行参数和执行入口
  dataset/                两种负载、KV 语料及文件布局
  benchmark/              回放、计时、统计和 HBM 字节校验
  schemes/                本地读取、Bidaw 缓存和 RoCE 服务与客户端
  hardware/               主机缓冲、AscendCL 与 libibverbs 绑定
native/                   RoCE C++ 实现和 CMake 构建入口
scripts/                  实验矩阵与双进程编排
tests/                    负载、缓存策略和回放测试
docs/                     论文机制映射、指标与 RoCE 运行步骤
results/ascend-910b2/      实测原始数据，按 I/O 模式归档
report/
  build.py / figures.py    读取结果并生成网页和矢量图
  static/                 网页模板、样式和交互
.github/workflows/        Python 测试及 GitHub Pages 部署
```

原生加载、Strata 和 SolidAttention 共用 `schemes/local.py` 中的文件读取。Strata 的批内共享在 `benchmark/replay.py`，SolidAttention 的 K/V 交织布局在 `dataset/corpus.py`，后台预取在 `schemes/local.py`。生成的语料、构建产物和新跑的测量分别放在 `data/`、`build/` 和 `runs/`。

使用 Python 3.12 安装 `python3 -m pip install -r report/requirements.txt`，再运行 `python3 report/build.py` 生成网页、SVG 和 PDF。GitHub Actions 检查数据和测试，并在 `main` 推送时部署 Pages。
