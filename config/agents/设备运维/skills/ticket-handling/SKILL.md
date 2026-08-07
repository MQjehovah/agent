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

### 硬约束：编排外禁止私自调接口

- **只允许**调用：本流程规定的工单读写工具 + **当前已选型剧本 / 已加载技能**里写明的设备动作
- 路由表、剧本步骤、子技能流程里**没有写到的**接口/工具：**禁止私自调用**
- **工单全程禁止 `dingtalk_*`**（含工作通知/群消息/Webhook）：钉钉 MCP 可存在，但本技能路径下不得调用；评论失败也不得降级钉钉
- **未命中处置剧本**（crash/offline/locate/dock/resource）或处置失败：加载 **`device-evidence-collect`** 取证 → 评论 → **状态 `6` 转人工**；不得借取证下发控制类接口，也不得扫无关工具碰运气
- 现场需要编排与取证技能均未覆盖的能力：评论说明 → 改 `6` 转人工；**不要**自行扩调用面

## 可复用子技能（选型后加载）

| 技能 | 用途 |
|------|------|
| `device-shadow-status` | 读云端影子实时状态（810 枚举） |
| `device-return-station` | 低电/回桩（supplyState 确认） |
| `device-collision-handling` | 碰撞倒退脱困（云端优先） |
| `device-pull-bag` | 按时间匹配录包；有 ticket_id 再挂附件（成功路径或由 evidence-collect 委托） |
| `device-evidence-collect` | 无处理能力/处置失败：按故障选日志或录包取证并分析写回工单 |
| `device-offline-recovery` | 工单离线类恢复（可含软重启） |
| `device-cannot-back-station` | 回站/对桩**失败排查**（非单纯回桩下发） |
| `device-remote-operations` | 仅定位剧本：`relocate` + 再读影子（禁止当万能工具箱） |

## 分析范围

- **工单要处置的对象**：`get_ticket` 的 `faultList[]`（`faultCode` / `faultName` / happenTime）
- **设备实时状态**：加载 `device-shadow-status`（`get_device_shadow`）——**仅用于核对本单故障是否仍在、设备是否可操作**

### 硬约束：禁止用影子实时故障替换本单故障

1. **选型、取证、评论主结论必须围绕 `faultList`**，不得改以影子 `faults[]` 里「当前最显眼」的码为主题
2. 影子里与本单 **不同码/不同现象** 的告警 → **禁止**写成【故障定位】主结论；最多一句旁注「另有实时告警，非本单」
3. 本单故障已恢复、影子另有无关告警 → 仍按**本单**结案（可 `3`）；不要把无关告警当未恢复依据
4. 挂 bag/相机也服务于**本单**现象；勿因影子无关 fault 改走另一类取证叙事

| 建议做 | 禁止 |
|--------|------|
| 按路由表对每条**工单故障**选型并加载对应 device-* | 把影子无关 fault 当主故障定位 |
| 用影子判断本单故障是否仍在 | 评论主结论写成影子其它码/名，却不提本单 faultList |
| 多故障串行、结论合成一条评论 | 评论里堆工具名 / 原始字段名 |
| 取证类先 evidence | 因影子其它告警就对本单取证类乱处置 |

## 可用工具

**工单 CRUD（ticket_ops，本编排始终可用）：**  
`get_ticket` / `change_ticket_status` / `create_ticket_comment` / `create_ticket_attachment` / `list_ticket_statuses` / `set_ticket_token`

**设备 / 终端动作：** 不在此维护白名单。  
**以当前已加载子技能写明的工具为准**（含 `soft_restart`、`relocate`、`upload_bag_file`、`connect_terminal` 等）。技能未写明 → 禁止调用。

查实时状态用影子。失败则说明原因，评论 + 改状态收尾。

## 状态枚举与结案判据

