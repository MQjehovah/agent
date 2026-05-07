

## 技能

### 技能的使用遵循‌**渐进式披露**‌原则 ‌

1. ‌**发现阶段**‌：会话启动时，Agent 仅加载每个技能的 `name` 和 `description`（约 50–100 tokens），用于判断是否匹配当前任务。
2. ‌**激活阶段**‌：当用户输入触发技能（如关键词或指令），Agent 将完整的 `SKILL.md` 内容注入当前会话的系统提示（System Prompt）。
3. ‌**执行阶段**‌：若技能包含脚本或资源（如 `scripts/`, `references/`），则在执行过程中按需加载 ‌**5**。

### 技能的核心文件结构

一个标准技能必须包含以下内容：

```plaintext
<skill-name>/
├── SKILL.md          # 必需：含 YAML 元数据 + 执行指令
├── scripts/          # 可选：Shell/Python 脚本
├── references/       # 可选：参考文档（如 API 手册）
└── assets/           # 可选：模板、图标等静态资源
```

其中 `SKILL.md` 的 YAML 头信息示例：

```yaml
---
name: pdf-processing
description: Extracts text and tables from PDF files. Use when working with PDFs.
---
```


TODO:

* [ ] 完成记忆向量化/或者优化目前文件存储记忆的搜索方式，记忆直接塞入prompt会导致分散注意力
* [ ] 优化自学习模块，现在经验提取有限
