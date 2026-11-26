# RoCE RC 存储到 HBM

本轮可运行的网络路径是 **存储进程 → RoCE RC → 客户端 DRAM → NPU HBM**。两个进程通过 TCP 控制连接交换 QP 地址、页号和读取计数。KV payload 通过 libibverbs 的 RC SEND/RECV 传输。存储侧先从文件读入对齐缓冲，随后填充两个注册发送槽。客户端预投递两个接收，轮转接收槽拼回普通 DRAM，最后通过 AscendCL 同步搬入 HBM。每个请求校验接收完成类型和字节数，回放通过 NPU 回读对比参考字节。

同节点测量使用 mlx5_2、端口 1、RoCE v2 GID 索引 5 和 1024 B 路径 MTU。两个本地进程使用同一物理端口、不同 RC QP。每个进程使用一个 32 KiB MR，分为两个 16 KiB 槽，以适应工作区的 64 KiB memlock。所有配置使用同一套文件布局和原生 H2D。计时中的 RC 段包含应用注册窗口的整理和 completion 等待。跨机时延、交换机拥塞、链路有效带宽和直达设备内存需要单独测量。 [rdma-core RC 示例](https://github.com/linux-rdma/rdma-core/blob/master/libibverbs/man/ibv_rc_pingpong.1)

## 同节点复跑

要求 Linux、libibverbs 开发头文件、可访问的 RoCE 网口和客户端 CANN。先检查 GID 索引是否与当前端口上的 RoCE v2 路径匹配。

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native -j 2
python3 scripts/run_matrix.py --corpus data/kv --only-roce \
  --hca mlx5_2 --gid-index 5 --waves 2 --warmup 0 --repeat 1 --verify all \
  --run-label roce-full-hbm --output runs/roce-smoke
```

编排器启用回环控制监听和临时端口，每种负载启动全新的服务进程。使用 `--roce` 可把同节点 RoCE 加入五方案矩阵。

## 独立端点

两个获授权的端点各自编译桥接库。存储端可运行在有 RoCE 网口的纯 CPU 节点。服务端使用固定语料，客户端的 manifest 需要匹配。对应端口各自选择 HCA 与 GID 索引，并使用相同的 MR 大小和 I/O 模式。双方从同一个受控环境注入 `SUPERLOAD_ROCE_KEY`，控制连接限于受信实验网络。

```bash
# Storage host
python3 -m superload serve-roce --corpus data/kv --bind "$STORAGE_IP" \
  --hca "$STORAGE_HCA" --gid-index "$STORAGE_GID" --control-port 35482

# NPU host
python3 -m superload bench --corpus data/kv --scheme roce --profile interactive-prefix \
  --control-host "$STORAGE_IP" --hca "$CLIENT_HCA" --gid-index "$CLIENT_GID" \
  --waves 4 --warmup 1 --repeat 1 --verify all --output runs/roce-cross-host.json
```

服务进程处理一个客户端。新的负载由新服务进程接收。跨机性能测试需要同时记录两端拓扑、端口计数和文件系统。
