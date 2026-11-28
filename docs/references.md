# 相关工作与参考文献

## Agent 会话与生产负载

ReAct 将推理与工具调用交替组织。Reflexion 把对任务反馈的反思写入记忆，供后续尝试复用。Parrot 则从服务端建模一次应用任务内多次 LLM 调用的依赖关系。 [ReAct][1] · [Reflexion][2] · [Parrot][3]

CachedAttention 研究如何把多轮会话的历史 KV 保留在分层缓存中。生产环境的轨迹研究观察到复用时间和概率随请求类型而变化。Mooncake 将存储容量用于保存历史 KV，本项目从其公开 Tool & Agent 请求轨迹中构造回载请求对。 [CachedAttention][4] · [KVCache Cache in the Wild][5] · [Mooncake][6] · [公开轨迹](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)

1. Shunyu Yao et al. ICLR 2023. [*ReAct: Synergizing Reasoning and Acting in Language Models*][1]
2. Noah Shinn et al. NeurIPS 2023. [*Reflexion: Language Agents with Verbal Reinforcement Learning*][2]
3. Chaofan Lin et al. OSDI 2024. [*Parrot: Efficient Serving of LLM-based Applications with Semantic Variable*][3]
4. Bin Gao et al. USENIX ATC 2024. [*Cost-Efficient Large Language Model Serving for Multi-turn Conversations with CachedAttention*][4]
5. Jiahao Wang et al. USENIX ATC 2025. [*KVCache Cache in the Wild: Characterizing and Optimizing KVCache Cache at a Large Cloud Provider*][5]
6. Ruoyu Qin et al. FAST 2025. [*Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture for Serving LLM Chatbot*][6]

## KV 管理与前缀复用

PagedAttention 通过分页组织与共享 KV，SGLang 的 RadixAttention 复用请求间的共同前缀，Prompt Cache 则显式描述可复用的提示词片段。它们分别从设备内存管理、前缀索引和提示词结构扩大 KV 的复用机会。 [PagedAttention][7] · [SGLang][8] · [Prompt Cache][9]

CacheGen 用压缩和流式传输降低加载 KV 的开销。CacheBlend 通过选择性重算复用 RAG 文档片段的 KV，CacheSlide 研究 Agent 提示词中固定片段换位后的复用。命中范围、传输体积和重算量共同决定一次复用需要完成多少工作。 [CacheGen][10] · [CacheBlend][11] · [CacheSlide][12]

7. Woosuk Kwon et al. SOSP 2023. [*Efficient Memory Management for Large Language Model Serving with PagedAttention*][7]
8. Lianmin Zheng et al. NeurIPS 2024. [*SGLang: Efficient Execution of Structured Language Model Programs*][8]
9. In Gim et al. MLSys 2024. [*Prompt Cache: Modular Attention Reuse for Low-Latency Inference*][9]
10. Yuhan Liu et al. SIGCOMM 2024. [*CacheGen: KV Cache Compression and Streaming for Fast Large Language Model Serving*][10]
11. Jiayi Yao et al. EuroSys 2025. [*CacheBlend: Fast Large Language Model Serving for RAG with Cached Knowledge Fusion*][11]
12. Yang Liu et al. FAST 2026. [*CacheSlide: Unlocking Cross Position-Aware KV Cache Reuse for Accelerating LLM Serving*][12]

## 分层存储与设备搬运

FlexGen 在 GPU、CPU 与磁盘之间放置推理张量。InfiniGen 从主机内存选择性预取解码需要的 KV。Strata 为分层上下文缓存设计 GPU 辅助 I/O 和缓存感知调度。DirectKV 允许 GPU 内核直接访问驻留在主机内存的 KV。 [FlexGen][13] · [InfiniGen][14] · [Strata][15] · [DirectKV][16]

在更通用的加速器存储系统中，G10 研究跨 GPU 内存、主机内存和 SSD 的张量迁移，BaM 研究由 GPU 发起的细粒度存储访问。这些工作表明，数据布局、访问粒度与设备间的数据路径会影响搬运效率。 [G10][17] · [BaM][18]

13. Ying Sheng et al. ICML 2023. [*FlexGen: High-Throughput Generative Inference of Large Language Models with a Single GPU*][13]
14. Wonbeom Lee et al. OSDI 2024. [*InfiniGen: Efficient Generative Inference of Large Language Models with Dynamic KV Cache Management*][14]
15. Zhiqiang Xie et al. OSDI 2026. [*Strata: Hierarchical Context Caching for Long Context Language Model Serving*][15]
16. Shutian Luo and Haiying Shen. OSDI 2026. [*No Buffer, No Bottleneck: Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs*][16]
17. Haoyang Zhang et al. MICRO 2023. [*G10: Enabling An Efficient Unified GPU Memory and Storage Architecture with Smart Tensor Migrations*][17]
18. Zaid Qureshi et al. ASPLOS 2023. [*GPU-Initiated On-Demand High-Throughput Storage Access in the BaM System Architecture*][18]

## 请求调度与队首阻塞

Orca 和 FastServe 在推理迭代边界作调度，其中 FastServe 支持迭代级抢占。Sarathi-Serve 将 prefill 切块，以便新请求和在途解码共享批次。Llumnix 通过实例间重调度应对请求差异。更早的 Shinjuku 在微秒级 CPU 服务中研究过切换在途任务如何缓解队首阻塞。 [Orca][19] · [Sarathi-Serve][20] · [FastServe][21] · [Llumnix][22] · [Shinjuku][23]

