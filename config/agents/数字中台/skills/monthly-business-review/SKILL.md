---
name: monthly-business-review
description: 月度经营分析（ERP 接口优先）。适用于"XX月经营情况 / 经营分析 / 经营报告 / 月度复盘"类任务。固定 ERP 只读调用集 + 月度聚合查询集，产出"数据统计 + 经营分析与建议"两大部分报告；无数据源章节留占位标注，禁止编造。
version: "1.1.0"
---

# 月度经营分析（ERP 接口优先）

## 数据通道（先读这一节）

本技能走两条只读通道，**ERP 接口优先，月度聚合用数据库查询兜底**：

1. **ERP 只读接口（个人凭据）**——命名工具 `erp_statistics` / `erp_query_order` / `erp_order_by_code` / `erp_order_materials` / `erp_pkg_stock` / `erp_payment_query` / `erp_request` 等。
   - 若当前工具集**没有** `erp_*` 工具：改用 `market_execute`（`kind="mcp"`、`capability="erp"`、`tool="<工具名>"`、`params={...}`）逐请求调用，效果相同。
   - 若两条路都不可用：报告中标注"ERP 通道不可用"，不得编造。
   - 注意：ERP 看板类接口是**生成时点的实时口径（当前周/月）**，不是目标月历史值；报告中必须标注口径，不得冒充目标月数据。
2. **数据库只读查询（月度聚合兜底）**——数据库查询工具（`execute_query` 类）。
   - 用于"按自然月 `create_time` 聚合"的口径（ERP 接口无历史月份过滤）。
   - 通道不可用（工具缺失/连接失败）时：该指标标注"【数据缺口·DB 通道不可用】"，不得编造。

**无数据源章节（固定占位，禁止编造）**：
研发版本情况、认证进展、质量关键指标、生产效率、制造费用率、齐套率、年初销售目标 —— 报告保留小节并标注"【数据缺口·待中台数据源补充】"。

## 使用前提

1. **确定目标月份**：从用户输入解析（如"9月"→ `2026-09`；拿不准先调 `get_current_time` 确认）。
2. **替换日期占位符**（下文所有占位符都要替换）：
   - `{M_START}` = 本月首日 `YYYY-MM-01`，`{M_END}` = 次月首日（如 `2026-09-01` / `2026-10-01`）
   - `{P_START}` = 上月首日，`{P_END}` = 本月首日（如 `2026-08-01` / `2026-09-01`）
3. **金额口径**：本币金额 = `material_quantity * purchase_price * exch_rate`，注意 `exch_name`/`exch_rate` 币种折算。

## 执行方式（严格按此推进，最多 3 轮）

- **第 1 轮**：一次性**并行**发起「A. ERP 调用集」（7 条）与「B. 月度聚合查询集」（10 条），不要逐条确认、不要先 describe。
- **第 2 轮**：若有条目报错，只修正该条并重试 **1 次**（SQL：列名 / `only_full_group_by`；ERP：参数名 / 路径），其余结果直接采用。
- **第 3 轮**：按「报告模板」撰写完整报告并输出，**不再新增任何查询**。默认直接输出完整报告内容；仅当用户明确要求保存文件时，才写入 `.agent/report/` 目录。

## A. ERP 固定调用集（7 条，只读）

| # | 调用 | 用途 |
|---|------|------|
| 1 | `erp_statistics(kind="overview")` | 总览：订单数、周销售/发货/采购/到货金额、交付率 |
| 2 | `erp_statistics(kind="sales_revenue")` | 销售/实际发货（月口径看板） |
| 3 | `erp_statistics(kind="fund_flow")` | 资金流：销售回款、售后、库存资金比 |
| 4 | `erp_statistics(kind="inventory")` | 库存统计：库存余额、月出入库 |
| 5 | `erp_statistics(kind="product_delivery")` | **机型发货结构（台数/占比）** |
| 6 | `erp_statistics(kind="aftersale_delivery")` | 售后发货 TOP（质量问题信号） |
| 7 | `erp_request(method="POST", path="/erp/query", params={"pageCurrent":1,"pageSize":50}, body={"startTime":"{M_START} 00:00:00","endTime":"{M_END} 00:00:00"})` | 付款单（支持时间过滤，月度收支） |

> 第 7 条若经 `market_execute` 调用：`kind="mcp"`, `capability="erp"`, `tool="erp_request"`, `params={"method":"POST","path":"/erp/query","params":{...},"body":{...}}`。
> 报告中所有 ERP 看板数值标注"ERP 实时看板（生成时点）"。

