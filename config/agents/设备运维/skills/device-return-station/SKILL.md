---
name: device-return-station
description: |
  云端回桩（remote_operation）：先影子确认 supplyState，再 device_back_to_station。
  工单低电与非工单「回桩」共用；对桩失败排查用 device-cannot-back-station。
---

## 适用场景

- 用户要求远程回桩 / 回工作站
- 低电（如 `0x20200004`）需回站充电
- 确认设备是否已在对桩供电过程中

## 不适用

- 「回站失败 / 对桩失败 / 充电点失败」排查 → 用 `device-cannot-back-station`
- 仅查询状态、不下发回桩 → 用 `device-shadow-status`

## 前置条件

- 知道 `deviceId` + `productId`
- 实时状态用 `get_device_shadow`（可先加载 `device-shadow-status`）

## 流程

```
get_device_shadow
  → 看 battery（若因低电回桩：是否仍低）
  → 看 supplyState / supplyEngaged（勿依赖 is_charge）
       supplyState∈{1,2,3} = 前往工作站/加排水/仅充电 → 已对桩供电
  → 已 supplyEngaged：不下发，只说明当前电量与对桩状态
  → 未对桩：device_back_to_station(device_id, product_id)
  → 等待数秒后再次 get_device_shadow
  → 确认 supplyState∈{1,2,3} / supplyEngaged 后再报成败
```

不要在只下发回桩、尚未用影子确认 `supplyState` 之前就写「回桩成功」。  
可参考 `robotMode`、`control_mode` 作说明，但**回桩成败以 `supplyState` 为准**。

## 成功 / 失败判据

| 结果 | 判据 |
|------|------|
| 无需下发 | 下发前已 `supplyEngaged`（或 supplyState∈{1,2,3}） |
| 成功 | 下发后复核 `supplyState∈{1,2,3}` 或 `supplyEngaged=true` |
| 失败 | 下发后仍未进入 1/2/3；说明原因，工单场景倾向转人工 |

## 工单衔接

若由 `ticket-handling` 调用：把上述结论素材交给编排器写 `create_ticket_comment`，本技能不直接改工单状态。

## 注意事项

- 810 常无 `is_charge`，一律用 `supplyState`
- 下发失败（接口报错）如实说明，勿假装已回桩
