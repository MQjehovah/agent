import re
import logging

logger = logging.getLogger("agent.learning")

CORRECTION_KEYWORDS = [
    "不对", "错了", "不是这样", "重新", "搞错了",
    "搞反了", "不准确", "有问题", "弄错了", "记错了",
]

REFLECT_SKIP_TOOLS = {
    "file_operation", "grep", "glob", "todo", "memory",
    "shell", "web_search", "web_fetch", "ask_user",
}

MAX_SUMMARY_LENGTH = 6000
TOOL_RESULT_MAX = 300

# --- 任务反思 prompt ---
REFLECT_PROMPT = (
    "分析以下任务执行过程，提取值得长期记住的经验。\n"
    "要求：\n"
    "1. 只提取真正有通用价值的知识点（方法、规律、避坑经验）\n"
    "2. 不要包含具体数据或一次性信息\n"
    "3. 每条经验用一句话概括\n"
    "4. 如果没有值得记住的经验，回复 SKIP\n\n"
    "格式:\n"
    "SAVE: <知识点>\n"
    "或\n"
    "SKIP\n\n"
    "任务: {task}\n"
    "执行摘要:\n{summary}"
)

REFLECT_SYSTEM_PROMPT = "你是经验提取助手。只有真正有通用价值的知识才保存。用 SAVE: 或 SKIP 回复。兼容中英文冒号。"

# --- 每日提取 prompt ---
DAILY_EXTRACT_PROMPT = (
    "请从以下 Agent [{agent_id}] 的对话片段中提取关键信息。\n"
    "要求：\n"
    "1. 只保留有价值的信息，过滤掉闲聊、重复、工具调用细节\n"
    "2. 每条信息用一句话概括\n"
    "3. 按以下分类输出（某分类无内容则省略）\n\n"
    "## 关键决策\n- ...\n\n"
    "## 用户偏好\n- ...\n\n"
    "## 重要事实\n- ...\n\n"
    "## 待办事项\n- ...\n\n"
    "对话片段：\n{chunk}\n\n"
    "只输出提取结果，不要额外说明。如果对话无有价值信息，输出「无关键信息」。"
)

DAILY_EXTRACT_SYSTEM_PROMPT = "你是记忆提取助手。输出简洁的结构化摘要。"

CHUNK_SIZE = 50000

# --- 模式检测 prompt ---
PATTERN_CLASSIFY_PROMPT = (
    "分析以下任务，判断它属于哪种任务模式。\n"
    "要求：\n"
    "1. 给出一个简短的英文标识符作为 pattern_key（小写+下划线，如 daily_report_generation）\n"
    "2. 判断该模式适合创建为「skill」（技能）还是「subagent」（子代理）\n"
    "   - skill: 简单的重复性工作流、格式化输出、模板化任务\n"
    "   - subagent: 需要独立上下文、专业工具、多步骤深度操作的任务\n"
    "3. 给出建议的中文名称（2-6个字）和一句描述\n\n"
    "严格按以下JSON格式输出，不要输出其他内容：\n"
    '{{"pattern_key": "xxx", "category": "skill", "suggested_name": "名称", "description": "描述"}}\n\n'
    "任务: {task}\n"
    "执行摘要:\n{summary}"
)

PATTERN_CLASSIFY_SYSTEM_PROMPT = (
    "你是任务模式分析助手。你的工作是将任务归类到一个模式标识符，并判断适合创建为技能还是子代理。"
    "只输出JSON，不要输出其他内容。"
)

# --- 自动创建 prompt ---
AUTO_CREATE_SKILL_PROMPT = (
    "根据以下任务模式信息，生成一个完整的技能模板（SKILL.md 内容）。\n\n"
    "技能名称: {name}\n"
    "描述: {description}\n"
    "历史任务示例:\n{examples}\n\n"
    "要求：\n"
    "1. 生成完整的 SKILL.md 内容，包含 YAML frontmatter 和 Markdown 正文\n"
    "2. frontmatter 包含: name, description, version(1.0.0), author(自学习系统)\n"
    "3. 正文包含:\n"
    "   - 概述: 这个技能做什么\n"
    "   - 使用场景: 什么情况下使用\n"
    "   - 工作流: 详细的执行步骤\n"
    "   - 输出格式: 期望的输出格式\n"
    "4. 使用 {{user_input}} 作为用户输入的占位符\n"
    "5. 根据历史任务示例推断合适的工作流程\n\n"
    "直接输出 SKILL.md 的完整内容，不要额外说明。"
)

AUTO_CREATE_SUBAGENT_PROMPT = (
    "根据以下任务模式信息，生成一个完整的子代理模板（PROMPT.md 内容）。\n\n"
    "子代理名称: {name}\n"
    "描述: {description}\n"
    "历史任务示例:\n{examples}\n\n"
    "要求：\n"
    "1. 生成完整的 PROMPT.md 内容，包含 YAML frontmatter 和 Markdown 正文\n"
    "2. frontmatter 包含: name, description\n"
    "3. 正文包含:\n"
    "   - 角色定义: 这个子代理是谁，擅长什么\n"
    "   - 核心原则: 工作时遵循的原则\n"
    "   - 工作流程: 接收任务后的标准处理流程\n"
    "   - 工具使用: 推荐使用哪些工具\n"
    "   - 输出规范: 结果交付的格式要求\n"
    "   - 限制与边界: 不应该做什么\n"
    "4. 根据历史任务示例推断合适的专业领域和工作方式\n\n"
    "直接输出 PROMPT.md 的完整内容，不要额外说明。"
)

AUTO_CREATE_SYSTEM_PROMPT = "你是AI代理系统模板生成专家。你根据任务模式信息生成高质量的、可直接使用的配置文件。"

# --- 模式检测配置 ---
PATTERN_TRIGGER_THRESHOLD = 3
PATTERN_MAX_EXAMPLES = 5
PATTERN_FILE = "task_patterns.json"
CREATION_LOG_FILE = "creation_log.jsonl"