| status | 含义 | 何时使用 |
|--------|------|----------|
| 1 / 2 | 已创建 / 处理中 | 开始可改 `5` |
| 3 | 已完成 | **本单现象已消除/已恢复**（处置成功，或取证证明已恢复且当前无活跃相关异常） |
| 4 | 关闭/拒绝 | 只汇报不改 |
| 5 | 线上运维 | 处置中；**已下发动作、需等待观察**时可保持 `5` 并在评论说明「待观察」 |
| 6 | 问题分析 | **仍异常 / 未恢复 / 需人工跟进** / 终端不可达无法取证 |

**改 `3` 须同时满足：**

1. 本单 `faultList` 对应现象**已不成立**：影子侧已缓解，**或**日志等取证明确写出释放/恢复时间点且影子当前无同码活跃故障
2. 评论已写清依据（处置动作或取证时间点+摘录）
3. 不依赖「好像好了」——无影子/日志复核不得改 `3`

**典型可改 `3`（取证类）：** 急停等已触发又已释放、瞬时告警已过且当前影子正常、人为误触且设备已恢复运行/空闲且无活跃同码故障。

**改 `6`：** 处置失败、取证后仍异常或原因不明需人工、硬件嫌疑未排除、终端不可达无法取证（评论说明原因）。

**保持 `5`：** 已下发回桩/重启等，短时内影子尚未稳定，评论写明待观察点；不要假装已完成。

## 标准流程

```
get_ticket
  → 校验 → status∈{1,2} 可改 5
  → 加载 device-shadow-status（实时状态）
  → 打开 fault-routing.md：按 faultList 选型
  → 加载并执行选中的 device-* / 剧本
  → （失败或无能力）device-evidence-collect
  → create_ticket_comment → 按结案判据改 3 / 5 / 6
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
子技能未写明的接口不要调用；不确定就停在评论+转人工。

### 第四步：执行剧本（委托子技能）

失败或无法闭环时：**统一**加载 **`device-evidence-collect`**（不要另写第二套拉包/拉日志逻辑）。

#### `playbook-crash`

加载并执行 **`device-collision-handling`**。  
成功且需挂录包时，可由 collision 技能内按需委托 **`device-pull-bag`**。  
未清 / 失败 → **`device-evidence-collect`**（优先录包）。

#### `playbook-offline`

加载 **`device-offline-recovery`**（可含软重启）。  
取证类工单仅因实时离线时，更宜在评论说明离线，而不是默认软重启。  
仍失败且已在线可连终端 → **`device-evidence-collect`**（日志）。

#### `playbook-locate`

**仅允许：**

```
get_device_shadow → relocate(device_id, product_id, ...) → 再 get_device_shadow
```

通过加载 **`device-remote-operations`** 的定位步骤执行（该技能在定位场景下禁止扩调用其它控制接口）。  
失败 → **`device-evidence-collect`**（录包 + 定位日志）。

#### `playbook-dock`

加载 **`device-cannot-back-station`**（对桩失败排查）。  
单纯「低电回桩下发」用 `playbook-resource` → `device-return-station`，不要混用。  
无法闭环 → **`device-evidence-collect`**（日志为主）。

#### `playbook-resource`

加载并执行 **`device-return-station`**（`0x20200004` / 需回桩时）。  
水量等其它资源类：默认只核对影子指标；是否远程处置按现场判断。  
无法处置 → **`device-evidence-collect`**（日志）。

不要在子技能尚未确认 `supplyState` 之前就写「回桩成功」。

#### `playbook-evidence`（无匹配处置剧本 / 仅分析类 / 处置失败收尾）

```
加载并执行 device-evidence-collect
  → 按故障现象选相关日志或录包（有就取，没有就说明）
  → 行走/底盘/感知相关，或日志空：find_bags_near_time →【有切片必须】upload → create_ticket_attachment
  → 行走/底盘/感知相关：【须】get_camera_image（相关方位 1～2）→【有 url 必须】create_ticket_attachment
  →【必须】create_ticket_comment（结论+摘录；已挂 bag 名与相机方位须写明）
  →【必须】change_ticket_status：已恢复 → 3；仍异常/需人工 → 6
