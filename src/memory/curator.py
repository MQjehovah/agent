import logging
from datetime import datetime, timedelta

logger = logging.getLogger("agent.memory.curator")

CURATE_SYSTEM_PROMPT = "你是知识提炼助手。只提取通用、可公开的知识，排除个人偏好和敏感数据。"
CURATE_PROMPT = """\
从以下用户私有记忆中，提取【通用知识类】条目（排除：个人偏好、敏感数据、特定客户/设备明细）。
对每个候选，重写为去语境的通用表述。

用户记忆：
{chunk}

输出格式（每行一条，无可提取的则只输出 NONE）：
GENERIC: <通用表述> | REASON: <判为通用的理由>
"""


class MemoryCurator:
    """定时学习所有 user 私有记忆，提炼通用知识生成审批申请。"""

    def __init__(self, storage, llm_client=None):
        self._storage = storage
        self._llm = llm_client
        self._last_run = None  # ISO 时间戳，增量取材

    async def curate_once(self) -> int:
        """执行一次提炼，返回生成的申请数"""
        if not self._llm:
            logger.info("[curator] 无 LLM，跳过")
            return 0
        since = self._last_run or (datetime.now() - timedelta(days=1)).isoformat()
        rows = self._fetch_recent_user_memories(since)
        if not rows:
            logger.info("[curator] 无新增 user 记忆")
            return 0

        text = "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)
        existing = self._existing_global_set()
        created = 0
        try:
            resp = await self._llm.chat(
                messages=[
                    {"role": "system", "content": CURATE_SYSTEM_PROMPT},
                    {"role": "user", "content": CURATE_PROMPT.format(chunk=text[:4000])},
                ],
                tools=None, stream=False, use_cache=False,
            )
            out = (resp.choices[0].message.content or "").strip()
            if not out or out == "NONE":
                return 0
            source_users = list({r["owner_id"] for r in rows if r.get("owner_id")})
            for line in out.splitlines():
                if not line.startswith("GENERIC:"):
                    continue
                body = line.split("|", 1)[0][len("GENERIC:"):].strip()
                reason = ""
                if "REASON:" in line:
                    reason = line.split("REASON:", 1)[1].strip()
                if body and body not in existing:
                    self._storage.save_proposal(
                        content=body,
                        source_users=str(source_users[:10]),
                        reason=reason,
                    )
                    existing.add(body)
                    created += 1
        except Exception as e:
            logger.warning(f"[curator] 提炼失败: {e}")
        self._last_run = datetime.now().isoformat()
        logger.info(f"[curator] 生成 {created} 条申请")
        return created

    def _fetch_recent_user_memories(self, since_iso: str):
        """增量取材：updated_at > since 的 user 私有记忆"""
        with self._storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT owner_id, category, content FROM memories "
                "WHERE scope='user' AND updated_at > ? ORDER BY id LIMIT 200",
                (since_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _existing_global_set(self):
        """现有 global 记忆内容集合，用于去重"""
        with self._storage.get_connection() as conn:
            rows = conn.execute("SELECT content FROM memories WHERE scope='global'").fetchall()
        return {r["content"] for r in rows}
