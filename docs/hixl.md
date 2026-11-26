# HIXL 存储端和 NPU 端

代码使用公开 HIXL C++ API。存储进程把固定语料的所需 KV 页读入已注册的 `aclrtMallocHost` 窗口。轻量控制通道只传页 ID、窗口地址、长度和读计数。客户端通过 HIXL `TransferSync(READ)` 将数据拉到已经注册的 HBM 地址，然后才通知服务端复用窗口。Direct I/O 时存储端先读入对齐的 DRAM，再写入上述注册窗口，成本记录在控制/整理时间中。 [HIXL 内存与传输](https://www.hiascend.com/document/detail/en/CANNCommunityEdition/910/commlib/hixlug/llmcdv2_42_29.html)

## 编译和本地探针

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native -j 2
python3 scripts/probe_hixl_loopback.py --corpus data/kv --output runs/hixl-loopback.json
```

已分配的单张 910B2 上，双进程分别注册了源主机窗口和目标 HBM。两进程绑同一设备时，`Connect` 返回 `103900`，CANN 日志说明 `not support connect with self device`。此时脚本记录 `blocked`，没有传输样本。

## 双机复现

准备两个 HIXL 兼容的昇腾端点、可用的路由和厂商支持的数据链路。存储端也需要 CANN/ACL 可用，当前实现使用存储侧的逻辑 NPU 设备初始化 HIXL 和分配可注册主机内存。两侧从相同提交和种子产生语料，并各自编译桥接库。客户端只读取本地语料 manifest 以核对种子和形状。

配置本机地址及端口。两个节点从受控环境注入相同的 `SUPERLOAD_HIXL_KEY`，控制通道使用受信实验网段。主机端口用于 HIXL 建链和控制消息，数据链路以运行时日志及端口计数为准。若设备需要手工配置通信资源，可在两端分别传入 `--comm-res <local-config.json>`。 [HIXL Initialize 配置](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/commlib/hixlug/llmcrv2_42_25.html)

**存储端**

```bash
python3 -m superload serve-hixl --corpus data/kv --device 0 --io buffered \
  --bind "$STORAGE_IP" --engine "$STORAGE_IP:35481" --control-port 35482
```

**推理端**

```bash
python3 -m superload bench --scheme hixl --corpus data/kv --device 0 --io buffered \
  --local-engine "$CLIENT_IP" --control-host "$STORAGE_IP" --control-port 35482 \
  --profile interactive-prefix --waves 4 --warmup 1 --repeat 1 --verify all \
  --storage-label remote-ascend --run-label cross-host-smoke --output runs/hixl-smoke.json
```

服务进程每次处理一个客户端，客户端正常结束后服务进程也退出。重新启动服务，再按主实验设置测两种负载。正式比较前确认链路类型、有效带宽、端到端回读和服务端文件系统，分别采集文件读、控制/整理和 HIXL 传输耗时。单个 100 Gb/s 端口的裸线速约为 11.64 GiB/s，运行时确认该端口承载数据后可用作上限比较。
