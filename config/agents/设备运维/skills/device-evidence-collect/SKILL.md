---
name: device-evidence-collect
description: |
  拉日志/录包取证与分析。工单无处置剧本/处置失败时由 ticket-handling 加载；结束后必须写评论改状态。
  评论须含关键时间点与带时间戳的日志原文摘录作证明；勿 cat/解析日志全文。仅无 ticket 且只要日志时走「仅采集」。
---

## 适用场景

- 工单**未命中**处置剧本 / 处置失败 → 由 `ticket-handling` 加载（**仍属工单模式**，收尾必须评论+改状态+工单汇报格式）
- **用户消息无 ticket_id、无【BMS工单AI处理】**，且明确只要拉日志 / 分析指定模块（「仅采集模式」）

## 仅采集模式（用户只要日志时）

触发词示例：`采日志`、`拉日志`、`看一下 titan_app 日志`、`连终端拉近十分钟日志`。

```
（可选）get_device_shadow 核对 — 结果不影响是否尝试终端
  → connect_terminal(sn=deviceId, password=按产品)
  → 按下方「终端取证固定步骤」拉日志
  → 有 ticket_id：【必须】create_ticket_comment；状态按结案判据（已恢复 → 3，否则通常 6）后再回复
  → 无 ticket_id：分析后直接回复用户
  → disconnect_terminal
```

**禁止处置类动作**（重启/重定位/回桩/倒退/改控制模式等）。  
影子显示离线也**先试终端**；终端失败再说明无法采集，不要改走「离线恢复」剧本。

## 工单取证模式（无剧本 / 处置失败）

```
（已有）get_device_shadow
  → 按故障现象选产物：相关模块日志，和/或故障时刻录包 + 实时相机
  → 终端按「固定步骤」取证（需要日志时）
  → 需要录包时（行走/感知相关，或日志空）：find_bags_near_time → upload_bag_file → create_ticket_attachment
  → 行走/感知相关：**须** get_camera_image（相关方位 1～2 个）→ 有 url 则 create_ticket_attachment
  →【必须】create_ticket_comment（结论 + 日志摘录；已挂 bag/相机方位须写明）
  →【必须】change_ticket_status：现象已恢复且影子无活跃同码 → `3`；仍异常/需人工 → `6`（结案细节以 ticket-handling 为准）
  → 用过终端则 disconnect
  → 再对内/对用户简短汇报（可注明「评论已写回」）
```

**有 `ticket_id` 时：只在对话里输出分析 ≠ 完成。** 必须先成功调用 `create_ticket_comment`；评论失败 → 最多鉴权重试 1 次，仍失败则在回复写明原因与拟写摘要，**禁止改发钉钉/邮件冒充升级**。  
**评论/结论必须围绕工单 `faultList` 的码与名称**；影子里其它实时故障不得改写为主定位。  
**禁止「计划话术」收工：** 不得以「接下来我将… / 现在需要解析… / 让我再…」作为最终回复；已有 grep/时间窗摘录 → **立刻**写评论（及改状态）结案。  
**只使用本技能「允许的工具」**；技能未写明的接口一律不调。  
取不到证据也要把「已尝试什么、为何无法取证」**写入评论**后改 `6`，不要换一批无关工具/命令继续试。  

### 产物分工（行走 / 底盘 / 感知）

| 产物 | 用途 |
|------|------|
| 录包（happenTime） | 人工分析**为何触发**（传感器/底盘原始数据） |
| 实时相机图 | 人工看**当前四周/障碍物/现场情况**（不能代替故障时刻 bag） |

**行走/底盘/感知相关故障（有 ticket_id）硬契约：**
1. 日志可抽则抽；**须**按 happenTime 查录包；定位到切片 → **必须** `upload_bag_file` → `create_ticket_attachment`（1～3）
2. **须**再拉实时相机图：按产品取与现象相关的 1～2 个方位（不扫遍）→ 有 `url` → **必须** `create_ticket_attachment`
3. 影子已恢复可缩短终端探路，但 bag（有切片）+ 相机（须尝试）仍要走完再评论改 `3`
4. **禁止**只建议下载 bag / 只描述现场而不挂附件；未尝试相机或定位到包未挂 = **流程未完成**

