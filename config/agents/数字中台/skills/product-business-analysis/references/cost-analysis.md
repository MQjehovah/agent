# Cost / 成本分析（口径受限，对齐真实数据库）

## 数据库能算的

| 成本项 | 口径 | 数据源 |
|--------|------|--------|
| 物料/采购成本 | `SUM(material_quantity*purchase_price)`（含税单价） | `tb_erp_order_material`、`tb_erp_purchase` |
| BOM 成本 | `SUM(bom_item.quantity * material.price)` | `tb_erp_bom_item` + `tb_erp_material` |
| 毛利近似（产品级） | 销售额 − BOM 成本 | 派生 |

## 数据库不能算的（严禁编造）

- 人工成本、制造费用、固定/变动成本、销售/管理费用
- 盈亏平衡点（无固定成本与单位变动成本数据）
- 标准毛利率（无完整损益表）

## 注意

- `tb_erp_material.price` 可能未维护，BOM 成本可能算出 0。此时**如实报告"成本数据缺失"**，不得用采购价冒充成本强算毛利。

参考具体 SQL：见 SKILL.md「4. 成本分析」。
