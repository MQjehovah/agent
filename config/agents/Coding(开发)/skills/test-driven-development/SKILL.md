---
name: test-driven-development
description: Drives development with tests. Use when implementing any logic, fixing any bug, or changing any behavior.
---

# 测试驱动开发

## Overview
测试不是开发后的"附加步骤"，而是驱动开发的核心引擎。先写测试定义"正确"的含义，再写代码使其通过。红-绿-重构循环确保每一步都有可验证的完成标准。

## When to Use
- 实现任何新逻辑（函数、类、API 端点）
- 修复任何 bug（先写失败测试证明 bug 存在）
- 重构任何已有代码（测试是安全网）
- 更改任何行为
- NOT for 纯配置变更、复制粘贴操作、临时调试脚本
- NOT for 没有明确可测行为的 UI 布局微调

## Core Process

1. **Red** — 写一个描述期望行为的测试，运行它。测试必须失败（证明它确实在测试新行为）
2. **Green** — 写最少量的代码让测试通过。可以 dirty，可以 hardcode，只要通过
3. **Refactor** — 在测试保护下重构代码到可读、可维护的状态
4. **重复** — 进入下一个行为

### Prove-It Pattern（用于 Bug 修复）
1. 写一个暴露 bug 的测试（Red）
2. 确认测试因预期原因失败（不是测试写错了）
3. 修复代码使测试通过（Green）
4. 确保不破坏已有测试

### 测试金字塔
- **80% 单元测试** — 快速、隔离、覆盖业务逻辑
- **15% 集成测试** — 验证模块间交互、I/O 边界
- **5% E2E 测试** — 关键路径的端到端验证

### 原则
- **DAMP over DRY** — 测试中可读性 > 去重。适度的重复让测试独立、易理解
- **Arrange-Act-Assert** — 每个测试三段式：准备→执行→断言
- **不要 mock 一切** — mock 外部 I/O（网络、数据库、文件系统），但不要 mock 项目内的业务逻辑。测试真实交互，而非 mock 的实现
- **一个 test case 验证一个行为** — 如果一个 test case 有多个 assert，确保它们只测试一个概念

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "我先把功能写完，再补测试" | 你不会补的 |
| "这个太简单了，不用测试" | 简单代码出 bug 的频率和复杂代码一样高 |
| "先提交再写测试，不耽误时间" | 没有测试保护的提交是技术债 |
| "测试重构时都得重写，浪费时间" | 没有测试的重构叫"猜"，有测试的重构叫"改" |
| "覆盖到就行，测试质量不重要" | 脆弱的测试比没有测试更可怕——它在给你假信心 |
| "mock 掉所有依赖测试才干净" | mock 了实现细节的测试只测试了 mock 本身 |

## Red Flags
- 测试失败时第一反应是"改测试"而不是"改代码"
- 测试文件比被测试代码行数还少
- 一个测试方法名里出现了 "and"
- 测试中有条件语句（if/for/while）
- 测试依赖特定执行顺序
- 提交信息里写 "add tests later"

## Verification
- [ ] 每个新增功能都有对应的失败测试（Red）
- [ ] 每个 bug 修复前都有暴露 bug 的测试
- [ ] 测试在 CI 中运行并通过
- [ ] 测试名称清晰描述了期望行为（Given_When_Then 或类似风格）
- [ ] 无测试使用 sleep/延时作为同步手段