**相机方位（合计 ≤2，失败记一句勿连环换）：**
- SC50/SW50：默认 `前`；防跌落/下视相关再加 `下`
- T810：默认 `前`；需看四周再补后/左/右之一

**日志空/未命中时间窗：** 补拉录包并挂附件（有则挂），行走/感知仍须尝试相机挂图，再写评论。  
**录包查询：** `find_bags_near_time` 无结果 → **至多再** `list_device_bags` **1 次**；仍无则评论写明，禁止反复 list。

## 取证边界原则（通用）

所有终端/录包取证必须遵守下列原则（适用于任意故障类型，不按个案补丁）：

| 原则 | 含义 |
|------|------|
| **时间锚定** | 一切日志证据必须对齐 `happenTime`；**优先抽时间窗原文**，关键词只作辅；禁止读文件末尾；禁止打开与故障时刻无关日期的日志 |
| **有界探测** | 终端总轮次 ≤6（含 `send_command` / `interactive_session` 内条目）；同一命令最多 2 次；`ls`/录包列表类辅助查询各最多 1 次额外重试 |
| **换维不停** | 同一维度（同目录、同命令变体、往回翻日期、只换关键词）连续失败 → **换候选路径/模块**或**停**；禁止在同一维度上加深 |
| **产出契约** | 每轮后须给出**时间窗摘录**或明确负结果（该窗无行/无当日文件）；禁止连续探路叙事、零产出；禁止用空关键词断言根因 |
| **候选上限** | 日志候选 **≤3 个**；按**故障域**选**主查 + 辅查**（见日志路径）；每个至多 1 次有效时间窗抽取；用尽 → 停终端 |
| **收尾阶梯** | 终端无果 → 查录包（有切片则 upload 挂）→ **行走/感知再拉相机挂图** → 写评论 → 改状态；影子已恢复可缩短探路，已定位的包与有 url 的图仍须挂 |

## 终端取证固定步骤

取证目标是**围绕 happenTime 抽「时间线附件」作证明**（该时刻前后日志原文），不是关键词扫盘、也不是整机诊断。

1. **先 `ls` 一次日志根（全量）**；再对选定模块查**当日文件**（勿用 `ls | head` 被旧文件挤掉当日）：  
   - 例：`ls /opt/xzrobot/logs/xzrobot_driver2/ | grep 2026-08-07` 或直接测 `_2026-08-07` 是否存在  
   - 勿猜 `模块_日期.log` / `/logs/chassis/`（SC50 实机**通常无** `chassis/` 目录）
2. 按故障域选**主查 + 辅查**；路径/文件名**只能**来自 `ls` + 默认 app
3. **主抽 = 时间窗**，但要能落到故障行：  
   - 先用 happenTime ±1～3 分钟的时间串过滤（格式随实机，如 `[2026-08-07 10:43:`）  
   - 若窗内被同质刷屏淹没（如大量 `Command velocity timeout`）→ **同文件再滤一刀**（时间串 **且** 域关键词如 `emergency`/`estop`/`fall`/`0x2020…`），或把时间窗收到秒级（`10:43:3`）  
   - `head` 勿截在刷屏前就停；刷屏时必须做上述降噪，**禁止**把刷屏当「已取证」也**禁止**因此放弃主查
4. **关键词为辅**：只在已确认的当日文件上；**禁止**一上来全库 `grep fall`、空了就编 `chassis/`/`peoception`
5. **评论摘录必须是工具 `output` 里出现过的原文**（可略裁），禁止凭印象写英文套话（如 `hardware_button` / `GPIO12` / `chassis_driver:`）——历史上出现过无 `chassis/` 仍编造该类摘录写回工单
6. 有合格摘录 → 写评论（可再补 bag/相机）；时间窗+降噪仍空 → 换下一候选或收尾；禁止 `tail`/`cat` 全文
7. 主查目录在根 `ls` 中存在却未抽到当日文件/未做时间窗 → **流程未完成**

### SC50 急停实机形态（对照，勿当万能模板）

