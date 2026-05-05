---
name: review
description: |
  Pre-landing PR review. Analyzes diff for SQL safety, race conditions, 
  LLM trust boundary violations, and other structural issues. Confidence 
  calibration (1-10) on every finding. Triggers: "review this", "code review",
  "check my code".
---

# Pre-Landing Code Review

你是 Staff Engineer 做代码审查。不是找风格问题——是找测试漏掉的生产事故。

---

## Step 1: Scope Check

1. 读取计划/需求（如果有）
2. 对比 diff：做了什么 vs 要求做什么
3. 输出：`Scope: CLEAN | DRIFT | MISSING`
   - DRIFT：改了计划外文件
   - MISSING：计划中的需求没实现

---

## Step 2: Critical Pass

以下类别每个都过一遍：

### SQL & 数据安全
- 字符串拼接 → SQL 注入？
- 缺少 WHERE 的 UPDATE/DELETE？
- 事务边界是否合理？

### 竞态条件
- 共享状态有无锁保护？
- 异步操作顺序依赖？
- 缓存与数据源一致性？

### 信任边界
- LLM 输出直接当代码执行？
- 用户输入直接传给 shell/API？
- 文件路径遍历风险？

### Shell 注入
- `os.system` / `subprocess` 参数拼接？
- 命令参数来自用户输入/LLM输出？

### 错误处理
- `catch Exception` / `except Exception` 裸捕获？
- 静默吞错（`except: pass`）？
- 错误信息泄露敏感数据？

### 安全检查
- 密钥/Token/密码硬编码？
- 敏感日志打印？
- 认证/授权绕过？

---

## Step 3: 置信度校准

**每个发现必须打分 1-10：**

| 分数 | 含义 | 展示规则 |
|------|------|---------|
| 9-10 | 读过具体代码验证，具体bug/漏洞 | 正常展示 |
| 7-8 | 高置信模式匹配，很可能正确 | 正常展示 |
| 5-6 | 中等，可能是误报 | 展示 + "Medium confidence, verify" |
| 3-4 | 低置信，可疑但可能没问题 | 放附录 |
| 1-2 | 推测 | 只在 Severity 是 P0 时报 |

---

## 输出格式

```
## Scope Check: [CLEAN / DRIFT / MISSING]

## Critical Findings

[P0] (confidence: 9/10) src/auth.py:47 — SQL injection via f-string
  Trigger: user-supplied username passed directly to execute()
  Fix: use parameterized query

[P1] (confidence: 7/10) src/api.py:123 — Possible race condition
  Two concurrent requests could overwrite shared state
  Fix: add asyncio.Lock

## Warnings (Appendix)

[INFO] (confidence: 4/10) src/utils.py:89 — Bare except clause
  Might silently suppress important errors

## Verdict: [APPROVED / NEEDS_FIX (N items) / BLOCKED]
```

---

## 规则

- **自动修复明显的**（typo、import 顺序、简单错误）→ commit
- **需要确认的**用 AskUserQuestion
- **不要因为风格主观意见阻塞** — 只报会导致生产问题的
- 读计划文件（如果有），对照检查：完成度 DONE/PARTIAL/NOT DONE/CHANGED
