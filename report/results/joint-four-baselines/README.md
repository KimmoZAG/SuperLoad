# 联合切分与四种方案的混跑对照

本批测量在单机昇腾 910B2 上执行，读取路径为 Longhorn ext4 → 主机 DRAM → NPU HBM。每轮包含 12 批前缀请求（每批 4 个）和 36 个稀疏请求。每页为 2 MiB。Buffered 的名义输入速率为 0.16 GiB/s，Direct I/O 为 0.10 GiB/s。每种模式下，五个方案使用种子 42、43、44，按种子共享到达时刻和页 ID。

| 方案 | Buffered 前缀 P95 (ms) | Buffered 稀疏 P95 (ms) | Direct 前缀 P95 (ms) | Direct 稀疏 P95 (ms) |
| --- | ---: | ---: | ---: | ---: |
| LRU | 173.8 | 164.8 | 859.0 | 993.1 |
| Strata | 155.8 | 89.4 | 332.0 | 254.2 |
| Bidaw | 215.3 | 139.5 | 3438.6 | 1512.4 |
| SolidAttention | 199.0 | 160.6 | 4024.1 | 4123.3 |
| 联合切分 | 109.9 | 73.7 | 227.2 | 238.9 |

每轮单独计算两类请求从到达到 HBM 就绪的 P95。表中取三个种子的中位数。Strata、Bidaw 和 SolidAttention 使用 [SuperLoad 的机制实现](../../../docs/experiment.md)，测量范围是混合 KV 回载。

- [指标汇总](metrics.csv)包含各方案的 P95、逻辑吞吐及实际读写字节相对逻辑字节的比例。
- [逐种子对比](paired.csv)记录联合切分相对四个方案在相同种子下的 P95 变化。
- [原始记录](raw-results.tar.gz)包含 30 轮 JSON、两种 I/O 模式的运行日志和运行前的 NPU 状态。

从仓库根目录重算两个 CSV。重新测量时使用[实验说明](../../../docs/mixed-kv.md#联合切分与四种方案的混跑对照)中的命令。

```bash
python3 -m scripts.mixed_kv.summarize_mechanisms --suite joint \
  report/results/joint-four-baselines/raw-results.tar.gz \
  --output runs/joint-four-baselines-summary
```
