# Direct I/O 分块与锁页对照

在同一条 910B2 存储到 HBM 通路上，对照整请求与 16 MiB 分块的传输开销、块间轮转的调度效果，以及主机缓冲路径的影响。

## 实验设置

从原五种方法对照复用相同的 20 对 Direct I/O 请求，使用 layer-major 布局和 Longhorn ext4。回载量按单次请求实际需要从存储读取的 KV 字节数计，低回载量组为 8–32 MiB，高回载量组为 ≥128 MiB。每轮对每个请求对随机执行 12 种组合，含两种到达条件、三种调度和两种缓冲路径。两轮共记录 480 次配对测量。首轮逐对逐条件回读 HBM 的首末块，第二轮在首对重复校验，字节检查位于计时区间外。

| 方法 | 调度与传输 |
| --- | --- |
| 原生 FIFO（整请求） | 当前请求完成后切换，每个请求作为一个整体读入，再执行一次 H2D。 |
| 16 MiB 分块 FIFO | 每块先读入再做 H2D，当前请求全部完成后切换。 |
| SuperLoad | 每块先读入再做 H2D，在 16 MiB 块边界允许切换请求。 |

普通主机缓冲路径从对齐的 DRAM 缓冲同步拷到 HBM。CANN 锁页路径把同一普通缓冲中的数据由 CPU 拷到预分配的 `aclrtMallocHost` 缓冲，再同步拷到 HBM。CPU 中转计入就绪延迟与双请求吞吐，并单独记录。P95 从名义到达计到 KV 全部进入 HBM。每轮用最近秩法计算 P95，以传输字节数除以累计双请求完成时间计算吞吐，表中汇总取两轮中位数。

## 实测结果

高回载量请求早到 25 ms 时的结果如下。

| 主机缓冲 | 方法 | 8–32 MiB P95（ms）↓ | ≥128 MiB P95（ms）↓ | 双请求吞吐（GiB/s）↑ |
| --- | --- | ---: | ---: | ---: |
| 普通 | 原生 FIFO（整请求） | 3,656 | 3,566 | 0.1511 |
| 普通 | 16 MiB 分块 FIFO | 3,756 | 3,676 | 0.1496 |
| 普通 | SuperLoad | **383** | 3,540 | 0.1482 |
| CANN 锁页路径 | 原生 FIFO（整请求） | 3,832 | 3,748 | 0.1493 |
| CANN 锁页路径 | 16 MiB 分块 FIFO | 3,528 | 3,452 | 0.1500 |
| CANN 锁页路径 | SuperLoad | **381** | 3,842 | 0.1495 |

- **分块成本**。普通主机缓冲下，整请求改为分块 FIFO 后，双请求平均完成时间增加 22 ms，其中 H2D 平均增加 28 ms。
- **轮转收益**。相比分块 FIFO，SuperLoad 在两种缓冲路径下均让 20 对中的每对低回载量请求更早就绪（先在每对内平均两轮）。普通主机缓冲和 CANN 锁页路径下，低回载量组 P95 分别下降 89.8% 和 89.2%，吞吐分别下降 0.94% 和 0.34%。配对平均的高回载量请求就绪时间分别增加 117 ms 和 103 ms。
- **锁页路径**。在 SuperLoad 中，双请求 H2D 平均耗时从 95 ms 降至 25 ms，新增 CPU 中转耗时为 64 ms。双请求平均完成时间减少 19 ms，低回载量组 P95 变化约 2 ms。

[两种到达条件与分阶段汇总 JSON](https://kimmozag.github.io/SuperLoad/assets/direct-ablation-summary.json) · [原始测量与回放脚本](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-direct-ablation-910b2-2026-11-28)

## 复现

原始 ZIP 内含 `plan.json`、`selection.json`、`direct.json` 和回放脚本。从 SuperLoad 仓库根目录在独立目录中准备回放，`archive` 指向已下载的 ZIP。

```bash
work=runs/direct-ablation
mkdir -p "$work"
python3 -m zipfile -e "$archive" "$work/input"
git archive "$(cat "$work/input/revision.txt")" superload | tar -xf - -C "$work"
cp "$work/input/"{plan.json,selection.json,run_direct_ablation.py,add_chunk_fifo.py,run.sh,revision.txt} "$work/"
(cd "$work" && python3 add_chunk_fifo.py && bash run.sh)
python3 report/build.py --direct-ablation-results "$work"
```
