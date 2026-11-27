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

## What is your key idea?（核心思想）

## What is your design?（技术方案/系统设计）

## What is the experimental plan?（实验设计）

## What is the takeaway?（主要结论与启示）
