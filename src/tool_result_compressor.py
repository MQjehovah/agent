"""
工具结果智能压缩器

替代 _execute_tool_safe 中的粗暴截断（result[:3000]），
按工具类型提取关键信息，在有限预算内保留最有价值的内容。
"""
import json
import logging
import os

logger = logging.getLogger("agent.tools.compressor")

MAX_OUTPUT_CHARS = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", 4000))

# 这些工具的结果需要跨轮次完整保留，不压缩
_KEEP_FULL = {"skill", "execute_skill", "ask_user", "todo_write", "todowrite"}


def compress_tool_result(tool_name: str, result: str, budget: int = None) -> str:
    """根据工具类型智能压缩结果。

    Args:
        tool_name: 工具名称
        result: 原始结果字符串（通常是 JSON）
        budget: 最大字符数，默认 MAX_OUTPUT_CHARS

    Returns:
        压缩后的结果字符串
    """
    budget = budget or MAX_OUTPUT_CHARS

    if not result or len(result) <= budget:
        return result

    if tool_name in _KEEP_FULL:
        return result

    try:
        data = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return _head_tail(result, budget)

    compressor = _COMPRESSORS.get(tool_name)
    if compressor:
        try:
            compressed = compressor(data, budget)
            if compressed and len(compressed) <= budget:
                return compressed
        except Exception as e:
            logger.debug(f"工具 {tool_name} 智能压缩失败，回退到通用截断: {e}")

    return _compress_generic_json(data, budget, tool_name)


# ── 通用工具 ──────────────────────────────────────────────

def _head_tail(text: str, budget: int) -> str:
    """保留头部 + 尾部，中间省略。比纯头部截断保留更多结构信息。"""
    if len(text) <= budget:
        return text
    head = int(budget * 0.65)
    tail = budget - head - 60
    return (
        f"{text[:head]}\n"
        f"... [省略 {len(text) - head - tail} 字符] ...\n"
        f"{text[-tail:]}"
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _compress_generic_json(data: dict, budget: int, tool_name: str) -> str:
    """通用 JSON 压缩：保留 success 标志 + 关键字段，截断长内容字段。"""
    if not isinstance(data, dict):
        return _head_tail(json.dumps(data, ensure_ascii=False), budget)

    keep_keys = {"success", "status", "error", "path", "count", "total_matches",
                 "total_lines", "showing", "has_more", "return_code", "agent_id",
                 "query", "pattern"}
    content_keys = {"content", "stdout", "stderr", "matches", "results", "files", "result"}

    parts = []
    for k, v in data.items():
        if k in keep_keys:
            parts.append(f'"{k}": {json.dumps(v, ensure_ascii=False)}')
        elif k in content_keys:
            if isinstance(v, str):
                parts.append(f'"{k}": "{_truncate(v, budget // 3)}"')
            elif isinstance(v, list):
                parts.append(f'"{k}": "[{len(v)} 项，已压缩]"')
            else:
                parts.append(f'"{k}": "..."')

    body = ", ".join(parts)
    compressed = "{" + body + "}"
    if len(compressed) > budget:
        return _head_tail(json.dumps(data, ensure_ascii=False), budget)
    return compressed


# ── 按工具类型的专用压缩器 ─────────────────────────────────

def _compress_subagent(data: dict, budget: int) -> str:
    """子代理结果：提取状态 + 结果摘要。"""
    status = data.get("status", "unknown")
    success = data.get("success", False)
    result_text = data.get("result", "")

    header = json.dumps({
        "success": success,
        "status": status,
        "agent_id": data.get("agent_id", ""),
    }, ensure_ascii=False)

    # 给结果文本分配大部分预算
    result_budget = budget - len(header) - 100
    if len(result_text) > result_budget:
        result_text = _head_tail(result_text, result_budget)

    return f'{header}\n\n## 子代理输出\n{result_text}'


def _compress_knowledge_search(data: dict, budget: int) -> str:
    """知识库搜索：保留 Top 3 结果，每条截断内容。"""
    results = data.get("results", [])
    count = data.get("count", len(results))
    query = data.get("query", "")

    top_n = min(3, len(results))
    per_item_budget = (budget - 200) // max(top_n, 1)

    items = []
    for i, r in enumerate(results[:top_n]):
        title = r.get("title", "")
        content = _truncate(r.get("content", ""), per_item_budget)
        score = r.get("score", 0)
        items.append(f"### {i+1}. {title} (score: {score:.2f})\n{content}")

    body = "\n\n".join(items)
    if count > top_n:
        body += f"\n\n(共 {count} 条结果，已展示前 {top_n} 条)"

    return json.dumps({
        "success": True,
        "query": query,
        "count": count,
        "showing": top_n,
        "results": body,
    }, ensure_ascii=False)


def _compress_file_read(data: dict, budget: int) -> str:
    """文件读取：保留头尾行 + 行数统计。"""
    content = data.get("content", "")
    total_lines = data.get("total_lines", 0)
    path = data.get("path", "")

    if len(content) <= budget - 200:
        return json.dumps(data, ensure_ascii=False)

    lines = content.split("\n")
    head_lines = int((budget - 300) * 0.6 // 20)  # 预估每行 ~20 字符
    tail_lines = head_lines // 2

    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:]) if tail_lines > 0 else ""

    compressed_content = f"{head}\n... [省略 {len(lines) - head_lines - tail_lines} 行] ...\n{tail}"

    return json.dumps({
        "success": True,
        "path": path,
        "total_lines": total_lines,
        "showing": f"{head_lines}头+{tail_lines}尾",
        "has_more": True,
        "content": compressed_content,
    }, ensure_ascii=False)


