---
name: device-remote-operations
description: |
  定位剧本专用远程动作：get_device_shadow → relocate → 再读影子。
  禁止当作万能工具箱；其它故障走对应 device-* 或 device-evidence-collect。
---

## 适用场景

- 上游 `ticket-handling` 选型为 **`playbook-locate`**
- 非工单且用户明确要求「重定位 / 定位丢失恢复」

## 定位流程（仅此）

```
get_device_shadow(device_id, product_id)
  → 确认在线且存在定位异常（或用户明确要求 relocate）
  → relocate(device_id, product_id, position=...)   # position 按现场/地图需要；不明可先空 param
  → 等待数秒 → 再 get_device_shadow
  → locate 恢复：留下成功结论素材
  → 仍异常：加载 device-evidence-collect（录包 + 定位日志）→ 转人工
```

## 允许的工具（定位场景）

- `get_device_shadow`
- `relocate`

## 禁止

- 禁止在本技能内调用：`soft_restart`、`device_backward`、`set_control_mode`、`factory_reset`、`remote_action`、工程模式、终端控制命令等
- 碰撞 → `device-collision-handling`；低电回桩 → `device-return-station`；离线 → `device-offline-recovery`；无能力 → `device-evidence-collect`
