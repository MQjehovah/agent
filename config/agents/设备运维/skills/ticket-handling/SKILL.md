---
name: ticket-handling
description: |
  BMS 工单处理编排入口。拉单后按 fault-routing 选型；设备动作委托 device-* 可复用技能。
  定时任务等取证类（如 0x20300005）优先 playbook-evidence，不宜顺手软重启/倒退。
  对外评论围绕本工单故障，可引用相关实时事实，少写工具名与原始字段名。
---

## 触发条件

出现以下任一情况时**优先**激活本技能：

- 用户/Webhook 提供 `ticket_id`
- 文案含 `【BMS工单AI处理】`、工单号 `SHxxxx`、`BMS工单`
- 明确要求「按工单处理 / AI 处理工单」

## 角色

本技能是**编排器**：

1. 工单读写与结案习惯在本文件（拉单 / 改状态 / 评论 / 附件）
2. **先读** [`references/fault-routing.md`](references/fault-routing.md)，再**加载并执行**对应 `device-*` 技能
3. 路由表是优先建议，不是死命令；但不要跳过选型直接乱操作

## 可复用子技能（选型后加载）

| 技能 | 用途 |
|------|------|
| `device-shadow-status` | 读云端影子实时状态（810 枚举） |
| `device-return-station` | 低电/回桩（supplyState 确认） |
| `device-collision-handling` | 碰撞倒退脱困（云端优先） |
| `device-pull-bag` | 按时间匹配录包；有 ticket_id 再挂附件 |
| `device-offline-recovery` | 工单离线类恢复（可含软重启） |
| `device-cannot-back-station` | 回站/对桩**失败排查**（非单纯回桩下发） |

## 分析范围

- **工单要处置的对象**：`get_ticket` 的 `faultList[]`
- **设备实时状态**：加载 `device-shadow-status`（`get_device_shadow`）

| 建议做 | 不太建议 |
|--------|----------|
| 按路由表对每条工单故障选型并加载对应 device-* | 跳过影子去猜设备状态 |
| 用影子实时状态判断本单故障是否仍在 | 把影子里与本单无关的 fault 全写进 BMS 评论 |
| 多故障串行、结论合成一条评论 | 评论里堆工具名 / 原始字段名 |
| 取证类先 evidence | 因离线就对取证类单乱倒退/乱操作 |

## 可用工具

**工单专属（ticket_ops）：** `get_ticket` / `change_ticket_status` / `create_ticket_comment` / `create_ticket_attachment` / `list_ticket_statuses` / `set_ticket_token`

**云端设备（remote_operation）：** `get_device_shadow` / `list_device_bags` / `find_bags_near_time` / `device_backward` / `device_back_to_station`

**终端（按需）：** `connect_terminal` 等。查实时状态用影子。失败则说明原因，评论+改 `6` 收尾。

## 状态枚举

| status | 含义 | 行为 |
|--------|------|------|
| 1 / 2 | 已创建 / 处理中 | 开始可改 `5` |
| 3 / 4 | 已完成 / 关闭拒绝 | 只汇报不改 |
| 5 | 线上运维 | 处置中 |
| 6 | 问题分析 | 需人工 |

## 标准流程

```
get_ticket
  → 校验 → status∈{1,2} 可改 5
  → 加载 device-shadow-status（实时状态）
  → 打开 fault-routing.md：按 faultList 选型
  → 加载并执行选中的 device-* / 剧本
  → create_ticket_comment → 可 3 或 6
  → 对内汇报（含剧本 ID）
```

### 第一步：拉单与接手

1. `get_ticket(ticket_id)`；失败则停  
2. `status`∈{3,4}：摘要结束  
3. 无 `deviceId`：倾向改 `6`  
4. 无 `faultList`：可读影子补查；仍无法定性 → `6`  
5. `status`∈{1,2}：`change_ticket_status(..., 5)`  

### 第二步：读设备实时状态

