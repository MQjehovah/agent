"""多模态用户消息构造：把文本里的图片引用 / 直接传入的图片转成 OpenAI 兼容 content 数组。

- 路径引用：消息里出现 `*.attachments/xxx.png` 或 `xxx.jpg` 等（desktop 附件即以此形式拼进文本），
  在 workspace 下解析成真实文件后 base64 内联。
- 直接传入：`data_urls`（data:image/*;base64,... 或 {name,data_url}）。
两者皆无 → 原样返回文本（保持纯文本语义）。

返回：str（纯文本）或 list（[{type:text},{type:image_url}...]），后者可被 LLM 客户端透传、
被会话以 JSON 序列化落库（见 storage._encode_content / _decode_content）。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

logger = logging.getLogger("agent.multimodal")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
# 匹配以图片扩展名结尾的路径片段（含 .attachments/xxx.png、绝对/相对路径、带引号或标点结尾）
_PATH_RE = re.compile(r"([^\s\"'<>()\[\]，。；、]+\.(?:png|jpe?g|webp|gif|bmp))", re.IGNORECASE)
_MAX_BYTES = 10 * 1024 * 1024  # 单图上限 10MB


def _mime_for(path_or_url: str) -> str:
    ext = os.path.splitext(path_or_url.split("?", 1)[0])[1].lower()
    return _MIME.get(ext, "image/png")


def _resolve(token: str, workspace: str) -> str | None:
    token = token.strip().strip("\"'")
    cands = [token]
    if not os.path.isabs(token) and workspace:
        cands.append(os.path.join(workspace, token))
        cands.append(os.path.join(workspace, ".attachments", os.path.basename(token)))
    for c in cands:
        try:
            if os.path.isfile(c):
                return c
        except OSError:
            continue
    return None


def _path_to_data_url(path: str) -> str | None:
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            logger.warning("图片超过上限，忽略内联: %s", path)
            return None
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{_mime_for(path)};base64,{b64}"
    except OSError as exc:
        logger.warning("读取图片失败，忽略内联: %s (%s)", path, exc)
        return None


def _norm_data_urls(data_urls: Any) -> list[str]:
    out: list[str] = []
    if not data_urls:
        return out
    items = data_urls if isinstance(data_urls, (list, tuple)) else [data_urls]
    for it in items:
        url = ""
        if isinstance(it, str):
            url = it
        elif isinstance(it, dict):
            url = str(it.get("data_url") or it.get("dataUrl") or it.get("url") or "")
        if url.startswith("data:image/"):
            out.append(url)
    return out


def build_user_content(
    text: str,
    workspace: str = "",
    data_urls: Any = None,
    max_images: int = 8,
) -> str | list[dict[str, Any]]:
    """构造用户消息 content：纯文本→str；含图→[{type:text},{type:image_url}...]。"""
    text = text or ""
    image_urls: list[str] = _norm_data_urls(data_urls)

    if isinstance(text, str) and text:
        seen: set[str] = set()
        for m in _PATH_RE.finditer(text):
            p = _resolve(m.group(1), workspace)
            if p and p not in seen:
                seen.add(p)
                url = _path_to_data_url(p)
                if url:
                    image_urls.append(url)

    if not image_urls:
        return text

    # 去重 + 截断
    dedup: list[str] = []
    for u in image_urls:
        if u not in dedup:
            dedup.append(u)
    dedup = dedup[:max_images]

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for u in dedup:
        parts.append({"type": "image_url", "image_url": {"url": u}})
    logger.info("用户消息内联图片 %d 张（workspace=%s）", len(dedup), workspace or "-")
    return parts
