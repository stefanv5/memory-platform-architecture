# 竞品全景与 ContextDB 架构（Competitive Landscape）

五家竞品（Hindsight / Mem0 / Zep / MemOS / Cognee）的全景对比，以及在"团队/公司级 Agent 上下文数据库（contextDB）"目标下按 7 项需求（R1 团队级 / R2 自动沉淀 / R3 高效召回 / R4 降 token / R5 图谱导航 / R6 正确性 / R7 RSI）的逐项深拆与自研架构建议。

- [01 Hindsight 五方对比](01-hindsight-vs-competitors.md) — 核心引擎机制（Retain/Recall/Consolidation/Reflect）、五竞品架构对比表、基准数字与口径警告、GitHub 生态数据
- [02 ContextDB 需求深拆与架构建议](02-contextdb-requirements-and-architecture.md) — 能力矩阵（R1–R7）、逐竞品按需求深拆、跨竞品缺口分析、自研蓝图（Fork Hindsight + RSI 闭环 + 权限内联 SQL）
- [03 RSI 深度调研：沉淀之后数据会怎样](03-rsi-memory-evolution-deep-dive.md) — L0–L5 演化分级框架、六产品触发器/执行者/守护逐家核查（含两处评级修正）、学界自进化算法菜单（Generative Agents/A-MEM/AWM/ExpeL/Mem-α 等）、RSI 六环闭环的算法落位
- [04 Hindsight vs Cognee 数据流对比](04-dataflow-hindsight-vs-cognee.md) — 代码级逐阶段走读（v0.10.1 vs v1.6.0）：摄入/巩固/查询/后台四条路径的调用链与 file:line 锚点、流水线式智能 vs 写入式智能的结构差异、两侧"值得抄"机制清单与反直觉发现
- [05 Cognee vs Hindsight 当前架构与实现对比](05-code-architecture-cognee-vs-hindsight.md) — 先分别介绍接入、数据模型、存储、检索与后台演进，再比较性能成本、事务、扩容、权限和扩展边界；包含对 04 若干结论的源码修订与同条件验证方案。当前实现判断优先参考本篇。

## 证据等级

文档内标注区分四类来源：`[代码]`（子代理逐行验证）、`[官方]`（竞品官方文档/README）、`[论文]`（arXiv）、`[自报]`（厂商营销口径）。各家基准 harness 互不相同，跨厂商数字仅作方向性参考；关键口径警告见 01 文档 §4.2 与 02 文档 §1（OmniMemEval 的 full-context 发现）。

## 与 Cognee 分析的关系

`docs/cognee/` 是对单一竞品（Cognee）的源码级架构研究；本目录是五家横向对比 + 目标架构设计。02 文档 §3.5 的 Cognee 结论与本仓库 cognee 分析可互为印证。
