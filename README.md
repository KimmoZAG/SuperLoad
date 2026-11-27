# SuperLoad

SuperLoad 是一项面向昇腾 NPU 的 Agent 前缀 KV Cache 回载调度工作。高回载量请求占用存储到 HBM 的通道时，随后到达的低回载量请求可能等到通道释放。SuperLoad 将回载切成最多 16 MiB 的块，在块边界轮转请求，让后到的低回载量请求更早获得通道。

[研究问题](overview.md) · [实验方法](docs/comparison.md) · [仓库结构](docs/repository.md)

## 实测结果

在单台昇腾 910B2 上，使用 Mooncake Tool & Agent 公开轨迹生成配对回载。回载量按单次请求实际需要从存储读取的 KV 字节数计，低回载量组为 **8–32 MiB**，高回载量组为 **≥128 MiB**。下图展示高回载量请求早到 25 ms 时，两组的 P95 与双请求吞吐。延迟从请求到达计到所需 KV 全部进入 HBM。

![高回载量请求早到 25 ms 时五种方法在 Buffered 和 Direct I/O 下的 P95 与吞吐](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.svg)

Direct I/O 下，相比原生 FIFO，SuperLoad 将低回载量组的 P95 从 **4,014 ms** 降到 **549 ms**。高回载量组的 P95 从 3,916 ms 升到 4,775 ms，双请求吞吐从 0.124 GiB/s 降到 0.112 GiB/s。

[完整图表](https://kimmozag.github.io/SuperLoad/#comparison) · [原始测量](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-27)

## 运行方法对照

运行环境为 Linux、Python 3.10+ 和昇腾 910B2，设备测量需要 Ascend 驱动与 CANN。

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

结果写入 `runs/compare/`。每种模式默认测量两轮，首次生成的两种 KV 文件布局合计约 15.4 GiB。Direct I/O 需要支持 `O_DIRECT` 的文件系统。

[问题验证的复现说明](docs/prefix-reload.md)

<a name="构建与发布"></a>
<details>
<summary>构建与发布报告</summary>

```bash
python3 -m pip install -r report/requirements.txt
python3 -m unittest discover -s tests -v
python3 report/build.py --comparison-results runs/compare
```

默认构建使用两份固定的 GitHub Release 附件，并校验 SHA-256。网页首屏海报的实测指标来自方法对照，展开区从 [overview.md](overview.md) 同步已填写的章节。输出位于 `build/site/`。用 `--comparison-results` 或 `--prefix-results` 可预览本地复测。反复构建同一输出目录时，构建会清理已退役的旧图和静态文件。

发布方法对照时，将 `plan.json`、`buffered.json` 和 `direct.json` 放在 ZIP 根目录，可附环境记录。发布后更新 `report/comparison-results-release.json` 的标签、文件名和 SHA-256。重新发布问题验证时，更新 `report/prefix-results-release.json`。运行 `python3 report/build.py` 检查固定附件与网页，再推送 `main` 触发 Pages 部署。

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

</details>
