# SuperLoad Overview

## What is the problem?（问题定义）

Agent 会话间歇推进，不同任务积累的可复用上下文规模各异。高速缓存容量有限，部分前缀 KV 会留在存储层。会话续接时，回载量不同的请求可能共用一条加载通路。按整请求串行回载时，已经开始的高回载量请求会占用通路，后到的低回载量请求只能等前者完成。

**如何缩短低回载量请求因高回载量请求占用通路而产生的等待，从而降低低回载量组的 KV 就绪尾延迟？**

- **回载路径与分组**。HBM 和主机 DRAM 未命中且存储命中的前缀 KV 块，经存储 → DRAM → 昇腾 NPU HBM 回载。回载量按单次请求实际需要从存储读取的 KV 字节数计，实验中的低回载量组为 8–32 MiB，高回载量组为 ≥128 MiB。
- **评价标准**。用低回载量组的 HBM 就绪 P95 衡量尾延迟，计时从请求到达至所需 KV 全部进入 HBM。实验也记录高回载量组的 HBM 就绪 P95 和双请求吞吐，以量化调度的影响。
- **验证范围**。公开轨迹回放支持两组请求在约 3 秒的时间窗内共现。昇腾 910B2 的配对实验验证了受控到达条件下的队首阻塞。线上毫秒级争用频率仍需细粒度服务日志验证。[回放与实测方法](docs/prefix-reload.md)

## Why it matters?（问题重要性）

**回载的延迟收益。** 命中存储层可以复用已有前缀 KV。回载还需要等待共享通路，当回载总耗时超过直接重算时，这次缓存命中就失去了延迟优势。

**配对证据。** 我们使用 Mooncake Tool & Agent 线上请求轨迹，通过分层缓存回放筛选存储回载请求。在昇腾 910B2 的 Direct I/O 实验中，高回载量请求早到 25 ms。对每对请求取两轮中位数，分别统计原生 FIFO 和 SuperLoad 下低回载量组的 HBM 就绪延迟。[数据与测量方法](docs/comparison.md) · [回载延迟与重算阈值](https://kimmozag.github.io/SuperLoad/#recompute-crossover)

**重算参考。** ThunderServe 论文报告，LLaMA-30B 在 4 张 A40 上处理 1,024-token 输入时，非分离式基线的 prefill 耗时为 884 ms。以该论文值代入当前的配对延迟，20 对均满足「SuperLoad 回载 < 重算参考 < 原生 FIFO 回载」。这个结果描述给定参考值下的交叉条件。实际重算耗时随模型、输入和设备而变。[ThunderServe，MLSys 2025，附录 H 表 5](https://proceedings.mlsys.org/paper_files/paper/2025/file/c2a0e26dd9ee7d57e92bb1c24b39659a-Paper-Conference.pdf)

## Why existing works fail?（现有方法的局限性）

高回载量请求先占用共享 KV 回载通道时，后到的低回载量请求可能长时间排队。

- **原生 FIFO** 按到达顺序逐个完成回载。排在后面的低回载量请求必须等到先到的请求完成，形成队首阻塞。
- **Bidaw** 按等待时间和回载量对待发请求排序。两组请求同时在队列中时可以重排，后到请求要等当前回载完成才有机会获得通道。[Bidaw §3.2](https://www.usenix.org/system/files/fast26-hu-shipeng.pdf)
- **Strata I/O** 将同一页的各层 KV 连续存放，用更大的连续传输缩短回载时间。布局优化压缩了高回载量请求占用通道的时间，低回载量请求何时获得通道仍由原有调度决定。[Strata §4.2.1](https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf)
- **Libra 流水** 通过分块重叠传输与计算来提高资源利用率。请求级 FIFO 的本机流水实现仍会让后到的低回载量请求等待当前回载完成。[Libra §4.2–4.3](https://www.usenix.org/system/files/nsdi26-ruan-libra.pdf)

要保护低回载量请求的尾延迟，共享通道需要在高回载量请求完成前有机会重新选择服务对象。

## What is your key idea?（核心思想）

## What is your design?（技术方案/系统设计）

## What is the experimental plan?（实验设计）

## What is the takeaway?（主要结论与启示）
