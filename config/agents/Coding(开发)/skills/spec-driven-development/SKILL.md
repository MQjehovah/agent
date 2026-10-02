---
name: spec-driven-development
description: Writes a spec/PRD before any code. Use when starting a new feature, project, or significant change. Use when requirements are unclear.
---

# Spec-Driven Development

## Overview

规范驱动开发：先写规格说明，再写代码。这是防止范围蔓延、需求误解和返工的第一道防线。一份好的 PRD（产品需求文档）让开发有据可依，让评审有标准可循。**No spec, no code** — 没有规格就不写代码。

## When to Use

- 开始一个新功能、新项目或重大变更
- 需求不明确，需要结构化梳理
- 涉及多方协作，需要对齐理解
- 用户说"帮我做个 X"但没有具体描述
- NOT for 紧急修复（hotfix）或纯技术重构且不影响外部行为

## Core Process

### 1. 理解上下文

- 阅读现有代码、文档、相关 issue
- 了解用户痛点和使用场景
- 确定当前项目架构和约束

### 2. 编写 PRD

PRD 必须包含以下章节：

```
## 目标 (Objectives)
- 要解决什么问题？为什么要做？
- 成功指标是什么？

## 用户故事 (User Stories)
- 作为 <角色>，我想要 <功能>，以便 <价值>
- 每个故事独立可测试

## 验收标准 (Acceptance Criteria)
- Given/When/Then 格式
- 边界条件、错误场景

## 边界与约束 (Boundaries)
- 本次不做什么（显式排除）
- 技术/时间/资源限制

## 开放问题 (Open Questions)
- 需要进一步澄清的点
- 待决策事项
```

### 3. 用户评审确认

- 将 PRD 呈现给用户
- 逐节确认理解一致
- 更新开放问题
- **获得明确批准后方可进入编码阶段**

### 4. 编码阶段

- 以 PRD 为准绳
- 每个实现对照验收标准验证
- 发现 PRD 遗漏时，先更新 PRD 再改代码

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "需求很简单，直接写代码更省时间" | 简单需求的 spec 只需 5 分钟，却能省去 1 小时的返工。 |
| "用户自己也不知道要什么，写 spec 没用" | 正因如此才更需要 spec 来帮助用户理清思路。 |
| "先写一点代码看看效果，再改 spec" | 代码一旦写出就有惯性，很少有人会回头修正 spec。 |
| "PRD 太长没人读" | PRD 长度取决于复杂度，1 页也可以；关键是结构完整。 |
| "敏捷开发不需要详细文档" | 敏捷的"可工作的软件优于详尽文档"不等于没有文档。 |

## Red Flags

- 没有 PRD 就开始写代码
- PRD 缺少验收标准
- 用户没有明确确认 spec
- 编码过程中发现关键需求在 PRD 中未提及
- PRD 中的开放问题过多（>5 个）却没有解决计划
- PRD 未定义"本次不做什么"

## Verification

- [ ] PRD 已完成所有必要章节（目标、用户故事、验收标准、边界、开放问题）
- [ ] 每个验收标准采用 Given/When/Then 格式
- [ ] 用户已阅读并明确批准 PRD
- [ ] 所有开放问题已有明确的决策路径
- [ ] 编码过程中所有变更先更新 PRD 再改代码
- [ ] 最终实现通过了 PRD 中的所有验收标准
