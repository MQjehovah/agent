---
name: monthly-business-review
description: 月度经营分析（领导汇报口径，两大部分：数据统计 + 分析建议）。覆盖销售（定/发/收）、供应链与生产（效率/发货台数/库存/制造费用率/齐套率）、研发（版本/认证）、质量、售后、财务（收支/费用）；中台月度接口优先 + ERP 实时看板补充 + 数据库兜底；输出含丰富图表的 email-safe HTML（或对话版 Markdown+Mermaid）；缺口如实标注，禁止编造。
version: "1.2.0"
---

# 月度经营分析（中台接口优先 · 领导汇报口径）

## 目标

围绕公司经营的**销售、供应链、生产、研发、质量、售后、财务**各环节，输出**两大部分**报告：

1. **第一部分 上月数据统计**：销售（定/发/收）→ 供应链与生产 → 研发 → 质量 → 财务
2. **第二部分 经营情况分析和建议**：目标差距与冲刺计划 → 供应链/研发/质量建议 → 财务费用管控建议

要求：数据驱动、结论先行、缺口如实标注（`【数据缺口·原因】`），**禁止编造/估算任何数字**。

## 使用前提

1. **确定目标月份**：`{M}`=`YYYY-MM`（如 `2026-09`）；`{M_START}`/`{M_END}`=本月首日/次月首日；`{P_START}`/`{P_END}`=上月区间；`{Y}`=年份。
2. **金额口径**：本币（含税）；接口返回自带 `formula/note` 字段时以其为准。
3. **工具名（重要）**：ERP 连接器工具名前缀视环境而定——市场运行环境为 `mcp_erp_*`（如 `mcp_erp_erp_request`）；独立 Agent 环境为 `erp_*`。**不要臆造其他别名**；同一调用失败最多重试 1 次。

## 数据通道（优先级从高到低）

### A2. 中台月度经营接口（首选）

调用方式：`erp_request(method="GET", path="<path>", params={...})`（POST 见 #15）。

**判成功**：返回体 `success==true && returnCode==200`。**接口实测状态表（2026-10-10，先打 ✅ 项；❌ 项重试 1 次仍失败即标缺口）**：

| # | 用途 | 路径 | 状态 | 关键字段 / 说明 |
|---|------|------|------|----------------|
| 1 | 销售/采购/入出库**多月趋势** | GET `/statistics/business/trend` | ✅ | `[{month, saleAmount, purchaseAmount, inboundAmount}]`；`deliveryAmount/outboundAmount` 恒为 0（未回写，勿用于结论，发货金额用 #2） |
| 2 | 月发货量/金额 | GET `/statistics/delivery-monthly` | ✅ | `{deliveryQuantity, deliveryAmount}` |
| 3 | 客户 TOP | GET `/statistics/business/top-customers` | ❌ 2100001 | 执行失败→标缺口（连续多日故障） |
| 4 | 经营概览（区间） | GET `/statistics/business/overview` | ❌ 2100001 | 订单数/到货等区间汇总→标缺口 |
| 5 | **生产月度**（达成/良率/工时） | GET `/statistics/business/production-monthly` | ⚠️ 全 0 | 接口正常但**报工数据未录入**：计划/完工/良率/工时均为 0 → 标注「生产报工数据未录入」；`byProduct` 结构 | 
| 6 | **质量月度** | GET `/statistics/business/quality-monthly` | ✅（部分） | `aftersaleCount` 有数（售后单数）；`firstPassRate/rejectRate`=0 系**检测数据未录入**；客诉/返修无口径 |
| 7 | **齐套率月度** | GET `/statistics/business/kitting-rate` | ⚠️ 全 0 | 接口正常但**生产计划未录入** → 标注；字段 `kittingRate/shortageRows/topShortageMaterials` |
| 8 | **财务收支月度** | GET `/statistics/business/cashflow-monthly` | ⚠️ 全 0 | 收款/付款/净额均为 0（**收付款凭证未录入**）；结构 `{receiveAmount, payAmount, netAmount, byCategory[]}` |
| 9 | **费用月度** | GET `/statistics/business/expense-monthly` | ✅ | `totalAmount`＋`byType[]`（7 类：对外采购/其他/行政办公/出差报销/业务招待/日常报销/借款）=付款申请口径 ✅ |
| 10 | **研发版本月度** | GET `/rd/statistics/version-monthly` | ❌ 500 | 服务器业务异常 → 标缺口（待研发侧） |
| 11 | **认证月度** | GET `/rd/statistics/certification-monthly` | ❌ 500 | 同上 |
| 12 | **目标达成（OKR）** | GET `/oa/okr/statistics` | ⚠️ 全 0 | 接口正常、**目标尚未录入**（total=0）→ 第二部分按实绩分析 |
| 13 | **年度目标** | GET `/oa/okr/targets` | ⚠️ 全 0 | 同上，`year={Y}` |
| 14 | 绩效月度 | GET `/oa/performance/statistics` | ✅ | `{total, avgScore, maxScore, minScore, deptDistribution, gradeDistribution}`（一行概要即可） |
| 15 | 付款台账明细 | POST `/finance/payment/query` | ❌ 500 | 用 #9 费用总额替代，注明「付款申请口径」 |