Bidaw 将 KV 回载开销纳入交互式推理调度，Libra 通过块级 KV 传输支持跨实例的微请求执行。本项目在昇腾的本机回载路径上对照了 Bidaw 的待发请求排序、Strata 的存储布局和合并读取、Libra 的块级搬运流水。 [Bidaw][24] · [Strata][15] · [Libra][25] · [对照实现与实验协议](https://github.com/KimmoZAG/SuperLoad/blob/main/docs/comparison.md)

19. Gyeong-In Yu et al. OSDI 2022. [*Orca: A Distributed Serving System for Transformer-Based Generative Models*][19]
20. Amey Agrawal et al. OSDI 2024. [*Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve*][20]
21. Bingyang Wu et al. NSDI 2026. [*FastServe: Iteration-Level Preemptive Scheduling for Large Language Model Inference*][21]
22. Biao Sun et al. OSDI 2024. [*Llumnix: Dynamic Scheduling for Large Language Model Serving*][22]
23. Kostis Kaffes et al. NSDI 2019. [*Shinjuku: Preemptive Scheduling for μsecond-scale Tail Latency*][23]
24. Shipeng Hu et al. FAST 2026. [*Bidaw: Enhancing Key-Value Caching for Interactive LLM Serving via Bidirectional Computation–Storage Awareness*][24]
25. Chaoyi Ruan et al. NSDI 2026. [*Libra: Flexible Request Partitioning and Scheduling for Serving Unbalanced and Dynamic LLM Workloads*][25]

## 推理解耦与回载、重算的权衡

DistServe、Splitwise 和 ThunderServe 将 prefill 与 decode 放在不同资源上，推理阶段间需要交接 KV。SYMPHONY 解耦计算与 KV 存储，用预取提示和优先级管理降低远端取数进入关键路径的机会。 [DistServe][26] · [Splitwise][27] · [ThunderServe][28] · [SYMPHONY][29]

KVPR 在解码期间重算部分 KV，并与其余 KV 的搬运重叠，展示了在传输受限时以计算换传输的另一种做法。SuperLoad 报告中的回载与重算临界图采用 ThunderServe 附录 H 表 5 的 prefill 时间作为论文参考，统计回载时间跨过该参考值的配对比例。 [KVPR][30] · [ThunderServe 附录 H](https://proceedings.mlsys.org/paper_files/paper/2025/file/c2a0e26dd9ee7d57e92bb1c24b39659a-Paper-Conference.pdf)

26. Yinmin Zhong et al. OSDI 2024. [*DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving*][26]
27. Pratyush Patel et al. ISCA 2024. [*Splitwise: Efficient Generative LLM Inference Using Phase Splitting*][27]
28. Youhe Jiang et al. MLSys 2025. [*ThunderServe: High-performance and Cost-efficient LLM Serving in Cloud Environments*][28]
29. Saurabh Agarwal et al. NSDI 2026. [*SYMPHONY: Enabling Compute-Memory Disaggregation in LLM Serving Systems*][29]
30. Chaoyi Jiang et al. Findings of ACL 2025. [*KVPR: Efficient LLM Inference with I/O-Aware KV Cache Partial Recomputation*][30]

[1]: https://react-lm.github.io/
[2]: https://proceedings.neurips.cc/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html
[3]: https://www.usenix.org/conference/osdi24/presentation/lin-chaofan
[4]: https://www.usenix.org/conference/atc24/presentation/gao-bin-cost
[5]: https://www.usenix.org/conference/atc25/presentation/wang-jiahao
[6]: https://www.usenix.org/conference/fast25/presentation/qin
[7]: https://doi.org/10.1145/3600006.3613165
[8]: https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html
[9]: https://proceedings.mlsys.org/paper_files/paper/2024/hash/a66caa1703fe34705a4368c3014c1966-Abstract-Conference.html
[10]: https://doi.org/10.1145/3651890.3672274
[11]: https://doi.org/10.1145/3689031.3696098
[12]: https://www.usenix.org/conference/fast26/presentation/liu-yang
[13]: https://proceedings.mlr.press/v202/sheng23a.html
[14]: https://www.usenix.org/conference/osdi24/presentation/lee
[15]: https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang
[16]: https://www.usenix.org/conference/osdi26/presentation/luo
[17]: https://doi.org/10.1145/3613424.3614309
[18]: https://doi.org/10.1145/3575693.3575748
[19]: https://www.usenix.org/conference/osdi22/presentation/yu
[20]: https://www.usenix.org/conference/osdi24/presentation/agrawal
[21]: https://www.usenix.org/conference/nsdi26/presentation/wu-bingyang
[22]: https://www.usenix.org/conference/osdi24/presentation/sun-biao
[23]: https://www.usenix.org/conference/nsdi19/presentation/kaffes
[24]: https://www.usenix.org/conference/fast26/presentation/hu-shipeng
[25]: https://www.usenix.org/conference/nsdi26/presentation/ruan-libra
[26]: https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin
[27]: https://www.microsoft.com/en-us/research/publication/splitwise-efficient-generative-llm-inference-using-phase-splitting/
[28]: https://proceedings.mlsys.org/paper_files/paper/2025/hash/c2a0e26dd9ee7d57e92bb1c24b39659a-Abstract-Conference.html
[29]: https://www.usenix.org/conference/nsdi26/presentation/agarwal
[30]: https://aclanthology.org/2025.findings-acl.997/
