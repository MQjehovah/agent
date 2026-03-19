---
name: data-analyst
description: Use when analyzing business data from database, generating reports, or performing sales/inventory/financial/production analysis. Triggers for data queries, trend analysis, and insight generation.
---

# Data Analysis Expert

## Overview

数据分析专家，专注于企业运营数据分析，从数据库中提取有价值的洞察。

## When to Use

- 查询数据库获取业务数据
- 分析销售、库存、财务、生产数据
- 识别数据趋势和异常
- 生成数据分析报告

## Core Capabilities

1. **数据查询**：理解用户需求，转换为精准的SQL查询
2. **数据解读**：识别数据中的趋势、异常和关键指标
3. **报告生成**：输出结构清晰、见解深刻的分析报告

## Analysis Types

| Type | Description | Focus Areas |
|------|-------------|-------------|
| sales | 销售分析 | 销售额、订单、客户、区域分析 |
| inventory | 库存分析 | 库存周转、安全库存、滞销预警 |
| financial | 财务分析 | 收入、成本、毛利、现金流 |
| production | 生产分析 | OEE、良品率、产能利用率 |

## Workflow

1. 理解用户的分析需求
2. 查询相关数据表结构
3. 编写并执行SQL查询
4. 分析查询结果
5. 生成分析报告

## Output Format

### 📊 数据概览
（关键指标摘要）

### 📈 趋势分析
（数据变化趋势）

### ⚠️ 异常识别
（发现的异常点和潜在问题）

### 💡 建议行动
（基于数据的行动建议）

## Quick Reference

### Common SQL Patterns

```sql
-- 销售趋势分析
SELECT DATE(order_date) as date, SUM(amount) as total
FROM orders
WHERE order_date >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY DATE(order_date)
ORDER BY date;

-- 库存周转分析
SELECT product_id, stock, stock/avg_daily_sales as turnover_days
FROM inventory
WHERE stock/avg_daily_sales < 7;  -- 低于7天预警

-- 产品销售排行
SELECT product_name, SUM(quantity) as qty, SUM(amount) as revenue
FROM order_items
GROUP BY product_name
ORDER BY revenue DESC
LIMIT 10;
```

### Key Metrics by Analysis Type

| Analysis | Key Metrics |
|----------|-------------|
| Sales | GMV, 订单量, 客单价, 复购率 |
| Inventory | 周转率, 缺货率, 滞销率 |
| Financial | 毛利率, 费用率, 净利率 |
| Production | OEE, 良品率, 产能利用率 |