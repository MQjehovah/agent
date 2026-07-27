---
name: gitlab-merge
description: 通过 GitLab API 将指定项目的源分支合入目标分支。全程云端操作，不克隆仓库。接收参数：<project_path> <source_branch> <target_branch>
---

# GitLab 云端分支合并

你收到一个合并任务，请直接执行固化脚本完成操作。

## 参数

从任务描述中提取以下参数，如果未提供则询问用户：
- `PROJECT_PATH`: 项目路径，如 `cloud/xz-sc50`、`cloud/xz-data`
- `SOURCE_BRANCH`: 源分支，如 `dev`、`test`
- `TARGET_BRANCH`: 目标分支，如 `test`、`master`

## 一键执行

直接运行固化脚本，传入三个参数即可（路径为相对于本技能目录的路径）：

```bash
python "./scripts/gitlab_merge.py" <PROJECT_PATH> <SOURCE_BRANCH> <TARGET_BRANCH>
```

**示例**：将 `cloud/xz-data` 的 `dev` 合入 `test`：
```bash
python "./scripts/gitlab_merge.py" cloud/xz-data dev test
```

**示例**：将 `cloud/xz-sc50` 的 `test` 合入 `master`：
```bash
python "./scripts/gitlab_merge.py" cloud/xz-sc50 test master
```

## 执行后

1. 脚本会自动完成：认证 → 获取项目 → 确认分支 → 创建MR → 冲突检查 → 执行合并
2. 读取结果文件获取详情：
```bash
cat "./scripts/merge_result.txt"
```
3. 将合并结果报告给用户

## 退出码说明

| 退出码 | 含义 |
|--------|------|
| 0 | 合并成功 |
| 1 | 失败（认证失败 / 项目不存在 / 分支不存在 / 有冲突 / 合并被拒绝） |

## 异常处理

| 异常 | 处理 |
|------|------|
| 认证失败 | 检查密码/Token，查看环境变量 `IT_SYSTEM_PASSWORD` |
| 项目不存在 | 确认 PROJECT_PATH 拼写，检查项目是否已归档 |
| 分支不存在 | 确认分支名拼写 |
| 存在冲突 | 通知用户需人工解决冲突 |
| MR 已存在 | 脚本会自动复用已有的 open MR |
| 合并被拒绝 | 检查是否有 pipeline 阻塞或保护分支规则 |
