---
name: observability-and-instrumentation
description: Structured logging, metrics, and tracing. Use when adding telemetry or shipping anything that runs in production.
---

# 可观测性与埋点

## Overview

可观测性让你在不修改代码的情况下理解系统内部状态。三大支柱：结构化日志、RED 指标、分布式追踪。目标是在用户发现问题之前你已知道问题所在。

## When to Use

- 添加新的服务或端点时
- 排查线上问题时发现缺少关键指标
- 设计告警规则时
- 发布新功能到生产环境前

- NOT for：替代单元测试或集成测试
- NOT for：无差别地记录所有信息（小心 PII 和成本）

## Core Process

1. **结构化日志** — 使用 JSON 格式，机器可解析，包含 timestamp、level、service、traceId、message、context 字段。避免字符串拼接和不可解析的日志
2. **RED 指标** — Rate（每秒请求量）、Errors（错误率/绝对数）、Duration（请求耗时分布，p50/p95/p99）。每个服务端点都必须有 RED
3. **分布式追踪（OpenTelemetry）** — 在服务边界传递 trace context，支持跨服务的请求链路追踪。关键路径必须设 Span
4. **症状驱动告警** — 告警规则面向用户可见的症状（如登录失败率 > 5%），而非内部指标（如 CPU > 80%）。告警必须可行动，否则是噪音

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "先上线，日志以后再加" | 没有日志的线上问题是黑盒调试，事后补日志需要重新部署 |
| "打印足够的 info 就行了" | 非结构化的文本日志无法被工具自动聚合和分析 |
| "监控 CPU 就够了" | 高 CPU 不一定影响用户，低 CPU 也可能服务已挂 |
| "告警越多越安全" | 告警疲劳导致重要告警被忽略 |

## Red Flags

- 调用链路上某环缺失 RED 指标
- Error 日志只记录 error message 无 stack trace 和 context
- 相同的错误信息出现在多个不相关的组件中无法聚合
- 告警规则从未被触发过（死规则）或一直处于触发状态（噪音）
- 没有 traceId 无法关联一次请求的完整日志

## Verification

- [ ] 每个端点/操作都有 Rate/Errors/Duration 指标
- [ ] 日志是 JSON 结构化格式，包含 traceId
- [ ] OpenTelemetry 已集成，关键服务间 trace 串联
- [ ] 告警规则基于用户症状，每个告警都有对应 runbook
- [ ] 无 PII 信息写入日志
- [ ] 日志级别正确（ERROR/真实错误，WARN/异常但可恢复，INFO/重要状态变化）
