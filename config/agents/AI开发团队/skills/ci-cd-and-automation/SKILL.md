---
name: ci-cd-and-automation
description: Shift Left CI/CD with quality gates. Use when setting up or modifying build and deploy pipelines.
---

# CI/CD 与自动化

## 概述
将质量问题左移（Shift Left），在 CI/CD 流水线中设置质量门禁，尽早发现并阻止问题进入生产环境。强调快速反馈和自动化部署。

## 何时使用
- 搭建新项目的 CI/CD 流水线
- 修改现有构建或部署流程
- 添加新的质量检查或安全扫描阶段
- NOT for 代替本地开发测试——CI/CD 不是本地验证的替代方案

## 核心流程
1. **质量门禁流水线**：lint → typecheck → test → build → security scan → deploy
   - 每个阶段失败则阻断后续流程
   - 无绿色通过不得合入主干
2. **Shift Left**：将问题发现左移——lint 在 typecheck 前，typecheck 在 test 前，越早发现问题修复成本越低
3. **特性开关（Feature Flags）**：通过配置逐步发布新功能，实现灰度发布和即时回滚
4. **失败反馈循环**：
   - 快速失败（Fail Fast）——管道在几秒内报告失败结果
   - 清晰的错误消息——指出具体失败的步骤和原因
   - 通知相关责任人——Slack/钉钉/邮件即时通知
5. **自动化部署**：构建产物不可变（Immutable Artifact），使用版本号标记，支持一键回滚
6. **流水线即代码**：CI/CD 配置存储在代码仓库中（如 GitHub Actions、GitLab CI），与业务代码版本一致

## 常见自我合理化
| 合理化 | 现实 |
|---|---|
| 本地测试通过了，不用跑 CI | CI 环境与本地环境不同——忽略 CI 等于忽视环境差异 |
| lint 失败不影响功能，先合入 | lint 失败是代码质量的信号——质量门禁不能绕过 |
| 手动部署更方便调试 | 手动部署不可重复、不可审计——自动化部署才是可靠的基础 |
| 出了问题再改也不迟 | 生产环境出问题的修复成本是 CI 发现的 100 倍 |
| 测试太慢，先跳过让开发快一点 | 慢的测试需要优化而非跳过——跳过测试只会让问题堆积 |

## 红旗标志
- 合入代码时跳过 CI 检查
- 通过 `[skip ci]` 绕过流水线
- 质量门禁被注释掉或弱化
- 部署流程依赖手动操作
- 生产环境回滚需要复杂的多步骤流程
- CI 失败但无人关注和修复

## 验证清单
- [ ] CI 流水线包含 lint → typecheck → test → build → security scan → deploy 全流程
- [ ] 每个步骤失败会阻断后续流程
- [ ] 特性发布使用 Feature Flags 而非代码分支
- [ ] 部署包使用版本标记，支持一键回滚
- [ ] CI 配置以代码形式存储在仓库中
- [ ] 失败通知自动发送到团队成员
- [ ] CI 平均完成时间在可接受范围内
