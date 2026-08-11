---
name: device-cannot-back-station
description: |
  回站/对桩/前往充电点失败排查。影子只判断现在能否操作；
  未闭环须转 device-evidence-collect，按通用「happenTime 证据」补历史日志。
  可远程动作仅 relocate + device_back_to_station。
---

## 触发条件

1. 与工作站反复对接（反复对桩）
2. 停在桩前但不对接（不对桩）
3. 停留清洁区，上报「无法返回工作站」「找不到回桩点/停靠点」「前往充电点失败」
4. 工单路由为 **`playbook-dock`**（名称以 fault-routing 为准，勿自造剧本 ID）

## 前置知识

- 设备常自恢复对接多次后才上报
- 对接常见前置：工作站预约/通讯、激光看到反光贴、点位距离约 1.5～2m
- **低电单纯回桩下发**走 `device-return-station`，不要用本技能替代
- 遵循 ticket-handling **「当前态 ≠ 故障时刻证据」**：本技能的影子/下发只服务处置；结案证据交给 evidence-collect

## 允许的工具

| 工具 | 用途 |
|------|------|
| `get_device_shadow` | 在线、位姿、faults、supplyState、battery |
| `get_clean_info` | 清洁/补给相关只读（若接口可用） |
| `relocate` | 仅「未返回/路径异常且怀疑定位」时 |
| `device_back_to_station` | 排查后尝试回充（非硬件确认前慎用） |

禁止：已删除的 FAE 工具名（`get_cost_map` / `get_chassis_info` 等）；禁止工程模式乱控。  
终端拉日志 → **`device-evidence-collect`**（不在本技能扩工具面）。

## 诊断流程

### 共通

1. `get_device_shadow(device_id, product_id)`  
2. 解读：`isOnline`、`supplyState`、`faults[]`、电量、是否已在前往工作站（1/2/3）  
3. 按现象分支；硬件/地图/点位嫌疑 → 转 evidence-collect 后人工

### 现象1：反复对桩

等待最终报错（影子 faults / 工单 faultName），对照：

| 报错倾向 | 可能原因 | 远程可做 |
|----------|----------|----------|
| 对桩超时 | 极片/微动/安装 | 不可远程修硬件 → 取证转人工 |
| 加水/排水异常 | 液位计、球阀、通讯模式 | 只读确认；硬件 → 人工 |
| 充电异常 | 极片、工作站供电、电池 | 只读确认；硬件 → 人工 |

### 现象2：不对桩 / 找不到回桩点或停靠点

1. 影子确认是否大致到桩前、定位是否明显异常  
2. 预约失败 / Lora / 占用 / ID / 找不到点 → 多为配置/现场类，**仍须 evidence 补当时证据**再转人工  
3. 「对桩超时」+ 反光识别嫌疑 → 取证 + 转人工  

### 现象3：未返回工作站

1. 影子看位姿与 faults  
2. 怀疑定位 → `relocate` → 再读影子 → `device_back_to_station`  
3. 怀疑禁行区/虚拟墙/窄门 → **无法远程改地图**，取证转人工  
4. 回充下发后复核 `supplyState`；未进入 1/2/3 → 不写成功；**evidence 后**改 `6` 或保持 `5` 待观察

## 通用顺序

```
get_device_shadow
  → 分类现象
  → （可选）定位可疑：relocate → device_back_to_station → 再读影子
  → 未对桩成功 / 需人工：device-evidence-collect（happenTime 证据，见 ticket-handling 总原则）
  → 交编排写评+改状态
```

## 常见问题速查

| 现象 | 优先检查 | 常见原因 | 能否远程处理 |
|------|----------|----------|-------------|
| 反复对桩 | supply/faults + **当时日志** | 极片、液位计、通讯 | 多数否 |
| 不对桩 / 找不到点 | **当时日志** + 点位/预约 | Lora、反光贴、地图停靠点 | 多数否 |
| 不返回工作站 | 定位、路径 + **当时日志** | 定位丢、禁行区 | 部分可；不成则取证转人工 |
