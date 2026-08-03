---
name: device-remote-operations
description: |
  通用远程运维：状态以 get_device_shadow 为准；定位丢失用 relocate；其它按影子分支到专项技能。
---

## 流程

```
get_device_shadow(device_id, product_id)
  → 解读 isOnline / locate / crash / battery / supplyState / faults
  → 碰撞 → device-collision-handling
  → 低电回桩 → device-return-station
  → 定位丢失 → relocate(device_id, product_id, position=...) → 再读影子
  → 离线 → device-offline-recovery
  → 其它 → 取证（device-pull-bag）或转人工
```

常用 `remote_operation`：`soft_restart`、`relocate`、`set_control_mode`、`device_backward`、`device_back_to_station`、`upload_bag_file`。状态一律 `get_device_shadow`。
