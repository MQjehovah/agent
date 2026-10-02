---
name: frontend-ui-engineering
description: Component architecture and UI development patterns. Use when building or modifying user-facing interfaces.
---

# 前端 UI 工程

## Overview

好的 UI 工程 = 可维护的组件架构 + 一致的设计系统 + 正确的状态管理 + 无障碍体验。组件是 UI 的原子单位，设计系统是确保一致性的契约。

## When to Use

- 构建新页面或新功能界面时
- 重构现有 UI 组件时
- 建立或扩建设计系统时
- 需要确保界面无障碍合规时

- NOT for：纯后端功能（没有用户界面）
- NOT for：自动化测试脚本的编写

## Core Process

1. **组件设计** — 遵循单一职责原则，每个组件只做一件事。组件应可组合（composition over configuration），小而专而非大而全
2. **设计系统（Design Tokens）** — 将颜色、间距、字体、阴影等视觉基础抽象为 Design Tokens，构建可复用的组件库。不允许硬编码视觉值
3. **状态管理** — 区分 UI 状态（加载、空态、错误、边界情况）和业务数据状态。本地状态归组件，全局状态归 store
4. **响应式设计** — Mobile First，使用 CSS Grid / Flexbox 自适应性布局。定义明确的断点（breakpoints）
5. **无障碍（WCAG 2.1 AA）** — 键盘导航（所有交互元素可键盘到达）、屏幕阅读器支持（ARIA labels、语义化 HTML）、色彩对比度 >= 4.5:1

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "这个样式就这一次，不抽 token 了" | 每一个硬编码值都是未来的技术债 |
| "用户不会用键盘操作" | 很多残障用户依赖键盘导航，而且键盘导航通常是 power user 的习惯 |
| "先把功能做出来，UI 重构再说" | 没有组件化的 UI 重构等于重写 |
| "色彩对比度差一点没关系" | 全球约 8% 的男性有色觉障碍，达不到 4.5:1 对比度他们看不清 |

## Red Flags

- 组件 props 超过 10 个（职责不单一）
- 页面中重复出现相同的样式代码片段
- 无法用 Tab 键遍历表单或导航
- 硬编码的颜色值散落在各处 CSS 中
- 组件内部直接调用 API 或操作全局状态
- 没有处理加载态、空态、错误态的组件

## Verification

- [ ] 组件遵循单一职责，props 数量合理
- [ ] 所有视觉值使用 Design Tokens，无硬编码
- [ ] 页面 / 组件在移动端和桌面端均正常显示
- [ ] 键盘可导航所有交互元素（Tab / Enter / Escape）
- [ ] 色彩对比度 >= 4.5:1（可用浏览器工具检测）
- [ ] 所有图片和图标有合适的 alt 或 ARIA label
- [ ] 组件已覆盖加载态、空态、错误态、边界态