```

**原则：** 未命中 crash/offline/locate/dock/resource 等处置剧本 = **无远程修复路径** → 取证写清 → 按是否已恢复结案。  
**不要**软重启/倒退碰运气；**不要**为找线索扫无关云端工具；evidence-collect 白名单外的接口一律不调。  
终端取证遵循 `device-evidence-collect` **取证边界原则**；取证结束检查单不变。  
**取证结束检查单（缺一不可）：** 行走/感知：已尝试 bag（有切片则挂）+ 相机（有 url 则挂）→ `create_ticket_comment` → 状态已改 → 再对内汇报。  
**禁止**只说「录包已定位/建议下载」或「请人工看现场」而不挂附件。  
**禁止计划话术收工：** 不得以「接下来我将…」结束回合；已有摘录或负结果必须先写评论再汇报。

### 第五步：合成评论与改状态（取证后不可跳过）

```
create_ticket_comment(ticket_id, content)   # 有分析结论就必须调用
change_ticket_status(ticket_id, status)     # 已恢复 → 3；仍异常/需人工 → 6
```

**评论失败时（禁止降级钉钉）：**
1. 鉴权类错误可再试：`set_ticket_token`（如适用）→ **最多再** `create_ticket_comment` **1 次**
2. 仍失败 → 在对内汇报里写明失败原因与拟写评论摘要；**不要**改发钉钉/邮件/Webhook 冒充已升级
3. **禁止**调用任何 `dingtalk_*` / 工作通知 / 群消息当作评论兜底

评论结构（对外）：`【AI分析|AI处理】现象 → 已做动作 → 取证/复核结论 → 建议人工点`  
- 多故障 → 一条评论  
- 可写与本单相关的实时事实；少写工具名/原始字段名  
- 按上方结案判据改 `3` / `5` / `6`  
- **日志已找到却未 `create_ticket_comment` = 流程未完成**；不要用非工单的 `📋 设备` 格式代替写回工单  
- **日志取证评论必须含：关键时间点 + 若干行带时间戳的原文摘录 + 路径**（见 `device-evidence-collect`）；禁止只有「已分析日志，存在异常」这类空结论  
- 禁止以「接下来我将解析日志…」作为最终回复

| 场景 | 评论示例 |
|------|----------|
| 急停已释放可完成 | `【AI分析】…急停为人为按钮触发，已于 … 释放；影子当前无活跃同码故障。无需远程处置，已完成。` + 时间点与摘录 |
| 行走感知取证挂包 | `【AI分析】…与行走/感知相关。已挂故障时刻录包（供分析触发原因）与实时相机图（供看四周/障碍）；日志有则附摘录。请人工结合附件复核。` |
| 相机取证挂图 | `【AI分析】…已拉取方位「…」实时图挂附件；失败则说明编解码/业务异常。` |
| 日志取证仍需人工 | `【AI分析】…已拉取日志（路径…）。结论：…仍异常/原因不明。关键时间点：…。摘录：…。已转问题分析。` |
| 定时任务取证 | `【AI分析】工单故障为「存在故障定时任务执行失败」。已拉取相关日志（含时间点与摘录）：…；当前无法远程修复，已转问题分析。` |
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

期望调用序列见 [`references/playbook-call-sequences.md`](references/playbook-call-sequences.md)。

## 实践提示

- 先路由、再加载子技能；**未命中处置剧本 → evidence → 已恢复改 `3`，否则 `6`**
- 实时状态走 `device-shadow-status`
- 倒退留给碰撞相关；离线类以影子确认并转人工为主
- 不对 3/4 重复改状态；挂 bag/相机少而准（各 1～3），不编造 URL；**已定位的包必须挂；行走/感知有 url 的相机图必须挂**
- **编排/技能未写明的接口一律不调**；缺处置能力先走 `device-evidence-collect` 取证分析，再转人工
