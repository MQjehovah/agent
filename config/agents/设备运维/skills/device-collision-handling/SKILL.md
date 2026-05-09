---
name: device-collision-handling
description: 处理设备碰撞故障，包括碰撞检测、紧急停止、脱困和恢复
---

## 触发条件

- `get_device_detail` 返回 `faultDTOList` 中包含碰撞相关故障码（如 `COLLISION`、`collision`）
- 设备上报碰撞告警
- 设备异常停止且 `hasFault=true`，故障码涉及碰撞

## 紧急处理流程

### 第一步：立即停止设备

调用 `stop_robot(sn)` 或 `move_robot(sn, mode=0)` 立即停止设备移动。

**这是最高优先级操作，不要跳过。**

### 第二步：评估碰撞情况

调用 `get_device_detail(sn)` 获取当前状态：
- 记录 `position` — 当前位姿
- 记录 `faultDTOList` — 碰撞故障详情
- 检查 `battery` — 确认设备还在运行
- 检查 `locate` — 碰撞后定位可能丢失

### 第三步：尝试脱困

1. 调用 `backward(sn)` 让设备后退
2. 等待 2 秒
3. 调用 `stop_robot(sn)` 停止后退
4. 调用 `get_device_detail(sn)` 检查状态：
   - 如果碰撞故障码已消失 → 进入恢复验证
   - 如果碰撞故障码仍在 → 尝试转向后后退

**转向后后退方案：**
1. 调用 `move_robot(sn, mode=7)` 左旋转
2. 等待 2 秒
3. 调用 `backward(sn)` 后退
4. 等待 2 秒
5. 调用 `stop_robot(sn)` 停止

### 第四步：恢复验证

脱困成功后，验证设备完整状态：

调用 `get_device_detail(sn)` 确认：
- `hasFault=false` — 故障已清除
- `locate=true` — 定位正常
- `connect=true` — 连接正常

**如果定位丢失：**
- 调用 `relocate(sn, position)` 恢复定位
- position 使用碰撞前记录的位姿，或最近已知位姿

**如果故障未清除：**
- 调用 `fault_diagnose(sn)` 诊断
- 等待 5 秒后再次查询
- 若故障码包含硬件损坏相关描述，升级人工运维

### 第五步：恢复运行

设备恢复正常后：
1. 确认设备是否需要继续执行任务
2. 如需继续，调用 `set_control_mode(sn, mode=1)` 切回自动模式
3. 调用 `get_device_detail(sn)` 做最终确认

## 特殊情况处理

### 设备被困死角

如果多次后退+转向仍无法脱困：
1. 调用 `connect_terminal(sn)` 接入远程终端
2. 通过终端手动控制设备移动
3. 脱困后调用 `disconnect_terminal(sn)` 断开

### 碰撞后设备离线

碰撞后设备失去连接：
1. 等待 10 秒看设备是否自动恢复
2. 调用 `soft_restart(sn)` 尝试重启
3. 如果 30 秒后仍离线，升级人工运维——可能存在硬件损伤

### 连续碰撞

同一设备短时间内多次碰撞：
1. 调用 `get_cost_map(sn)` 获取感知地图
2. 检查设备周围是否有新的障碍物或环境变化
3. 若环境正常但设备反复碰撞，可能是传感器故障，升级人工运维

## 一键处理

如果情况明确（单次碰撞、设备在线），可直接调用 `handle_collision(sn)` 执行自动碰撞恢复流程（停止→后退→停止→验证）。

## 升级条件

立即升级人工运维：
- 碰撞后设备硬件有明显损伤提示
- 多次脱困尝试均失败
- 传感器数据异常（感知地图明显不正常）
- 设备碰撞后出现漏液、冒烟等描述
