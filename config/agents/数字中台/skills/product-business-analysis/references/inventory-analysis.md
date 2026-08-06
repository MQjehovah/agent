# Inventory / 库存分析（对齐真实数据库）

## 可计算指标

| 指标 | 口径 | 数据源 |
|------|------|--------|
| 库存数量/金额 | `SUM(quantity)` / `SUM(quantity*price)` | `tb_erp_repository_material`（`quantity>0`） |
| SKU 数 | `COUNT(DISTINCT material_code)` | `tb_erp_repository_material` |
| 安全库存预警 | `quantity < warning_quantity` | `tb_erp_material`（category=PRODUCT） |
| ABC 分类 | 按库存金额降序累计占比 | `tb_erp_repository_material` |
| 核心产品库存 | SW50/GT/Titan810 | `tb_erp_repository_material` |

## 数据库不能算的

- 库存周转率 / 周转天数（无期初/期末快照、无销售成本历史）——报告需标注数据缺口。
- 缺货天数、持有成本、损耗成本：无数据源。

## 注意

- 库存"金额"口径 = `quantity * price`（`tb_erp_repository_material.price`），不是物料主档价格。

参考具体 SQL：见 SKILL.md「3. 库存分析」。
