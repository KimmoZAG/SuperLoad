# SuperLoad 混合 KV 回载实验

本页记录混合回放的三组实验：同机 RoCE 与本地路径对照、四方案对照、Strata 排队诊断。脚本位于 [scripts/mixed_kv](../scripts/mixed_kv)。正式实验读取真实 KV 形状的确定性合成字节，保留逐请求到达与就绪时间、逐传输分段时间和读写字节。

## 准备源码快照

在 Linux 上从当前仓库根目录创建独立快照。基准代码固定在原始测量使用的提交。快照将语料扩展到 384 页以隔离两类请求的页空间，并为同机 RoCE 回放提供独立的接收与 H2D 操作。准备完成后，后续实验命令均在快照目录运行，使用 Python 3.10+。分析已有记录时，可直接在当前仓库的根目录运行汇总模块。

~~~bash
repo="$PWD"
snapshot=$(mktemp -d)
git archive 920debdb2803273b0f4a6cb9d3991c17b8e5d3a1 | tar -xf - -C "$snapshot"
cd "$snapshot"
export PYTHONPATH="$repo"
python3 -m scripts.mixed_kv.prepare_snapshot patch "$PWD"
out=$(mktemp -d)
source /usr/local/Ascend/ascend-toolkit/set_env.sh
~~~

## 测量设置

- 稠密前缀和稀疏解码沿用上游页访问生成器。两个独立的绝对时间到达流共享调度队列。前缀页和稀疏页分属独立的文件区间及页 ID 空间。
- 常规混合回放包含 6 批前缀请求和 18 个稀疏请求。稀疏占优回放包含 3 批前缀请求和 24 个稀疏请求。每批前缀有 4 个同时到达的请求。
- 独跑保留混跑里的同类请求顺序和到达间隔。等总负载独跑将单类到达时间按其字节数压缩到混跑的名义总速率。混跑和等总负载独跑设为 0.28 GiB/s 名义逻辑负载。
- Direct I/O 对同机 RoCE 的四种调度重复两档名义负载，先跑 0.28 GiB/s，再跑 0.14 GiB/s。后一档保持请求及页序列相同，拉长到达间隔。
- 每个配置用种子 42、43、44 各回放一次。同一个种子的配置顺序随机，同一场景在各策略和路径上复用相同到达序列。P95 表格使用各次 P95 的中位数。
- 每种策略都使用最多 4 页的传输块、两个 8 MiB 客户端 DRAM 槽、一条同步 H2D 拷贝通道和 4 GiB HBM 地址空间。用于回读的已完成页面留在地址空间内，只有仍在传输中的页面及下一次同会话回放可用的预取页参与复用。同机 RoCE 注册窗口为每端 32 KiB。
- 就绪从排定到达时刻计到最后一页的 H2D 完成。探索性 EDF 使用前缀 500 ms 和稀疏 150 ms 作为调度参数。后文的 Strata 排队诊断按回载 P95、等速独跑对照和等待归因报告结果。正式计时后抽样进行 HBM 回读。
- 测量路径是本地文件到 HBM，以及同机双进程的文件 → RoCE RC → 接收端 DRAM → HBM。存储读取与 H2D 可以重叠，分段耗时不能直接相加。RC 阶段含完成轮询和接收缓冲整理。

## 对照策略

| 策略 | 取数排序 | H2D 排序 | 页合并与预取 |
|---|---|---|---|
| FIFO | 按请求到达顺序 | FIFO | 单独按需取页 |
| 固定策略 | FIFO | FIFO | 合并在途页，下一次稀疏请求最多预取 2 页 |
| 入口 EDF | 按请求截止时间 | FIFO | 与固定策略相同 |
| 跨阶段反馈 | 根据截止时间、尚待取数的页面和各阶段近期成本排序 | 可为更紧迫的块等待至多 5 ms，再在备齐的块中排序 | 合并在途页，根据待服务请求和阶段成本控制预取 |
| 去合并或去预取 | 同跨阶段反馈 | 同跨阶段反馈 | 每个对照关闭一个机制 |

## 复跑

使用准备好的源码快照。进入正式计时前暂停保活并检查 NPU 状态，计时结束后恢复原定到期时间。

~~~bash
python3 -m scripts.mixed_kv.prepare_snapshot corpus "$out/pilot-corpus"
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native -j 2

mkdir -p "$out/pilot"
cat "$out/pilot-corpus/layer-major.bin" >/dev/null
python3 -m scripts.mixed_kv.mixed_load --suite smoke --corpus "$out/pilot-corpus" --seeds 42 --output "$out/pilot/hbm-smoke"
python3 -m scripts.mixed_kv.mixed_load --corpus "$out/pilot-corpus" --output "$out/pilot/buffered"
python3 -m scripts.mixed_kv.mixed_load --suite direct-check --io direct --corpus "$out/pilot-corpus" --output "$out/pilot/direct"
python3 -m scripts.mixed_kv.mixed_load --suite direct-check --io direct --offered-gib-s 0.14 --corpus "$out/pilot-corpus" --output "$out/pilot/direct-low"
python3 -m scripts.mixed_kv.summarize "$out/pilot" --output "$out/pilot-summary" --full
~~~

## 四种方案的混跑对照

