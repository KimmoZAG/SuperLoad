# 实验复现与审计

正式测量使用报告清单固定的原始 ZIP 和测量提交。ZIP 包含回放计划、三种 I/O／缓冲配置的原始记录、HBM 全量回读结果和实现文件的 SHA-256。 [正式 Release](https://github.com/KimmoZAG/SuperLoad/releases/tag/results-superload-910b2-2026-11-28) · [发布清单](../report/comparison-results-release.json)

## 无需 NPU 的结果审计

从仓库根目录运行以下命令。入口下载固定的 Release 并核对 ZIP 校验值，从测量提交读取实现文件，检查计划、三种配置的覆盖范围、随机交错执行顺序和 HBM 回读记录，再从原始测量重算逐轮数据、两组 HBM 就绪 P95 和双请求吞吐。

```bash
python3 -m experiments audit --output runs/audit/published.json
```

已下载 ZIP 时可以指定 `--archive`。如果本地 Git 已有发布清单中的测量提交，审计也可离线执行。使用单独解压的源代码时，`--source` 可指定与发布校验值匹配的源码目录。

```bash
python3 -m experiments audit \
  --archive superload-910b2-2026-11-28.zip --output runs/audit/published.json
```

本批公开数据包含 1,152 个计时窗口、12 个全量回读窗口和 492 页 HBM 回读。审计检查这些记录与协议一致。重新执行逐页校验需要昇腾设备。

## 昇腾真机复跑

在 Linux 上安装 Ascend 驱动与 CANN，准备支持 `O_DIRECT` 的文件系统。将 `--device` 和 `--numa-node` 设置为当前机器的逻辑 NPU 编号及其 CPU NUMA 节点。以下示例对应原测量环境。

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m experiments reproduce --corpus data/reproduction --output runs/reproduction \
  --device 0 --numa-node 2 --storage-label longhorn-ext4
```

入口使用发布包中的 `plan.json` 和测量提交，沿用原始随机种子、固定分块轮转基线的传输粒度和重复次数。首次生成语料时需要约 7.3 GiB。随后运行 Buffered I/O 普通缓冲、Buffered I/O CANN 锁页缓冲和 Direct I/O 三种配置。基准计时结束后，入口选取最小与最大的请求对，分别在两种到达条件下清零目标 HBM、重新回载并逐字节校验。

每次复跑写入独立输出目录，保存原始结果、各配置日志、CANN 与 NPU 信息、源代码提交、实际命令和文件校验值。可用 `--pair-limit 2 --repeat 1` 检查运行链路。这样产生的是子集测量，完整复跑使用默认样本和重复次数。路径、设备与验证说明见 [完整实验协议](comparison.md)。

```bash
python3 -m experiments audit --results runs/reproduction
python3 -m pip install -r report/requirements.txt
python3 report/build.py --comparison-results runs/reproduction --output build/reproduction
```

需要单独重做 HBM 回读时，可在与测量一致的源码目录中运行以下命令，并将回读结果写到独立文件。

```bash
python3 -m experiments verify-hbm --results runs/reproduction --corpus data/reproduction \
  --device 0 --numa-node 2 --output runs/reproduction-hbm-check.json
```
