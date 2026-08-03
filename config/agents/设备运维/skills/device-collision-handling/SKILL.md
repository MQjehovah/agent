---
name: device-collision-handling
description: |
  处理设备碰撞/撞障脱困。状态以云端影子为准；倒退用 device_backward。
  工单 playbook-crash 与非工单碰撞告警共用；需要录包时再加载 device-pull-bag。
---

## 适用场景

- 用户报告碰撞 / 撞障 / 被困
- 影子 `crash=true` 或工单故障名称含碰撞撞障

## 前置条件

- 知道 `deviceId` + `productId`
- 实时状态用 `get_device_shadow`（可先加载 `device-shadow-status`）

## 流程

```
get_device_shadow
  → 确认在线与 crash / 相关 faults
  → device_backward(device_id, product_id)
  → 再读影子确认 crash 是否缓解
  → （按需）device-pull-bag；有 ticket_id 且有 url 再挂附件
```

离线则说明无法远程倒退，升级人工。多次倒退无效勿死循环。

工单场景：结论素材交给 `ticket-handling` 写评论。

## 升级条件

- 多次倒退后影子仍显示碰撞
- 碰撞后离线且无法恢复
- 需要终端手动脱困时再 `connect_terminal`