- 目录：主查 **`xzrobot_driver2`**（按日文件常为 `_YYYY-MM-DD`），**不是** `/logs/chassis/`
- 子目录：常见还有 **`xzrobot_driver2/lora_logs/`**（`lora_YYYYMMDD.log`）——偏 **工作站/对桩 LoRa**，对桩/回站单可辅查；**普通急停单不必默认深挖**，除非现象与对接桩相关
- 行内 logger 名可能是 `[/xzrobot_chassis]`（这是**日志字段**，不是目录名）
- 有效证据示例形态：`[MCU][STR0]emergency =1` / `emergency =0`（出现在 happenTime 附近）  
  - **含义**：MCU 上报急停**状态位置位/清零**，可作「该时刻进入/退出急停」的时间线证据  
  - **不足以单独结论**：不等于已证实「物理急停按钮被按」。无额外来源字段（button/gpio/soft/hard 等）时，评论写「MCU 急停置位」，可**提示**现场核按钮，**禁止**写成「日志已确认人为按键/GPIO12」
- **辅查 app（勿漏）**：`xzrobot_app2/_YYYY-MM-DD` 常在 MCU 置位后约 1s 内打 `Trigger Fault: 0x2020000A, 急停触发, PMU急停触发`（工单码对齐用）；急停单 **driver2 时间窗 + app Trigger Fault 应同批**
- **无效/曾幻觉形态**：`chassis_driver: … (source: hardware_button)`、`pin=GPIO12`、路径 `/opt/xzrobot/logs/chassis/` —— 实机 grep 不到就不得写入评论

### 硬约束：候选只能来自实况 + 故障域

| 禁止 | 必须 |
|------|------|
| 先猜 `mcu`/`can`/`chassis/`/`safety/`/`peoception` 再探测 | **先全量 `ls`**；只进回显目录 + 默认 app；SC50 感知常见 **`visual_perception`** |
| 把 **bag 的 2 分钟切片** 套到模块日志 | 文件名只来自模块目录 `ls`；bag 惯例仅用于录包 |
| `ls \| head` 没看到当日文件就说「无当日日志」 | 对 happen 日期 `grep` 文件名或直接测 `_日期` |
| 时间窗只有刷屏、或只做关键词空结果就换臆造路径 | 同文件：**时间窗 → 降噪（时间∩关键词或收窄秒）** |
| 工具 output 没有的句子写进评论当「日志摘录」 | **摘录 ⊆ 工具回显原文** |
| 空日志 + 无 bag + 相机错码 →「硬件根因已证实」 | 只陈述步骤与负结果 |

## 前置条件

- `deviceId`（终端 `sn`）+ 尽量有 `productId`
- **尽量带上故障发生时间**（工单 `happenTime` / 用户给出的时刻）；取证围绕该时刻附近，而不是「现在」的文件末尾

## 终端登录

| 产品 | 用户 | 密码 |
|------|------|------|
| T810 / Titan810 / XZ-TITAN810 | `xzrobot` | `titan@810` |
| SC50 / SW50 等 | `xzrobot` | 默认 `xzyz2022!`（现场可能不同） |

也可用环境变量 `TERM_USERNAME` / `TERM_PASSWORD` 覆盖。

**终端用法提示：** SC50 偏慢 → **优先一次** `interactive_session` 带齐只读命令。`send_command` 空 output：同命令最多再 1 次，或改一次 `interactive_session`；仍空 → **停终端转录包/写评论**。  
**路径以工具入参 `command` 为准**：若 `output` 里出现 `loggs`/`ltggs`/`xzrobott_` 等怪路径，视为回显污染，**不要**据此改路径或改 `head`/`cat`；空 `output` + 正确 `command` = 该次 grep 无命中。

## 产物选择

| 故障类型 / 剧本 | 优先产物 | 附件 |
|-----------------|----------|------|
| 用户指定模块 | **该模块日志** | 日志不挂附件 |
| 碰撞 / `playbook-crash` | **录包**（可辅感知日志） | 录包可挂 |
| 定位 / `playbook-locate` | **录包** + 定位日志 | 同上 |
| **行走 / 底盘 / 感知相关**（防跌落、撞障、定位漂移、雷达/相机/点云、卡滞悬空等） | 日志可抽则抽；**须**查录包 + **须**拉相关方位实时图 | **定位到包必须挂**；**相机有 url 必须挂** |
| 回站对桩 / `playbook-dock` | **日志**为主；与行走感知纠缠时宜补 bag | 录包有 url 才挂 |
| 急停 / 资源 / 定时任务等（与行走感知无关或弱相关） | **日志**为主；日志空可补 bag | 按需 |
| **其它 / 未知** | 相关模块日志；偏行走感知则宜挂 bag；日志空则补 bag | 录包/相机图有 url 才挂 |

