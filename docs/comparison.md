# Agent 前缀回载方法对照

实验比较应用层队列、文件布局与传输粒度对低回载量请求和高回载量请求争用的影响。现象验证与缓存敏感性见 [前缀回载争用](prefix-reload.md)。

## 负载和到达条件

从 23,608 条 Mooncake Tool & Agent 请求生成回放。HBM、DRAM、存储层的模拟 KV 预算分别为 8、16、64 GiB，前 100 个时间窗用于预热。用相同的 LRU 模型找出存储命中且 HBM、DRAM 缺失的页，按每个请求实际需要从存储读取的 KV 字节数分组。低回载量组为 8–32 MiB，高回载量组为 ≥128 MiB。从两组同时出现的 232 个候选时间窗中随机抽取 24 对。当前模型与配置下，测量窗内的存储回载页未出现跨请求共享。公开时戳的分辨率约为 3 秒，组内细粒度顺序由实验控制。[轨迹与字段](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)

每对在两种条件下回放。第一种同时到达，FIFO 同时收到请求时先将高回载量请求入队。第二种高回载量请求早到 25 ms，低回载量请求可在通道被占用时到达。五种方法使用相同的页、字节和到达时刻。合成文件按抽样页号紧凑存放，读合并收益取决于实际部署的文件布局。两种 I/O 模式均重复两轮。Buffered I/O 测全部 24 对，每对先对两种文件布局预读，然后随机交错两种到达条件、五种方法和两种主机缓冲，共 20 个条件。Direct I/O 测从中抽取的 20 对，使用普通缓冲，绕过工作区客户端页缓存并随机执行每对的 10 个条件。

## 方法

| 方法 | 测量实现 |
| --- | --- |
| 原生 FIFO | 按到达顺序整请求读取，文件使用 layer-major 布局。完成同步 H2D 后再处理下一请求。 |
| Bidaw | 已知存储缺页量，用等待时间除以回载块数决定待服务请求的 Disk-HRRN 优先级。缓存模型固定，测量范围为论文的存储队列排序。 [论文 §3.2](https://www.usenix.org/system/files/fast26-hu-shipeng.pdf) |
| Strata I/O | 使用 page-first 布局，按文件页位置排序并合并相邻读，按 FIFO 处理。共享页和模型批调度需要相应的工作负载。 [论文](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang) |
| Libra 流水 | 保留 FIFO，在请求内切成 16 MiB 块，两个 DRAM 缓冲交替工作。将论文的计算与跨实例 KV 传输流水移植到本机读文件和 H2D 两阶段。主线程拷入当前块，读线程并行读下一块。 [论文 §4.3](https://www.usenix.org/system/files/nsdi26-ruan-libra.pdf) |
| SuperLoad | 使用固定分块轮转，采用与 Libra 相同的 16 MiB 块。每一块同步读入 DRAM 并拷入 HBM，然后在排队请求间轮转。在块结束时接纳本块执行期间到达的请求。 |

## 主机缓冲

Buffered I/O 的两组都用 `preadv`，将页缓存中的数据读入各自预分配的普通缓冲或 `aclrtMallocHost` 缓冲，再同步传入 HBM。两组沿用相同的请求顺序规则、文件布局和传输粒度。Libra 保留双缓冲，读线程填充下一块时主线程传输当前块。锁页组预留一个 864 MiB 主缓冲和两个 16 MiB 的流水缓冲。

## 指标和解释范围

每对由同一逻辑 NPU 上的 AscendCL 写入 HBM。第一轮逐对逐条件回读两个请求的首末块，第二轮每个条件复核一对。文件 I/O、同步 H2D、排队与流水重叠分别计时。字节检查位于计时区间外。

每轮按最近秩法分别计算两组请求的 HBM 就绪 P95，报告各轮中位数并展示单轮值。吞吐使用两项请求都完成之前的累计窗口耗时和实际写入 HBM 字节。排队时延截止到请求首次获得工作线程，固定分块轮转中后续的等待计入就绪时延。流水重叠以相邻块的读阶段和 H2D 调用的时间交集计算。

实验只回放这条数据通路。低回载量请求以受控时差到达，模型计算、在线请求批处理和生产毫秒级到达顺序属于后续服务端评估范围。普通缓冲与锁页缓冲的变化以同批 Buffered I/O 数据为准。Direct I/O 来自原发布批次，使用不同的样本数，方法对比以同一 I/O 条件内的配对结果为准。

## 实测结果

每张图的三行依次为 Buffered I/O 普通缓冲、Direct I/O 普通缓冲和 Buffered I/O 锁页缓冲。三列为低回载量组 HBM 就绪 P95、高回载量组 HBM 就绪 P95 和双请求吞吐。

| 到达条件 | 图表 |
| --- | --- |
| 高回载量请求早到 25 ms | [SVG](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.svg) · [PDF](https://kimmozag.github.io/SuperLoad/figures/method-comparison-25ms.pdf) |
| 同时到达 | [SVG](https://kimmozag.github.io/SuperLoad/figures/method-comparison-0ms.svg) · [PDF](https://kimmozag.github.io/SuperLoad/figures/method-comparison-0ms.pdf) |

- **高回载量请求早到 25 ms。** 在锁页缓冲下，相比原生 FIFO，SuperLoad 的低回载量组 P95 从 230.9 ms 降到 23.4 ms（−89.9%），高回载量组 P95 从 252.0 ms 升到 286.8 ms，双请求吞吐从 2.263 升到 2.303 GiB/s。Libra 流水的吞吐最高（2.336 GiB/s）。
- **同时到达。** 在锁页缓冲下，Bidaw 的低回载量组 P95 最低（36.7 ms），SuperLoad 为 48.2 ms。两者的双请求吞吐分别为 2.222 和 2.311 GiB/s。
- **缓冲路径的影响。** 高回载量请求早到 25 ms 时，改用锁页缓冲后，SuperLoad 每对请求的平均累计 H2D 耗时从 99.8 ms 降到 30.1 ms，低回载量组 P95 从 38.0 ms 降到 23.4 ms。同时到达时，SuperLoad 的低回载量组 P95 从 42.7 ms 升到 48.2 ms。

高回载量请求早到 25 ms 时，原 Direct I/O 对照中 SuperLoad 将低回载量组 P95 从 4,014 ms 降到 549 ms，高回载量组 P95 从 3,916 ms 增至 4,775 ms，双请求吞吐从 0.124 GiB/s 降到 0.112 GiB/s。Strata I/O 将每对请求的平均读调用从 332.8 次降到 3.7 次。三种调度的锁页与分块消融见 [Direct I/O 补充对照](direct-io-ablation.md)。

[复现命令](../README.md#运行方法对照) · [完整图表](https://kimmozag.github.io/SuperLoad/#comparison) · [汇总 JSON](https://kimmozag.github.io/SuperLoad/assets/comparison-summary.json) · [原始测量](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-28) · [原 Direct I/O 发布批次](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-comparison-910b2-2026-11-27)
