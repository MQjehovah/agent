# 假工单期望调用序列（编排回归）

供人工核对与 `tests/unit/test_ticket_playbook_sequences.py` 使用。  
失败/无能力/**未命中处置剧本**路径**一律**含 `device-evidence-collect`；结案：**已恢复 → 3**，否则 → 6。定位剧本禁止扩调用。

机器可读定义：同目录 [`playbook-call-sequences.json`](playbook-call-sequences.json)。

## 序列摘要

| 剧本 | 成功路径（技能顺序） | 失败/收尾 |
|------|----------------------|-----------|
| crash | shadow → collision-handling（可 pull-bag） | evidence-collect（录包） |
| offline | shadow → offline-recovery | 在线则可 evidence-collect（日志） |
| locate | shadow → relocate → shadow | evidence-collect（录包+定位日志） |
| dock | shadow → cannot-back-station（可 relocate+back） | evidence-collect（日志） |
| resource | shadow → return-station | evidence-collect（日志） |
| evidence | shadow → evidence-collect（行走/感知须挂 bag+实时相机图） | 已恢复 → 状态 3；否则 → 状态 6 |

## 禁止示例

- **工单路径禁止一切 `dingtalk_*`**（MCP 可启用，但 ticket 流程不得调用；评论失败不得降级钉钉）
- evidence / 无匹配处置剧本：禁止处置类（`soft_restart`、`device_backward`、`relocate` 等）；只走 evidence-collect 允许的工具，禁止扫无关接口碰运气
- locate：禁止 `soft_restart`、`device_backward`、`set_control_mode`、工程模式
- evidence-collect：禁止控制类下发；日志不得当附件；遵循取证边界原则；**已定位录包必须 upload 挂附件**；**行走/感知须拉相机，有 url 必须挂**（bag=触发原因，相机=当前环境）