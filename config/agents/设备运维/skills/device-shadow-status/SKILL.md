---
name: device-shadow-status
description: |
  通过云端 get_device_shadow（remote_operation）查询设备实时状态。
  查设备实时状态请用本技能（get_device_shadow）。
---

## 适用场景

- 用户要求查看设备实时状态 / 影子
- 工单或运维处置前核对在线、电量、对桩、定位、碰撞、当前 faults
- 需要解读 810 的 `robotMode` / `control_mode` / `supplyState`

## 前置条件

- 知道 `deviceId` + `productId`

## 流程

```
get_device_shadow(device_id, product_id)
  → 读摘要字段
  → 用人话汇报（少堆原始字段名）
```

### 重点字段

| 字段 | 用途 |
|------|------|
| `isOnline` / 在线相关 | 是否在线 |
| `battery` | 电量 |
| `locate` | 定位 |
| `crash` | 是否碰撞中 |
| `faults[]` | 当前故障列表 |
| `robotMode` / `robotModeName` | 机器人状态 |
| `control_mode` / `controlModeName` | 手动/自动 |
| `supplyState` / `supplyStateName` / `supplyEngaged` | 对桩/供电 |

**对桩/供电：看 `supplyState∈{1,2,3}` 或 `supplyEngaged=true`，不要看 `is_charge`（810 影子常无）。**

### 影子枚举（810）

**robotMode**：`IDLE` 空闲 / `TASK` 任务中 / `PAUSE` 暂停 / `FAULT` 错误 / `MAP` 建图 / `OTA` / `FACTORY` 工厂

**control_mode**：`CONTROL_MODE_MANUAL` 手动 / `CONTROL_MODE_AUTO` 自动

**supplyState**：`0` 空闲 · `1` 前往工作站 · `2` 加排水中 · `3` 仅充电 · `4` 退桩 · `5` 手动补给 · `6` 等待外设关闭；**1/2/3 = 已对桩供电**

## 输出习惯

- 对内：可列关键原始值便于后续剧本判断
- 对外（工单评论等）：用人话写相关事实（如电量约 xx%、已在充电过程），少写工具名与枚举常量

## 注意事项

- 影子 = 实时状态源
- 失败时如实说明，不要编造在线/电量/对桩状态
