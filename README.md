# SuperLoad

Agent 会话间歇推进，不同任务积累的可复用上下文规模各异。高速缓存容量有限，部分前缀 KV 会留在存储层。会话续接时，回载量不同的请求可能共用一条加载通路。

**如何缩短低回载量请求因高回载量请求占用通路而产生的等待，从而降低低回载量组的 KV 就绪尾延迟？**

SuperLoad 按页合并文件读取，把存储读和 H2D 放在两个有界阶段，并优先推进已到达的低回载量请求。它为 Direct I/O、Buffered I/O 普通缓冲和 CANN 锁页缓冲选择对应的分块和传输方式。

[研究概览](overview.md) · [实验方法](docs/comparison.md) · [公开报告](https://kimmozag.github.io/SuperLoad/) · [仓库结构](docs/repository.md)

## 实测结果

同一台昇腾 910B2 使用 Mooncake Tool & Agent 公开轨迹回放同一批请求对。对照包含原生 FIFO、Bidaw、Strata I/O、Libra、原 SuperLoad 和优化后的 SuperLoad。低回载量组为 8–32 MiB，高回载量组为 ≥128 MiB。

![高回载量请求早到 25 ms 时六种方法的两组 HBM 就绪 P95 和双请求吞吐](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.svg)

高回载量请求早到 25 ms 时，Direct I/O 的低回载量组 HBM 就绪 P95 从原生 FIFO 的 **4153.56 ms** 降至优化后 SuperLoad 的 **229.38 ms**。三种配置的正式复测中，优化后 SuperLoad 在 **18/18 项**指标上领先同批最强对照。

[同时到达的图](https://kimmozag.github.io/SuperLoad/figures/method-comparison-0ms.svg) · [逐方法图表与精确数值](https://kimmozag.github.io/SuperLoad/#comparison) · [原始结果 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-superload-910b2-2026-11-28)

## 运行方法对照

使用 Linux 和 Python 3.10+。实测需要昇腾驱动、CANN 和支持 `O_DIRECT` 的文件系统。使用发布包内的 `plan.json` 可以复用论文图的同一批请求对，也可以从公开轨迹创建新的对照计划。

```bash
mkdir -p data
curl -fLo data/toolagent_trace.jsonl \
  https://raw.githubusercontent.com/kvcache-ai/Mooncake/main/FAST25-release/traces/toolagent_trace.jsonl
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m superload prefix-comparison-plan \
  --trace data/toolagent_trace.jsonl --output runs/compare/plan.json
```

三项命令使用同一计划、同一语料和相同的随机种子。发布实验使用 `--seed 20261128`，默认重复两轮。在原测量机上，普通 Buffered I/O 使用 `--numa-node 2`，其他机器可按 NPU 的 PCI NUMA 节点设置该参数。

```bash
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io buffered --numa-node 2 --seed 20261128 --storage-label longhorn-ext4 \
  --output runs/compare/buffered.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io buffered --host-buffer pinned --seed 20261128 --storage-label longhorn-ext4 \
  --output runs/compare/buffered-pinned.json
python3 -m superload prefix-compare --plan runs/compare/plan.json --corpus data/compare \
  --io direct --seed 20261128 --storage-label longhorn-ext4 --output runs/compare/direct.json
```

[基准与指标说明](docs/comparison.md) · [单通道问题验证](docs/prefix-reload.md)

## 构建与发布

```bash
python3 -m pip install -r report/requirements.txt
python3 -m unittest discover -s tests -v
python3 report/build.py --comparison-results runs/compare
```

默认构建从两份固定 GitHub Release 读取原始测量并校验 SHA-256。Pages 输出位于 `build/site/`，包含桌面和移动端 SVG、PDF、逐方法数值表和汇总 JSON。本地可用 `--comparison-results` 或 `--prefix-results` 覆盖对应批次。网页首屏、临界区间和对照图使用同一批正式复测。

发布方法对照时，将 `plan.json`、`buffered.json`、`buffered-pinned.json`、`direct.json` 放在 ZIP 根目录。列出环境和实现校验值后，把 ZIP 上传为 Release 附件。用标签、附件名和 SHA-256 更新 [发布清单](report/comparison-results-release.json)。默认构建通过后推送 `main`，由 GitHub Actions 部署 Pages。

[方法对照原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-superload-910b2-2026-11-28) · [问题验证原始数据](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-prefix-910b2-2026-11-27)

## 维护者

本仓库主要由以下两位共同维护。

- Kimmo Zhang · [995496585@qq.com](mailto:995496585@qq.com)
- Zhenye Huang · [2774557052@qq.com](mailto:2774557052@qq.com)