加载并执行 **`device-shadow-status`**。  
对照每条工单故障：同码是否仍在；相关指标（battery / water / locate / crash / supplyState）。  
离线/crash 等写入判断依据；是否加开 offline/crash 剧本看路由表。

### 第三步：故障路由（先做再动手）

打开 **`references/fault-routing.md`**：

1. **先**用每条 `faultCode`/`faultName` 收集剧本  
2. 结合影子实时指标判断是否仍存在、是否可远程处置  
3. 取证/资源类单不宜再叠加倒退等远程动作  
4. 去重后按建议优先级串行；同剧本只跑一次  
5. 每条留下一句对外结论素材  

选型后再调子技能；不要「先倒退试试」再回头看路由。

### 第四步：执行剧本（委托子技能）

#### `playbook-crash`

加载并执行 **`device-collision-handling`**；需要录包时再加载 **`device-pull-bag`**（有 ticket_id 可挂附件）。

#### `playbook-offline`

加载 **`device-offline-recovery`**（可含软重启）。  
取证类工单仅因实时离线时，更宜在评论说明离线，而不是默认软重启。

#### `playbook-locate`

见路由表；定位类走 `relocate`（`device-remote-operations`）。

#### `playbook-dock`

加载 **`device-cannot-back-station`**（对桩失败排查）。  
单纯「低电回桩下发」用 `playbook-resource` → `device-return-station`，不要混用。

#### `playbook-resource`

加载并执行 **`device-return-station`**（`0x20200004` / 需回桩时）。  
水量等其它资源类：默认只核对影子指标；是否远程处置按现场判断。

不要在子技能尚未确认 `supplyState` 之前就写「回桩成功」。

#### `playbook-evidence`（如 `0x20300005`）

```
device-shadow-status 判断故障是否仍在 / 是否在线
  → （可）device-pull-bag；有 url 则 create_ticket_attachment
  → 评论说明取证结果 / 离线无法取证 → 倾向状态 6
```

这类单：**软重启、倒退通常不是合适手段**；离线则说明无法远程取证即可。

### 第五步：合成评论与改状态

```
create_ticket_comment(ticket_id, content)
```

- 多故障 → 一条评论  
- 可写与本单相关的实时事实（离线、电量等）；少写工具名/原始字段名  
- 证据足可 `3`；否则 `6`  

| 场景 | 评论示例 |
|------|----------|
| 定时任务取证/离线 | `【AI分析】工单故障为「存在故障定时任务执行失败」。当前设备离线，暂无法远程取证与恢复定时任务，已转问题分析，请人工确认网络与任务配置。` |
| 碰撞已处置并挂附件 | `【AI处理】已远程倒退尝试脱困。故障时刻附近录包已挂附件，请复核后闭环。` |
| 电量已对桩（未下发） | `【AI分析】经核查，设备电量约 xx%，当前已在工作站对桩/充电过程中，暂未再下发回桩。建议确认充电正常后闭环。` |
| 回桩成功（supplyState∈1/2/3） | `【AI处理】经核查设备电量偏低且原先未对桩，已远程下发回桩；复核显示设备已进入前往工作站/充电过程，请关注后续电量恢复后闭环。` |
| 回桩未确认对桩 | `【AI分析】已远程下发回桩，但复核时设备尚未进入对桩供电状态，请人工现场确认路径与工作站。已转问题分析。` |

### 第六步：对内汇报

```
📋 工单: {code} (id={id})
📱 设备: {deviceId} / {productId}
📌 状态: {旧} → {新}
⚠️ 工单故障: …
🗺 路由: [playbook-evidence, ...]
🔍 各剧本结果（对内，含影子实时要点与所用 device-*）
💬 评论: 已写回 / 失败
```

## 实践提示

- 先路由、再加载子技能；`0x20300005` → evidence  
- 实时状态走 `device-shadow-status`  
- 倒退留给碰撞相关；离线类以影子确认并转人工为主  
- 不对 3/4 重复改状态；挂 bag 宜少而准，不编造 URL  