### A. ERP 实时看板（生成时点口径，作补充；**必须标注"ERP 实时看板（生成时点）"，不得冒充目标月**）

| 用途 | 调用 | 状态 | 说明 |
|------|------|------|------|
| 经营概览（周） | `erp_statistics(kind="overview")` | ✅ | 周口径：totalSalesOrders、weeklySaleAmount/Purchase/Delivery/Arrival |
| 销售/资金 | `erp_statistics(kind="sales_revenue")` / `kind="fund_flow"` | ✅ | **库存余额**=`fund_flow.inventoryBalanceAmount`（时点，9月约 1503 万）；`salesRevenueAmount` 时点销售额；`fundFlowRatio` |
| 机型发货结构 | `erp_statistics(kind="product_delivery")` | ✅ | `[{productType, quantity, percentage}]`（周内时点，如 T810 28 台） |
| 售后出库 TOP | `erp_statistics(kind="aftersale_delivery")` | ✅ | `[{rank, materialName, quantity}]` 物料排行 |
| 库存明细 | `erp_statistics(kind="inventory")` | ❌ 404 | 用 fund_flow 库存余额替代 |

### B. 数据库查询（兜底，仅当 DB 查询工具可用时）

仅当 A2/A 均取不到且当前有只读 SQL 工具（execute_query 类）时执行；报错一次即放弃转缺口。

**销售汇总（本月+上月）**
```sql
SELECT '本月' AS period, COUNT(DISTINCT s.code) AS order_cnt,
       COUNT(DISTINCT s.customer_code) AS cust_cnt,
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2) AS amount_cny
FROM tb_erp_sale s JOIN tb_erp_order_material om ON s.code=om.order_code
WHERE s.create_time >= '{M_START}' AND s.create_time < '{M_END}'
UNION ALL
SELECT '上月', COUNT(DISTINCT s.code), COUNT(DISTINCT s.customer_code),
       ROUND(SUM(om.material_quantity*om.purchase_price*om.exch_rate),2)
FROM tb_erp_sale s JOIN tb_erp_order_material om ON s.code=om.order_code
WHERE s.create_time >= '{P_START}' AND s.create_time < '{P_END}'
```

**采购汇总（本月+上月）**
```sql
SELECT DATE_FORMAT(p.create_time,'%Y-%m') AS ym, COUNT(DISTINCT p.code) AS purchase_cnt,
       ROUND(SUM(pm.material_quantity*pm.purchase_price*pm.exch_rate),2) AS amount_cny
FROM tb_erp_purchase p LEFT JOIN tb_erp_order_material pm ON p.code=pm.order_code
WHERE p.create_time >= '{P_START}' AND p.create_time < '{M_END}'
GROUP BY DATE_FORMAT(p.create_time,'%Y-%m')
```

**库存概况（当前时点）**
```sql
SELECT SUM(rm.quantity) AS total_qty, SUM(rm.quantity*rm.price) AS stock_value,
       COUNT(DISTINCT rm.material_code) AS sku_cnt
FROM tb_erp_repository_material rm WHERE rm.quantity > 0
```

**售后/借货/到货计数（本月）**
```sql
SELECT
  (SELECT COUNT(*) FROM tb_erp_aftersale WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS aftersale_cnt,
  (SELECT COUNT(*) FROM tb_erp_borrow   WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS borrow_cnt,
  (SELECT COUNT(DISTINCT code) FROM tb_erp_arrival WHERE create_time >= '{M_START}' AND create_time < '{M_END}') AS arrival_cnt
```

## 执行方式（严格，最多 3 轮）

