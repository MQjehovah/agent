---
name: monthly-business-review
description: 月度经营复盘。适用于"XX月经营情况 / 经营分析 / 经营报告 / 月度复盘"类任务。固化已验证可用的批量 SQL 查询集与报告模板，一次并行查完、直接出报告，禁止自由探索数据库。
version: "1.0.0"
---
# 月度经营复盘

## 使用前提

1. **确定目标月份**：从用户输入解析（如"7月"→ `2026-07`；拿不准时先调 `get_current_time` 确认）。
2. **替换日期占位符**（下文 SQL 中所有占位符都要替换）：
   - `{M_START}` = 本月首日 `YYYY-MM-01`，`{M_END}` = 次月首日（如 `2026-07-01` / `2026-08-01`）
   - `{P_START}` = 上月首日，`{P_END}` = 本月首日（如 `2026-06-01` / `2026-07-01`）
3. **金额口径**：本币金额 = `material_quantity * purchase_price * exch_rate`，注意 `exch_name`/`exch_rate` 币种折算。

## 执行方式（严格按此推进，最多 3 轮）

- **第 1 轮**：一次性**并行**发起下面「固定查询集」全部 10 条，不要逐条确认、不要先 describe。
- **第 2 轮**：若有查询报错（列名 / `only_full_group_by` / MySQL 不支持 PG 语法），只修正该条并重试 **1 次**，其余结果直接采用。
- **第 3 轮**：按「报告模板」直接撰写报告内容并输出，**不再新增任何查询**。

## 固定查询集

### 1. 销售汇总（本月 + 上月，一次出环比）

```sql
SELECT '本月' AS period,
       COUNT(DISTINCT s.code) AS order_cnt,
       COUNT(DISTINCT s.customer_code) AS cust_cnt,
       ROUND(SUM(om.material_quantity * om.purchase_price * om.exch_rate), 2) AS amount_cny
FROM tb_erp_sale s
JOIN tb_erp_order_material om ON s.code = om.order_code
WHERE s.create_time >= '{M_START}' AND s.create_time < '{M_END}'
UNION ALL
SELECT '上月',
       COUNT(DISTINCT s.code),
       COUNT(DISTINCT s.customer_code),
       ROUND(SUM(om.material_quantity * om.purchase_price * om.exch_rate), 2)
FROM tb_erp_sale s
JOIN tb_erp_order_material om ON s.code = om.order_code
WHERE s.create_time >= '{P_START}' AND s.create_time < '{P_END}'
```

### 2. 客户 TOP15（本月）

```sql
SELECT s.customer_code,
       COALESCE(c.name, CONCAT('未建档[', s.customer_code, ']')) AS customer_name,
       COUNT(DISTINCT s.code) AS order_cnt,
       ROUND(SUM(om.material_quantity * om.purchase_price * om.exch_rate), 2) AS amount_cny
FROM tb_erp_sale s
LEFT JOIN tb_erp_customer c ON s.customer_code = c.code
LEFT JOIN tb_erp_order_material om ON s.code = om.order_code
WHERE s.create_time >= '{M_START}' AND s.create_time < '{M_END}'
GROUP BY s.customer_code, c.name
ORDER BY amount_cny DESC LIMIT 15
```

### 3. 产品销量 TOP15（发货口径）

```sql
SELECT om.material_code,
       COALESCE(m.name, CONCAT('未知物料[', om.material_code, ']')) AS material_name,
       SUM(om.material_quantity) AS qty,
       ROUND(SUM(om.material_quantity * om.purchase_price * om.exch_rate), 2) AS amount_cny
FROM tb_erp_deliver d
JOIN tb_erp_order_material om ON d.code = om.order_code
LEFT JOIN tb_erp_material m ON om.material_code = m.code
WHERE d.create_time >= '{M_START}' AND d.create_time < '{M_END}'
GROUP BY om.material_code, m.name
ORDER BY amount_cny DESC LIMIT 15
```

### 4. 币种构成（本月）

```sql
SELECT om.exch_name,
       COUNT(DISTINCT om.order_code) AS orders,
       ROUND(SUM(om.material_quantity * om.purchase_price * om.exch_rate), 2) AS local_amount
FROM tb_erp_order_material om
JOIN tb_erp_sale s ON om.order_code = s.code
WHERE s.create_time >= '{M_START}' AND s.create_time < '{M_END}'
GROUP BY om.exch_name
ORDER BY local_amount DESC
```

### 5. 采购汇总（本月 + 上月）

