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