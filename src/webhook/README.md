# Webhook 插件

外部系统通过 HTTP Webhook 触发 Agent 执行任务。

## 配置

配置文件: `config/webhook.json`

```json
{
    "host": "0.0.0.0",
    "port": 8081,
    "path": "/webhook/execute",
    "tokens": ["your-secret-token"],
    "callback_timeout": 30,
    "max_content_length": 10000
}
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| host | 监听地址 | 0.0.0.0 |
| port | 监听端口 | 8081 |
| path | API 路径前缀 | /webhook/execute |
| tokens | 认证令牌列表，为空则不认证 | [] |
| callback_timeout | 回调超时时间(秒) | 30 |
| max_content_length | 最大内容长度 | 10000 |

## API 接口

### 1. 提交任务

```
POST /webhook/execute
```

**请求体:**

```json
{
    "content": "帮我分析今天的销售数据",
    "task_id": "optional-custom-task-id",
    "session_id": "optional-session-id",
    "callback_url": "http://your-server/callback",
    "sync": false
}
```

| 参数 | 必填 | 说明 |
|------|------|------|
| content | 是 | 任务内容 (也支持 `task` 或 `prompt` 字段名) |
| task_id | 否 | 自定义任务ID，不提供则自动生成 |
| session_id | 否 | 会话ID，用于保持上下文，不提供则自动生成 |
| callback_url | 否 | 任务完成后回调地址 |
| sync | 否 | 是否同步执行，默认 false |

**异步执行响应:**

```json
{
    "task_id": "abc123",
    "status": "pending",
    "message": "Task submitted successfully",
    "status_url": "/webhook/execute/abc123"
}
```

**同步执行响应 (sync=true):**

```json
{
    "task_id": "abc123",
    "status": "completed",
    "result": "Agent 执行结果..."
}
```

### 2. 查询任务状态

```
GET /webhook/execute/{task_id}
```

**响应:**

```json
{
    "task_id": "abc123",
    "status": "running",
    "created_at": "2024-01-15T10:30:00",
    "error": null
}
```

状态值: `pending` | `running` | `completed` | `failed`

### 3. 获取任务结果

```
GET /webhook/execute/{task_id}/result
```

**响应:**

```json
{
    "task_id": "abc123",
    "status": "completed",
    "result": "Agent 执行结果...",
    "error": null
}
```

### 4. 列出任务

```
GET /webhook/tasks?status=completed&limit=10
```

**响应:**

```json
{
    "count": 5,
    "tasks": [
        {"task_id": "abc123", "status": "completed", "created_at": "..."},
        {"task_id": "def456", "status": "pending", "created_at": "..."}
    ]
}
```

### 5. 健康检查

```
GET /health
```

**响应:**

```json
{
    "status": "ok",
    "service": "webhook"
}
```

## 认证

当配置了 `tokens` 时，请求需携带认证信息:

**方式一 - Authorization Header:**

```bash
curl -H "Authorization: Bearer your-secret-token" \
     -H "Content-Type: application/json" \
     -d '{"content":"hello"}' \
     http://localhost:8081/webhook/execute
```

**方式二 - X-Webhook-Token Header:**

```bash
curl -H "X-Webhook-Token: your-secret-token" \
     -H "Content-Type: application/json" \
     -d '{"content":"hello"}' \
     http://localhost:8081/webhook/execute
```

## 回调通知

当提供 `callback_url` 时，任务完成后会 POST 请求回调地址:

```json
{
    "task_id": "abc123",
    "status": "completed",
    "result": "执行结果...",
    "error": null,
    "completed_at": "2024-01-15T10:35:00"
}
```

## 使用示例

### curl 示例

```bash
# 异步执行
curl -X POST http://localhost:8081/webhook/execute \
  -H "Content-Type: application/json" \
  -d '{"content": "帮我写一个Python脚本计算斐波那契数列"}'

# 同步执行 (等待结果)
curl -X POST http://localhost:8081/webhook/execute \
  -H "Content-Type: application/json" \
  -d '{"content": "分析数据", "sync": true}'

# 带会话上下文
curl -X POST http://localhost:8081/webhook/execute \
  -H "Content-Type: application/json" \
  -d '{"content": "继续上次的任务", "session_id": "my-session-123"}'

# 带回调通知
curl -X POST http://localhost:8081/webhook/execute \
  -H "Content-Type: application/json" \
  -d '{"content": "生成报告", "callback_url": "http://my-server/webhook/callback"}'

# 查询任务状态
curl http://localhost:8081/webhook/execute/abc123

# 获取任务结果
curl http://localhost:8081/webhook/execute/abc123/result
```

### Python 示例

```python
import httpx

# 提交任务
response = httpx.post(
    "http://localhost:8081/webhook/execute",
    json={"content": "帮我分析数据", "sync": True},
    headers={"X-Webhook-Token": "your-token"}
)
result = response.json()
print(result["result"])

# 异步提交 + 轮询
response = httpx.post(
    "http://localhost:8081/webhook/execute",
    json={"content": "复杂任务"}
)
task_id = response.json()["task_id"]

import time
while True:
    status = httpx.get(f"http://localhost:8081/webhook/execute/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(2)

result = httpx.get(f"http://localhost:8081/webhook/execute/{task_id}/result").json()
print(result)
```

### JavaScript 示例

```javascript
// 提交任务
const response = await fetch('http://localhost:8081/webhook/execute', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-Webhook-Token': 'your-token'
    },
    body: JSON.stringify({
        content: '帮我写代码',
        sync: true
    })
});

const result = await response.json();
console.log(result.result);
```

## 启动

Agent 启动时会自动加载 Webhook 插件:

```python
# agent.py 中已配置
def _init_webhook_plugin(self):
    from webhook import WebhookPlugin
    self.webhook_plugin = WebhookPlugin()
    self.webhook_plugin.register_agent(self.run_with_session_id)
    self.webhook_plugin.start()
```

启动 Agent 后，Webhook 服务监听在 `http://0.0.0.0:8081`。