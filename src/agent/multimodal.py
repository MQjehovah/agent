"""多模态用户消息：构造图片**引用**（不内联字节），按需物化给视觉模型。

设计（对齐行业做法）：
- 消息里只存引用：`{"type":"image_ref","ref":"store:<sha>.<ext>|<路径>","name":...}`（轻量、可落库不膨胀）。
- 仅在**调用模型的那一刻**把「当前最后一轮用户消息」的引用物化成 data URL（见 materialize_messages）；
  历史轮次的图片降级为文本占位（需要时由 `view_image` 工具按需重看），避免每轮重发像素。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from agent import attachments

logger = logging.getLogger("agent.multimodal")

_PATH_RE = re.compile(r"([^\s\"'<>()\[\]，。；、]+\.(?:png|jpe?g|webp|gif|bmp|svg))", re.IGNORECASE)
_MAX_IMAGES = 8


def _norm_refs(refs: Any, workspace: str = "") -> list[dict]:
    out: list[dict] = []
    if not refs:
        return out
    for it in (refs if isinstance(refs, (list, tuple)) else [refs]):
        if isinstance(it, dict):
            ref = str(it.get("ref") or it.get("path") or "").strip()
            if ref:
                out.append({"ref": ref, "name": str(it.get("name") or os.path.basename(ref))})
        elif isinstance(it, str) and it.strip():
            s = it.strip()
            if s.startswith("data:image/"):
                # 兼容直接传 data URL：入库转引用
                try:
                    import base64

                    raw = base64.b64decode(s.split(",", 1)[1])
                    meta = attachments.save_bytes(raw, "pasted.png", workspace)
                    out.append({"ref": meta["ref"], "name": meta["name"]})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("data URL 图片入库失败: %s", exc)
            else:
                out.append({"ref": s, "name": os.path.basename(s)})
    return out


def build_user_content(text: str, workspace: str = "", data_urls: Any = None, refs: Any = None):
    """构造用户消息 content：无图→str；有图→[{type:text},{type:image_ref}...]。

    - refs：前端已上传后的引用（{ref,name}）；
    - data_urls：兼容旧格式（data:image/...），就地存入附件库转成引用；
    - 文本里的图片路径：转成路径引用（desktop 附件即此形式）。
    """
    text = text or ""
    image_refs: list[dict] = _norm_refs(refs, workspace) + _norm_refs(data_urls, workspace)

    if isinstance(text, str) and text:
        seen = {r["ref"] for r in image_refs}
        for m in _PATH_RE.finditer(text):
            tok = m.group(1).strip()
            if tok not in seen:
                seen.add(tok)
                image_refs.append({"ref": tok, "name": os.path.basename(tok)})

    if not image_refs:
        return text

    dedup: list[dict] = []
    seen2: set[str] = set()
    for r in image_refs:
        if r["ref"] not in seen2:
            seen2.add(r["ref"])
            dedup.append(r)
    dedup = dedup[:_MAX_IMAGES]

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for r in dedup:
        parts.append({"type": "image_ref", "ref": r["ref"], "name": r["name"]})
    logger.info("用户消息附加图片引用 %d 个", len(dedup))
    return parts


def _last_image_user_index(messages: list[dict]) -> int:
    """最后一条「含 image_ref 的用户消息」下标（重试追加上来的纯文本 user 不顶掉图片）。"""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        c = messages[i].get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_ref" for p in c):
            return i
    return -1


def materialize_messages(messages: list[dict], workspace: str = "") -> list[dict]:
    """把引用物化成模型可用的 content（仅最后一条含图用户消息内联图片；其余降级为占位文本）。

    返回新列表（不改动会话/落库内容）；无 image_ref 时原样返回。
    """
    if not any(isinstance(m.get("content"), list) for m in messages):
        return messages
    last_idx = _last_image_user_index(messages)
    out: list[dict] = []
    changed = False
    for i, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        new_parts = []
        for p in c:
            if isinstance(p, dict) and p.get("type") == "image_ref":
                name = p.get("name") or ""
                if i == last_idx:
                    url = attachments.to_data_url(str(p.get("ref", "")), workspace)
                    if url:
                        new_parts.append({"type": "image_url", "image_url": {"url": url}})
                    else:
                        new_parts.append({"type": "text", "text": f"[图片无法加载：{name}]"})
                else:
                    new_parts.append({"type": "text", "text": f"[图片：{name}（如需重看可调用 view_image）]"})
                changed = True
            else:
                new_parts.append(p)
        out.append({**m, "content": new_parts})
    return out if changed else messages
