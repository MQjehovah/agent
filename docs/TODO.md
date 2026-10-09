# 平台优化 TODO（Feature 需求池 + List 待办）

> 更新：2026-10-09 · 来源：CubePlex / CubeLoop 调研 + 近期线上问题复盘
> 约定：**Feature = 仅记录需求（不一定开发，需讨论）**；**List = 已确认/下一步执行的代办项**。
> 另：RAG 编辑器方向的累计清单在 `rag/docs/TODO.md`（两文互不替代）。

## Feature（需求池，待讨论）

- [ ] **F1 MCP OAuth 接入 + 凭据三级作用域**
  许多 SaaS MCP（GitHub/Slack/Linear/Notion…）只支持 OAuth；现市场仅静态密钥。
  方向：market 托管 OAuth 客户端（发现 RFC9728/8414 → DCR RFC7591 或预配置 → 授权码+PKCE → 刷新），
  凭据按 用户/团队/组织 三级作用域存加密 vault；relay 注入 Bearer 并 401 自愈一次。
  （体验：能力详情「连接账号」→浏览器授权→回跳显示已连接/可断开）
- [ ] **F2 团队空间 Workspace**：技能/记忆/工具授权/产物/工作状态归属"团队"而非个人（部门或项目维度）
- [ ] **F3 三层记忆 × 类型标签**：个人/团队/组织 × preference/project_fact/procedure/correction/decision/org_policy；按 scope 注入
- [ ] **F4 Artifacts 产物卡片**：生成物识别、版本化、预览面板（web/desktop）、下载
- [ ] **F5 持久沙箱**：每用户（或每团队）长驻执行环境，文件/依赖/工作树跨任务存活（代码类任务刚需）
- [ ] **F6 Topics 群内话题**：群聊按话题隔离上下文/执行，共享 Agent 身份与知识
- [ ] **F7 群聊「共享 vs 每人独立」可配**：敏感场景每人独立上下文
- [ ] **F8 多渠道扩展**：飞书（已有插件基座待接用户身份）/企微/Slack 按需求排
- [ ] **F9 成本按部门/团队聚合报表**：usage 现有用户维度，加组织维度视图

## List（待办项）

- [x] ~~**L1 agent 全量对齐部署**~~：**已完成（2026-10-09，e4aeede9）**——git archive 导出 HEAD → 备份（`agent-build.bak-fullsync-20261009_142533.tgz`）→ 覆盖 `/home/xzrobot/agent-build` + 清理 5 个陈旧文件 → 重建镜像/容器；冒烟：healthz 200、web 200、平台轨 relay 正常。残留观察：`mysql_query` 平台能力因 DB_PORT 平台密钥为空连接失败（市场数据侧，非代码）
- [x] ~~**L2 market 漂移来源排查**~~：**已并入（2026-10-09）**——漂移来自并行工作线（gitlab `9fcbc88` 等 3 个提交：市场走查/试用个人网关密钥）；已 merge 整合（`1e295ee`）并用其 `deploy.py` 全量部署（含 409 文案补"撤回"、白名单用例适配禁自审）
- [ ] **L3 部署工程化（含本次对账脚本常态化）**：agent 改 compose 声明挂载（防再丢 shared）、各仓 `scripts/deploy`=同步+构建+重建+冒烟、部署后对账脚本入库（market 已有 `deploy.py` 可移植；agent 仍为手动 git-archive 流程）
- [ ] **L4 渐进披露优化**：工具激活改为 dispatcher 间接调用、不改 tools 数组（保 prompt 缓存字节稳定）
- [ ] **L5 模型回退链**：主模型限流/故障自动切备用（CubeLoop FallbackBoundModel 思路）
- [ ] **L6 Faux LLM 确定性测试模式**：预置响应序列，跑通整条 Agent 链路（子代理/工具/流式）回归
- [ ] **L7 仓库规范补齐**：CHANGELOG、pre-commit（镜像 CI）
- [ ] **L8 钉钉群目录真机验证**：共享挂载恢复后，等一条真实群消息确认回写（mtime 更新）
- [ ] **L9 遗留决定项**：RAG `compiler_kind` 是否彻底收口（当前仅兼容保留、默认 wiki）；agent `.env` 死配置清扫（MARKET_ACT_AS 等）