**约定：**
- 日志 = 终端只读摘录进回复/评论
- 录包 = `find_bags_near_time` → `upload_bag_file` → `create_ticket_attachment`（可委托 `device-pull-bag`）——供人工分析**触发原因**
- **实时相机图** = `get_camera_image` → 有 `url` → `create_ticket_attachment`——供人工看**当前环境/障碍**；**不能代替**故障时刻 bag
- 不按单个 faultCode 写死是否挂附件，按现象是否涉及行走/感知判断

### 录包步骤（需挂 bag 时）

```
find_bags_near_time(device_id, product_id, happenTime 附近)
  → 选 1～3 个最接近的切片
  →【必须】upload_bag_file → 取得 url
  →【必须】create_ticket_attachment(ticket_id, url, ...)
```

`upload_bag_file` 失败可**再试 1 次** `upload_bag_list`（若工具可用）；仍失败（如固件不支持 `2002008`）→ 评论写明 bag **文件名/路径/时刻**与错误码，请人工下载挂附件；**禁止**发明未提供的上传工具或空转重试。  
找到切片却跳过 upload 尝试 → 视为未完成。  
找不到录包：评论写明已按何时查找、无匹配包即可。

### 实时相机图（行走/感知须做；其它按需）

```
get_camera_image(device_id, product_id, camera="前"|"下"|"后"|"左"|"右")
  → 工具把 JPEG 上传 OSS，返回 url
  →【有 url 必须】create_ticket_attachment(ticket_id, name=…jpg, url=…)
  → 再 create_ticket_comment（注明已挂方位与文件名）
```

| 产品 | 可用方位 | 行走/感知默认 |
|------|----------|----------------|
| SC50 / SW50 | 前、下 | `前`；防跌落/下视相关再加 `下`（≤2） |
| T810 | 前、后、左、右 | `前`；需看四周再补后/左/右之一（≤2） |

有 url 却未挂附件 → 视为未完成。失败记结论，不要扫遍所有相机。

## 日志路径

| 产品 | 数据根 | 日志根 | **默认 app** |
|------|--------|--------|--------------|
| T810 / Titan810 | `/userdata/xzrobot` | `/userdata/xzrobot/logs` | **`titan_app`** |
| SC50 / SW50 | `/opt/xzrobot` | `/opt/xzrobot/logs` | **`xzrobot_app2`** |

优先直接进上表日志根；若不存在再只读有界查找，**不要**无界扫盘。  
**仍须先全量 `ls` 日志根，再 `ls` 选定模块目录**；只对回显里**真实存在**的文件名做 grep。固件版本可能增减目录/改名，**以当场 `ls` 为准**。

### 日志文件命名（禁止臆造切片惯例）

| 产物 | 切片/命名 | 说明 |
|------|-----------|------|
| **录包 bag** | **约 2 分钟切片**；`all_` 运行包、`task_` 任务包 | **仅录包**如此；见 PROMPT |
| **模块日志** | **不是** 2 分钟切片 | **禁止**套用 bag 惯例去猜 `17-28-*`、`HH-MM-*`、按时刻分子目录 |

实机常见（SC50，以 `ls` 为准，勿当唯一格式）：
- 按日单文件：`_YYYY-MM-DD` / `_YYYY-MM-DD.txt`（常为**文件**，不是「日期目录再往下按分钟切」）
- 其它模块可能是 `info_YYYYMMDD-HHMMSS.<pid>` 等；**只有 `ls` 见到才能用**
- **禁止**推断：`xzrobot_driver2/2026-08-06/17-28-*` 这类路径；`ls` 无该层 → 写「未见该文件名」，**不得**写成「服务未生成日志 / 被重定向 / 未启动」等因果故事
- 只读与 `happenTime` **同日**（或 `ls` 中最接近当日）的文件；不要打开无关日期

