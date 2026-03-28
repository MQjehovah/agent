---
name: product-business-analysis
description: Use when analyzing product sales data, customer metrics, cost structures, inventory status, or making data-driven business decisions. Triggers for revenue analysis, cost optimization, inventory management, and profit analysis requests.
---

# Product Business Analysis

## Overview

系统化的产品经营分析方法论，帮助进行销售、成本、库存、利润等多维度数据分析，支持数据驱动的经营决策。

## When to Use

- 分析销售数据、营收趋势
- 评估成本结构、寻找优化空间
- 管理库存、计算周转效率
- 分析利润、制定经营策略
- 进行综合经营决策

## Analysis Frameworks

### 1. Revenue Analysis

**Core Metrics:**
| Metric | Formula | Benchmark |
|--------|---------|-----------|
| Revenue Growth | (Current-Previous)/Previous | >10% |
| ARPU | Total Revenue/Users | Industry-specific |
| Repeat Rate | Repeat Customers/Total | >30% |
| LTV | Avg Purchase × Frequency × Lifespan | >3× CAC |

**Analysis Steps:**
1. 趋势分析: 同比、环比变化
2. 结构拆解: 产品线/渠道/客户群体
3. 驱动因素: 增长或下滑原因
4. 预测: 基于历史趋势

### 2. Cost Analysis

**Cost Structure:**
- Fixed Costs: 租金、薪资、折旧
- Variable Costs: 原材料、佣金、物流

**Key Metrics:**
| Metric | Formula | Target |
|--------|---------|--------|
| Gross Margin | (Revenue-COGS)/Revenue | >30% |
| Cost Ratio | Total Cost/Revenue | <70% |
| Unit Economics | Revenue-Variable Cost per Unit | >0 |

**Optimization Paths:**
1. 规模效应: 固定成本分摊
2. 效率提升: 流程优化
3. 采购优化: 供应商谈判
4. 结构调整: 产品组合优化

### 3. Inventory Analysis

**Core Metrics:**
| Metric | Formula | Benchmark |
|--------|---------|-----------|
| Turnover Rate | COGS/Avg Inventory | 4-6×/year |
| Days of Inventory | 365/Turnover Rate | 60-90 days |
| Stockout Rate | Stockout Days/Total Days | <5% |

**ABC Classification:**
- A类(高价值): 20% SKU, 80% value → 重点管理
- B类(中等): 30% SKU, 15% value → 常规管理
- C类(低价值): 50% SKU, 5% value → 简化管理

**Warning Signals:**
- 周转率 < 行业平均 → 库存积压风险
- 缺货率 > 5% → 销售损失风险
- 库存天数 > 90天 → 资金占用风险

### 4. Profit Analysis

**Profit Structure:**
```
Revenue
- COGS
= Gross Profit
- Operating Expenses
= Operating Profit
- Interest & Taxes
= Net Profit
```

**Key Metrics:**
| Metric | Formula | Benchmark |
|--------|---------|-----------|
| Gross Margin | Gross Profit/Revenue | >30% |
| Operating Margin | Operating Profit/Revenue | >15% |
| Net Margin | Net Profit/Revenue | >10% |
| Break-even Point | Fixed Costs/Contribution Margin | - |

**Optimization Matrix:**
```
           高毛利     低毛利
高销量     明星产品   问题产品
低销量     利润产品   鸡肋产品
```

## Decision Framework

### Data-Driven Decision Checklist

- [ ] 数据完整性: 数据是否全面可靠？
- [ ] 时间范围: 分析周期是否合理？
- [ ] 对比基准: 是否有行业或历史对比？
- [ ] 因果关系: 相关性是否意味着因果？
- [ ] 可执行性: 建议是否可落地？

### Analysis Template

```markdown
## [主题]经营分析

### 1. 数据概览
- 核心指标现状
- 同比/环比变化

### 2. 问题识别
- 异常指标
- 潜在风险

### 3. 原因分析
- 直接原因
- 根本原因

### 4. 建议方案
- 短期措施
- 长期策略

### 5. 预期效果
- 量化目标
- 时间规划
```

## Quick Reference

### Common Analysis Scenarios

| Question | Approach |
|----------|----------|
| 销售下滑原因？ | 趋势分析 → 结构拆解 → 渠道/产品定位问题 |
| 成本如何优化？ | 成本拆解 → 识别驱动因素 → 对标分析 |
| 库存是否合理？ | 周转率计算 → 行业对比 → ABC分类 |
| 产品利润如何？ | 利润结构分析 → 产品矩阵 → 优化方向 |

### Key Formulas

```
毛利率 = (营收 - 成本) / 营收 × 100%
周转率 = 销售成本 / 平均库存
盈亏平衡点 = 固定成本 / (单价 - 单位变动成本)
LTV = 客单价 × 购买频次 × 客户生命周期
ROI = (收益 - 成本) / 成本 × 100%
```

## Common Mistakes

1. **过度依赖单一指标**: 综合多个指标判断
2. **忽视行业差异**: 参考行业基准
3. **混淆相关与因果**: 深入分析因果关系
4. **忽略时间维度**: 考虑趋势和周期性
5. **数据质量不足**: 确保数据准确性