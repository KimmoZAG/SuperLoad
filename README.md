# SuperLoad

SuperLoad 是一项面向昇腾 NPU 的 Agent 前缀 KV Cache 回载调度工作。高回载量请求占用存储到 HBM 的通道时，随后到达的低回载量请求可能等到通道释放。SuperLoad 将回载切成最多 16 MiB 的块，在块边界轮转请求，让后到的低回载量请求更早获得通道。

[研究问题](overview.md) · [实验方法](docs/comparison.md) · [仓库结构](docs/repository.md)

## 实测结果

在单台昇腾 910B2 上，使用 Mooncake Tool & Agent 公开轨迹生成配对回载。回载量按单次请求实际需要从存储读取的 KV 字节数计，低回载量组为 **8–32 MiB**，高回载量组为 **≥128 MiB**。下图展示高回载量请求早到 25 ms 时，两组的 HBM 就绪 P95 与双请求吞吐。Buffered I/O 同批比较普通缓冲和 CANN 锁页缓冲。

![高回载量请求早到 25 ms 时五种方法在 Buffered I/O 普通缓冲、Direct I/O 和 Buffered I/O 锁页缓冲下的对照](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.svg)

Direct I/O 下，相比原生 FIFO，SuperLoad 将低回载量组的 P95 从 **4,014 ms** 降到 **549 ms**。高回载量组的 P95 从 3,916 ms 升到 4,775 ms，双请求吞吐从 0.124 GiB/s 降到 0.112 GiB/s。

Buffered I/O 锁页缓冲下，高回载量请求早到 25 ms 时，SuperLoad 将低回载量组 P95 从原生 FIFO 的 **230.9 ms** 降到 **23.4 ms**。同时到达时，Bidaw 的低回载量组 P95 最低（**36.7 ms**）。

[同时到达的结果](https://kimmozag.github.io/SuperLoad/figures/method-comparison-0ms.svg) · [两种到达条件的完整图表](https://kimmozag.github.io/SuperLoad/#comparison) · [原始测量](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-28)

## Direct I/O 补充对照

在相同的 16 MiB 分块下，相比分块 FIFO，SuperLoad 在普通主机缓冲和 CANN 锁页路径下分别将低回载量组 P95 降低 **89.8%** 和 **89.2%**（高回载量请求早到 25 ms）。双请求吞吐分别下降 0.94% 和 0.34%。

[实验设置与复现](docs/direct-io-ablation.md) · [原始测量](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-direct-ablation-910b2-2026-11-28)

## 运行方法对照

运行环境为 Linux、Python 3.10+ 和昇腾 910B2，设备测量需要 Ascend 驱动与 CANN。

```bash
mkdir -p data
curl -fLo data/toolagent_trace.jsonl \
  https://raw.githubusercontent.com/kvcache-ai/Mooncake/main/FAST25-release/traces/toolagent_trace.jsonl
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m superload prefix-comparison-plan --trace data/toolagent_trace.jsonl --output runs/compare/plan.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io buffered --host-buffer paired --storage-label longhorn-ext4 --output runs/compare/buffered.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io direct --pair-limit 20 --storage-label longhorn-ext4 --output runs/compare/direct.json
```

结果写入 `runs/compare/`。Buffered I/O 同批运行两种主机缓冲，Direct I/O 使用普通缓冲。每种模式默认测量两轮，首次生成的两种 KV 文件布局合计约 15.4 GiB。Direct I/O 需要支持 `O_DIRECT` 的文件系统。

[问题验证的复现说明](docs/prefix-reload.md)

<a name="构建与发布"></a>
<details>
<summary>构建与发布报告</summary>

```bash
python3 -m pip install -r report/requirements.txt
python3 -m unittest discover -s tests -v
python3 report/build.py --comparison-results runs/compare
```

默认构建使用三份固定的 GitHub Release 附件，并校验 SHA-256。网页首屏海报的实测指标来自方法对照，展开区从 [overview.md](overview.md) 同步已填写的章节。输出位于 `build/site/`。用 `--comparison-results`、`--prefix-results` 或 `--direct-ablation-results` 可预览对应的本地复测。反复构建同一输出目录时，构建会清理已退役的旧图和静态文件。

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

[方法对照原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-28) · [问题验证原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-prefix-910b2-2026-11-27)

</details>

## 维护者

本仓库主要由以下两位共同维护。

- Kimmo Zhang · [995496585@qq.com](mailto:995496585@qq.com)
- Zhenye Huang · [2774557052@qq.com](mailto:2774557052@qq.com)
