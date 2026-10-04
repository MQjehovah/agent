import json
import logging

logger = logging.getLogger("agent.team.pipeline_builder")

DEFAULT_PIPELINE = [
    {"stage": "requirements", "role": "产品经理", "output": "requirements.md", "deps": []},
    {"stage": "architecture", "role": "软件架构师", "output": "architecture.md", "deps": ["requirements"]},
    {"stage": "implementation", "role": "代码工程师", "output": None, "deps": ["architecture"]},
    {"stage": "testing", "role": "测试工程师", "output": "test_report.md", "deps": ["implementation"]},
    {"stage": "security", "role": "安全审查师", "output": "security_report.md", "deps": ["implementation"]},
    {"stage": "deployment", "role": "DevOps工程师", "output": None, "deps": ["testing", "security"]},
    {"stage": "documentation", "role": "文档专员", "output": None, "deps": ["testing"]},
]

FEEDBACK_PIPELINE = [
    {"stage": "requirements", "role": "产品经理", "output": "requirements.md", "deps": []},
    {"stage": "architecture", "role": "软件架构师", "output": "architecture.md", "deps": ["requirements"]},
    {"stage": "implementation", "role": "代码工程师", "output": None, "deps": ["architecture"]},
    {"stage": "testing", "role": "测试工程师", "output": "test_report.md", "deps": ["implementation"],
     "feedback_to": "implementation", "max_loops": 3},
    {"stage": "security", "role": "安全审查师", "output": "security_report.md", "deps": ["implementation"]},
    {"stage": "deployment", "role": "DevOps工程师", "output": None, "deps": ["testing", "security"]},
    {"stage": "documentation", "role": "文档专员", "output": None, "deps": ["testing"]},
]

# 精简三节点流水线(常驻): 规划(架构师) → 开发(代码工程师) → 审核(测试工程师, 不过则回退开发)。
# 用于只保留规划/开发/审核三个常驻节点、避免多角色串行拖慢任务的团队。
LITE_PIPELINE = [
    {"stage": "planning", "role": "软件架构师", "output": None, "deps": []},
    {"stage": "development", "role": "代码工程师", "output": None, "deps": ["planning"]},
    {"stage": "review", "role": "测试工程师", "output": None, "deps": ["development"],
     "feedback_to": "development", "max_loops": 3},
]


def allowed_pipeline_roles(frontmatter: dict) -> "set[str] | None":
    """TEAM.md frontmatter 的 ``pipeline_roles`` 白名单。

    返回允许参与流水线的角色名集合; 缺失/为空/非法时返回 None(=不限制)。
    用于把团队可用成员收敛到少数几个节点(其余角色目录保留但不加载)。
    """
    raw = (frontmatter or {}).get("pipeline_roles")
    if not isinstance(raw, list):
        return None
    names = {str(x).strip() for x in raw if str(x).strip()}
    return names or None


def build_pipeline(
    task: str,
    members: dict,
    mode: str = "auto",
    llm_client=None,
) -> list[dict]:
    """构建执行流水线

    Args:
        task: 原始需求
        members: 可用团队成员 {role_name: template}
        mode: "auto"(LLM动态生成) | "feedback"(带反馈循环的默认) | "default"(线性) | "lite"(规划/开发/审核三节点)
        llm_client: LLM客户端(mode=auto时需要)

    Returns:
        阶段列表，每个阶段含 stage/role/output/deps/feedback_to/max_loops
    """
    if mode == "default":
        pipeline = [dict(s) for s in DEFAULT_PIPELINE]
    elif mode == "feedback":
        pipeline = [dict(s) for s in FEEDBACK_PIPELINE]
    elif mode == "lite":
        pipeline = [dict(s) for s in LITE_PIPELINE]
    elif mode == "auto" and llm_client:
        pipeline = _generate_pipeline_with_llm(task, members, llm_client)
    else:
        pipeline = [dict(s) for s in FEEDBACK_PIPELINE]

    # 过滤掉不存在成员的阶段
    available = [s for s in pipeline if s["role"] in members]

    # 验证依赖完整性
    stage_names = {s["stage"] for s in available}
    for stage in available:
        stage["deps"] = [d for d in stage.get("deps", []) if d in stage_names]

    return available


