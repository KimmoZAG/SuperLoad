# Agent 前缀回载六种方法对照

在昇腾 910B2 上比较六种方案，从 Longhorn ext4 文件读入主机 DRAM，再将 KV 传入 HBM。配对使用同一批 Mooncake Tool & Agent 公开轨迹回放。 [单通道问题验证](prefix-reload.md) · [轨迹与字段](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)

## 负载与到达条件

使用固定的 64 对前缀回载计划，选取历史对照和优化确认均未使用的 16 个时间窗，每窗一对存储页互不重叠的请求。发布包中的计划记录源计划和排除计划的 SHA-256。回载量按实际读取 KV 字节数计，实验中低回载量请求为 8–32 MiB，高回载量请求为 136–504 MiB。文件中的 KV 内容由确定性字节生成器创建，两个布局保存相同逻辑内容。 [样本重建步骤与排除清单](reproduction.md#从公开轨迹重建正式样本)

每个请求对在「同时到达」和「高回载量请求早到 25 ms」下回放。两项请求同时到达时，整请求 FIFO 先服务高回载量请求。公开轨迹只提供约 3 秒粒度的时间窗，实验独立设置窗内到达间隔。三种 I/O／主机缓冲配置各测相同的 16 对请求、两轮。每轮打乱请求对顺序，每对随机交错六种方法和两种到达条件。

## 对照方法

| 方法 | 实现 |
| --- | --- |
| 原生 FIFO | 按到达顺序逐个完成请求。layer-major 文件读入 DRAM 后同步 H2D。 |
| Bidaw | 用 Disk-HRRN 重排待发请求。 [论文 §3.2](https://www.usenix.org/system/files/fast26-hu-shipeng.pdf) |
| Strata I/O | 使用 page-first 布局，按文件位置排序并合并相邻页，沿用 FIFO。 [论文 §4.2](https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf) |
| Libra | 在请求内每 16 MiB 切一块，两组缓冲交替重叠下一块文件读取和当前块 H2D。 [论文 §4.3](https://www.usenix.org/system/files/nsdi26-ruan-libra.pdf) |
| 原 SuperLoad | 每次同步加载 16 MiB，在块边界重新接纳和轮转请求。 |
| 优化后 SuperLoad | 使用 page-first 布局、低回载量优先、有界读队列和独立 H2D 阶段。 |

三个论文方法均按本机回载路径移植所列机制。比较范围覆盖文件 I/O、主机缓冲、调度和 H2D。各方法的请求范围、通路范围和争用处理见 [现有方法的局限性](https://kimmozag.github.io/SuperLoad/#limitations)。

## 优化后 SuperLoad 的配置

| I/O 与主机缓冲 | 分块与传输 | 主机缓冲池 |
| --- | --- | ---: |
| Buffered I/O 普通缓冲 | 8 MiB 首段，64 MiB 主体。一个读线程，一个同步 H2D 工作线程。请求本地 2 MiB 透明大页，并在测量前记录实际大页字节数。 | 192 MiB |
| Buffered I/O CANN 锁页缓冲 | 16 MiB 首段，64 MiB 主体。三个读线程和两个 CANN 异步拷贝流。完成事件确认后才回收缓冲。 | 320 MiB |
| Direct I/O 普通缓冲 | 8 MiB 首段，32 MiB 主体。低回载量请求完成后，高回载量后续段扩大至 128 MiB。一个读线程和一个同步 H2D 工作线程。 | 384 MiB |

普通 Buffered I/O 对照使用原有普通缓冲，透明大页是优化后 SuperLoad 的配置。锁页组使用 CANN 锁页内存，Direct I/O 组用对齐普通缓冲接收文件直读。Buffered I/O 在每对开始前预热两种文件布局的页缓存。Direct I/O 绕过读取端的 Linux 页缓存，后端缓存仍可能影响耗时。

## 指标与范围

HBM 就绪 P95 从请求到达到最后一段 H2D 完成，包含在途等待。每轮对各组按最近秩法计算 P95，本批 16 对对应轮内最大值。双请求吞吐用实际写入 HBM 的字节数除以累计双请求完成时间。图中空心圆是单轮值，实心菱形是两轮中位数。

1,152 个计时窗口均在计时后验证两个请求的首末 HBM 页，并核对实际读写字节数。另选最小和最大两对请求，在三种配置及两种到达条件下逐次清零目标 HBM 后回载并逐字节回读，共验证 492 页。发布包中的 `verification.json` 给出 12 个全量校验窗口。

实验覆盖单机双请求回载。服务端批处理、模型计算和真实毫秒级到达序列对应不同的实验口径。

## 结果与复现

每格为同一条件下该指标的最强对照和优化后 SuperLoad。优化后 SuperLoad 领先 **18/18 项**。

| I/O／缓冲 | 到达条件 | 8–32 MiB P95（ms） | ≥128 MiB P95（ms） | 双请求吞吐（GiB/s） |
| --- | --- | ---: | ---: | ---: |
| Buffered I/O · 普通缓冲 | 同时到达 | 72.82（Bidaw）→ 48.46 | 668.09（Libra）→ 237.02 | 0.679（Libra）→ 2.139 |
| Buffered I/O · 普通缓冲 | 高回载量请求早到 25 ms | 123.55（原 SuperLoad）→ 28.86 | 646.83（Libra）→ 236.55 | 0.676（Libra）→ 2.136 |
| Buffered I/O · CANN 锁页缓冲 | 同时到达 | 49.73（Bidaw）→ 43.04 | 324.90（Libra）→ 117.17 | 1.466（Libra）→ 3.594 |
| Buffered I/O · CANN 锁页缓冲 | 高回载量请求早到 25 ms | 44.77（原 SuperLoad）→ 20.42 | 322.65（原 SuperLoad）→ 124.43 | 1.455（Libra）→ 3.491 |
| Direct I/O · 普通缓冲 | 同时到达 | 287.20（Bidaw）→ 157.34 | 1861.33（Strata I/O）→ 1299.80 | 0.283（Strata I/O）→ 0.343 |
| Direct I/O · 普通缓冲 | 高回载量请求早到 25 ms | 558.63（原 SuperLoad）→ 229.38 | 1802.24（Strata I/O）→ 1210.80 | 0.282（Strata I/O）→ 0.374 |

[完整逐方法图与表](https://kimmozag.github.io/SuperLoad/#comparison) · [同时到达 PDF](https://kimmozag.github.io/SuperLoad/figures/method-comparison-0ms.pdf) · [高回载量请求早到 25 ms PDF](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.pdf) · [原始测量](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-superload-910b2-2026-11-28) · [复现与审计](reproduction.md)
