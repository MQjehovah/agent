---
name: prove-it-tdd
description: 严格 Prove-It 测试驱动开发模式。先写失败测试，证明它失败，再写代码，证明它通过，最后重构。
lifecycle_stage: BUILD
trigger_patterns:
  - "tdd"
  - "test.?driven"
  - "先测试"
  - "测试驱动"
  - "prove.?it"
estimated_tokens: 3000
---

# Prove-It TDD — 严格测试驱动开发

## 核心原则

> **先证它失败，再证它通过。**

不是"先写测试、再写代码"这么简单。每一步都必须有**可见的证据**证明当前状态。

---

## 五步铁律（不可跳过任何一步）

### Step 1: RED — 写一个失败测试

写一个测试，明确描述你**想要**的行为。

```python
# tests/test_user_service.py
def test_get_user_returns_user():
    """给定有效用户ID，返回用户对象"""
    service = UserService()
    user = service.get_user("user_001")
    assert user is not None
    assert user.id == "user_001"
    assert user.name == "张三"
```

**铁律**：测试必须针对**还不存在的代码**。它必然失败。
**检查**：`pytest tests/test_user_service.py -v` 输出测试 **FAILED**。

> ⚠️ 如果测试通过了——你在测已经存在的东西，不要继续，回去写一个新功能的测试。

---

### Step 2: PROVE IT FAILS — 证明它确实失败

运行测试，**保存失败证据**：

```
pytest tests/test_user_service.py -v
```

输出必须包含：
```
FAILED tests/test_user_service.py::test_get_user_returns_user - ...
```

**铁律**：你必须亲眼看到这个 FAILED。如果没看到，说明测试写得不对（可能是语法错误，也可能是测试通过了一个意外行为）。

> ⚠️ 不要跳过这一步。跳过 = 你不知道你的测试是有效的。

---

### Step 3: GREEN — 写恰好够用的代码

写**最小的**代码让测试通过：

```python
# src/user_service.py
class UserService:
    def get_user(self, user_id: str) -> dict:
        return {"id": user_id, "name": "张三"}
```

**铁律**：
- 只写让当前测试通过的最小代码
- 不要写"可能以后用得上"的代码
- 不要优化、不要重构、不要加注释
- 如果有多条测试失败，只修当前这一条

**检查**：`pytest tests/test_user_service.py -v` 输出测试 **PASSED**。

---

### Step 4: PROVE IT PASSES — 证明它确实通过

运行测试，**保存通过证据**：

```
pytest tests/test_user_service.py -v
```

输出必须包含：
```
PASSED tests/test_user_service.py::test_get_user_returns_user
```

**铁律**：你必须亲眼看到这个 PASSED。

> ⚠️ 如果这一步花了超过 3 次尝试 → 你的设计可能有问题。停下来，重新思考接口设计。

---

### Step 5: REFRAME — 重构并重新证明

现在代码通过了，可以重构了。

**重构范围**：
- 提取重复代码
- 优化命名
- 调整目录结构
- 添加边界检查

**重构后铁律**：再次运行测试，证明重构没破坏任何东西。

```
pytest tests/ -v
```

输出必须全部 **PASSED**。

---

## 完整 TDD 循环模板

每次循环的输出必须包含以下证据：

```
=== TDD CYCLE ===
Step 1: 测试 test_X 已编写
Step 2: 运行测试 → FAILED（证据附后）
  → pytest 输出: ...
Step 3: 实现代码
Step 4: 运行测试 → PASSED（证据附后）
  → pytest 输出: ...
Step 5: 重构完成，运行全部测试 → ALL PASSED
  → pytest 输出: ...
=== CYCLE COMPLETE ===
```

---

## 常见违规与纠正

| 违规行为 | 纠正方法 |
|---------|---------|
| 一次写了多个测试 | 只留一个，其他注释掉，逐个循环 |
| 写了比"恰好通过"更多的代码 | 删除多余代码，只留最小实现 |
| 跳过 Step 2（证明失败） | 回退到 Step 1 重新开始 |
| 重构时改了测试逻辑 | 重构只改实现代码，不动测试 |
| 测试太慢（>1 秒） | 使用 mock 替代真实依赖 |

---

## 适用于当前项目的模板

```python
"""TDD 循环记录
Cycle 1:
  测试: test_...
  状态: RED → GREEN → REFRAME
  证据:
    $ pytest tests/... -v
    → FAILED → PASSED
  重构说明: ...
"""
```