def _compress_grep(data: dict, budget: int) -> str:
    """grep 结果：保留前 N 条匹配 + 统计。"""
    matches = data.get("matches", [])
    total = data.get("total_matches", len(matches))
    pattern = data.get("pattern", "")
    truncated = data.get("truncated", False)

    # 每条匹配保留 file + line + content（去掉 context）
    per_match = 150
    max_matches = (budget - 200) // per_match
    max_matches = min(max_matches, len(matches))

    compact = []
    for m in matches[:max_matches]:
        file = m.get("file", "")
        line = m.get("line", "")
        content = _truncate(m.get("content", ""), 100)
        compact.append(f"{file}:{line}: {content}")

    body = "\n".join(compact)
    if total > max_matches:
        body += f"\n(共 {total} 处匹配，已展示前 {max_matches} 条)"

    return json.dumps({
        "success": True,
        "pattern": pattern,
        "total_matches": total,
        "showing": max_matches,
        "truncated": truncated or total > max_matches,
        "matches": body,
    }, ensure_ascii=False)


def _compress_shell(data: dict, budget: int) -> str:
    """shell 结果：保留 exit code + stdout 摘要 + stderr。"""
    rc = data.get("return_code", -1)
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")

    stdout_budget = int(budget * 0.7)
    stderr_budget = int(budget * 0.2)

    if len(stdout) > stdout_budget:
        stdout = _head_tail(stdout, stdout_budget)
    if len(stderr) > stderr_budget:
        stderr = _truncate(stderr, stderr_budget)

    return json.dumps({
        "success": data.get("success", rc == 0),
        "return_code": rc,
        "stdout": stdout,
        "stderr": stderr,
    }, ensure_ascii=False)


def _compress_web_search(data: dict, budget: int) -> str:
    """web_search 结果：保留 Top 5。"""
    results = data.get("results", [])
    query = data.get("query", "")
    count = data.get("count", len(results))

    top_n = min(5, len(results))
    per_item = (budget - 100) // max(top_n, 1)

    items = []
    for r in results[:top_n]:
        title = _truncate(r.get("title", ""), 80)
        snippet = _truncate(r.get("snippet", ""), per_item - 80)
        items.append(f"- {title}: {snippet}")

    return json.dumps({
        "success": True,
        "query": query,
        "count": count,
        "showing": top_n,
        "results": "\n".join(items),
    }, ensure_ascii=False)


def _compress_web_fetch(data: dict, budget: int) -> str:
    """web_fetch 结果：截断正文。"""
    url = data.get("url", "")
    content = data.get("content", "")
    content_length = data.get("content_length", len(content))

    if len(content) > budget - 200:
        content = _head_tail(content, budget - 200)

    return json.dumps({
        "success": data.get("success", True),
        "url": url,
        "content_length": content_length,
        "content": content,
    }, ensure_ascii=False)


_COMPRESSORS = {
    "subagent": _compress_subagent,
    "knowledge_search": _compress_knowledge_search,
    "file": _compress_file_read,
    "grep": _compress_grep,
    "shell": _compress_shell,
    "web_search": _compress_web_search,
    "web_fetch": _compress_web_fetch,
}