这一组实验使用 `paper_comparison.py`，在同一台 910B2 的文件到 DRAM 再到 HBM 路径上执行。它复用 SuperLoad 的原生文件布局、AscendCL 搬运和 HostCache，只改造请求回放及调度。

- Strata 机制使用 page-first 布局，将同一到达批次的前缀请求合成一次去重后的页集合。`strata-io` 使用相同布局和 FIFO，每次服务一个请求。
- Bidaw 机制在早到达的前缀批次和已到达的稀疏任务中选择请求，优先服务 DRAM 全命中请求，再按等待时间与缺页字节之比选择。前缀批次全部回载就绪后才将该批回复长度发布给复用距离估计。`native-lru` 使用相同的 24 页主机缓存，由 FIFO 调度和 LRU 淘汰。
- SolidAttention 机制使用 token-wise K/V 交织布局，从上一已服务的同一稀疏会话预测至多 6 页，异步读入主机内存。只有一个预取任务可在途，命中的页经相同 H2D 通道到达 HBM。`solid-io` 保留交织布局和 FIFO。
- 各方案共用一个串行按需加载通道，固定 128 MiB 按需中转窗口、48 MiB 缓存与预取预算以及 4 GiB 的 HBM 回读空间。三种布局的数据内容相同。种子和到达序列对齐，Buffered 与 Direct 的名义混跑输入均为 0.28 GiB/s。

在源码快照中生成三种布局。归档保留逐请求时间戳及文件读、预取和 H2D 计数。

~~~bash
python3 -m superload build-corpus "$out/corpus"
for file in "$out/corpus/"*.bin; do cat "$file" >/dev/null; done
python3 -m scripts.mixed_kv.paper_comparison --suite smoke --seeds 42 --corpus "$out/corpus" --output "$out/mechanisms/hbm-smoke"
python3 -m scripts.mixed_kv.paper_comparison --corpus "$out/corpus" --output "$out/mechanisms/buffered"
python3 -m scripts.mixed_kv.paper_comparison --suite direct --io direct --corpus "$out/corpus" --output "$out/mechanisms/direct"
tar -czf "$out/mechanism-results.tar.gz" -C "$out/mechanisms" hbm-smoke buffered direct
python3 -m scripts.mixed_kv.summarize_mechanisms "$out/mechanism-results.tar.gz" --output "$out/mechanism-summary"
~~~

## Strata 排队诊断

复用上一组生成的 page-first 文件。基线把同批前缀的唯一页一起读入主机并以一条 H2D 命令搬运。诊断比较名义速率匹配的稀疏独跑和混跑，并用种子对齐稀疏请求的页序列。对每轮最慢约 10% 的稀疏请求，将开始前的等待与前序文件读取和 H2D 时间区间求交集。需要对比固定分片和读写重叠时，可用同一入口的 `solve` 和 `pipeline` 批次。

~~~bash
python3 -m scripts.mixed_kv.strata_yield --suite diagnose --io buffered --rate 0.16 \
  --corpus "$out/corpus" --output "$out/diagnose-buffered"
python3 -m scripts.mixed_kv.strata_yield --suite diagnose --io direct --rate 0.16 \
  --corpus "$out/corpus" --output "$out/diagnose-direct"
python3 -m scripts.mixed_kv.strata_yield --suite diagnose-low --io direct --rate 0.10 \
  --corpus "$out/corpus" --output "$out/diagnose-direct-low"
tar -czf "$out/strata-diagnosis-results.tar.gz" -C "$out" \
  diagnose-buffered diagnose-direct diagnose-direct-low
python3 -m scripts.mixed_kv.summarize_strata_yield "$out/strata-diagnosis-results.tar.gz" \
  --output "$out/strata-diagnosis-summary"
~~~

## 联合切分与四种方案的混跑对照

`joint` 在同一个去重后的前缀批和一个已到达的稀疏请求之间选择文件读区间和 H2D 切点。调度器根据分段耗时和各请求的最后一页预测就绪时间，比较相对独跑耗时的最大放大倍数。读线程和同步 H2D 可以并行，双方使用同一个 128 MiB 中转窗口中互不重叠的范围。

同一批次包含 LRU、Strata、Bidaw、SolidAttention 和联合切分。每轮回放 12 批前缀请求与 36 个稀疏请求，沿用排队诊断的名义速率。五个方案在同一 I/O 模式和种子下使用相同的到达时刻及页 ID。联合切分的读取和 H2D 耗时按 I/O 模式单独校准，校准在计时之外。

[联合切分的五方案测量记录](../report/results/joint-four-baselines/README.md) 包含汇总数据和可重算的原始归档。

~~~bash
for file in "$out/corpus/"*.bin; do cat "$file" >/dev/null; done
python3 -m scripts.mixed_kv.paper_comparison --suite joint --scale 2 --io buffered \
  --offered-gib-s 0.16 --corpus "$out/corpus" --output "$out/joint/buffered"
python3 -m scripts.mixed_kv.paper_comparison --suite joint --scale 2 --io direct \
  --offered-gib-s 0.10 --corpus "$out/corpus" --output "$out/joint/direct"
tar -czf "$out/joint-results.tar.gz" -C "$out/joint" buffered direct
python3 -m scripts.mixed_kv.summarize_mechanisms --suite joint "$out/joint-results.tar.gz" \
  --output "$out/joint-summary"
~~~
