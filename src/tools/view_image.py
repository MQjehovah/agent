"""查看图片工具：把本地图片交给视觉模型，返回文字描述/可见文字。

用途：消息历史里图片以引用存在（不重复内联像素），需要"重看"时由 agent 主动调用本工具，
按需把图片交给当前模型（视觉）并取回文字结论——既避免历史膨胀，又保留看图能力。

ref 支持附件引用（``store:<sha>.<ext>``）或工作区内文件路径（如 ``.attachments/xxx.png``）。
"""

import contextlib
import json

from . import BuiltinTool


class ViewImageTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "view_image"

    @property
    def description(self) -> str:
        return (
            "查看一张图片的内容：把图片交给视觉模型，返回文字描述与可见文字(OCR)。"
            "ref 为附件引用(store:...)或工作区文件路径(如 .attachments/xxx.png)。"
            "当历史消息里出现「[图片：…（如需重看可调用 view_image）]」时用它重看原图。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "图片引用或文件路径"},
                "question": {"type": "string", "description": "针对图片的问题，默认描述内容并列出文字"},
            },
            "required": ["ref"],
        }

    async def execute(self, ref: str = "", question: str = "", **kwargs) -> str:
        ref = (ref or "").strip()
        if not ref:
            return json.dumps({"ok": False, "error": "缺少 ref"}, ensure_ascii=False)

        from agent import attachments
        from agent.core import current_agent

        agent = current_agent()
        if agent is None:
            return json.dumps({"ok": False, "error": "无法获取当前 agent 上下文"}, ensure_ascii=False)
        workspace = getattr(agent, "workspace", "") or ""
        data_url = attachments.to_data_url(ref, workspace)
        if not data_url:
            return json.dumps({"ok": False, "error": f"图片不存在或无法读取：{ref}"}, ensure_ascii=False)

        prompt = (question or "详细描述这张图片的内容，并逐条列出可见文字。").strip()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        # 工具在线程池的临时事件循环里执行：必须自建一次性客户端，
        # 复用 agent.client 会把它绑到临时循环、关闭后主循环再用即 "Event loop is closed"。
        client = getattr(agent, "client", None)
        if client is None:
            return json.dumps({"ok": False, "error": "当前 agent 无 LLM 客户端"}, ensure_ascii=False)
        from openai import AsyncOpenAI

        vc = AsyncOpenAI(base_url=client.base_url, api_key=client.api_key, max_retries=0)
        try:
            resp = await vc.chat.completions.create(
                model=client.model, messages=messages, max_tokens=800,
            )
            text = (resp.choices[0].message.content or "").strip()
            return json.dumps({"ok": True, "description": text}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"视觉调用失败: {exc}"}, ensure_ascii=False)
        finally:
            with contextlib.suppress(Exception):
                await vc.close()
