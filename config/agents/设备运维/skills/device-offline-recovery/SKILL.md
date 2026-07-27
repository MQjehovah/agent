---
name: device-offline-recovery
description: 处理设备离线（connect=false）或无响应的恢复流程
---

## 触发条件

- `get_device_detail` 返回 `connect=false`
- 调用 API 超时或无响应
- 监控系统上报设备离线告警

## 恢复流程

### 第一步：确认离线状态

调用 `get_device_detail(sn)` 确认设备确实离线：
- 如果返回正常且 `connect=true`，可能是短暂网络抖动，记录后结束
- 如果 `connect=false`，继续恢复流程

### 第二步：软重启

调用 `soft_restart(sn)` 发送软重启指令。

**注意：** 即使设备离线，软重启指令也会被平台缓存，设备上线后会执行。

### 第三步：等待恢复

等待时间建议：
- 首次查询：10 秒后调用 `get_device_detail(sn)` 检查
- 若仍离线：再等 20 秒后第二次查询（设备启动需要时间）
- 若仍离线：再等 30 秒后第三次查询

### 第四步：判断结果

| 状态 | 处理 |
|------|------|
| connect=true, locate=true, hasFault=false | 恢复成功 |
| connect=true, locate=false | 在线但定位丢失，切换到 `device-remote-operations` 技能处理定位 |
| connect=true, hasFault=true | 在线但有故障，切换到 `device-remote-operations` 技能处理故障 |
| connect=false（等待60秒后仍离线） | 升级给人工运维 |

### 第五步：恢复后检查

如果设备恢复在线，额外检查：
1. `battery` — 如果电量极低（<10%），尝试调用 `forward_charge(sn)` 让其回充
2. `hasFault` — 如果有残留故障，按通用故障流程处理
3. `locate` — 如果定位丢失，按定位丢失流程处理

## 升级条件

以下情况立即升级人工运维：
- 软重启后等待超过 60 秒设备仍离线
- 设备频繁离线（同一设备24小时内3次以上）
- 批量设备同时离线（可能是网络或平台问题）
- 设备离线且电量耗尽（battery=0）

## 常见原因

| 原因 | 特征 | 能否远程恢复 |
|------|------|-------------|
| 软件崩溃 | 突然离线，之前无告警 | 能（soft_restart） |
| 网络断连 | API超时，无返回 | 不能 |
| 电量耗尽 | 离线前电量持续下降 | 不能（需人工充电） |
| 硬件故障 | 反复离线，重启后很快又离线 | 不能 |
| 平台维护 | 批量设备同时离线 | 等待恢复即可 |
