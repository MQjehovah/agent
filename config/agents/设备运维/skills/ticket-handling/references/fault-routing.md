# 工单故障路由表

`ticket-handling` 编排参考。按**处置动作族**选型，再加载对应 `device-*` 技能。  
下列为**优先建议**，可按现场判断微调；不要跳过选型直接乱操作。

**码与名称：** 表中 `faultCode` 仅为提示；选型以工单 `faultName` / 故障域归类为准（名称命中可覆盖单码歧义）。

**硬约束：**

1. 只调用当前剧本 / 已加载技能写明的接口；编排未覆盖的**处置**禁止私自调用
2. **未命中下表任一处置剧本**（或虽命中但无远程处置能力）→ 一律 `playbook-evidence`：加载 **`device-evidence-collect`** 取证 → 评论 → **已恢复改 `3`，否则 `6`**
3. 处置失败同样走 evidence-collect，再转人工
4. 取证时**只走 evidence-collect 允许的工具**；不要为「多找线索」扫无关云端接口
5. **主分析对象 = 工单 `faultList`**；影子实时 `faults` 只核对本单是否仍在，**禁止**用无关实时码替换本单结论

## 选型顺序（建议）

1. **工单 `faultList` 的 `faultCode` / `faultName`**（要处置什么）——**唯一选型主依据**
2. **设备影子实时状态**（加载 `device-shadow-status`）——只回答：本单还在吗、设备能否操作
3. **未命中 → `playbook-evidence`（默认）**

影子 = 实时状态，用来判断本工单故障是否仍在、能否远程处置。  
不宜仅因实时离线/碰撞，就对「取证类」工单去乱倒退或假装软重启。  
**禁止：** 工单 faultList 是 A，评论主结论写成影子里无关的 B。

多条 `faultList` 时：按剧本优先级去重后**串行**（同剧本只跑一次）：

| 优先级 | 剧本 ID | 说明 |
|--------|---------|------|
| 1 | `playbook-crash` | 工单碰撞/撞障相关时优先 |
| 2 | `playbook-offline` | 工单本身是离线/断连类时 |
| 3 | `playbook-locate` | 定位类 |
| 4 | `playbook-dock` | 回站/对桩**失败排查** |
| 5 | `playbook-resource` | 电量/水量等；低电回桩走 `device-return-station` |
| 6 | `playbook-evidence` | **默认兜底**：无匹配剧本 / 未知故障 / 仅取证类 → 取证后按是否已恢复结案 |

结案判据见 `ticket-handling`（`3` / `5` / `6`）。  
对外评论：各条结论合成一条；可写相关实时事实，少写工具名/原始字段名。

---

## 路由表

| 剧本 ID | 何时优先选用 | 建议步骤（加载技能） | 成功判据 | 默认状态 |
|---------|--------------|----------------------|----------|----------|
| `playbook-crash` | 碰撞/撞障类码或名称；用影子 `crash` 看是否仍碰撞 | **`device-collision-handling`**；成功可按需挂 bag；未清/失败 → **`device-evidence-collect`**（优先录包） | 碰撞现象缓解 | 未清 → `6` |
| `playbook-offline` | 工单明确离线/断连 | **`device-offline-recovery`**；仍失败且已在线 → **`device-evidence-collect`**（日志） | 影子恢复在线 | 仍离线 → `6` |
| `playbook-locate` | 工单定位类 | **`device-remote-operations`**：仅 shadow → `relocate` → 再 shadow；失败 → **`device-evidence-collect`**（录包+定位日志） | 影子 locate 恢复 | 否则 → `6` |
| `playbook-dock` | 回站/对桩/充电点失败 | **`device-cannot-back-station`**；无法闭环 → **`device-evidence-collect`**（日志为主） | 现象消除或给出排查结论 | 硬件/不明 → `6` |
| `playbook-resource` | `0x20200004` 或电量/水量类 | **`device-return-station`**；无法处置 → **`device-evidence-collect`**（日志） | supplyEngaged 或电量恢复 | 未进入 1/2/3 → `6` 或保持 `5` 待观察 |
| `playbook-evidence` | **未命中上表**、未知码、定时任务等仅分析类 | **`device-evidence-collect`**；不做倒退/重启等处置 | 取证与分析写清 | **已恢复 → `3`；否则 `6`** |

### faultCode 补充（提示用，可继续扩；与名称冲突时以 faultName/故障域为准）

