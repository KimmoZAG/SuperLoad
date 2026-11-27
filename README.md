# SuperLoad

Ascend 910B2 上的 Agent 前缀 KV Cache 回载基准。根据 Mooncake 公开请求轨迹推导存储回载块，在相同的回载请求上比较原生 FIFO、Bidaw、Strata I/O、Libra 流水和固定分块轮转。

[在线报告](https://kimmozag.github.io/SuperLoad/#comparison) · [研究问题](overview.md) · [方法对照](docs/comparison.md) · [问题验证](docs/prefix-reload.md)

## 运行方法对照

在 Linux 上用 Python 3.10+。设备测量使用 Ascend 驱动和 CANN。

```bash
mkdir -p data
curl -fLo data/toolagent_trace.jsonl \
  https://raw.githubusercontent.com/kvcache-ai/Mooncake/main/FAST25-release/traces/toolagent_trace.jsonl
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m superload prefix-comparison-plan --trace data/toolagent_trace.jsonl --output runs/compare/plan.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io buffered --storage-label longhorn-ext4 --output runs/compare/buffered.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io direct --pair-limit 20 --storage-label longhorn-ext4 --output runs/compare/direct.json
```

默认测量两轮，语料的两种等价布局合计约 15.4 GiB。Direct I/O 需要支持 `O_DIRECT` 的文件系统。问题验证使用 `prefix-plan` 和 `prefix-bench`，命令与条件见 [问题验证方法](docs/prefix-reload.md)。

## 构建与发布

```bash
python3 -m unittest discover -s tests -v
python3 -m pip install -r report/requirements.txt
python3 report/build.py --comparison-results runs/compare
```

公开结果由 GitHub Release 保存。将本地验证完成的 `plan.json`、`buffered.json`、`direct.json` 和环境记录放在 ZIP 根目录。发布后更新 `report/comparison-results-release.json` 中的标签、文件名及 SHA-256。运行 `python3 report/build.py` 检查两个数据附件、HTML 及 SVG 和 PDF 图，再推送 `main` 触发 Pages 部署。

```bash
batch=YYYY-MM-DD
tag="results-comparison-910b2-${batch}"
archive="$PWD/runs/comparison-910b2-${batch}.zip"
(cd runs/compare && python3 -m zipfile -c "$archive" plan.json buffered.json direct.json)
gh release create "$tag" "$archive" --target "$(git rev-parse HEAD)" \
  --title "Agent 前缀回载方法对照 · $batch" --notes "Ascend 910B2 原始配对测量" --latest=false
sha256sum "$archive"
```

[方法对照原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-27) · [问题验证原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-prefix-910b2-2026-11-27)

## 目录

| 目录 | 内容 |
| --- | --- |
| `superload/dataset/` | 前缀轨迹、分层缓存模型、测量计划和 KV 文件布局 |
| `superload/benchmark/` | 配对回放、方法对照、分段计时和 HBM 校验 |
| `superload/schemes/` | 本地 I/O、Disk-HRRN 和 Libra 流水机制 |
| `superload/hardware/` | 对齐的主机缓冲、AscendCL 与 CPU 后端 |
| `report/` | Release 清单、数据校验、网页和矢量图生成 |
| `docs/`、`tests/` | 实验方法和回归测试 |

生成的语料、测量与报告分别位于 `data/`、`runs/` 和 `build/`。
