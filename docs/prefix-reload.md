# Agent 前缀 KV 回载争用

研究对象是共用一条存储 → DRAM → NPU HBM 加载路径的前缀复用请求。我们检验线上轨迹在给定缓存配置下是否会在同一窗口出现低回载量与高回载量请求，以及高回载量请求占用单工作线程时对低回载量请求就绪延迟的影响。

## 从请求轨迹推导存储读取

输入为 [Mooncake Tool & Agent](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release) 的 23,608 条请求，`prefix-plan` 校验文件的 SHA-256。每个前缀块对应 512 tokens。请求按公开时间戳分组。主要设置为 HBM 8 GiB、DRAM 16 GiB、存储 64 GiB，同时改变上两层容量和存储保留空间。容量以 4 层、8 KV heads、128 head dim、2 字节 KV 的 8 MiB 数据块折算，表示 KV 空间预算。

同一时间戳的块均查询该组开始前的驻留快照。三个独立 LRU 依次处理 HBM、DRAM、存储访问。在该组结束时，根据最先命中的层更新访问过的层。首次出现和三个层级均已淘汰的块视为计算所得，并假定在下一时间戳前写入分层缓存。存储命中且 HBM、DRAM 都缺失的块才计入回载。该模型假定全部新 KV 都会写入存储。生产服务的准入、写回压力与模型计算需要另外测量。

预热前 100 个时间窗。按单次请求实际需要从存储读取的 KV 字节数分组。低回载量组为 1–4 个块（8–32 MiB），高回载量组至少为 16 个块（≥128 MiB），介于两组之间的请求仍参与缓存模拟。报告以全部预热后时间窗为分母，并对组内 LRU 的更新顺序使用三个随机种子。先确定两组共现的样本，再从其中固定种子抽取 64 个时间窗，每窗随机选择一对存储读取块互不重叠的请求。公开时戳约每 3 秒一个窗口，窗口内的微秒、毫秒级到达顺序未发布。

## 910B2 的配对测量

相同的请求块由确定性字节生成器写成 layer-major KV 文件，存储在 Longhorn ext4 工作区卷。每次以同一个同步工作线程通过 `preadv` 读到对齐的 DRAM，再执行同步 AscendCL H2D。对每组请求，随机顺序执行四个配对条件。

| 条件 | 实验设置 |
| --- | --- |
| 低回载量请求单独执行 | 只释放配对中的低回载量请求 |
| 低回载量请求先执行 | 同时释放两个请求，先服务低回载量请求 |
| 高回载量请求先执行 | 同时释放两个请求，先服务高回载量请求 |
| 高回载量请求早到 25 ms | 先释放高回载量请求，25 ms 后释放低回载量请求，并测量剩余阻塞时间 |

Buffered 每对先预读以观察热文件数据，运行 64 对、三轮。Direct I/O 绕过读取端的 Linux 页缓存，Longhorn 后端的缓存仍可能影响读取。从相同计划随机取 16 对、运行两轮，用于检查从页缓存直接读取与经文件系统读取是否都出现阻塞。每轮打乱配对顺序及每对的条件顺序。每对验证两个请求的首末块 HBM 内容，校验位于计时区间外。

就绪时延从名义到达到同步 H2D 完成。排队时延截止到调用加载器前，服务时间包括文件读取、整理和 H2D。P95 用最近秩法在各轮计算后取中位数。Direct I/O 每轮 16 对，其 P95 取当轮最大值，适合观察方向，用配对增量中位数判断典型代价。配对增量以低回载量请求单独执行时的就绪时延为基准。双请求吞吐用载入字节量除以各配对条件下的累计有效时间，覆盖配对开始至两项加载完成。

这项实验测量的是**给定缓存条件下的共现频率**和**单通道同步服务下的阻塞代价**。线上争用比例、服务端 TTFT、异步或多工作线程的影响需要相应的到达与执行数据。

## 复现

下载轨迹并启用 AscendCL 后运行以下命令。下载与环境命令见 [仓库说明](../README.md#运行方法对照)。

```bash
python3 -m superload prefix-plan --trace data/toolagent_trace.jsonl --output runs/prefix/plan.json
python3 -m superload prefix-bench --plan runs/prefix/plan.json --corpus data/prefix \
  --io buffered --storage-label longhorn-ext4 --output runs/prefix/buffered.json
python3 -m superload prefix-bench --plan runs/prefix/plan.json --corpus data/prefix \
  --io direct --repeat 2 --pair-limit 16 --storage-label longhorn-ext4 --output runs/prefix/direct.json
```

问题验证的结果文件固定在 [Release 清单](../report/prefix-results-release.json)，由 [报告构建](../report/build.py) 读取并校验。[网页图与汇总](https://kimmozag.github.io/SuperLoad/#prefix-contention) · [实验数据 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-prefix-910b2-2026-11-27)