| faultCode / 模式 | 建议剧本 | 备注 |
|------------------|----------|------|
| `0x20200004` | `playbook-resource` | 低电回桩；确认 supplyState 后再评论成败 |
| `0x2020000A` | `playbook-evidence` | 急停：取证；已释放且影子无活跃急停 → 可 `3`；仍急停/不明 → `6` |
| `0x2020000C` | `playbook-evidence` | 防跌落（行走/感知类一例）：取证；定位到 bag **必须挂**；实时相机 **须拉并挂**（有 url） |
| `0x20300005` | `playbook-evidence` | 定时任务失败：取证，不重启/不倒退 |
| `0x20100001` / `0x20100002` | `playbook-crash` | 碰撞/撞障类（若现场码不同以名称为准） |
| `0x20400001` / `0x20400002` | `playbook-locate` | 定位丢失/定位异常类 |
| `0x20500001` | `playbook-offline` | 离线/断连类 |
| `0x20600001` / `0x20600002` | `playbook-dock` | 回站失败/对桩失败/充电点失败类 |
| 名称含「碰撞」「撞障」 | `playbook-crash` | |
| 名称含「定位」 | `playbook-locate` | |
| 名称含「离线」「断连」 | `playbook-offline` | 此时才优先软重启 |
| 名称含「回站」「回桩」「对桩」「充电点失败」「离桩超时」「无法回桩」「无法返回工作站」「找不到回桩点」「停靠点」 | `playbook-dock` | 排查用 cannot-back；未闭环须 evidence（happenTime）；单纯低电回桩下发用 resource |
| 名称含「低电」「电量」「水量」 | `playbook-resource` | |
| 名称含「急停」 | `playbook-evidence` | 取证；已恢复可 `3`，勿自行发明远程复位 |
| 名称含「防跌落」 | `playbook-evidence` | 行走/感知类；bag + 实时相机均须挂（有则挂） |
| 名称含「任务暂停」「暂停超时」「定时任务」「手动模式下无法执行」 | `playbook-evidence` | **日志主查仅 app**（SC50=`xzrobot_app2`）；**不要**默认查 `xzrobot_driver2`；影子相关字段只陈述事实，勿杜撰界面操作步骤 |
| `0x20200016`（若现场为此码） | `playbook-evidence` | 任务暂停超时：同上，主查 app |
| **其它任意故障名/码** | **`playbook-evidence`** | **无剧本 = 取证后结案；已恢复才 `3`，否则 `6`。仅诊断/建议人工 ≠ `3`（通用于一切未消除故障）**；行走/感知须查 bag 并拉相机，定位到/有 url 则必须挂 |

> 上表扩展码为编排占位：若云端实际码值不同，以工单 `faultName` 模糊匹配为准，并回写修正本表。

---

## 路由算法（建议）

```
ticket = get_ticket(...)
加载 device-shadow-status   # 实时状态
candidates = []

# 1) 先按工单 faultList 映射剧本（要处置什么）
for fault in faultList:
  map faultCode / faultName → playbook
  candidates += that playbook

# 2) 用影子实时状态判断能否执行、是否仍存在
#    - 工单已是 evidence/resource：离线/crash 写入结论即可，不必再加 offline/crash 剧本
#    - 工单本身是碰撞/离线类：结合影子确认后再执行对应剧本

# 3) candidates 空 / 未命中处置剧本 → playbook-evidence
# 4) 去重，按优先级串行；每条加载对应 device-*
# 5) 失败/无能力 → device-evidence-collect → 按结案判据改 3/5/6（已恢复→3，待观察→5，仍异常/需人工→6）
# 6) 合成一条评论 → 再改状态（勿默认一律 6）
```

**实践提示：**

- 工单 `faultList` 决定处置目标；影子提供实时状态
- **影子无关 fault 禁止当主结论**（工单 A ≠ 影子里无关的 B）
- **没有处置剧本就取证转人工**（已恢复可 `3`），不要扫工具碰运气
- evidence 路径下：**行走/底盘/感知相关**故障须按 happenTime 查录包并拉实时相机；**定位到切片 / 有 url 则必须挂附件**（bag=触发原因，相机=当前四周/障碍）
- `0x20200004`：委托 `device-return-station`（supplyState 1/2/3=已对桩；`is_charge=true`+`dock=false` 一般为手动充电）
- 倒退只给碰撞；软重启只给工单离线类
- 影子里与本单无关的 fault：不必写进对外评论（最多一句旁注）
- **编排未写明的接口不调**；失败统一 evidence-collect

### 影子常用枚举（810）

**robotMode**：`IDLE` / `TASK` / `PAUSE` / `FAULT` / `MAP` / `OTA` / `FACTORY`  

**control_mode**：`CONTROL_MODE_MANUAL` / `CONTROL_MODE_AUTO`（影子字段，只陈述事实）  

**supplyState**：`0` 空闲 · `1` 前往工作站 · `2` 加排水中 · `3` 仅充电 · `4` 退桩 · `5` 手动补给 · `6` 等待外设关闭；**1/2/3 视为已对桩供电**
