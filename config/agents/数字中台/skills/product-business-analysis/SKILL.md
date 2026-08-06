---
name: product-business-analysis
description: 产品经营分析。分析销售、采购、库存、成本、现金流、生产等业务数据并生成报告。已按数字中台真实数据库校准：明确标注哪些指标数据库能算、哪些无数据源不得编造，每个维度附可用 SQL。
version: "1.0.0"
---

# Product Business Analysis（对齐真实数据库）

## 数据边界（先读这一节，避免编造指标）

**数据库能提供的：**

| 维度 | 能算的指标 | 数据源 |
|------|-----------|--------|
| 销售 | 销售额、订单数、客户数、客单价、客户/产品分布、币种构成、按月趋势、复购客户 | `tb_erp_sale` + `tb_erp_order_material` |
| 采购 | 采购额、采购单数、供应商分布、采购>销售风险 | `tb_erp_purchase` + `tb_erp_order_material` |
| 库存 | 当前库存数量/金额、SKU 数、安全库存预警、ABC 分类、核心产品库存 | `tb_erp_repository_material` + `tb_erp_material` |
| 成本 | 物料/采购成本（`purchase_price`）、BOM 成本（口径参考，可能为 0） | `tb_erp_order_material`、`tb_erp_bom`/`tb_erp_bom_item` |
| 现金流 | 收款/付款笔数与金额（**数据截至 2026-03-31**） | `tb_erp_paylist` |
| 生产 | 工单、产量、良品/不良数（良率）、在制品 | `tb_mes_work_order`、`tb_mes_production_report`、`tb_mes_wip_inventory` |

**数据库不能提供（严禁编造，需求涉及必须标注"数据缺口"并用代理指标近似）：**
- 净利润 / 营业利润 / 净利率：**没有 P&L（损益）数据**，只有收付款流水。
- 人工成本 / 制造费用 / 固定成本 / 管理费用 / 盈亏平衡点：无对应数据源。
- 库存周转率 / 周转天数：无期初/期末库存快照与销售成本历史，算不了。
- ARPU / LTV / 用户数 / 渠道 ROI 等互联网口径：数据结构不支持。

**必须遵守：**
- 金额一律 `material_quantity * purchase_price * exch_rate`（本币），必须 JOIN `tb_erp_order_material` 明细表。
- 时间过滤用半开区间：`create_time >= 'YYYY-MM-01' AND create_time < '下月01'`。
- 无法计算的指标在报告中明确写"无数据源"并给出可行替代口径，**不得估算填数**。

## 通用口径与查询纪律

- MySQL 聚合注意 `only_full_group_by`：SELECT 非聚合列必须全部进 `GROUP BY`；不支持 `NULLS LAST` 等 PG 语法。
- `tb_erp_sale.exch_name` 存在中英文混用（CNY/人民币、USD/美元、EUR/欧元）及脏数据 **"选项一"**：按原文 `GROUP BY`，异常项在报告中标注。
- 不确定列名时先 `describe_table`（不要凭记忆写列名）。
- 需要比较上期时，用 UNION ALL 一次取回，不要分多次查询。

## 维度分析指南 + 可用 SQL

以下 SQL 均已按真实表结构验证；占位符 `{S}`/`{E}` = 起始/结束时间（`YYYY-MM-01`），`{PS}`/`{PE}` = 上一期起始/结束。

### 1. 销售分析

销售额汇总（本月 + 上月，一次出环比）：
```sql
SELECT '本期' AS period, COUNT(DISTINCT s.code) AS order_cnt,
       COUNT(DISTINCT s.customer_code) AS cust_cnt,
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2) AS amount_cny
FROM tb_erp_sale s JOIN tb_erp_order_material om ON s.code=om.order_code
WHERE s.create_time >= '{S}' AND s.create_time < '{E}'
UNION ALL
SELECT '上期', COUNT(DISTINCT s.code), COUNT(DISTINCT s.customer_code),
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2)
FROM tb_erp_sale s JOIN tb_erp_order_material om ON s.code=om.order_code
WHERE s.create_time >= '{PS}' AND s.create_time < '{PE}'
```

