---
name: documentation-and-adrs
description: Architecture Decision Records and API docs. Use when making architectural decisions, changing APIs, or shipping features.
---

# 文档与架构决策记录

## Overview

文档是代码的说明书，ADR（架构决策记录）是技术演进的日记。好的文档记录 WHY 而非仅仅 WHAT，让后来者理解上下文而非猜测意图。

## When to Use

- 做出架构决策时（创建 ADR）
- 修改或新增 API 时同步更新文档
- 发布功能前确保文档完备
- 审核代码时发现缺少注释或文档

- NOT for：替代干净的代码（代码本身应自解释）
- NOT for：重复代码逻辑的注释

## Core Process

1. **ADR：记录决策** — 每次重大架构决策创建一个 ADR 文件，格式：Title → Context（背景）→ Decision（决策）→ Consequences（影响）→ Status（状态：Proposed / Accepted / Deprecated / Superseded）
2. **API 文档：从代码生成** — 优先使用文档生成工具（如 OpenAPI/Swagger、JSDoc、Sphinx），维护代码与文档的同步
3. **内联文档：解释 WHY 而非 WHAT** — 注释解释为什么选择某方案，而非描述代码在做什么
4. **文档审查**：将文档可读性纳入 Code Review 标准

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "下次再补文档" | 下次永远不存在，写代码时一并写文档 |
| "代码自解释，不需要注释" | 代码解释 WHAT，注释解释 WHY |
| "ADR 浪费时间，我们直接写代码" | 没有 ADR 的架构决策 3 个月后被遗忘，新人反复踩坑 |
| "API 变了但文档忘更新了" | 这就是用工具从代码生成文档的原因 |

## Red Flags

- PR 包含架构变更但无 ADR
- 文档和实际代码行为不一致
- 注释只解释了代码的字面意思（"给变量 x 赋值"）
- 没有 README 或 README 已过时半年以上

## Verification

- [ ] 所有 API 变更已同步更新文档
- [ ] 架构决策已创建或更新 ADR（含 Context + Decision + Consequences）
- [ ] 注释只出现在需要解释 WHY 的地方
- [ ] 文档可由工具从代码生成，无需手动维护
- [ ] ADR 状态已正确标记（Proposed / Accepted / Deprecated / Superseded）
