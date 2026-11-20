# 归档

做完的、或者被后来方案取代的计划稿与复盘稿。留档是为了回答「当时为什么这么改」，
**不随代码更新**——里面的行号、模块路径、判据项数与工具名都停在写完的那一天。

现在的设计与判据看 `../design.md`；每个决定的来龙去脉看 `../history.md`；
读数与失败模式看 `../review.md`；工具清单看 `../tools.md`；
角色职责看 `../ten-agent-design.md`。

| 文件 | 写于 | 是什么 | 现在由哪里接替 |
| --- | --- | --- | --- |
| `VSAgent_MultiAgent_Refactor_Design.md` | 2026-11-12 | 多 Agent 重构的总设计稿，按 Phase 与工具族写 | `../design.md` 十二、`../ten-agent-design.md` |
| `progress.md` | 2026-11-13～14 | 重构的逐阶段验收与 P-01～P-84 问题台账 | `../history.md` 十一、`../review.md` 六 |
| `subagent-matrix.md` | 2026-11-13 | 子 agent 依赖与限制的总览表 | `../ten-agent-design.md` 十三 |
| `pocket-issues.md` | 2026-11-14 | 口袋子会话的三类行为问题：不检索资料库、bash 调用过多、口袋覆盖不全 | `../review.md` 六 |
| `session-report-issues.md` | 2026-11-14 | 会话报告管线与运行时预算的四类问题 | `../review.md` 六 |
| `retrieval-overreliance.md` | 2026-11-14 | 检索结果被当成事实底座的跨角色问题 | `../review.md` 六 |
| `search-integration-plan.md` | 2026-11-14 | 搜索增强推理（SAR）的改造计划与实施记录 | `../design.md` §5.2 的 `search_reasoning/` 一行 |
| `runtime-fixes-plan.md` | 2026-11-11 | 运行期问题的第一轮修复计划 | `../history.md` 一、七 |
| `runtime-fixes-plan-2.md` | 2026-11-11 | 运行期问题的第二轮修复计划 | `../history.md` 一、七 |
| `knowledge-center-plan.md` | 2026-11-08 | 网页端知识中心 M0–M6 的改造计划 | `../design.md` §6.7、§8.1 |
| `intent-rework-plan.md` | 2026-11-19 | 意图走线重构的迁移计划：目标、边界、四个阶段与完成判据 | `../intent-flow.md` |

归档里的相对链接（如 `pocket-issues.md` 指向 `retrieval-overreliance.md`）仍然有效，
因为它们被一起移了进来。
