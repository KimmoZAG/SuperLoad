# SuperLoad Overview

## What is the problem?（问题定义）

在 Agent 推理的分层 KV Cache 中，如何调度共享存储到 NPU HBM 路径的大小前缀回载，降低小回载的尾延迟，同时维持整体回载吞吐、控制大回载的完成时间？

- **应用场景**。Agent 请求复用已有前缀。当前缀 KV 块仍保存在存储层、但已从 HBM 和主机 DRAM 淘汰时，需要通过存储 → DRAM → 昇腾 NPU HBM 的路径回载。
- **请求差异**。在相同的前缀回载场景中，请求需要从存储读取的字节数不同。公开轨迹回放显示，在给定的缓存配置下，大小回载可以出现在同一时间窗。
- **阻塞机制**。在原生同步加载路径上，大回载会占用工作线程直至文件读取和 H2D 拷贝完成。在此期间到达的小回载需要排队，形成队首阻塞。910B2 的配对实验验证了这一现象。
- **评价标准**。在相同请求轨迹、缓存预算和回载字节量下，用从请求到达到 KV 在 HBM 中可用的 P95 延迟衡量小回载的尾延迟，同时记录整体回载吞吐和大回载完成时间。
- **研究范围**。问题属于 LLM 推理系统的分层 KV Cache 加载调度。公开轨迹以约 3 秒为一个时间窗，层级驻留由缓存模型推导。线上毫秒级争用频率需要细粒度服务日志验证。[回放与实测方法](docs/prefix-reload.md)

## Why it matters?（问题重要性）

**真实后果。** 在线 Agent 请求可能因为等待 KV 回载而延迟生成首个 token。FlatCake 回放 Claude Code 的生产请求时发现，随着负载上升，Mooncake 的排队时间从 0.2 秒增至 5 秒，占高负载下首 token 延迟的 41%。

![FlatCake 图 3。Claude Code 生产轨迹回放中，Mooncake、Tutti 和 FlatCake 在低、中、高负载下的首 token 延迟分解。](https://arxiv.org/html/2610.38355v2/fig_ttft_breakdown.svg)

*图源：Huang 等，A Flat KV Cache Storage Architecture for LLM Serving，arXiv:2610.38355v2，图 3。*

**研究价值。** 把 KV 缓存扩展到存储后，什么决定了新增的缓存容量能否转化为稳定的推理性能？回答这个问题，可以为兼顾容量和响应时间的分层缓存设计提供依据。

**潜在收益。** 如果能减少并发回载带来的等待，在线 Agent 在高负载下就有望更稳定地给出首个 token，让更多请求在同样的硬件预算内满足延迟目标。

## Why existing works fail?（现有方法的局限性）

在共享的存储至 HBM 加载路径上，大回载先占用通道时，随后到达的小回载会遇到队首阻塞。我们在单台昇腾 910B2 上进行配对回放，在每种 I/O 模式内对同一组请求对测量两种到达条件。两请求同时到达，或大回载提前 25 ms 到达。[配对实验](docs/comparison.md)

**Bidaw 调整待发请求的顺序。** Disk-HRRN 根据等待时间和待回载 KV 量计算优先级。只要两个请求仍在待发队列中，调度器就有机会改变发出顺序。Direct I/O 下，两请求同时到达时，小回载 P95 从 FIFO 的 3,914 ms 降到 264 ms。大回载早到 25 ms 时，加载器已经在处理大回载，小回载 P95 为 3,956 ms，接近 FIFO 的 4,014 ms。[Bidaw §3.2](https://www.usenix.org/system/files/fast26-hu-shipeng.pdf)

**Strata I/O 缩短文件读取。** Strata 的页优先布局将同一页的各层 KV 连续存放。我们将这种布局用到文件中，使相邻页可以合并读取。大回载早到时，Direct I/O 下小回载 P95 从 FIFO 的 4,014 ms 降到 1,529 ms。读取更快缩短了通道被占用的时间，按整请求服务的加载器仍在大回载完成后才接续小回载。[Strata §4.2.1](https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf)

**Libra 流水重叠读取和 H2D。** Libra 的推理系统结合了延迟感知组批和分块 KV 传输。我们的单机对照采用其分块重叠思路，在拷贝当前块时读取同一请求的下一块，并保留请求级 FIFO。大回载早到时，Buffered 的双请求吞吐从 FIFO 的 0.533 GiB/s 提高到 0.840 GiB/s，小回载 P95 分别为 1,201 和 1,229 ms。请求内流水可以提高吞吐，小回载仍需等待大回载完成。[Libra §4.2–4.3](https://www.usenix.org/system/files/nsdi26-ruan-libra.pdf)

大回载启动后，晚到小回载的等待取决于加载器何时再次选择要服务的请求。在大回载完成之前提供让行机会，才能让小回载提前推进。选择让行边界时，还需要权衡大回载完成时间和双请求吞吐。

## What is your key idea?（核心思想）

## What is your design?（技术方案/系统设计）

## What is the experimental plan?（实验设计）

## What is the takeaway?（主要结论与启示）
