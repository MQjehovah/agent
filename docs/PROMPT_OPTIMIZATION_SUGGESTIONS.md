# Prompt 生成与动态拼装优化建议

> 基于 [claude-code](https://github.com/l3tchupkt/claude-code) 反编译源码分析，对比本项目 prompt 系统的差距与改进方案。

---

## 核心差距总览

| 维度 | claude-code | 本项目 | 差距 |
|------|-------------|--------|------|
| **Prompt 结构** | 分层 pipeline：静态区 → 动态区 → 缓存边界 | 简单字符串拼接 `+=` | 无分层，无缓存 |
| **CLAUDE.md 加载** | 目录向上遍历 + 优先级排序 + @include + 条件 paths | 固定读 workspace/PROMPT.md | 单文件，无发现机制 |
| **记忆注入** | 按任务相关性搜索，side-query 选 top-5 | 全量注入所有记忆 | 无关记忆浪费上下文 |
| **技能加载** | 按文件路径条件激活，支持延迟加载 | 启动时全量加载追加到 prompt | 无条件激活 |
| **环境上下文** | cwd, platform, git, shell, model info | 无 | 完全缺失 |
| **工具描述** | 每个工具有独立 prompt.ts，含使用指南和示例 | 硬编码 description 属性 | 缺少使用指南 |
| **压缩后恢复** | 恢复最近读取的 5 个文件 + 调用过的 skills | 只做摘要，丢失文件上下文 | 丢失关键状态 |
| **每轮动态注入** | 根据 recent tools、当前路径动态补充上下文 | 首次加载后不再变化 | 无动态调整 |

---

## 优化 1：Prompt 分层 Pipeline

**现状问题：** 当前 prompt 是在 `agent.py` 的 `initialize()` 中用 `+=` 顺序拼接的，所有内容被同等对待，没有缓存优化。

**claude-code 做法：** 将 prompt 分为 **静态区**（系统规则、工具指南）和 **动态区**（记忆、环境、会话状态），中间有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 边界，静态区可被 LLM prompt cache 缓存。

**建议新增 `src/prompt.py`：**

```python
"""
Prompt 分层拼装器

结构：
┌─────────────────────────────────────┐
│ Static Section (可被 prompt cache)    │
│  - 角色定义                          │
│  - 行为规则                          │
│  - 工具使用指南                      │
│  - 代码分析方法论                    │
│  - 工具描述汇总                      │
├────── DYNAMIC_BOUNDARY ──────────────┤
│ Dynamic Section (每轮可能变化)        │
│  - 环境上下文 (cwd, git, platform)   │
│  - 记忆上下文 (按任务筛选)            │
│  - 技能上下文 (按需加载)             │
│  - 子代理列表                        │
│  - 会话状态 (当前任务进度)           │
└─────────────────────────────────────┘
"""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.prompt")

DYNAMIC_BOUNDARY = "\n\n---\n\n"  # 静态区和动态区的分隔


@dataclass
class PromptSection:
    name: str
    content: str
    is_static: bool = True  # True=静态区，False=动态区
    priority: int = 0       # 排序优先级，数字越小越靠前


class PromptBuilder:
    def __init__(self):
        self._sections: list[PromptSection] = []

    def add(self, name: str, content: str, is_static: bool = True, priority: int = 0):
        """添加一个 prompt 区块"""
        if not content or not content.strip():
            return
        # 去重
        self._sections = [s for s in self._sections if s.name != name]
        self._sections.append(PromptSection(
            name=name, content=content.strip(),
            is_static=is_static, priority=priority
        ))

    def build(self) -> tuple[str, str]:
        """构建最终 prompt，返回 (static_prompt, dynamic_prompt)"""
        static = []
        dynamic = []

        # 按 priority 排序
        for section in sorted(self._sections, key=lambda s: s.priority):
            if section.is_static:
                static.append(f"## {section.name}\n\n{section.content}")
            else:
                dynamic.append(f"## {section.name}\n\n{section.content}")

        static_str = "\n\n".join(static)
        dynamic_str = DYNAMIC_BOUNDARY + "\n\n".join(dynamic) if dynamic else ""
        return static_str, dynamic_str

    def build_full(self) -> str:
        """构建完整 prompt"""
        s, d = self.build()
        return s + d

    def remove(self, name: str):
        self._sections = [s for s in self._sections if s.name != name]

    def list_sections(self) -> list[dict]:
        return [
            {"name": s.name, "is_static": s.is_static,
             "priority": s.priority, "chars": len(s.content)}
            for s in sorted(self._sections, key=lambda s: s.priority)
        ]
```

**在 Agent 中集成：**

```python
# agent.py 中改造 initialize()
def _build_prompt(self):
    from prompt import PromptBuilder
    builder = PromptBuilder()

    # === 静态区 (可缓存) ===
    builder.add("角色定义", self.system_prompt, is_static=True, priority=0)
    builder.add("工具使用指南", self._get_tool_guidelines(), is_static=True, priority=10)
    builder.add("代码分析方法论", self._get_code_analysis_guidelines(), is_static=True, priority=20)
    builder.add("工具列表", self._get_tool_summary(), is_static=True, priority=30)

    # === 动态区 (每轮可能变化) ===
    builder.add("环境上下文", self._get_env_context(), is_static=False, priority=50)
    builder.add("记忆上下文", memory_context, is_static=False, priority=60)
    builder.add("技能列表", skills_prompt, is_static=False, priority=70)
    builder.add("子代理列表", subagent_prompt, is_static=False, priority=80)

    self._prompt_builder = builder
    return builder.build_full()

def _get_env_context(self) -> str:
    """动态生成环境上下文（claude-code 的做法）"""
    import platform
    import subprocess
    cwd = os.getcwd()
    is_git = os.path.exists(os.path.join(cwd, ".git"))
    branch = ""
    if is_git:
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode().strip()
        except Exception:
            pass
    return (
        f"工作目录: {cwd}\n"
        f"Git 仓库: {'是' if is_git else '否'}\n"
        f"当前分支: {branch or 'N/A'}\n"
        f"平台: {platform.system()} {platform.release()}\n"
        f"模型: {self.client.model}"
    )
```

---

## 优化 2：环境上下文注入

**现状问题：** Agent 不知道自己在哪台机器上、哪个目录、是否在 git 仓库中。LLM 无法给出与环境相关的建议。

**claude-code 做法：** 每次构建 prompt 时注入 `cwd`, `isGit`, `platform`, `shell`, `model` 等信息。

**具体实现：** 见上面 `_get_env_context()` 方法，直接加入 PromptBuilder 动态区。

---

## 优化 3：按任务相关性筛选记忆

**现状问题：** 当前 `_init_memory()` 加载全部记忆内容追加到 system_prompt，无论是否与当前任务相关。

**claude-code 做法：** 用 side-query（一次轻量 LLM 调用）从 200 个记忆文件中选 top-5 相关的。

**建议新增 `src/memory/relevance.py`：**

```python
"""记忆相关性搜索 — 根据当前任务筛选相关记忆"""
import logging

logger = logging.getLogger("agent.memory.relevance")


async def find_relevant_memories(
    query: str,
    memory_manager,
    llm_client=None,
    max_results: int = 5,
) -> list[str]:
    """根据任务内容筛选相关记忆

    策略：优先关键词匹配，可选 LLM 语义匹配
    """
    # 策略 1：关键词匹配（零成本，总是执行）
    keyword_results = _keyword_search(query, memory_manager, max_results)

    # 策略 2：LLM 语义匹配（有成本，可选）
    if llm_client and len(keyword_results) < max_results:
        try:
            semantic_results = await _semantic_search(
                query, memory_manager, llm_client,
                max_results - len(keyword_results),
                exclude=set(keyword_results)
            )
            keyword_results.extend(semantic_results)
        except Exception as e:
            logger.warning(f"语义记忆搜索失败: {e}")

    return keyword_results[:max_results]


def _keyword_search(query: str, memory_manager, max_results: int) -> list[str]:
    """关键词匹配：从查询中提取关键词，在记忆中搜索"""
    all_memory = memory_manager.load_memory("")
    if not all_memory:
        return []

    # 提取查询中的关键词（简单分词）
    keywords = set(query.replace("的", " ").replace("了", " ")
                   .replace("在", " ").replace("是", " ")
                   .split())
    keywords = {k for k in keywords if len(k) >= 2}

    if not keywords:
        return [all_memory] if all_memory else []

    # 按关键词命中数排序记忆段落
    paragraphs = all_memory.split("\n\n")
    scored = []
    for p in paragraphs:
        if not p.strip():
            continue
        score = sum(1 for k in keywords if k in p)
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:max_results]]


async def _semantic_search(
    query: str, memory_manager, llm_client,
    max_results: int, exclude: set
) -> list[str]:
    """LLM 语义匹配：用轻量 LLM 调用筛选相关记忆"""
    all_memory = memory_manager.load_memory("")
    if not all_memory:
        return []

    # 将记忆分段
    paragraphs = [p for p in all_memory.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    # 构建选择 prompt
    numbered = "\n".join(f"[{i}] {p[:100]}" for i, p in enumerate(paragraphs[:50]))
    prompt = (
        f"从以下记忆段落中选出与任务最相关的 {max_results} 个段落编号。\n"
        f"如果不确定是否相关，不要选。\n"
        f"任务: {query}\n\n"
        f"记忆段落:\n{numbered}\n\n"
        f"只返回编号列表，如: 3, 7, 15"
    )

    response = await llm_client.chat(
        messages=[
            {"role": "system", "content": "你是记忆检索助手，只返回相关编号。"},
            {"role": "user", "content": prompt}
        ],
        tools=None, stream=False, use_cache=False
    )
    text = response.choices[0].message.content or ""

    # 解析编号
    import re
    indices = [int(x) for x in re.findall(r'\d+', text) if int(x) < len(paragraphs)]
    return [paragraphs[i] for i in indices[:max_results]]
```

**在 Agent.run() 中使用：**

```python
# agent.py 中替换全量记忆加载
if self.memory:
    from memory.relevance import find_relevant_memories
    relevant = await find_relevant_memories(task, self.memory, self.client)
    if relevant:
        memory_context = "\n\n".join(relevant)
        # 注入动态区而非静态区
        self._prompt_builder.add(
            "记忆上下文", f"以下是与当前任务相关的记忆:\n{memory_context}",
            is_static=False, priority=60
        )
```

---

## 优化 4：工具使用指南（Tool Guidelines）

**现状问题：** 工具只有一句 `description`，LLM 不知道该怎么组合使用。

**claude-code 做法：** 每个工具有独立的 `prompt.ts`，包含详细使用指南、示例、注意事项。系统 prompt 中还有总的工具使用规则。

**建议新增工具指南并注入到静态区：**

```python
# agent.py 中新增方法
def _get_tool_guidelines(self) -> str:
    return """### 工具使用规则

1. **优先使用专用工具而非 shell 命令：**
   - 读文件 → file_operation(read) 而非 cat/head/tail
   - 编辑文件 → edit 而非 sed/awk
   - 写文件 → file_operation(write) 而非 echo/heredoc
   - 搜索文件名 → glob 而非 find/ls
   - 搜索内容 → grep 而非 grep/rg 命令
   - shell 仅用于没有专用工具的系统命令

2. **代码分析遵循 "先搜后读" 策略：**
   - 第一步：glob 找文件列表 → grep 搜索关键函数/类
   - 第二步：file_operation(read, offset=行号, limit=50) 精确读取
   - 禁止一次性读取整个项目

3. **file_operation(read) 使用规则：**
   - 默认只读 200 行，最大 2000 行
   - 大文件先用 grep 找行号，再用 offset+limit 精确读取
   - 多个文件并行读取时，每个 limit 控制在 50-100 行

4. **edit 使用规则：**
   - old_text 必须精确匹配，提供足够上下文使其唯一
   - 修改前先 file_operation(read) 确认内容

5. **多工具调用：**
   - 独立的操作可以并行调用
   - 有依赖关系的操作必须顺序执行

6. **ask_user 使用场景：**
   - 执行危险操作前请求确认
   - 需要用户提供额外信息时
   - 展示中间结果请用户决策"""

def _get_code_analysis_guidelines(self) -> str:
    return """### 代码分析方法论

分析、理解或修改代码时，遵循以下步骤：

**第一步：定位目标文件（不读内容）**
```
glob("**/*.py")     → 找到相关文件列表
grep("class |def ") → 快速了解接口
```

**第二步：定向读取（只读需要的部分）**
```
file_operation(read, path="main.py", offset=10, limit=50) → 只读核心逻辑
```

**第三步：按需深入**
```
grep("function_name") → 找调用链
edit(path="...", old_text="...", new_text="...") → 精确修改
```

**禁止：** 逐文件 file_operation(read) 全量读取 → 会撑爆上下文
**推荐：** glob → grep → file_operation(read, offset, limit)"""
```

---

## 优化 5：工具描述增强 — 每个 prompt 内嵌使用指南

**现状问题：** 工具的 `description` 属性太简短。对比：

```
# 本项目
"file_operation": "文件操作工具。支持读取、写入、追加、删除文件内容。"

# claude-code 的 FileReadTool prompt
"Performs exact string replacements in files.
Usage:
- You must use your Read tool at least once before editing...
- The edit will FAIL if old_string is not unique...
- Use replace_all for renaming strings across the file."
```

**建议改造工具 description：**

```python
# src/tools/file.py — 增强 description
@property
def description(self) -> str:
    return """文件操作工具。

使用规则：
- 读文件用 file_operation(read)，不要用 shell 的 cat/head/tail
- 默认读取 200 行，可通过 offset 和 limit 分段读取大文件
- 大文件先用 grep 找到目标行号，再用 offset+limit 精确读取
- 修改文件前先 read 确认内容
- 写文件会覆盖原内容，追加用 append

示例：
file_operation(read, path="/src/main.py")  — 读取前 200 行
file_operation(read, path="/src/main.py", offset=100, limit=50)  — 读取第 101-150 行
file_operation(write, path="/tmp/out.txt", content="hello")  — 写入文件"""
```

---

## 优化 6：压缩后状态恢复

**现状问题：** 当上下文压缩后，之前读取的文件内容全部丢失，Agent 可能需要重新读取同样的文件。

**claude-code 做法：** 压缩后恢复最近 5 个读取的文件（每个截断到 5000 token），以及调用过的 skills。

**建议在 Agent 中跟踪读取的文件：**

```python
# agent.py 新增
class Agent:
    def __init__(self, ...):
        # ...
        self._recent_files: OrderedDict[str, str] = OrderedDict()  # path -> preview
        self._recent_skills: list[str] = []
        self._max_recent_files = 5

    def track_file_read(self, path: str, content: str):
        """记录最近读取的文件（用于压缩后恢复）"""
        # 只保留前 200 行作为预览
        lines = content.split("\n")
        preview = "\n".join(lines[:200])
        if len(preview) > 5000:
            preview = preview[:5000] + "\n... [截断]"

        self._recent_files[path] = preview
        self._recent_files.move_to_end(path)
        # 保留最近 N 个
        while len(self._recent_files) > self._max_recent_files:
            self._recent_files.popitem(last=False)

    def get_recent_files_context(self) -> str:
        """生成最近文件的上下文（用于压缩后注入）"""
        if not self._recent_files:
            return ""
        parts = ["以下是本次会话中最近读取的文件（压缩后恢复）："]
        for path, preview in self._recent_files.items():
            parts.append(f"\n### {path}\n```\n{preview}\n```")
        return "\n".join(parts)
```

**在 `_execute_tool_safe` 中跟踪：**

```python
async def _execute_tool_safe(self, name: str, args: Dict) -> str:
    result = await self._execute_tool(name, args)

    # 跟踪文件读取
    if name == "file_operation" and args.get("operation") == "read":
        path = args.get("path", "")
        if path and '"success": true' in result:
            try:
                content = json.loads(result).get("content", "")
                self.track_file_read(path, content)
            except Exception:
                pass

    return result
```

**在压缩后注入恢复上下文：**

```python
# agent_session.py compress_if_needed 中，LLM 压缩后：
compressed = [
    *system_msgs,
    {"role": "assistant", "content": f"[对话历史摘要]\n{summary}"},
    *recent_msgs,
]

# 如果 Agent 有最近文件，追加到摘要后面
# (通过返回压缩后的 messages 和额外的恢复上下文)
```

---

## 优化 7：动态 Prompt 每轮更新

**现状问题：** prompt 在 `initialize()` 时拼装一次，之后不再变化。即使上下文压缩了，静态 prompt 部分也没机会更新。

**claude-code 做法：** 每轮 _think 前都会 `resolveSystemPromptSections()`，动态区按需重新计算。

**建议在 Agent 的主循环中每轮更新动态区：**

```python
# agent.py 的 run() 主循环中
for i in range(self.max_iterations):
    # 每轮更新动态 prompt
    self._update_dynamic_prompt(session, task)

    response = await self._think(session.messages)
    # ...

def _update_dynamic_prompt(self, session, task):
    """每轮更新动态 prompt 区块"""
    if not self._prompt_builder:
        return

    # 更新环境上下文（可能有变化）
    self._prompt_builder.add(
        "环境上下文", self._get_env_context(),
        is_static=False, priority=50
    )

    # 根据任务更新记忆上下文
    if self.memory:
        memory_context = self.memory.load_memory(task)
        if memory_context:
            self._prompt_builder.add(
                "记忆上下文", memory_context,
                is_static=False, priority=60
            )

    # 重建 session 的 system prompt
    _, dynamic = self._prompt_builder.build()
    # 更新 session 中的系统消息
    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0]["content"] = self._prompt_builder.build_full()
```

---

## 实施优先级

| 优先级 | 优化项 | 预期效果 | 工作量 |
|--------|--------|----------|--------|
| **P0** | 优化 1: Prompt 分层 Pipeline | 为缓存优化和动态更新打基础 | 中 |
| **P0** | 优化 3: 记忆相关性筛选 | 避免无关记忆浪费上下文 | 中 |
| **P0** | 优化 4: 工具使用指南 | 指导 LLM 正确组合使用工具 | 小 |
| **P1** | 优化 2: 环境上下文注入 | LLM 了解运行环境 | 小 |
| **P1** | 优化 5: 工具描述增强 | 提升工具调用准确率 | 小 |
| **P1** | 优化 6: 压缩后状态恢复 | 压缩后不丢失关键上下文 | 中 |
| **P2** | 优化 7: 动态 Prompt 每轮更新 | 实现真正的动态上下文 | 大 |