```sql
SELECT DATE_FORMAT(p.create_time, '%Y-%m') AS ym,
       COUNT(DISTINCT p.code) AS purchase_cnt,
       ROUND(SUM(pm.material_quantity * pm.purchase_price * pm.exch_rate), 2) AS amount_cny
FROM tb_erp_purchase p
LEFT JOIN tb_erp_order_material pm ON p.code = pm.order_code
WHERE p.create_time >= '{P_START}' AND p.create_time < '{M_END}'
GROUP BY DATE_FORMAT(p.create_time, '%Y-%m')
```

### 6. 交付/发货汇总（本月）

```sql
SELECT COUNT(DISTINCT d.code) AS deliver_cnt,
       COUNT(DISTINCT s.code) AS order_cnt
FROM tb_erp_sale s
LEFT JOIN tb_erp_deliver d ON s.code = d.order_code
WHERE s.create_time >= '{M_START}' AND s.create_time < '{M_END}'
```

### 7. 库存概况（当前时点）

```sql
SELECT SUM(rm.quantity) AS total_qty,
       SUM(rm.quantity * rm.price) AS stock_value,
       COUNT(DISTINCT rm.material_code) AS sku_cnt
FROM tb_erp_repository_material rm
WHERE rm.quantity > 0
```

### 8. 现金流：收款/付款（paylist，本月）

```sql
SELECT vouch_type,
       COUNT(*) AS cnt,
       SUM(CASE WHEN vouch_date >= '{M_START}' AND vouch_date < '{M_END}' THEN amount ELSE 0 END) AS amt_month
FROM tb_erp_paylist
GROUP BY vouch_type
```

### 9. 售后 / 借货 / 到货 计数（本月）

```sql
SELECT
  (SELECT COUNT(*) FROM tb_erp_aftersale WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS aftersale_cnt,
  (SELECT COUNT(*) FROM tb_erp_borrow   WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS borrow_cnt,
  (SELECT COUNT(DISTINCT code) FROM tb_erp_arrival WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS arrival_cnt
```

### 10. 新增客户（本月首次下单客户；tb_erp_customer 无 create_time，用首次销售代理）

```sql
SELECT COUNT(*) AS new_customer_cnt
FROM (
  SELECT s.customer_code
  FROM tb_erp_sale s
  WHERE s.create_time < '{M_END}'
  GROUP BY s.customer_code
  HAVING MIN(s.create_time) >= '{M_START}'
) t
```

## 数据质量提示

- `tb_erp_sale.exch_name` 同时存在英文（CNY/USD/EUR）与中文（人民币/美元/欧元）值，且有脏数据 **"选项一"**：按原文 `GROUP BY` 统计，报告中标注异常项，不要臆断口径。
- `tb_erp_sale.state` 当前全部为 `ERP_ORDER_STATE_CREATED`；`tb_erp_purchase.state` 有 `ERP_ORDER_STATE_CREATED / CLOSED / FINISHED`。
- `tb_erp_paylist`（收款/付款）数据**仅到 2026-03-31**，查询 2026 年 4 月之后的回款会得到空/旧数据——报告中必须标注"现金流数据截至 2026-03-31"。
- `tb_erp_deliver.type`：`ERP_DELIVER_TYPE_XS`=销售发货、`ERP_DELIVER_TYPE_SH`=售后、`ERP_DELIVER_TYPE_CG`=采购。

## 报告模板

```markdown
# 霞智科技 YYYY年M月经营复盘

## 一、核心结论
| 指标 | 本月 | 上月 | 环比 | 说明 |
|------|------|------|------|------|
| 销售额 | | | | |
| 订单数 | | | | |
| 客户数 | | | | |
| 采购额 | | | | |
| 库存 | | | | |

## 二、销售分析
- 销售额 / 订单数 / 客户数变化及原因
- 客户 TOP、产品 TOP、币种构成、异常币种（如"选项一"）核查

## 三、采购与供应链
- 采购额、采购>销售 的现金流风险、到货情况

## 四、库存
- 总量 / 金额 / SKU、核心产品（SW50/GT/Titan810）库存是否健康

## 五、现金流
- 收款/付款情况；数据缺失要明确标注（如收款单数据截止日期）

## 六、风险与建议
- 3~5 条可执行建议

**直接输出完整报告内容**，不要写文件；除非用户明确要求保存到 `.agent/report/` 目录。
```

## 红线

- **只执行上面 10 条查询**，不要 describe 其他表、不要重复或追加查询。
- SQL 报错：先自查（列名 / `only_full_group_by`：SELECT 非聚合列必须进 GROUP BY / MySQL 不支持 `NULLS LAST` 等 PG 语法），修正该条重试 **1 次**。
- 数据缺失或口径存疑时，**在报告中明确标注**，不要用额外查询去"确认"。
