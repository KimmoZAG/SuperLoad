# SuperLoad

Ascend 910B2 上的 Agent 前缀 KV Cache 回载基准。用 Mooncake 公开的工具调用与 Agent 请求轨迹确定前缀复用和到达窗口，在分层容量模型中识别从存储回载的请求。用同窗的大小回载做配对，在 910B2 上测量文件读取、H2D 和排队延迟。

[在线实验报告](https://kimmozag.github.io/SuperLoad/#prefix-contention) · [负载与测量方法](docs/prefix-reload.md) · [原始测量 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-prefix-910b2-2026-11-27)

## 快速运行

在 Linux 下用 Python 3.10+。真实 HBM 测量需要 Ascend 驱动和 CANN。

```bash
mkdir -p data runs/prefix
curl -fLo data/toolagent_trace.jsonl \
  https://raw.githubusercontent.com/kvcache-ai/Mooncake/main/FAST25-release/traces/toolagent_trace.jsonl
python3 -m superload prefix-plan --trace data/toolagent_trace.jsonl --output runs/prefix/plan.json
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m superload prefix-bench --plan runs/prefix/plan.json --corpus data/prefix \
  --device 0 --io buffered --repeat 3 --storage-label longhorn-ext4 --output runs/prefix/buffered.json
python3 -m superload prefix-bench --plan runs/prefix/plan.json --corpus data/prefix \
  --device 0 --io direct --repeat 2 --pair-limit 16 --storage-label longhorn-ext4 --output runs/prefix/direct.json
```

默认计划为 64 对请求，固定布局语料约占 17 GiB。Direct I/O 要求文件系统支持 `O_DIRECT`，并从同一配对计划抽取 16 对做敏感性测量。

## 早期机制实验

合成前缀和稀疏解码用于观察独立机制。五种方案、早期结果和原始测量见 [机制实验](docs/experiment.md)、[RoCE 运行说明](docs/roce.md) 及 [早期 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-ascend-910b2-2026-11-26)。

早期的合成混跑、联合切分和逐种子对照见 [混合回放实验](docs/mixed-kv.md) 与 [联合切分与四方案对照](report/results/joint-four-baselines/README.md)。

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

## 发布实验结果

1. 用 `python3 report/build.py --prefix-results runs/prefix` 检查本地结果。归档根目录直接包含 `plan.json`、`direct.json`、`buffered.json`，可附环境记录。
2. 确认实验代码提交已推送到 GitHub，用 `gh auth login` 登录并为数据批次指定标签。

   ```bash
   batch=YYYY-MM-DD
   tag="results-prefix-910b2-${batch}"
   archive="$PWD/runs/prefix-910b2-${batch}.zip"
   (cd runs/prefix && python3 -m zipfile -c "$archive" plan.json direct.json buffered.json)
   gh release create "$tag" "$archive" --target "$(git rev-parse HEAD)" \
     --title "Ascend 910B2 实验数据 · $batch" --notes "原始测量与运行参数" --latest=false
   sha256sum "$archive"
   ```

3. 发布后更新 `report/prefix-results-release.json` 的标签、文件名和 SHA-256。运行 `python3 report/build.py` 验证附件，再提交并推送清单。GitHub Actions 会重新部署 Pages。

## 目录

```text
superload/
  cli.py                  命令行参数和执行入口
  dataset/                轨迹回放、机制微基准、KV 语料及文件布局
  benchmark/              回放、计时、统计和 HBM 字节校验
  schemes/                本地读取、Bidaw 缓存和 RoCE 服务与客户端
  hardware/               主机缓冲、AscendCL 与 libibverbs 绑定
native/                   RoCE C++ 实现和 CMake 构建入口
scripts/                  实验矩阵、双进程编排及早期合成混跑
tests/                    负载、缓存策略和回放测试
docs/                     论文机制映射、指标与 RoCE 运行步骤
report/
  build.py / figures.py    读取结果并生成网页和矢量图
  prefix.py               Agent 前缀回载报告与矢量图
  mixed.py / mixed-results.csv  早期混跑的表格与分轮摘要
  *results-release.json   数据发布版本及 SHA-256
  static/                 网页模板、样式和交互
.github/workflows/        Python 测试及 GitHub Pages 部署
```

原生加载、Strata 和 SolidAttention 共用 `schemes/local.py` 中的文件读取。Strata 的批内共享在 `benchmark/replay.py`，SolidAttention 的 K/V 交织布局在 `dataset/corpus.py`，后台预取在 `schemes/local.py`。生成的语料、构建产物和新跑的测量分别放在 `data/`、`build/` 和 `runs/`。

使用 Python 3.12 安装 `python3 -m pip install -r report/requirements.txt`，再运行 `python3 report/build.py` 生成网页、SVG 和 PDF。构建会下载并校验两个 Release 附件，验证过的归档缓存于 `build/results/`。GitHub Actions 在 `main` 推送时检查测试、数据和报告，再部署 Pages。
