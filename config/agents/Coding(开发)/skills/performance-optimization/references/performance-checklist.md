# 性能检查清单

## Core Web Vitals
- [ ] LCP（最大内容绘制）< 2.5s
- [ ] CLS（累计布局偏移）< 0.1
- [ ] INP（交互到下次绘制）< 200ms
- [ ] TTFB（首字节时间）< 800ms

## N+1 查询检测
- [ ] 检查 ORM / SQL 日志中是否有循环查询
- [ ] 关联数据使用 JOIN 或 Batch 加载（eager loading）
- [ ] GraphQL 场景使用 DataLoader 批量合并请求

## 无界循环/数据拉取
- [ ] 无未分页的全量数据查询
- [ ] 无未设终止条件的循环或递归
- [ ] 后台任务/定时任务有超时机制

## 打包体积
- [ ] 运行了打包分析（`webpack-bundle-analyzer` / `source-map-explorer`）
- [ ] 无体积过大的依赖项
- [ ] 代码分割（Code Splitting）已落地
- [ ] Tree Shaking 生效

## 缓存策略
- [ ] 静态资源设置了合理的 Cache-Control / ETag
- [ ] 频繁查询的结果已使用 Redis / Memcached 缓存
- [ ] 数据库查询缓存是否生效
- [ ] 缓存失效策略（TTL / 事件驱动）已定义

## 数据库查询优化
- [ ] 查询走了正确的索引——检查 `EXPLAIN`
- [ ] 避免 SELECT *，只取所需字段
- [ ] 大表上的慢查询已识别并优化
