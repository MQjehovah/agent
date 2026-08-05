---
name: device-remote-operations
description: |
  通用远程运维：状态以 get_device_shadow 为准；仅执行本技能/上游剧本写明的动作，禁止私自扩调用。
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

仅在**本技能步骤或上游 ticket-handling 剧本明确要求**时调用：`soft_restart`、`relocate`、`set_control_mode`、`device_backward`、`device_back_to_station`、`upload_bag_file`。  
步骤未写到的接口（工程模式、`remote_action`、终端等）禁止私自调用。状态一律 `get_device_shadow`。
