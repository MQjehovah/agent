---
name: gitlab-merge
description: 通过 GitLab API 将指定项目的源分支合入目标分支。全程云端操作，不克隆仓库。接收参数：<project_path> <source_branch> <target_branch>
---

# GitLab 云端分支合并

你收到一个合并任务，请严格按以下步骤逐步执行 shell 命令完成操作。

## 参数

从任务描述中提取以下参数，如果未提供则询问用户：
- `PROJECT_PATH`: 项目路径，如 `cloud/xz-sc50`
- `SOURCE_BRANCH`: 源分支，如 `test`
- `TARGET_BRANCH`: 目标分支，如 `master`

## 环境常量

- `GITLAB_URL`: `http://gitlab.xzrobot.com`
- `ACCOUNT`: `s_software`（LDAP 账号）

## 执行步骤

### Step 1: 获取认证

先用 LDAP 方式登录获取 cookie：

```bash
curl -s -c /tmp/gitlab_cookies.txt -L \
  -X GET "${GITLAB_URL}/users/sign_in" \
  | grep -oP 'name="authenticity_token" value="\K[^"]+' > /tmp/gitlab_csrf.txt
```

```bash
curl -s -c /tmp/gitlab_cookies.txt -b /tmp/gitlab_cookies.txt -L \
  -X POST "${GITLAB_URL}/users/auth/ldapmain/callback" \
  -d "authenticity_token=$(cat /tmp/gitlab_csrf.txt)&username=s_software&password=<从配置或记忆中查找密码>"
```

验证登录是否成功：
```bash
curl -s -b /tmp/gitlab_cookies.txt "${GITLAB_URL}/api/v4/user" | head -c 200
```
- 如果返回了用户信息（含 `"username": "s_software"`）→ 继续
- 如果返回 401 → 停止，报告认证失败

> 如果 LDAP 登录不适用，查找环境中是否已有 Private Token，改用 `-H "PRIVATE-TOKEN: xxx"` 方式。

### Step 2: 获取项目 ID

```bash
ENCODED_PATH=$(echo "${PROJECT_PATH}" | sed 's/\//%2F/g')
curl -s -b /tmp/gitlab_cookies.txt \
  "${GITLAB_URL}/api/v4/projects/${ENCODED_PATH}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'项目ID: {d[\"id\"]}, 默认分支: {d.get(\"default_branch\",\"N/A\")}, Web: {d[\"web_url\"]}')"
```

记录 `PROJECT_ID`。

### Step 3: 确认分支存在

```bash
curl -s -b /tmp/gitlab_cookies.txt \
  "${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/repository/branches/${SOURCE_BRANCH}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'源分支: {d[\"name\"]}, 最新commit: {d[\"commit\"][\"short_id\"]} - {d[\"commit\"][\"title\"]}')"
```

```bash
curl -s -b /tmp/gitlab_cookies.txt \
  "${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/repository/branches/${TARGET_BRANCH}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'目标分支: {d[\"name\"]}, 最新commit: {d[\"commit\"][\"short_id\"]} - {d[\"commit\"][\"title\"]}')"
```

- 两个分支都返回信息 → 继续
- 任一返回 404 → 停止，报告分支不存在

### Step 4: 创建 Merge Request

```bash
MR_RESP=$(curl -s -b /tmp/gitlab_cookies.txt \
  -X POST \
  "${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/merge_requests" \
  -H "Content-Type: application/json" \
  -d "{
    \"source_branch\": \"${SOURCE_BRANCH}\",
    \"target_branch\": \"${TARGET_BRANCH}\",
    \"title\": \"Merge branch ${SOURCE_BRANCH} into ${TARGET_BRANCH}\",
    \"remove_source_branch\": false
  }")

echo "$MR_RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
if 'iid' in d:
    print(f'MR创建成功: !{d[\"iid\"]}')
    print(f'状态: {d[\"merge_status\"]}')
    print(f'链接: {d[\"web_url\"]}')
else:
    print(f'创建失败: {d.get(\"message\", d)}')
    sys.exit(1)
"
```

记录 `MR_IID`。

### Step 5: 等待并检查合并状态

等待 GitLab 完成冲突检查（约3-5秒）：

```bash
sleep 5

MERGE_STATUS=$(curl -s -b /tmp/gitlab_cookies.txt \
  "${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/merge_requests/${MR_IID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['merge_status'])")

echo "合并状态: ${MERGE_STATUS}"
```

判断逻辑：
- `can_be_merged` → 进入 Step 6
- `cannot_be_merged` → **停止，报告存在冲突，需人工解决**
- `unchecked` / `checking` → 再等5秒重试，最多3次，仍未通过则停止

### Step 6: 执行合并

```bash
curl -s -b /tmp/gitlab_cookies.txt \
  -X PUT \
  "${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/merge_requests/${MR_IID}/merge" \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
if d.get('state') == 'merged':
    print(f'✅ 合并成功!')
    print(f'合并提交: {d.get(\"merge_commit_sha\",\"N/A\")}')
    print(f'合并时间: {d.get(\"merged_at\",\"N/A\")}')
else:
    print(f'❌ 合并失败: {d.get(\"message\", d)}')
"
```

### Step 7: 清理 & 汇报

清理临时文件：
```bash
rm -f /tmp/gitlab_cookies.txt /tmp/gitlab_csrf.txt
```

输出最终报告：

```
## ✅ 合并完成

| 项目 | 详情 |
|------|------|
| **项目** | {PROJECT_PATH} (ID: {PROJECT_ID}) |
| **源分支** | {SOURCE_BRANCH} |
| **目标分支** | {TARGET_BRANCH} |
| **MR** | !{MR_IID} |
| **合并状态** | merged |
| **MR 链接** | {WEB_URL} |
```

## 异常处理

| 异常 | 处理 |
|------|------|
| Step 1 认证失败 | 检查密码/Token，查找配置文件或记忆中的凭证 |
| Step 2 项目 404 | 确认路径拼写，检查项目是否已归档 |
| Step 3 分支 404 | 确认分支名，用 `.../repository/branches` 列出所有分支 |
| Step 4 MR 已存在 | 用 `?state=opened&source_branch=xxx&target_branch=xxx` 查询已有 MR |
| Step 5 有冲突 | 停止并通知用户，给出冲突文件列表 |
| Step 6 合并失败 | 检查是否被 pipeline 阻塞或有保护分支规则 |
