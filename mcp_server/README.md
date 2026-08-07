# Device Operations MCP Server

设备远程运维 MCP 服务器，封装设备运维 REST API 和终端交互功能。

## 安装

```bash
cd mcp_server
pip install -r requirements.txt
```

## 配置

设置环境变量：

```bash
export TICKET_API_BASE_URL="https://bms-cn.rosiwit.com"
export DEVICE_API_USERNAME="your-user"
export DEVICE_API_PASSWORD="your-password"
export WS_BASE_URL="wss://your-terminal-server:10000"
```

或在 `.env` 文件中配置：

```env
OPENAI_API_KEY=your-api-key
TICKET_API_BASE_URL=https://bms-cn.rosiwit.com
DEVICE_API_USERNAME=your-user
DEVICE_API_PASSWORD=your-password
WS_BASE_URL=wss://dev.xzrobot.com:10000
```

## 运行

```bash
python src/terminal.py
python src/remote_operation.py
```

## 工单工具 (ticket_ops.py)

鉴权：[`cloud_common.py`](src/cloud_common.py)。

| 工具 | 说明 |
|------|------|
| `get_ticket` / `change_ticket_status` / `create_ticket_comment` / `create_ticket_attachment` | 工单 CRUD |
| `list_ticket_statuses` / `set_ticket_token` | 状态枚举 / 手动 token |

## 云端运维 (remote_operation.py)

仅 rosiwit-cloud（**已删除全部 FAE /xz_sc50/fae 老接口**）。

| 工具 | 说明 |
|------|------|
| `get_device_shadow` | 设备影子实时状态 |
| `list_device_bags` / `find_bags_near_time` | 录包列表与按时间匹配 |
| `upload_bag_file` / `upload_bag_list` | 上传录包取 OSS url |
| `soft_restart` / `factory_reset` | 重启 / 恢复出厂 |
| `set_control_mode` | 手动/自动 |
| `relocate` / `station_relocation` / `station_dock` | 地图重定位 / 工作站 |
| `device_backward` / `device_back_to_station` | 倒退 / 回桩 |
| `get_clean_info` / `device_clean` | 清洁信息 / 清扫 |
| `get_camera_image` / `get_point_cloud` | 相机实时图（方位中文；base64 自动上传 OSS 返回 url 可挂附件）/ 点云 |
| `plan_path` / `get_pending_task` / `resume_pending_task` | 路径与挂起任务 |
| `execute_terminal` / `remote_action` | 远程终端 / 通用动作 |
| `set_cloud_token` | 手动 token |

统一体 `RemoteForm`：`deviceId` + `productId` + `id` + `param`。环境：`TICKET_API_BASE_URL`（或 `CLOUD_API_BASE_URL`）+ `DEVICE_API_USERNAME` / `DEVICE_API_PASSWORD`。

## 终端工具列表 (terminal.py)

### 连接管理

| 工具 | 说明 |
|------|------|
| `connect_terminal` | 连接设备终端并自动登录 |
| `disconnect_terminal` | 断开终端连接 |
| `get_session_status` | 获取会话状态 |
| `set_ws_base_url` | 设置 WebSocket 基础 URL |

### 命令执行

| 工具 | 说明 |
|------|------|
| `send_command` | 发送命令并解析响应（智能分离命令回显、输出、提示符） |
| `send_raw` | 发送原始数据（不添加换行符） |
| `interactive_session` | 交互式会话，执行多个命令 |
| `execute_with_retry` | 执行命令并支持失败重试 |
| `wait_for_prompt` | 等待终端提示符出现 |

### 输出解析

| 工具 | 说明 |
|------|------|
| `receive_output` | 接收终端原始输出 |
| `parse_output` | 解析终端输出结构 |
| `strip_ansi` | 移除 ANSI 转义序列 |
| `clear_buffer` | 清空输出缓冲区 |
| `get_buffer` | 获取缓冲区内容 |
| `resize_terminal` | 调整终端窗口大小 |

## 终端输出解析

终端模块包含智能输出解析器 (`terminal_parser.py`)，能够：

1. **分离命令回显** - 识别并过滤用户输入的命令回显
2. **提取命令输出** - 分离实际的命令执行结果
3. **识别提示符** - 检测 `$`, `#`, `user@host:~$` 等提示符
4. **过滤 ANSI 序列** - 移除颜色、光标控制等转义序列
5. **错误检测** - 识别 `error`, `failed`, `permission denied` 等错误关键词

### 使用示例

```python
# 连接终端
connect_terminal(sn="SN12345")

# 执行命令并获取解析后的输出
result = send_command(sn="SN12345", command="ls -la")
# result.output: 清理后的命令输出
# result.command_success: 命令是否成功执行

# 交互式执行多个命令
interactive_session(sn="SN12345", commands=["cd /tmp", "ls", "pwd"])
```

## 在 Claude Code 中使用

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "terminal": {
      "command": "python",
      "args": ["mcp_server/src/terminal.py"]
    },
    "remote_operation": {
      "command": "python",
      "args": ["mcp_server/src/remote_operation.py"],
      "env": {
        "TICKET_API_BASE_URL": "https://bms-cn.rosiwit.com",
        "DEVICE_API_USERNAME": "",
        "DEVICE_API_PASSWORD": ""
      }
    }
  }
}
```