## B. 固定月度聚合查询集（10 条 SQL，目标月 {M_START}~{M_END}）

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
- `tb_erp_paylist`（收款/付款）数据可能较旧（以实际查询为准），报告中必须标注现金流数据的截止情况；ERP `/erp/query` 付款单可作为更新来源。
- `tb_erp_deliver.type`：`ERP_DELIVER_TYPE_XS`=销售发货、`ERP_DELIVER_TYPE_SH`=售后、`ERP_DELIVER_TYPE_CG`=采购。
- ERP 看板接口为生成时点口径（当前周/月），SQL 为自然月口径；两类数据并列时分别标注。

## 报告模板（直接输出完整报告，不写文件）

```markdown
# 霞智科技 YYYY年M月经营分析

> 数据来源：ERP 只读接口（生成时点看板 + 单据查询） + 业务库月度聚合（{M_START}~{M_END}）
> 口径说明：金额=数量×含税单价×汇率（本币）；ERP 看板为生成时点口径；SQL 为自然月口径

## 第一部分 上月数据统计

### 1. 销售（定 / 发 / 收）
| 指标 | 本月 | 上月 | 环比 | 说明/口径 |
|------|------|------|------|----------|
| 销售订单数 / 客户数 / 含税金额 | | | | 月度聚合 |
| 发货单数 | | | | 月度聚合 |
| 发货台数（机型结构） | | | — | ERP 看板（生成时点） |
| 收款 / 付款 | | | | ERP 付款单（时间过滤）+ paylist（标注截止） |

- 客户 TOP5、产品 TOP5（发货口径）、币种构成与异常项核查
- 未建档客户、零单价订单、异常币种提示

### 2. 供应链
- **生产效率**：【数据缺口·待中台数据源补充】
- **发货台数**：见 1.销售（ERP 机型发货结构）
- **库存数据**：库存余额/月出入库（ERP 看板）+ 总量/金额/SKU（月度聚合）+ 核心机型库存健康度
- **制造费用率**：【数据缺口·待中台数据源补充】
- **齐套率**：【数据缺口·待中台数据源补充】

### 3. 研发
- **版本情况**：【数据缺口·待中台数据源补充】
- **认证进展**：【数据缺口·待中台数据源补充】

### 4. 质量
- **售后情况**：售后单数（月度聚合）、TOP 售后物料（ERP 看板）、环比
- **质量问题与关键指标**：【数据缺口·待中台数据源补充】

### 5. 财务收支
| 指标 | 本月 | 说明 |
|------|------|------|
| 销售回款 / 资金流（ERP 看板） | | 生成时点口径 |
| 付款单（按时间过滤） | | ERP |
| 收/付款流水（paylist） | | 标注数据截止 |
| 采购>销售 资金缺口 | | 采购额-销售额 |

## 第二部分 经营情况分析和建议

### 1. 销售业绩与目标差距
- 与年初目标差距：**【年初目标未提供·待中台数据源补充】**
- 基于现有数据的冲刺分析：重点客户贡献与依赖度、停滞/大额订单、回款情况
- 订单冲刺计划：3~5 条可执行动作（客户/产品/回款维度）

### 2. 供应链 / 研发 / 质量建议
- 供应链（库存/采购/交付节奏）：1~3 条
- 研发（版本/认证，占位基础上给管理建议）：1~2 条
- 质量（售后集中问题）：1~3 条

### 3. 财务费用管控建议
- 基于采购>销售缺口、库存资金、付款节奏：2~4 条

## 附录：数据说明与缺口
- 数据口径与生成时点；ERP 看板与 SQL 口径差异
- 数据缺口清单（研发/认证/质量指标/制造费用率/齐套率/年初目标/DB 或 ERP 通道异常）
```

- **默认直接输出完整报告内容**，不要写文件。
- 仅当用户明确要求"保存/生成报告文件"时，才写入 `.agent/report/` 目录。

## 红线

- **ERP 只读**（禁止 PUT/DELETE/创建类接口）；SQL 只允许 SELECT。
- **只执行 A 组 7 条 + B 组 10 条**，不要 describe 其他表、不要重复或追加查询。
- SQL 报错：先自查（列名 / `only_full_group_by`：SELECT 非聚合列必须进 GROUP BY / MySQL 不支持 `NULLS LAST` 等 PG 语法），修正该条重试 **1 次**。
- 数据缺失或口径存疑时，**在报告中明确标注**，不要用额外查询去"确认"；**禁止编造任何指标数值**。