- **第 1 轮**：一次性并行发起 **A2 全部 15 条 + A 组 6 条**（共 21 条）；不逐条确认、不先探索其他表。
- **第 2 轮**：失败项只修正一次（参数名/日期格式/POST body）；❌ 项直接按状态表结论标缺口，不反复试。
- **第 3 轮**：按「报告模板」撰写完整报告并输出；不再新增查询。

## 凭证缺失时的行为

若 ERP 工具整体不可用（连接器未挂载/未配置凭据），**不要尝试猜测工具名**：直接输出报告骨架并将所有数据标 `【数据缺口·ERP连接器未连接（请先配置凭据）】`，并提示到市场该能力卡片「配置凭据」。

## 降级与缺口规则

1. 每项按 **A2 → A（时点）→ B（SQL）→ 缺口** 顺序取数。
2. 缺口格式：`【数据缺口·<原因>】`；原因写明（"接口未就绪(500)"、"数据未录入"、"无数据源(制造费用率)"、"目标尚未录入"）。
3. 口径存疑必须标注（如"趋势接口 deliveryAmount=0 与月发货口径冲突，以 #2 为准"）。
4. 报告末尾附「附录：数据来源与缺口」逐项列明。

## 报告模板（两大部分 · 严格按此结构）

### 第一部分 上月数据统计

#### 1. 销售（定 / 发 / 收）
| 指标 | 本月 | 上月/环比 | 来源 |
|------|------|----------|------|
| 销售金额（#1 saleAmount） | | ▲▼% | 中台 trend |
| 时点销售看板（A2 salesRevenueAmount） | — | | ERP 时点 |
| 发货量 / 发货金额（#2） | | | 中台 delivery-monthly |
| 机型发货结构（A5） | | | ERP 时点 |
| 收款 / 付款 / 净额（#8） | 全 0 则注明"收付款凭证未录入" | | 中台 cashflow |
| 客户结构（#3） | ❌→缺口 | | — |

> 小结 1~2 句（同比/环比、异常与口径提示）。

#### 2. 供应链与生产
- **生产效率/达成率/良率/工时**（#5；全 0 时注明"报工数据未录入"）
- **发货台数（机型结构）**（A5）+ 月度发货（#2）
- **库存**：金额=fund_flow.inventoryBalanceAmount（时点）；SKU/金额明细（SQL 可用时）
- **制造费用率**：【数据缺口·待财务口径】（无来源）
- **齐套率 / 缺料行数 / TOP 缺料**（#7；未录入时标注）
- **采购/入库**（#1 purchaseAmount/inboundAmount）
- 小结 1~2 句。

#### 3. 研发
| 指标 | 结果 | 来源 |
|------|------|------|
| 版本：计划/发布/延期/按期率（#10） | ❌ 500→缺口 | — |
| 认证：完成/进行中/逾期（#11） | ❌ 500→缺口 | — |
| 研发/年度目标达成（#12/#13） | 未录入 | 中台 OKR |
> 注明"待研发侧补录/接口修复"。

#### 4. 质量
- **一次合格率 / 不良率**（#6；数据未录入时标注）
- **售后情况**：售后单数（#6 aftersaleCount ✅）、客诉/返修（无口径→缺口）、售后出库 TOP 物料（A6 ✅，列 TOP5）
- 小结 1~2 句。

#### 5. 财务
| 指标 | 本月 | 来源 |
|------|------|------|
| 收款 / 付款 / 净额（#8） | 全 0→未录入 | 中台 |
| 费用合计 + 构成（#9 ✅） | 2,485.6 万（对外采购 2,068.6 万…） | 中台 |
| 资金看板（A3 fund_flow） | 时点 | ERP |
- 小结 1~2 句（费用结构、管控空间）。

#### 6. 人力资源（一行概要）
- 绩效（#14）：总人数 / 平均分 / 区间分布（可选一行，无则省略）。

### 第二部分 经营情况分析和建议

#### 1. 销售业绩与年初目标差距 · 订单冲刺计划
- **目标数据**：#13（年度）+ #12（当月进度）；若全 0 → `【目标尚未录入】`，按实绩给差距判断与建议。
- **差距测算**（有目标时）：年度目标 vs 累计实绩 → 剩余缺口金额/节奏要求（量化）。
- **冲刺计划 3~5 条**（每条有数据支撑）：重点订单转化（销售/发货比）、回款专项（收款口径缺失时用费用/台账替代参照）、客户结构（TOP 缺失时以机型结构/大单情况替代）、下月开局盘点等。

