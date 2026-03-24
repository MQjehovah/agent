# Device Operations MCP Server

设备远程运维 MCP 服务器，封装设备运维 REST API。

## 安装

```bash
cd mcp-device-ops
pip install -r requirements.txt
```

## 配置

设置环境变量：

```bash
export DEVICE_API_BASE_URL="http://your-api-server:8080"
```

或在代码中调用 `set_api_base_url` 工具。

## 运行

```bash
python src/server.py
```

## 工具列表

### 认证工具

| 工具 | 说明 |
|------|------|
| `set_api_base_url` | 设置API基础URL |
| `set_token` | 设置认证Token |
| `get_token` | 通过登录获取Token |

### 设备信息查询

| 工具 | 说明 |
|------|------|
| `get_device_detail` | 获取设备详情 |
| `get_real_time_state` | 获取实时数据 |
| `get_clean_info` | 获取清洁组件信息 |
| `get_camera_info` | 获取摄像头信息 |
| `get_chassis_info` | 获取底盘底层数据 |

### 故障诊断与恢复

| 工具 | 说明 |
|------|------|
| `fault_diagnose` | 故障诊断 |
| `soft_restart` | 软重启设备 |
| `relocate` | 设备重定位 |
| `factory_reset` | 重置设备参数 |

### 设备控制

| 工具 | 说明 |
|------|------|
| `move_robot` | 控制设备移动 |
| `stop_robot` | 停止设备 |
| `backward` | 设备倒退 |
| `forward_charge` | 前往充电站 |
| `set_control_mode` | 切换手自动模式 |

### 工程模式

| 工具 | 说明 |
|------|------|
| `start_factory_mode` | 开启工程模式 |
| `stop_factory_mode` | 退出工程模式 |
| `get_factory_params` | 获取工程参数 |
| `set_factory_params` | 设置工程参数 |

### 地图与点云

| 工具 | 说明 |
|------|------|
| `get_cost_map` | 获取感知地图 |
| `get_point_cloud` | 获取点云数据 |

### 任务管理

| 工具 | 说明 |
|------|------|
| `get_pending_task` | 获取断点续扫任务 |
| `resume_pending_task` | 开始断点续扫 |
| `send_task_info` | 发送任务数据 |
| `plan_path` | 路径规划 |

### OTA升级

| 工具 | 说明 |
|------|------|
| `start_ota` | 开始OTA升级 |

### 录包文件

| 工具 | 说明 |
|------|------|
| `get_bag_files` | 获取录包文件列表 |
| `upload_bag_file` | 上传录包文件 |

### 综合运维

| 工具 | 说明 |
|------|------|
| `diagnose_and_recover` | 综合故障诊断与恢复 |
| `handle_collision` | 处理设备碰撞故障 |

## 在 Claude Code 中使用

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "device-ops": {
      "command": "python",
      "args": ["mcp-device-ops/src/server.py"]
    }
  }
}
```

## 使用示例

```
用户: 设备 SN12345 出现故障，帮我诊断一下

AI: 我来帮你诊断设备 SN12345 的状态...
[调用 get_device_detail -> fault_diagnose]

用户: 设备定位丢失了，帮我重新定位到 [10, 20, 0]

AI: 正在为设备 SN12345 执行重定位...
[调用 relocate]