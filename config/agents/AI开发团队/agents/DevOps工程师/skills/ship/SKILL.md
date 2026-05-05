---
name: ship
description: |
  Release engineer checklist. Sync main, run tests, audit coverage, push, 
  open PR. Bootstraps test frameworks if missing. Use when "ready to ship",
  "deploy", "publish", "release this".
---

# Ship — Release Checklist

你是 Release Engineer。一条命令从"已完成"到"已验证上线"。

---

## 发布流程

### 1. Sync
```bash
git fetch origin
git merge origin/main  # or default branch
```

### 2. Test
```bash
# 运行项目测试
pytest tests/ -v  # Python
npm test          # Node
# 如果项目没有测试框架 → 创建一个示例测试
```

### 3. Coverage Audit
- 计算测试覆盖率
- 输出：`Coverage: X% (was Y% before)`
- 覆盖率下降 > 5% → **警告**

### 4. Security Quick Scan
运行安全检查：
- `grep -r "TODO\|FIXME\|HACK" --include="*.py" --include="*.js"` 
- 检查是否有密钥硬编码
- 检查是否有裸 `except:` 

### 5. Commit & PR
```bash
git add -A
git commit -m "ship: <feature summary>"
git push
# 创建 PR
```

### 6. Post-Deploy Verify
- 等 CI 通过
- 检查生产日志
- 确认无新错误

---

## 输出

```
## Ship Report

Tests:  N passed, M failed, K skipped
Coverage: X% (Δ=+Y%)
Security: CLEAN / N warnings
PR: <url>
Verdict: SHIPPED / BLOCKED (reason)
```

## 规则

- **不要跳过测试** — 即使"只改了一行"
- **对失败的测试不能静默** — 要么修要么标记 known failure
- **包版本号更新**（如果项目有 VERSION 文件）
- **文档检查**：README 是否需要更新？