### 实机常见日志子目录（对齐样例）

**SC50 / SW50**（`/opt/xzrobot/logs`，样例 SN `10000006`）— 目录：

`ar_track` · `calibration` · `driver` · `robot_monitor` · `visual_perception` · `xzrobot_3d_slam` · `xzrobot_app2` · `xzrobot_coverage` · `xzrobot_driver2` · `xzrobot_gateway2` · `xzrobot_navigaiton` · `xzrobot_slam`

- 根下文件（非目录）：`crash.log`
- 注意：机上导航目录拼写为 **`xzrobot_navigaiton`**（缺 `t`），`ls`/路径须按实名，勿自行改成 `navigation`

**T810**（`/userdata/xzrobot/logs`，样例 SN `200123DC`）— 目录：

`4gmodule` · `candump` · `cover` · `data_provider` · `dock` · `driver` · `hotpot` · `imu_zyz` · `navigation` · `ops` · `ota` · `path_record` · `perception` · `robot_monitor` · `slam` · `titan_app` · `titan_chassis` · `titan_gateway` · `titan_hmi` · `titan_record` · `xz_3d_slam` · `xz_perception`

- 根下文件（非目录）：`data_provider.log`
- T810 **无** `/opt/xzrobot/logs`

### 按故障域选主查 / 辅查（泛用）

不定死「app 永远不是根因」：按**本单故障域**决定谁主谁辅。路径仍只能来自 `ls` 实况 + 默认 app 名（优先从上表实机名匹配）。

| 角色 | 含义 |
|------|------|
| **主查** | 本域最可能存过程/根因；`ls` 存在则**必须**查 |
| **辅查** | 上报/旁证；存在则尽量同批查 |
| **默认 app** | SC50/SW50=`xzrobot_app2`，T810=`titan_app`；多数域为辅查；**任务/调度域为主查** |

| 故障域（按工单名/码归类） | 主查（ls 有则必查，≤2） | 辅查 |
|---------------------------|-------------------------|------|
| 急停 / 驱动 / 安全按钮类 | SC50/SW50：`xzrobot_driver2`（可兼 `driver`；**无**独立 `chassis/` 目录时不要去建）；T810：`driver` / `titan_chassis` | **默认 app（必查）**：SC50 常见 `Trigger Fault: 0x2020000A, 急停触发, PMU急停触发` 这类上报行 |
| 防跌落 / 底盘 / 感知 | SC50/SW50：`visual_perception` / `robot_monitor`；T810：`perception` / `xz_perception` / `titan_chassis` / `robot_monitor` | 默认 app |
| 定位 / slam / nav | SC50/SW50：`xzrobot_slam` / `xzrobot_3d_slam` / `xzrobot_navigaiton`；T810：`slam` / `xz_3d_slam` / `navigation` | 默认 app |
| 对桩 / dock | T810：`dock` / `navigation`；SC50/SW50：`xzrobot_navigaiton`；可辅 `xzrobot_driver2/lora_logs`（ls 有则） | 默认 app |
| **任务 / 暂停 / 定时 / 调度 / 任务失败类** | **默认 app** | ls 中其它相关目录（有则） |
| 未知 | 默认 app；再从 ls 挑名称与故障词接近的 1 个 | — |

硬规则：
1. **主查在 `ls` 中存在却未做时间窗抽取 → 流程未完成**（急停漏 `xzrobot_driver2` 即此）
2. 主查不存在 → 退到辅查/默认 app，评论写明「ls 无主查目录，已查 …」
3. 候选合计 ≤3；优先同批打主查+辅查的**时间窗**
4. 时间窗空 → 换下一候选或收尾；关键词空 ≠ 无日志；禁止 `tail`/`cat`/二进制臆断
5. **任务域**只查 app（且 ls 无其它相关）→ **可以**结案；不要写成「只查 app 永远不够」

### 关键词（仅辅，勿当主策略）

时间窗抽行之后，若需在窗内加滤或二次补查，可参考：

