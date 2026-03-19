---
name: inventory-monitor
description: Use when monitoring inventory status, detecting stock alerts, or managing inventory risks. Triggers for real-time inventory checks, safety stock analysis, and replenishment recommendations.
---

# Inventory Monitor Expert

## Overview

库存监控专家，实时监控库存状态，识别潜在风险并及时发出预警。

## When to Use

- 检查库存水平和状态
- 识别缺货风险
- 计算安全库存
- 生成库存预警报告

## Monitoring Metrics

| Metric | Formula | Purpose |
|--------|---------|---------|
| 库存周转天数 | 库存数量 / 日均消耗量 | 补货决策 |
| 安全库存 | 预警阈值 × 日均消耗量 | 风险缓冲 |
| 库存健康度 | (当前库存 - 安全库存) / 安全库存 | 状态评估 |

## Alert Levels

| Level | Condition | Action |
|-------|-----------|--------|
| 🔴 紧急 | 库存 < 3天消耗量 | 立即补货 |
| 🟠 警告 | 库存 < 7天消耗量 | 计划补货 |
| 🟡 关注 | 库存 < 14天消耗量 | 关注趋势 |
| 🟢 正常 | 库存 >= 14天消耗量 | 维持现状 |

## Workflow

1. 查询当前库存数据
2. 计算库存健康指标
3. 识别异常和风险
4. 生成监控报告
5. 发送预警通知（如需要）

## Output Format

### 📦 库存监控报告

**监控时间**: {{timestamp}}
**预警阈值**: {{warning_threshold}}天

---

#### 库存概览

| 物料编码 | 物料名称 | 当前库存 | 周转天数 | 状态 |
|----------|----------|----------|----------|------|
| ... | ... | ... | ... | ... |

#### 风险预警

🔴 紧急缺货:
- ...

🟠 库存警告:
- ...

#### 建议行动

1. ...
2. ...

---

## Quick Reference

### Common Queries

```sql
-- 低库存预警
SELECT product_id, product_name, stock, daily_usage,
       stock/daily_usage as days_left
FROM inventory
WHERE stock/daily_usage < 7
ORDER BY days_left ASC;

-- 滞销库存
SELECT product_id, product_name, stock, last_sale_date
FROM inventory
WHERE last_sale_date < DATE_SUB(NOW(), INTERVAL 90 DAY)
  AND stock > 0
ORDER BY stock DESC;

-- 库存周转率
SELECT product_id,
       SUM(sales_qty) / AVG(stock) as turnover_rate
FROM inventory_daily
GROUP BY product_id
HAVING turnover_rate < 4;  -- 年周转低于4次
```

### Safety Stock Calculation

```
安全库存 = (最大日消耗量 × 最长补货周期) - (平均日消耗量 × 平均补货周期)
再订货点 = 平均日消耗量 × 补货周期 + 安全库存
```