---
name: browser-testing-with-devtools
description: Chrome DevTools for live runtime data. Use when building or debugging anything that runs in a browser.
---

# 浏览器调试与 DevTools

## 概述
利用 Chrome DevTools 在运行时查看和操作 DOM、样式、网络、性能数据，快速定位前端问题。即使没有源码访问权限，也能通过 DevTools 理解页面行为。

## 何时使用
- 页面布局/样式异常
- 网络请求失败或数据不符
- JavaScript 运行时错误
- 性能瓶颈（加载慢、卡顿）
- 浏览器特有行为（跨域、存储、缓存）
- **不适用于**：后端 API 逻辑调试、纯 CLI 或 Node.js 程序

## 核心流程

### 1. 复现（REPRODUCE）
- 在 Chrome 中打开页面，打开 DevTools（F12 / Ctrl+Shift+I）
- **保持 DevTools 打开状态下刷新**，避免错过初始化阶段的请求和错误
- 记录复现步骤和屏幕截图

### 2. 检查（INSPECT）
- **Console**：查看错误、警告、console.log 输出。右键可保存日志
- **Network**：过滤请求类型（XHR/Fetch/Doc），查看请求/响应体、状态码、时序
- **Elements**：查看 DOM 结构和应用的计算样式。检查盒模型、伪类、事件监听
- **Sources**：设置断点、单步执行、查看调用栈和作用域变量

### 3. 诊断（DIAGNOSE）
- **问题假设**：根据 Inspection 结果提出根因假设
- **实验验证**：
  - 在 Console 中直接调用函数或修改变量
  - 在 Elements > Styles 中临时修改 CSS 以验证样式假设
  - 在 Network 中右键请求 → Copy as Fetch 重放请求
  - 使用 Overrides 模拟响应数据
- **Performance**：录制性能快照，分析 FPS、Layout Shift、Long Task
- **Coverage**：检查未使用的 CSS/JS

### 4. 修复（FIX）
- 在源码中实施修复，而不是停留在 DevTools 的临时修改
- 使用 DevTools 验证修复效果——清除缓存后硬刷新（Ctrl+F5）

### 5. 验证（VERIFY）
- 使用同一复现步骤确认问题不再出现
- 截取修复前后的视觉对比图（Screenshot）
- 检查有无副作用：其他页面或功能是否受影响
- 对有性能影响的修复，重新录制 Performance 面板确认改善

## 常用工具快速参考

| 面板 | 适用场景 | 关键技巧 |
|---|---|---|
| Console | JS 错误、日志 | `$0` 引用当前选中元素；`console.table()` 格式化数据 |
| Network | 请求失败/慢 | 勾选"Disable cache"；Timing 面板看瓶颈阶段 |
| Elements | DOM/样式 | 右键 → Break on → attribute modifications |
| Sources | JS 逻辑 | 条件断点；"Blackbox script" 忽略第三方库 |
| Performance | 卡顿/加载慢 | 点击"Record"后执行操作；分析 Main 线程火焰图 |
| Application | 存储/Cookie | 手动修改 IndexedDB/LocalStorage 进行测试 |
| Lighthouse | 综合优化 | 生成报告后关注"Opportunities"和"Diagnostics" |

## 常见错误归因

| 合理化 | 现实 |
|---|---|
| "本地没问题，一定是环境问题" | 先对比网络请求和 Console 错误，环境和代码都可能有问题 |
| "清缓存就好了" | 缓存问题反映的是代码中没有正确处理版本控制 |
| "Chrome 能跑就行" | 不同浏览器（Safari、Firefox）有不同行为，需要交叉测试 |

## 红色警告
- ❌ 仅在 DevTools 中修改而不改动源码
- ❌ 无视 Console 中的警告（warning 往往是潜在 bug）
- ❌ Network 面板显示 4xx/5xx 但不查看响应体
- ❌ 不检查手机模拟模式下的差异

## 验证清单
- [ ] Console 无报错和未处理的 warning
- [ ] Network 中关键请求返回 2xx，数据符合预期
- [ ] Elements 中 DOM 结构与设计一致，样式计算正确
- [ ] 关键交互流程完整跑通
- [ ] 修复前/后的屏幕截图已对比
- [ ] 移动端模拟模式无布局断裂
- [ ] Performance 录制无显著 Long Task 或 Forced Reflow
