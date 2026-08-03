---
name: device-offline-recovery
description: |
  设备离线核对：get_device_shadow 确认 isOnline；仍离线可 soft_restart，再读影子验证。
---

## 适用场景

- 工单或用户报告设备离线/断连
- 影子 `isOnline=false` 或无法拉到有效影子

## 流程

```
get_device_shadow(device_id, product_id)
  → 已在线：说明已恢复，对照工单故障是否仍在
  → 仍离线：soft_restart(device_id, product_id)
  → 等待后再次 get_device_shadow
  → 恢复：汇报；仍离线：评论/转人工；必要时 connect_terminal
```

取证类工单（如定时任务）不要「顺便重启看看」。低电且在线时可转 `device-return-station`。