def _generate_pipeline_with_llm(task: str, members: dict, llm_client) -> list[dict]:
    """用 LLM 根据需求和可用成员动态生成流水线"""
    member_list = "\n".join(f"- {name}: {m.get('description', '')}" for name, m in members.items())

    system_prompt = "你是一个软件开发流程编排器。根据需求和可用的团队成员，设计最优的执行流水线。"
    user_prompt = f"""## 需求
{task}

## 可用团队成员
{member_list}

## 输出格式
返回 JSON 数组，每个元素是一个执行阶段：
```json
[
  {{
    "stage": "阶段标识（英文）",
    "role": "负责的团队成员角色名（必须与上面列表一致）",
    "output": "产出文件名或 null",
    "deps": ["依赖的阶段标识"],
    "feedback_to": "如果此阶段失败需要回退到哪个阶段（通常用于测试→开发）",
    "max_loops": 3
  }}
]
```

设计原则：
1. **用最少的节点完成任务**：简单开发任务只保留 implementation(代码工程师) 一个节点
2. 需要先设计/拆解时才加 planning；需要独立验证/审查时才加 review
3. 最多 planning → implementation → review 三个节点，不得更多
4. 如果是开发任务，review/testing 阶段应设置 feedback_to 指向开发阶段
5. 只使用列出的团队成员

只返回 JSON 数组。"""

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            logger.info("LLM 动态流水线生成需要在同步上下文，使用默认流水线")
            return FEEDBACK_PIPELINE

        resp = loop.run_until_complete(
            llm_client.chat([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
        )
        content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        pipeline = json.loads(content)
        logger.info(f"LLM 动态生成流水线: {len(pipeline)} 个阶段")
        return pipeline
    except Exception as e:
        logger.warning(f"LLM 动态流水线生成失败，使用默认: {e}")
        return FEEDBACK_PIPELINE


async def generate_pipeline_async(task: str, members: dict, llm_client) -> list[dict]:
    """异步版本的 LLM 动态流水线生成"""
    member_list = "\n".join(f"- {name}: {m.get('description', '')}" for name, m in members.items())

    system_prompt = "你是一个软件开发流程编排器。判断需求类型并设计执行流水线。"
    user_prompt = f"""## 需求
{task}

## 可用团队成员
{member_list}

## 输出格式
返回 JSON 数组，每个元素是一个执行阶段：
```json
[
  {{
    "stage": "阶段标识（英文）",
    "role": "负责的团队成员角色名（必须与上面列表一致）",
    "output": "产出文件名或 null",
    "deps": ["依赖的阶段标识"],
    "feedback_to": "如果此阶段失败需要回退到哪个阶段",
    "max_loops": 3,
    "max_iterations": 0
  }}
]
```

## 判断规则(核心: 用最少的节点)
- **简单对话/问答/问候** → 返回空数组 []，不需要流水线
- **简单开发/小改动/bug 修复** → **只保留 implementation(代码工程师) 一个节点**，不要加 planning/review
- **需要先设计或拆解的开发任务** → planning + implementation
- **需要独立验证/审查的重要改动** → planning + implementation + review
- **最多三个节点** planning → implementation → review，不得更多
- 只使用上面列出的角色；不要臆造角色
- 开发任务中 review/testing 阶段设置 feedback_to 指向 implementation
- **简单问题/快速分析**：`max_iterations` 设为 50（快速回答、小范围修改）
- **常规代码分析**：`max_iterations` 设为 100（需要理解代码逻辑的任务）
- 复杂任务不设 `max_iterations`（或设为 0），使用 agent 默认值

只返回 JSON 数组。"""

    try:
        resp = await llm_client.chat([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
        content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        pipeline = json.loads(content)
        logger.info(f"LLM 动态生成流水线: {len(pipeline)} 个阶段")
        for s in pipeline:
            s.setdefault("deps", [])
            s.setdefault("feedback_to", None)
            s.setdefault("max_loops", 3)
            s.setdefault("max_iterations", 0)
        return pipeline
    except Exception as e:
        logger.warning(f"LLM 动态流水线生成失败，使用默认: {e}")
        return FEEDBACK_PIPELINE