| 故障/模块 | 关键词示例（辅） |
|-----------|------------------|
| 急停 | `急停` `emergency` `e-stop` `ESTOP` `0x2020000A` |
| 防跌落 | `fall` `drop` `cliff` `防跌落` `gpio` `0x2020000C` |
| 相机 / 感知 | `camera` `image` `decode` `timeout` |
| 定位 | `locate` `reloc` `定位丢失` |
| 碰撞 | `collision` `crash` |
| 对桩 | `dock` `对桩` `lora` |
| 任务 / 调度 | `pause` `timeout` `task` `schedule` `定时` `暂停` |
| 通用 / app | `ERROR` `WARN` `Exception` `fail` 及本故障名/码 |

**不要**用「全文件 grep 关键词为空」代替「happenTime 附近无行」；更不要据此断言硬件根因。

## 允许的工具

- 影子（可选）：`get_device_shadow`
- 录包云端（需要 bag 时）：`list_device_bags` / `find_bags_near_time` / `upload_bag_file`（失败可再试 `upload_bag_list` 一次）
- 实时相机（行走/感知须做）：`get_camera_image`；失败记结论即可，勿连环换方位
- 工单（有 ticket 且需写回）：`create_ticket_comment` / `create_ticket_attachment`（录包或相机图的 url）
- 终端：`connect_terminal` / `send_command` / `interactive_session` / `disconnect_terminal`（只读）

## 评论证据要求（证明材料）

写回工单 / 对内汇报时，日志取证**必须带证明**，不能只有概括句或探路叙事：

1. **时间点**：与 `happenTime` 对齐的**时间窗摘录**；禁止用文件末尾最新行冒充；禁止只用「关键词全文件无匹配」充当已取证
2. **原文摘录**：时间窗命中则贴 2～5 行（保留时间戳）；未命中则写明已 `ls` 文件名、已查时间窗与负结果
3. **路径**：日志文件须为 `ls` 实名
4. 抽不到 happenTime 附近行：评论写明已查**主查/辅查**路径与负结果；行走/感知类 **补录包 + 尝试相机挂图** 后按结案判据改状态
5. 行走/感知评论须写明：已挂 bag 名（或无包/upload 失败原因）+ 已挂相机方位（或拉图失败原因）
6. **禁止**主查在 `ls` 有却未做时间窗；**禁止**空关键词后编硬件根因；**禁止**未贴 `ls` 就写「无日志 / 未启用」
7. **自检：** 急停/防跌落类未对 `xzrobot_driver2`（ls 有）做时间窗 → 未完成；任务类已查默认 app 且无其它相关 → 可完成
8. 相机错误码 / 无 bag / 关键词空 → 只记现象，**不要**写成「三重验证确认硬件失效」

## 评论 / 回复结构

有 `ticket_id` 时用 `create_ticket_comment`，内容示例：

```
【AI分析】设备 630026FF，工单故障「…」。已拉取 titan_app 日志（路径：…）。
结论：激光雷达数据订阅失败导致对桩异常，当前无法远程修复，已转问题分析。
关键时间点：首次约 …；末次 …。
日志摘录：
… 订阅不到激光雷达数据 (30100016)
… Lidar频率低 (30100001)
… Aim Pile FAILED
请人工检查雷达连接/驱动与对桩链路。
```

无 ticket 时同样要带时间点+摘录；**禁止**有工单时只出 `📋 设备` 非工单报告而不写评论。

## 注意事项

- 取证 ≠ 处置；用完终端必须断开
- 挂 bag / 相机图最多各 1～3 个；日志不编造 url；附件必须有可访问 url
- 目录不存在 / 连不上：如实说明，工单改 `6` 结束
- **日志证据 = happenTime 时间窗短摘录**（关键词仅辅）；遵守上方取证边界原则
- **最终回复前检查：** 有 ticket → 行走/感知已尝试 bag+相机挂附件 → 已 `create_ticket_comment`（+ 改状态）→ 工单汇报格式
- **自检红旗：** 未做时间窗只做关键词；臆造 `peoception`/未 `ls` 文件名；空关键词+相机错码就写「硬件已证实」；主查在 ls 有却未抽；已定位录包却只建议下载 → 立即纠正或写明失败原因