客户 TOP15（含未建档）：
```sql
SELECT s.customer_code, COALESCE(c.name, CONCAT('未建档[', s.customer_code, ']')) AS customer_name,
       COUNT(DISTINCT s.code) AS order_cnt,
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2) AS amount_cny
FROM tb_erp_sale s
LEFT JOIN tb_erp_customer c ON s.customer_code=c.code
LEFT JOIN tb_erp_order_material om ON s.code=om.order_code
WHERE s.create_time >= '{S}' AND s.create_time < '{E}'
GROUP BY s.customer_code, c.name ORDER BY amount_cny DESC LIMIT 15
```

复购客户（本期下过 >1 单的客户数）：
```sql
SELECT COUNT(*) AS repeat_cust_cnt FROM (
  SELECT s.customer_code FROM tb_erp_sale s
  WHERE s.create_time >= '{S}' AND s.create_time < '{E}'
  GROUP BY s.customer_code HAVING COUNT(DISTINCT s.code) > 1
) t
```

按月销售趋势：
```sql
SELECT DATE_FORMAT(s.create_time,'%Y-%m') AS ym,
       COUNT(DISTINCT s.code) AS order_cnt,
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2) AS amount_cny
FROM tb_erp_sale s JOIN tb_erp_order_material om ON s.code=om.order_code
GROUP BY DATE_FORMAT(s.create_time,'%Y-%m') ORDER BY ym
```

产品销量 TOP（发货口径）与币种构成：见 `monthly-business-review` 技能。

### 2. 采购分析

采购汇总（本期+上期）：
```sql
SELECT DATE_FORMAT(p.create_time,'%Y-%m') AS ym,
       COUNT(DISTINCT p.code) AS purchase_cnt,
       ROUND(SUM(pm.material_quantity*pm.purchase_price*pm.exch_rate),2) AS amount_cny
FROM tb_erp_purchase p LEFT JOIN tb_erp_order_material pm ON p.code=pm.order_code
WHERE p.create_time >= '{PS}' AND p.create_time < '{E}'
GROUP BY DATE_FORMAT(p.create_time,'%Y-%m')
```

供应商 TOP：
```sql
SELECT p.supplier_code, COALESCE(sp.name, CONCAT('未建档[', p.supplier_code, ']')) AS supplier_name,
       COUNT(DISTINCT p.code) AS order_cnt,
       ROUND(SUM(pm.material_quantity*pm.purchase_price*pm.exch_rate),2) AS amount_cny
FROM tb_erp_purchase p
LEFT JOIN tb_erp_supplier sp ON p.supplier_code=sp.code
LEFT JOIN tb_erp_order_material pm ON p.code=pm.order_code
WHERE p.create_time >= '{S}' AND p.create_time < '{E}'
GROUP BY p.supplier_code, sp.name ORDER BY amount_cny DESC LIMIT 15
```

### 3. 库存分析

库存概况（当前时点）：
```sql
SELECT SUM(rm.quantity) AS total_qty,
       SUM(rm.quantity*rm.price) AS stock_value,
       COUNT(DISTINCT rm.material_code) AS sku_cnt
FROM tb_erp_repository_material rm WHERE rm.quantity > 0
```

核心产品库存与库存金额：
```sql
SELECT rm.material_code, COALESCE(m.name, '') AS name,
       SUM(rm.quantity) AS qty, SUM(rm.quantity*rm.price) AS value
FROM tb_erp_repository_material rm
LEFT JOIN tb_erp_material m ON rm.material_code=m.code
WHERE rm.material_code IN ('1110100030','1110100060','1110102010')
GROUP BY rm.material_code, m.name
```

