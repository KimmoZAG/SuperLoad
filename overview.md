# SuperLoad Overview

## What is the problem?（问题定义）

Agent 会话间歇推进，不同任务积累的可复用上下文规模各异。高速缓存容量有限，部分前缀 KV 会留在存储层。会话续接时，回载量不同的请求可能共用一条加载通路。按整请求串行回载时，已经开始的高回载量请求会占用通路，后到的低回载量请求只能等前者完成。

**如何缩短低回载量请求因高回载量请求占用通路而产生的等待，从而降低低回载量组的 KV 就绪尾延迟？**

基于 Mooncake Tool & Agent 线上请求轨迹的昇腾 910B2 Direct I/O 实验中，高回载量请求早到 25 ms 时，低回载量组 HBM 就绪 P95 在原生 FIFO 下为 **4.154 秒**，在优化后 SuperLoad 下为 **0.229 秒**。 [实验设置与逐方法结果](docs/comparison.md)

![左图为低回载量组在两种调度下的 HBM 就绪延迟分布，右图为不同重算参考耗时下的配对比例。](https://kimmozag.github.io/SuperLoad/figures/recompute-crossover.svg)

## Why it matters?（问题重要性）

命中存储层的前缀 KV 后，系统可以省去对这段上下文的重复 prefill。会话续接时，模型仍需等所需 KV 进入 HBM 才能使用这些状态。回载的延迟成本因此包括读取、拷贝和等待通路的时间。

低回载量请求自身需要搬运的数据较少。排在正在执行的高回载量请求后面时，它还要承担前者剩余的服务时间。同一个请求仅因到达顺序不同，就可能在一次续接中很快就绪，在另一次续接中长时间等待。

当这段等待超过无争用回载相对于重算节省的时间，缓存命中就会失去延迟优势。存储层扩大了可复用前缀的范围，共享通路决定这些命中能否及时转化为更快的会话续接。评估分层 KV Cache 的收益时，因而需要结合命中后的 KV 就绪尾延迟与直接重算耗时。

## Why existing works fail?（现有方法的局限性）

| 方法 | 优化对象 | 请求范围 | 通路范围 | 重叠争用处理 |
| --- | --- | --- | --- | --- |
| Bidaw | 请求顺序 | 待发请求间 | 调度队列 | 有限 |
| Strata I/O | 数据布局 | 单个请求 | 存储读取 | 间接缓解 |
| Libra | 搬运流水 | 单个请求 | 读取与 H2D | 间接缓解 |
| **SuperLoad** | **通路资源分配** | **在途与后到请求** | **读取与 H2D** | **跨请求让行** |

在本机回载实现中，Bidaw 能在多个待发请求之间分配服务顺序，这个决定在请求进入搬运通路前作出。Strata I/O 和 Libra 则改善已经开始的搬运。尤其是 Libra，已经能协调读取和 H2D 两个阶段，但流水中的资源仍服务于同一个请求。 [Bidaw §3.2](https://www.usenix.org/system/files/fast26-hu-shipeng.pdf) · [Strata §4.2.1](https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf) · [Libra §4.2–4.3](https://www.usenix.org/system/files/nsdi26-ruan-libra.pdf)

当高回载量请求已经占用通路、低回载量请求随后到达时，**两个问题同时出现：该让哪个请求继续，以及该把通路上的哪部分资源交给它。** SuperLoad 把这两个决策放在一起。这样，后到请求可以在高回载量请求完成前推进，而高回载量请求仍能沿流水线继续搬运。

## What is your key idea?（核心思想）

把一个回载请求拆成可调度的传输段，并让读取与 H2D 各自推进。高回载量请求占用某个传输段时，低回载量请求可以在下一次调度机会获得读取和拷贝资源。

## What is your design?（技术方案/系统设计）

将同一 KV 页按 page-first 布局存放，排序后合并相邻读。请求最多占用预设数量的主机缓冲，H2D 完成后才归还缓冲。Direct I/O 在低回载量请求完成后使用较大的连续段。Buffered I/O 普通缓冲请求 2 MiB 透明大页，锁页缓冲使用 CANN 异步流。 [各模式参数](docs/comparison.md#优化后-superload-的配置)

## What is the experimental plan?（实验设计）

将同一批 16 对留出请求在三种 I/O／缓冲配置下运行两轮，分别复测「同时到达」和「高回载量请求早到 25 ms」。六种方法随机交错，比较两组 HBM 就绪 P95 与双请求吞吐。 [完整实验协议](docs/comparison.md)

## What is the takeaway?（主要结论与启示）

前缀回载的低回载量组尾延迟同时受调度与数据路径影响。较细的早期调度边界给后到请求提供服务机会，读取和 H2D 的重叠、相邻页合并与合适的主机缓冲支持大段续传。 [六种方法实测](https://kimmozag.github.io/SuperLoad/#comparison)
