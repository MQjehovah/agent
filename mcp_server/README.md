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
export DEVICE_API_BASE_URL="http://your-api-server:8080"
export WS_BASE_URL="wss://your-terminal-server:10000"
```

或在 `.env` 文件中配置：

```env
OPENAI_API_KEY=your-api-key
DEVICE_API_BASE_URL=https://bms-cn.rosiwit.com
WS_BASE_URL=wss://dev.xzrobot.com:10000
```

## 运行

```bash
python src/terminal.py
python src/device_ops.py
```

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

## 设备运维工具列表 (device_ops.py)

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
    "device-ops": {
      "command": "python",
      "args": ["mcp_server/src/device_ops.py"],
      "env": {
        "DEVICE_API_BASE_URL": "https://bms-cn.rosiwit.com"
      }
    }
  }
}
```

## ComfyUI 远程调用 (comfyui_remote.py)

通过 ComfyUI 原生 HTTP API 远程驱动其它机器上的 ComfyUI（默认 `http://192.168.31.34:8188`），
无需在本地安装 ComfyUI 或 comfy-cli。环境变量 `COMFYUI_URL` 可覆盖目标地址。

### 工具列表

| 工具 | 说明 |
|------|------|
| `set_comfyui_server` / `get_comfyui_server` | 切换 / 查看目标 ComfyUI 地址 |
| `get_server_status` | 版本、内存、显卡与显存、队列长度 |
| `list_models` | 列出磁盘上的模型（checkpoints / loras / vae / ...） |
| `search_nodes` / `get_node_info` | 搜索节点、查看节点输入输出定义 |
| `validate_workflow` | 提交前校验节点类型、输入名与连线 |
| `upload_image` | 上传本地图片到远程 input 目录 |
| `run_workflow` | 提交工作流（API 格式或 UI 导出格式）、等待、下载并内联返回图片 |
| `get_job_status` / `wait_for_job` | 查询 / 等待任务状态 |
| `get_outputs` | 取回已完成任务的输出文件 |
| `get_queue_status` / `interrupt` / `clear_queue` | 队列查看、中断当前任务、清空队列 |
| `free_memory` | 释放远程显存 |
| `generate_image` | 内置 SD 文生图工作流快速出图 |

### 使用示例

```python
get_server_status()
list_models(folder="checkpoints")

upload = upload_image(file_path=r"C:\pics\in.png")
run_workflow(
    workflow_file=r"C:\workflows\txt2img_api.json",
    download_dir=r"C:\comfy_out",
    include_images=True,
)
generate_image(prompt="a cat astronaut", width=1024, height=1024, download_dir=r"C:\comfy_out")
```

工作流推荐使用 ComfyUI 的 **Workflow -> Export (API)** 导出的 JSON；直接使用前端导出的
UI 格式也可以，服务端会自动按目标 ComfyUI 的 `object_info` 转换，并在日志中给出无法映射的
控件警告。