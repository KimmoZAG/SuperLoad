# Ascend 910B2 · 2026-11-26

每个 JSON 包含参数、逐轮概要、每请求延迟和每批 I/O 计数。数值来自单卡 Ascend 910B2 的 HBM 数据路径，源语料位于 Longhorn ext4。所有正式样本均使用语料种子 1729 和回放种子 42。样本的 HBM 校验数量记录在每轮的 `verified_pages` 中。

- `buffered/` 是预热后执行的主实验。每种配置三轮，每轮 48 个测量批次、4 个预热批次。
- `direct/` 是同一语料的 O_DIRECT 敏感性对照。每种配置两轮，每轮 8 个测量批次、1 个预热批次。
- `hixl-loopback.json` 是单设备建链记录。源主机窗口和目标 HBM 注册成功。CANN 给出 `Connect = 103900`，诊断为 `not support connect with self device`。跨机传输结果留空。

运行 `python3 report/build.py` 可从这些数据重新生成网页与汇总 JSON。 [参数、指标与论文机制](../../docs/experiment.md)