#### 2. 供应链 / 研发 / 质量建议
- 供应链：采购-入库节奏（在途）、库存水位与周转、齐套/缺料（未录入时给推动录入的建议）、生产报工数据补录。
- 研发：版本/认证指标补录与接口修复；目标录入。
- 质量：检测数据补录、售后 TOP 物料的改善方向（结合 A6 具体物料）。
- 每条 1~3 行、引用第一部分量化数据。

#### 3. 财务费用管控建议（2~4 条）
- 基于 #9 费用构成（哪类占比高、是否集中于采购付款）；
- 给出可执行方向（大额付款节奏、行政/差旅占比、收付款凭证补录、避免用交易额推算现金流等）。

### 附录：数据来源与缺口
- 逐项列出：已取到（来源=中台接口/ERP 时点/SQL）+ 缺口清单（原因）+ 口径提示。

## HTML 输出规范（邮件版 · 丰富图表 · 清晰直观）

**硬性**：只用 `div/table/tr/td/p/h2/h3/ul/li/span/b` + 内联 style；禁止 Mermaid、`<style>`、flex/grid、SVG、外链图片字体；每个图形旁必须给数字。

**图表组件（全篇 ≥ 5 组，按有数据的画）**：

1. **指标卡一排**（3~4 个）：大数字 + 环比 `▲ +191.2%`（正向红 `#e5484d`、下降绿 `#16a34a`；逆指标反之）
```html
<td style="width:25%;padding:10px;background:#f5f7fa;text-align:center;border:1px solid #e5e7eb">
  <div style="color:#8f959e;font-size:12px">销售金额</div>
  <div style="font-size:20px;font-weight:700">2,285.2万</div>
  <div style="font-size:12px;color:#e5484d">▲ 环比 +191.2%</div>
</td>
```
2. **趋势条形图**（销售/采购，近 5~6 月）——嵌套 table 色条，宽度%=数值/区间最大值，条形旁给数字（模板见下）
```html
<table style="border-collapse:collapse;width:100%;font-size:12px">
  <tr><td style="width:56px;color:#646a73">9月</td>
    <td><table style="border-collapse:collapse;width:100%;table-layout:fixed"><tr>
      <td style="width:100%;height:14px;background:#2f6bff;border-radius:2px"></td><td style="width:0"></td>
    </tr></table></td>
    <td style="width:88px;text-align:right;font-weight:600">2,285.2万</td></tr>
</table>
```
3. **目标/达成进度条**（有目标或达成率数据时）：灰底 + 彩色填充 + 百分比文案
```html
<table style="border-collapse:collapse;width:100%;table-layout:fixed"><tr>
  <td style="background:#eef1f5;border-radius:6px;height:16px;padding:0">
    <table style="border-collapse:collapse;width:100%;height:16px"><tr>
      <td style="width:62%;background:#2f6bff;border-radius:6px"></td><td style="width:38%"></td>
    </tr></table>
  </td>
</tr></table>
```
4. **费用构成条形**（#9 byType，横向 7 类，按金额降序）
5. **机型结构 / 售后物料 TOP 条形**（A5/A6）

**结构**：严格按「两大部分」全章节输出（含 6 个小节小结、三类建议、附录缺口）。
**篇幅**：8000~14000 字符（信息优先，宁缺毋滥）；从 `<div` 开始、`</div>` 完整闭合；直接输出 HTML 源码，无需思考过程/解释。

### 发送邮件（可选）
收件人由调用方提供时：`send_email(to_recipients=[...], subject="霞智科技 {M} 经营分析报告", body=<HTML>, is_html=True)`；发送后回报发送结果。

## 对话版（在对话中直接输出报告时）

Markdown + **2~4 张 Mermaid 图表**（仅用已取到的数据；每图后保留数据表兜底）：
- 趋势：`xychart-beta`（bar 销售 + line 采购，数值不带千分位）
- 结构：`pie`（费用构成 / 机型结构）

## 红线

- **只读**：禁止 PUT/DELETE/创建类；SQL 只允许 SELECT。
- **只执行 A2 组 15 + A 组 6**（第 2 轮最多补 B 组 4 条）；不 describe 其他表、不重复追加查询。
- 接口未就绪/未录入/无数据源 → **一律标注缺口，禁止编造或强行估算**。
- 工具名不臆造；同一调用失败最多重试 1 次。
