---
name: using-agent-skills
description: Maps incoming work to the right skill workflow. Use when starting a session or deciding which skill applies.
---

# Using Agent Skills

## Overview

元技能：将用户的原始请求路由到最合适的工作流。Agent 拥有数十个专用技能（调试、规划、TDD、部署等），但只有正确触发才能生效。本技能定义了 6 个生命周期阶段以及每个阶段对应的技能映射关系，确保每次任务都有章可循。

## When to Use

- 会话开始时，确定当前请求属于哪个阶段
- 面对模糊请求时，用映射表选择正确技能
- 多步骤任务中，判断当前进度以切换技能
- NOT for 执行具体技术操作（应使用对应的子技能）

## Core Process

### 六阶段生命周期

```
DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP
```

**DEFINE** — 理解需求，澄清模糊目标
**PLAN** — 拆解任务，编写规格说明
**BUILD** — 实现代码/配置
**VERIFY** — 测试、检查、确保质量
**REVIEW** — 代码审查，接受反馈
**SHIP** — 部署、发布、完成

### Intent-to-Skill 路由表

| 用户说 | 对应阶段 | 应用的技能 |
|---|---|---|
| "build a feature" | DEFINE → PLAN → BUILD | spec-driven-development → planning → incremental-implementation + tdd |
| "fix a bug" | DEFINE → BUILD | debugging-and-error-recovery |
| "review this" | REVIEW | code-review-and-quality |
| "design a system" | DEFINE → PLAN | spec-driven-development |
| "add tests" | PLAN → BUILD | test-driven-development |
| "deploy" | SHIP | shipping-and-launch |
| "refactor this" | PLAN → BUILD → VERIFY | incremental-implementation + test-driven-development |
| "create a UI component" | BUILD | frontend-ui-engineering |
| "migrate X to Y" | PLAN → BUILD → VERIFY | deprecation-and-migration |
| "optimize performance" | DEFINE → BUILD → VERIFY | performance-optimization + observability-and-instrumentation |
| "improve security" | BUILD → VERIFY | security-and-hardening |
| "set up CI" | BUILD → SHIP | ci-cd-and-automation |
| "write docs" | SHIP | documentation-and-adrs |
| "requirements unclear" | DEFINE | interview-me 或 idea-refine |

### 使用流程

1. **捕获用户意图** — 解析自然语言，确定用户想做什么
2. **查路由表** — 找到匹配的技能和生命周期阶段
3. **确认** — 向用户简要说明计划使用的技能和步骤，获得确认
4. **加载技能** — 调用对应 SKILL.md，按步骤执行
5. **阶段推进** — 完成当前阶段后，检查是否需要切换到下一阶段的新技能
6. **循环** — 如果请求发生变化，重新执行步骤 1

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "我知道该怎么做，不需要查技能" | 技能文件存在的意义正是为了覆盖你没想到的边界情况。不查 = 遗漏。 |
| "这个请求很简单，直接做就行" | 简单 ≠ 不需要流程。跳过定义阶段是返工的第一来源。 |
| "先写代码再补文档也一样" | 顺序错误导致结构性缺陷，修复成本是前置设计的 10 倍。 |
| "反正最后都要改，规划不重要" | 规划的价值恰恰在于减少不必要的改动。 |

## Red Flags

- 跳过了 DEFINE 或 PLAN 阶段直接开始编码
- 用户说了模糊请求但没有使用 interview-me 或 idea-refine
- 一个请求同时涉及 3 个以上阶段但未分步执行
- 未向用户确认计划就开始执行
- 在 BUILD 中途发现需求理解有偏差

## Verification

- [ ] 已确定当前请求对应的生命周期阶段
- [ ] 已找到匹配的技能文件并加载
- [ ] 已向用户确认执行计划
- [ ] 当前阶段完成后，已明确下一步阶段和对应技能
- [ ] 请求发生变化时已重新路由
