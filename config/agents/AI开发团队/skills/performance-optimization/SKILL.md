---
name: performance-optimization
description: Measure-first performance approach. Use when performance requirements exist or you suspect regressions.
---

# 性能优化

## 概述
以数据驱动的性能优化方法论。先测量，再优化——不要猜测性能瓶颈。关注 Core Web Vitals 和用户体验指标。

## 何时使用
- 存在明确的性能需求或 SLI/SLO
- 怀疑代码变更导致性能退化
- 页面加载慢、交互卡顿或响应时间超标
- NOT for 无测量依据的"预感式"优化

## 核心流程
1. **测量**：在优化前建立基线——性能问题只有测量后才能确认
2. **设定目标**：Core Web Vitals — LCP < 2.5s、CLS < 0.1、INP < 200ms
3. **Profile 定位瓶颈**：前端用 Chrome DevTools Performance 面板；后端用 py-spy、cProfile 或 async-profiler
4. **N+1 查询检测**：检查数据库查询日志，使用 Django Debug Toolbar 或 Rails Bullet gem
5. **Bundle 分析**：使用 webpack-bundle-analyzer 或 vite-bundle-visualizer 检查冗余依赖
6. **缓存策略**：合理使用 HTTP 缓存（Cache-Control、ETag）、内存缓存（Redis/Memcached）、CDN 缓存
7. **验证改善**：优化后重新测量，确认有正向改善

## 常见自我合理化
| 合理化 | 现实 |
|---|---|
| 这段代码肯定慢，先优化再说 | **过早优化是万恶之源**——没有测量就不知道瓶颈在哪 |
| 加个缓存总能解决问题 | 缓存引入复杂度，应作为最后手段而非第一选择 |
| 这点性能损失无所谓 | 小损失在规模下放大——但同样需要先测量 |
| 用户感受不到这个优化 | 如果感受不到，优化就是浪费时间的过度工程 |
| 用更快的语言重写就好了 | 架构和算法比语言更重要 |

## 红旗标志
- 没有测量基线就开始"优化"
- 添加复杂缓存架构来解决简单查询问题
- 微观优化汇编级代码而非改进算法复杂度
- 以性能为名牺牲代码可读性
- 对非关键路径做过度优化

## 验证清单
- [ ] 优化前已建立性能基线测量数据
- [ ] LCP < 2.5s、CLS < 0.1、INP < 200ms
- [ ] 使用 Profiler 定位瓶颈而非猜测
- [ ] 无 N+1 查询问题
- [ ] Bundle 体积有分析记录
- [ ] 缓存策略有明确的失效机制
- [ ] 优化后有对比测量确认改善
