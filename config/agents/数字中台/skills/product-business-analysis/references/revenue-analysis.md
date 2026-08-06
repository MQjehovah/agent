# Revenue / 销售分析（对齐真实数据库）

## 可计算指标

| 指标 | 口径 | 数据源 |
|------|------|--------|
| 销售额(本币) | `SUM(material_quantity*purchase_price*exch_rate)` | `tb_erp_sale` + `tb_erp_order_material` |
| 订单数 | `COUNT(DISTINCT s.code)` | `tb_erp_sale` |
| 客户数 | `COUNT(DISTINCT s.customer_code)` | `tb_erp_sale` |
| 客单价 | 销售额 / 订单数 | 派生 |
| 客户分布 TOP | 按客户分组 | `tb_erp_customer` |
| 产品分布 TOP | 按物料分组（发货口径） | `tb_erp_deliver` + 明细 |
| 币种构成 | 按 `exch_name` 分组 | `tb_erp_order_material` |
| 按月趋势 | `DATE_FORMAT(create_time,'%Y-%m')` | `tb_erp_sale` |
| 复购客户数 | 本期下单 >1 单的客户数 | 派生 |

## 注意

- `tb_erp_sale.exch_name` 中英文混用且有脏数据"选项一"，按原文分组并标注。
- `tb_erp_sale.state` 当前全为 `ERP_ORDER_STATE_CREATED`，不要假定中文状态。
- 环比用 UNION ALL 一次取回，勿分多次查询。

参考具体 SQL：见 SKILL.md「1. 销售分析」。
