"""记忆相关性搜索 — 根据当前任务筛选相关记忆"""

import re
import logging

logger = logging.getLogger("agent.memory.relevance")


async def find_relevant_memories(
    query: str,
    memory_manager,
    llm_client=None,
    max_results: int = 5,
) -> list[str]:
    keyword_results = _keyword_search(query, memory_manager, max_results)

    shared_results = _search_shared_knowledge(query, memory_manager, max_results=2)
    keyword_results.extend(r for r in shared_results if r not in keyword_results)

    if llm_client and len(keyword_results) < max_results:
        try:
            semantic_results = await _semantic_search(
                query, memory_manager, llm_client,
                max_results - len(keyword_results),
                exclude_indices=set()
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

    # 提取查询中的关键词
    keywords = set(
        query.replace("的", " ").replace("了", " ")
        .replace("在", " ").replace("是", " ")
        .replace("和", " ").replace("与", " ")
        .replace("及", " ").replace("把", " ")
        .split()
    )
    keywords = {k for k in keywords if len(k) >= 2}

    if not keywords:
        # 无关键词时返回前 max_results 段
        paragraphs = [p for p in all_memory.split("\n\n") if p.strip()]
        return paragraphs[:max_results]

    paragraphs = [p for p in all_memory.split("\n\n") if p.strip()]
    scored = []
    for p in paragraphs:
        score = sum(1 for k in keywords if k in p)
        if score > 0:
            scored.append((score, p))

    if not scored:
        return paragraphs[:max_results]

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:max_results]]


async def _semantic_search(
    query: str,
    memory_manager,
    llm_client,
    max_results: int,
    exclude_indices: set,
) -> list[str]:
    """LLM 语义匹配：用轻量 LLM 调用筛选相关记忆"""
    all_memory = memory_manager.load_memory("")
    if not all_memory:
        return []

    paragraphs = [p for p in all_memory.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    numbered = "\n".join(f"[{i}] {p[:150]}" for i, p in enumerate(paragraphs[:50]))
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

    indices = [int(x) for x in re.findall(r'\d+', text) if int(x) < len(paragraphs)]
    return [paragraphs[i] for i in indices[:max_results]]


def _search_shared_knowledge(query: str, memory_manager, max_results: int = 2) -> list[str]:
    shared = memory_manager.load_shared_knowledge()
    if not shared:
        return []

    keywords = set(
        query.replace("的", " ").replace("了", " ")
        .replace("在", " ").replace("是", " ")
        .replace("和", " ").replace("与", " ")
        .replace("及", " ").replace("把", " ")
        .split()
    )
    keywords = {k for k in keywords if len(k) >= 2}
    if not keywords:
        return []

    lines = [l for l in shared.split("\n") if l.strip().startswith("-")]
    scored = []
    for line in lines:
        score = sum(1 for k in keywords if k in line)
        if score > 0:
            scored.append((score, line))

    scored.sort(key=lambda x: -x[0])
    return [line for _, line in scored[:max_results]]
