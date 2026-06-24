---
name: device-remote-operations
description: 通用设备远程运维技能，处理定位丢失、故障诊断、状态异常等常见设备故障
---

## 适用场景

- 设备定位丢失（locate=false）
- 设备状态异常（hasFault=true）
- 设备无响应（connect=false 或请求超时）
- 收到监控系统的故障告警

## 故障处理流程

```
获取设备详情 → 判断故障类型 → 执行恢复 → 等待3~5秒 → 验证结果
```

### 第一步：获取设备详情

调用 `get_device_detail(sn)` 获取设备当前状态，重点关注以下字段：

- `connect` — 是否在线
- `locate` — 是否有定位
- `hasFault` — 是否有故障
- `faultDTOList` — 具体故障列表（code、level、module、name、content）
- `position` — 当前位姿 [x, y, theta]
- `battery` — 电量

### 第二步：根据故障类型处理

#### 定位丢失恢复（locate=false）

**前提：** 设备在线（connect=true）

1. 记录设备当前 `position`（如果有）
2. 调用 `relocate(sn, position)` 执行重定位
   - position 格式: `[x, y, theta]`
   - 如果知道设备当前大概位置，使用该位置坐标
   - 如果不知道位置，可尝试使用最后一次上报的 position
3. 等待 3 秒
4. 调用 `get_device_detail(sn)` 验证 `locate` 是否变为 true
5. 若仍为 false，可重试一次（最多2次）
6. 若2次仍失败，升级给人工运维

#### 通用故障恢复（hasFault=true）

1. 调用 `fault_diagnose(sn)` 执行故障诊断
2. 等待 5 秒让诊断完成
3. 调用 `get_device_detail(sn)` 查看诊断后的状态
4. 根据 `faultDTOList` 中的故障码判断处理方式：

| 故障码特征                    | 建议操作                                 |
| ----------------------------- | ---------------------------------------- |
| 包含`PARAM`                 | 调用`factory_reset(sn)` 重置参数       |
| 包含`HANG`                  | 调用`soft_restart(sn)` 软重启          |
| 包含`COLLISION`             | 切换到`device-collision-handling` 技能 |
| 包含`LOCATE` / `POSITION` | 按定位丢失处理                           |
| 其他未知故障                  | 升级给人工运维                           |

5. 执行操作后等待 3 秒
6. 再次调用 `get_device_detail(sn)` 验证 `hasFault` 是否变为 false

#### 设备无响应（connect=false）

1. 调用 `soft_restart(sn)` 发送软重启指令
2. 等待 10 秒
3. 调用 `get_device_detail(sn)` 验证 `connect` 是否变为 true
4. 若仍为 false，等待 20 秒后再查一次（设备启动需要时间）
5. 若仍未恢复，升级给人工运维——可能是硬件故障或网络断连

### 第三步：验证与报告

操作完成后，再次调用 `get_device_detail(sn)` 确认设备状态：

**恢复成功标准：**

- connect = true
- locate = true
- hasFault = false

**报告内容：**

- 设备 SN
- 原始故障描述
- 执行的操作列表
- 最终设备状态
- 是否需要人工跟进

## 一键恢复

如果故障类型明确，可直接调用综合工具：

- `diagnose_and_recover(sn, position)` — 自动检测并恢复定位丢失、无响应、故障状态
- `handle_collision(sn)` — 碰撞专用恢复

## 常见错误及处理

| 错误                    | 原因                 | 处理                       |
| ----------------------- | -------------------- | -------------------------- |
| API返回 success=false   | Token 过期或网络问题 | 重新调用`get_token` 认证 |
| 设备详情无 sn 字段      | 设备不存在或SN错误   | 确认SN是否正确             |
| 重定位后仍 locate=false | 位姿数据不正确       | 尝试不同的 position 值     |
| 软重启后仍离线          | 硬件故障             | 升级人工运维               |

## 注意事项

- API 调用后务必等待设备响应再查询状态（控制命令 3~5 秒，软重启 10~20 秒）
- 重定位的 position 参数是 `[x, y, theta]`，其中 theta 是弧度
- 故障诊断是异步的，调用后需要等待一段时间才能看到结果
- 不要在同一设备上短时间内重复发送相同指令
