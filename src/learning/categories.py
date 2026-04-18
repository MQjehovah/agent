import re
import logging

CORRECTION_KEYWORDS = [
    "不对", "错了", "不是这样", "重新", "搞错了",
    "搞反了", "不准确", "有问题", "弄错了", "记错了",
]

REFLECT_SKIP_TOOLS = {
    "file_operation", "grep", "glob", "todo", "memory",
    "shell", "web_search", "web_fetch", "ask_user",
}

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

MAX_SUMMARY_LENGTH = 6000
TOOL_RESULT_MAX = 300