安全库存预警（成品）：
```sql
SELECT code, name, quantity, warning_quantity
FROM tb_erp_material
WHERE quantity < warning_quantity AND category='ERP_MATERIAL_CATEGORY_PRODUCT'
ORDER BY (warning_quantity-quantity) DESC
```

ABC 分类（按库存金额降序累计占比，A 类≈前 80% 金额）：
```sql
SELECT material_code, quantity, price, quantity*price AS stock_value
FROM tb_erp_repository_material WHERE quantity > 0
ORDER BY stock_value DESC LIMIT 30
```

### 4. 成本分析（口径受限）

- **物料/采购成本**：直接用 `tb_erp_order_material.purchase_price`（含税单价）。
- **毛利近似**（仅产品级）：`销售额 - 该产品按 BOM 的成本`。注意 `tb_erp_material.price` 可能未维护（BOM 成本会算成 0），算出来是 0 时如实报告"成本数据缺失"，**不要**用采购价当成本强行算毛利。
- **不能算**：人工、制造费用、固定/变动成本、盈亏平衡。

BOM 成本（某产品）：
```sql
SELECT b.material_code, ROUND(SUM(bi.quantity*COALESCE(m.price,0)),2) AS bom_cost
FROM tb_erp_bom b JOIN tb_erp_bom_item bi ON bi.bom_id=b.id
LEFT JOIN tb_erp_material m ON bi.material_code=m.code
WHERE b.material_code='{MATERIAL_CODE}' AND b.is_delete=0 AND b.is_active=1
GROUP BY b.material_code
```

### 5. 现金流分析

收/付款按类型汇总（**注意 paylist 数据截至 2026-03-31，必须标注**）：
```sql
SELECT vouch_type,
       COUNT(*) AS cnt,
       SUM(CASE WHEN vouch_date >= '{S}' AND vouch_date < '{E}' THEN amount ELSE 0 END) AS amt_month
FROM tb_erp_paylist GROUP BY vouch_type
```

### 6. 生产分析

产量与良率（按工单/工序）：
```sql
SELECT DATE_FORMAT(r.create_time,'%Y-%m') AS ym,
       SUM(r.report_quantity) AS total_qty,
       SUM(r.good_quantity) AS good_qty,
       SUM(r.reject_quantity) AS reject_qty,
       ROUND(SUM(r.good_quantity)/NULLIF(SUM(r.report_quantity),0)*100,1) AS yield_pct
FROM tb_mes_production_report r
WHERE r.create_time >= '{S}' AND r.create_time < '{E}'
GROUP BY DATE_FORMAT(r.create_time,'%Y-%m')
```

在制品概览：
```sql
SELECT COUNT(DISTINCT work_order_id) AS wo_cnt,
       SUM(quantity) AS wip_qty
FROM tb_mes_wip_inventory
```

## 报告模板

```markdown
## [主题]经营分析

### 1. 数据概览（本期 vs 上期）
| 指标 | 本期 | 上期 | 环比 |
|------|------|------|------|
| 销售额 | | | |
| 订单数 | | | |
| 客户数 | | | |
| 采购额 | | | |

### 2. 核心发现
- 增长/下滑的驱动（客户、产品、币种、月度趋势）
- 采购>销售、库存积压/缺货、现金流缺口等风险

### 3. 问题与原因
- 直接原因 → 根本原因（用已有数据支撑）

### 4. 建议
- 3~5 条可执行措施（数据可量化）

### 5. 数据缺口
- 列出本次无法从数据库获得的指标及原因（如：营业利润无 P&L 数据；现金流数据截至 2026-03-31）
```

## 红线

- **只查询上述已验证的 SQL 模式**；涉及新表/新字段先 `describe_table`。
- 无法计算的指标标注"无数据源"，**禁止编造或强行估算**。
- 报告中的每个数字必须能追溯到某条 SQL 的结果。
