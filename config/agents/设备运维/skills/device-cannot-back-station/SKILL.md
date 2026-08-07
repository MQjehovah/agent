---
name: device-cannot-back-station
description: |
  回站/对桩/前往充电点失败排查。以影子为准，辅以有限云端只读；
  可远程动作仅 relocate + device_back_to_station。无法闭环则转 device-evidence-collect。
---

## 触发条件

1. 与工作站反复对接（反复对桩）
2. 停在桩前但不对接（不对桩）
3. 停留清洁区，上报「无法返回工作站」或「前往充电点失败」
4. 工单路由为 `playbook-dock`

## 前置知识

- 设备常自恢复对接多次后才上报
- 对接常见前置：工作站预约/通讯、激光看到反光贴、点位距离约 1.5～2m
- **低电单纯回桩下发**走 `device-return-station`，不要用本技能替代

## 允许的工具

| 工具 | 用途 |
|------|------|
| `get_device_shadow` | 在线、位姿、faults、supplyState、battery |
| `get_clean_info` | 清洁/补给相关只读（若接口可用） |
| `relocate` | 仅「未返回/路径异常且怀疑定位」时 |
| `device_back_to_station` | 排查后尝试回充（非硬件确认前慎用） |

禁止：已删除的 FAE 工具名（`get_cost_map` / `get_chassis_info` 等）；禁止工程模式乱控。  
无法闭环 → **`device-evidence-collect`**（日志为主）。

## 诊断流程

### 共通

1. `get_device_shadow(device_id, product_id)`  
2. 解读：`isOnline`、`supplyState`、`faults[]`、电量、是否已在前往工作站（1/2/3）  
3. 按现象分支；硬件嫌疑（极片氧化、液位计、反光贴损坏等）→ 评论转人工 + evidence-collect

### 现象1：反复对桩

等待最终报错（影子 faults / 工单 faultName），对照：

| 报错倾向 | 可能原因 | 远程可做 |
|----------|----------|----------|
| 对桩超时 | 极片/微动/安装 | 不可远程修硬件 → 取证转人工 |
| 加水/排水异常 | 液位计、球阀、通讯模式 | 只读确认；硬件 → 人工 |
| 充电异常 | 极片、工作站供电、电池 | 只读确认；硬件 → 人工 |

### 现象2：不对桩

1. 影子确认是否大致到桩前  
2. 故障名是否含预约失败 / Lora / 工作站占用 / ID 不一致 → 现场/配置类，转人工  
3. 「对桩超时」+ 反光识别嫌疑 → 转人工（安装/反光贴）  
4. 远程无法改地图点位时：评论说明 + evidence-collect

### 现象3：未返回工作站

1. 影子看位姿与 faults  
2. 怀疑定位 → `relocate` → 再读影子 → `device_back_to_station`  
3. 怀疑禁行区/虚拟墙/窄门 → **无法远程改地图**，取证转人工  
4. 回充下发后复核 `supplyState`；未进入 1/2/3 → 不写成功，转 `6` 或保持 `5` 待观察

## 通用顺序

```
get_device_shadow
  → 分类现象（反复对桩 / 不对桩 / 未返回）
  → 硬件或地图配置问题 → device-evidence-collect → 状态 6
  → 定位可疑 → relocate → device_back_to_station → 再读影子
  → 仍失败 → device-evidence-collect
```

## 常见问题速查

| 现象 | 优先检查 | 常见原因 | 能否远程处理 |
|------|----------|----------|-------------|
| 反复对桩 | supply/faults、加水排水充电 | 极片、液位计、通讯 | 多数否（硬件） |
| 不对桩 | 预约/占用/ID、对桩超时 | Lora、反光贴、点位 | 多数否 |
| 不返回工作站 | 定位、路径阻断 | 定位丢、禁行区 | 部分可（重定位+回充） |
