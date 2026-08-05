# 工单故障路由表

`ticket-handling` 编排参考。按**处置动作族**选型，再加载对应 `device-*` 技能。  
下列为**优先建议**，可按现场判断微调；不要跳过选型直接乱操作。

**硬约束：** 只调用当前剧本 / 已加载技能写明的接口；编排与技能未覆盖的能力禁止私自调用，缺则评论转人工（倾向状态 `6`）。

## 选型顺序（建议）

1. **工单 `faultList` 的 `faultCode` / `faultName`**（要处置什么）
2. **设备影子实时状态**（加载 `device-shadow-status`）
3. 默认：`playbook-evidence`

影子 = 实时状态，用来判断本工单故障是否仍在、能否远程处置。  
不宜仅因实时离线/碰撞，就对「定时任务 / 取证类」工单去乱倒退或假装软重启。

多条 `faultList` 时：按剧本优先级去重后**串行**（同剧本只跑一次）：

| 优先级 | 剧本 ID | 说明 |
|--------|---------|------|
| 1 | `playbook-crash` | 工单碰撞/撞障相关时优先 |
| 2 | `playbook-offline` | 工单本身是离线/断连类时 |
| 3 | `playbook-locate` | 定位类 |
| 4 | `playbook-dock` | 回站/对桩**失败排查** |
| 5 | `playbook-resource` | 电量/水量等；低电回桩走 `device-return-station` |
| 6 | `playbook-evidence` | 定时任务/未知/其它，取证转人工 |

结案建议：处置清楚且证据足 → 可 `3`；否则 → `6`。  
对外评论：各条结论合成一条；可写相关实时事实，少写工具名/原始字段名。

---

## 路由表

| 剧本 ID | 何时优先选用 | 建议步骤（加载技能） | 成功判据 | 默认状态 |
|---------|--------------|----------------------|----------|----------|
| `playbook-crash` | 工单名称/内容含碰撞撞障，或工单碰撞类码；用影子 `crash` 看是否仍碰撞 | **`device-collision-handling`**；按需 **`device-pull-bag`** 挂附件 | 碰撞现象缓解或已挂附件说明 | 未清 → `6` |
| `playbook-offline` | 工单明确离线/断连 | **`device-offline-recovery`**（含软重启） | 影子显示恢复在线并可继续看本单 | 仍离线 → `6` |
| `playbook-locate` | 工单定位类 | 重定位流程 | 影子 locate 恢复 | 否则 → `6` |
| `playbook-dock` | 回站/对桩/充电点失败 | **`device-cannot-back-station`** | 现象消除或给出排查结论 | 硬件/不明 → `6` |
| `playbook-resource` | `0x20200004` 或电量/水量类名称 | **`device-return-station`**（低电回桩）；水量等只核对影子 | supplyEngaged 或电量恢复 | 下发后仍未进入 1/2/3 → `6` |
| `playbook-evidence` | `0x20300005`、未知码、或未命中上表 | **`device-shadow-status`** → 尽量 **`device-pull-bag`** 取证 → 评论转人工；**通常不做** 倒退等远程动作 | 取证说明写清 | 默认 `6` |

### faultCode 补充（可继续扩）

| faultCode / 模式 | 建议剧本 | 备注 |
|------------------|----------|------|
| `0x20200004` | `playbook-resource` | 加载 `device-return-station`；确认 supplyState 后再评论成败 |
| `0x20300005` | `playbook-evidence` | 定时任务失败：不宜软重启/倒退「修」；实时离线只作说明 |
| 名称含「碰撞」「撞障」 | `playbook-crash` | 加载 `device-collision-handling` |
| 名称含「定位」 | `playbook-locate` | |
| 名称含「离线」 | `playbook-offline` | 此时才优先考虑软重启 |
| 名称含「回站」「对桩」「充电点失败」 | `playbook-dock` | 排查用 `device-cannot-back-station`；单纯回桩下发用 resource |

未知码：倾向 `playbook-evidence`，先取证再决定是否远程操作。

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

# 3) candidates 空 → playbook-evidence
# 4) 去重，按优先级串行；每条加载对应 device-*
# 5) 每条留下对外结论素材 → 合成一条评论 → 改状态
```

**实践提示（软约束）：**

- 工单 `faultList` 决定处置目标；影子提供实时状态（`device-shadow-status`）
- `0x20200004` 低电：委托 `device-return-station`（supplyState 1/2/3=已对桩，勿看 is_charge）
- `0x20300005` 等取证类：优先 evidence；软重启/倒退收益通常不大
- 倒退：留给碰撞相关 → `device-collision-handling`
- 软重启：留给工单离线类；不要因取证类单「顺便重启看看」
- 影子里与本单无关的其它 fault：不必写进 BMS 对外评论
- **编排未写明的接口不调**；evidence 路径禁止额外控制类下发

### 影子常用枚举（810）

详细以 **`device-shadow-status`** 为准。摘要：

**robotMode**：`IDLE` 空闲 / `TASK` 任务中 / `PAUSE` 暂停 / `FAULT` 错误 / `MAP` 建图 / `OTA` / `FACTORY` 工厂  

**control_mode**：`CONTROL_MODE_MANUAL` 手动 / `CONTROL_MODE_AUTO` 自动  

**supplyState（对桩）**：`0` 空闲 · `1` 前往工作站 · `2` 加排水中 · `3` 仅充电 · `4` 退桩 · `5` 手动补给 · `6` 等待外设关闭；**1/2/3 视为已对桩供电**
