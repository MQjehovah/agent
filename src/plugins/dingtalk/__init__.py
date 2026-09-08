import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.dingtalk")
logging.getLogger("dingtalk_stream").setLevel(logging.WARNING)

# 未开户/被禁用用户的拒绝提示(不创建会话、不路由)
NOT_PROVISIONED_REPLY = "您尚未开通 AI 数字员工，请在员工工作台完成企业登录，或联系管理员开通后使用"

# 群共享会话命中敏感工具时，群内只发这条固定占位(最终文本私聊送达提问者)。
GROUP_SENSITIVE_PRIVATE_NOTICE = "查询结果含敏感数据，已私聊发送给你，请查收。"


def format_dingtalk_session_id(agent_uid: str | int, rand: str) -> str:
    """Build a single-chat dingtalk session id: ``dingtalk:{agent_uid}:{rand}``.

    单聊根保持原命名(uid 为 rbac_users.id 数字); 群根见
    ``dingtalk_group_root_prefix``/``is_group_conversation_id``, 二者前缀互不混淆。
    """
    return f"dingtalk:{agent_uid}:{rand}"


def is_group_conversation_id(conversation_id: str, conversation_type: str = "") -> bool:
    """钉钉群聊判定。

    优先用官方 ``conversation_type``：'2'=群聊，'1'=单聊(该字段可靠，
    单聊与群聊的 conversationId 都可能以 ``cid`` 开头，前缀不可靠)。
    仅当 conversation_type 缺失时回退 conversation_id 前缀推断(旧版兼容)。
    """
    ct = str(conversation_type or "").strip()
    if ct in ("1", "2"):
        return ct == "2"
    return bool(conversation_id) and str(conversation_id).startswith("cid")


def sanitize_dingtalk_cid(conversation_id: str) -> str:
    """安全化 openConversationId：仅保留字母数字并截断，供拼进会话前缀(LIKE 可查)。"""
    return re.sub(r"[^A-Za-z0-9]", "", str(conversation_id or ""))[:24]


def dingtalk_group_root_prefix(conversation_id: str) -> str:
    """群根规范前缀 ``dingtalk_group:{safe}:{hash}``(不含 rand)。

    同 cid 恒映射同前缀：safe 为 cid 可识别子串，hash 为 sha256 前 8 位消歧；
    前缀仅含字母数字，可用 ``LIKE 'dingtalk_group:{prefix}:%'`` 定位该群全部根。
    """
    cid = str(conversation_id or "")
    digest = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:8]
    return f"dingtalk_group:{sanitize_dingtalk_cid(cid)}:{digest}"


def resolve_dingtalk_staff_id(storage, user_tag) -> str | None:
    """Resolve the DingTalk staff id used for push APIs from an agent-run tag.

    New routing user_id is ``dingtalk:{agent_user.id}`` (numeric agent uid);
    the staff id is looked up via rbac_user_identities. Legacy tags that carry
    a known staff id directly are still accepted for backward compatibility.
    """
    tag = str(user_tag or "")
    cand = tag.split(":", 1)[1] if tag.startswith("dingtalk:") else tag
    if not cand:
        return None
    with storage.get_connection() as conn:
        if cand.isdigit():
            row = conn.execute(
                "SELECT platform_uid FROM rbac_user_identities "
                "WHERE platform = 'dingtalk' AND user_id = ?",
                (int(cand),)
            ).fetchone()
            if row:
                return row[0]
        row = conn.execute(
            "SELECT 1 FROM rbac_user_identities "
            "WHERE platform = 'dingtalk' AND platform_uid = ?",
            (cand,)
        ).fetchone()
    return cand if row else None


@dataclass
class DingTalkStreamConfig:
    client_id: str = ""
    client_secret: str = ""


@dataclass
class DingTalkConfig:
    stream: DingTalkStreamConfig = field(default_factory=DingTalkStreamConfig)
    enabled: bool = True

    def load_from_dict(self, data: dict):
        stream_data = data.get("stream", {})
        self.stream = DingTalkStreamConfig(
            client_id=stream_data.get("client_id", ""),
            client_secret=stream_data.get("client_secret", ""),
        )
        if "enabled" in data:
            self.enabled = data["enabled"]


@dataclass
class DingTalkSession:
    session_id: str
    conversation_id: str
    sender_id: str
    sender_nick: str
    robot_code: str
    agent_uid: str = ""
    role: str = "default"
    _plugin: "DingTalkPlugin | None" = field(default=None, repr=False)

    async def send_to_agent(self, content: str) -> str:
        if not self._plugin or not self._plugin.plugin_manager:
            return "PluginManager未就绪"

        try:
            result = await self._plugin.plugin_manager.execute(self.session_id, content)
            return result
        except Exception as e:
            logger.error(f"Session {self.session_id} 执行失败: {e!r}")
            return f"处理失败: {e}"

    async def send_image(self, image_path: str) -> bool:
        if not self._plugin or not self._plugin._client:
            logger.warning("DingTalk client not initialized")
            return False

        try:
            import dingtalk_stream
            await self._plugin._client.media.upload(
                media_type=dingtalk_stream.MediaType.IMAGE,
                file_path=image_path,
                conversation_id=self.conversation_id
            )
            logger.info(f"已发送图片: {image_path}")
            return True
        except Exception as e:
            logger.error(f"发送图片失败: {e!r}")
            return False


class DingTalkPlugin(BasePlugin):
    name = "dingtalk"
    description = "钉钉机器人插件，使用Stream模式接收和发送消息"
    version = "2.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "plugins", "dingtalk.json"
            )

        self.config = DingTalkConfig()

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.config.load_from_dict(data)
            except Exception as e:
                logger.error(f"Failed to load dingtalk config: {e!r}")
        else:
            logger.warning(f"DingTalk config file not found: {config_file}")

        self.sessions: dict[str, DingTalkSession] = {}
        # scope -> session_id：单聊 scope=("s", agent_uid)；群聊 scope=("g", cid 规范前缀)。
        # 群共享根同群同一根(整群共享上下文)，群间按不同 cid 隔离。
        self._session_by_key: dict[tuple[str, str], str] = {}
        # conversation_id -> asyncio.Lock：同会话消息串行处理，避免快速连发并发跑同一 agent 会话
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self._client = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._token_cache: dict = {}
        self.enabled = self.config.enabled

    def start(self):
        if not self.config.enabled:
            logger.info("钉钉插件已禁用")
            return

        if not self.config.stream.client_id or not self.config.stream.client_secret:
            logger.warning("DingTalk client_id or client_secret not configured")
            return

        try:
            import dingtalk_stream  # noqa: F401 — availability check
        except ImportError as e:
            logger.error(f"dingtalk-stream is required. Install: pip install dingtalk-stream. Error: {e!r}")
            return

        self._running = True

        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_stream_client())
        except RuntimeError:
            logger.warning("No running event loop, will start in separate thread")
            import threading
            self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
            self._thread.start()

    def _run_in_thread(self):
        asyncio.run(self._run_stream_client())

    async def _run_stream_client(self):
        import dingtalk_stream

        credential = dingtalk_stream.Credential(
            self.config.stream.client_id,
            self.config.stream.client_secret
        )

        self._client = dingtalk_stream.DingTalkStreamClient(credential)

        handler = AgentChatbotHandler(self)
        self._client.register_callback_handler(
            dingtalk_stream.ChatbotMessage.TOPIC,
            handler
        )

        while self._running:
            try:
                await self._client.start()
            except asyncio.CancelledError:
                logger.warning("DingTalk Stream client cancelled")
                raise
            except Exception as e:
                logger.error(f"DingTalk Stream client error: {type(e).__name__}: {e!r}")
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
                else:
                    break

        logger.info("DingTalk Stream client stopped")

    def stop(self):
        logger.info("Stopping DingTalk plugin...")
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("DingTalk plugin stopped")

    def get_tool_defs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "send_message_to_dingtalk",
                    "description": "发送文本消息到用户的钉钉单聊（oToMessages）。适用于定时任务完成后主动推送结果给用户。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要发送的文本内容",
                            }
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_image_to_dingtalk",
                    "description": "发送本地图片到钉钉对话中。适用于需要展示图片给用户的场景，例如截图、图表等。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_path": {
                                "type": "string",
                                "description": "图片的本地文件路径，例如: /path/to/image.png 或 screenshot.png"
                            }
                        },
                        "required": ["image_path"]
                    }
                }
            }
        ]

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "send_message_to_dingtalk":
            local_user_id = args.pop("_local_user_id", None)
            return await self._send_text(args.get("text", ""), local_user_id)
        if name == "send_image_to_dingtalk":
            local_user_id = args.pop("_local_user_id", None)
            return await self._send_image(args.get("image_path", ""), local_user_id)
        return f"Tool {name} not implemented"

    def _get_dingtalk_staff_id(self, local_user_id) -> str | None:
        from storage.storage import get_storage
        storage = get_storage()
        if not storage or not local_user_id:
            return None
        return resolve_dingtalk_staff_id(storage, local_user_id)

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._token_cache.get("token") and now < self._token_cache.get("expires", 0):
            return self._token_cache["token"]
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={
                        "appKey": self.config.stream.client_id,
                        "appSecret": self.config.stream.client_secret,
                    }
                )
                data = r.json()
                token = data.get("accessToken", "")
                expire_in = data.get("expireIn", 7200)
                self._token_cache = {"token": token, "expires": now + expire_in - 300}
                return token
        except Exception as e:
            logger.error(f"获取access_token失败: {e}")
            return ""

    async def _upload_media(self, access_token: str, image_path: str) -> str:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                with open(image_path, "rb") as f:
                    r = await client.post(
                        "https://oapi.dingtalk.com/media/upload",
                        params={"access_token": access_token, "type": "image"},
                        files={"media": (os.path.basename(image_path), f)},
                    )
                data = r.json()
                return data.get("media_id", "")
        except Exception as e:
            logger.error(f"上传图片失败: {e}")
            return ""

    async def _send_image(self, image_path: str, local_user_id=None) -> str:
        if not image_path or not os.path.isfile(image_path):
            return f"图片文件不存在: {image_path}"

        staff_id = self._get_dingtalk_staff_id(local_user_id)
        if not staff_id:
            return "当前用户未绑定钉钉账号，无法发送图片"

        access_token = await self._get_access_token()
        if not access_token:
            return "获取钉钉access_token失败"

        media_id = await self._upload_media(access_token, image_path)
        if not media_id:
            return "上传图片到钉钉失败"

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": access_token},
                    json={
                        "robotCode": self.config.stream.client_id,
                        "userIds": [staff_id],
                        "msgKey": "sampleImage",
                        "msgParam": json.dumps({"sampleImageMediaId": media_id}),
                    }
                )
                if r.status_code == 200:
                    logger.info(f"图片已发送给用户 staff_id={staff_id}: {image_path}")
                    return f"图片已发送: {image_path}"
                return f"发送图片失败: {r.text}"
        except Exception as e:
            return f"发送图片失败: {e}"

    async def _send_text(self, text: str, local_user_id=None) -> str:
        if not text:
            return "内容为空"

        staff_id = self._get_dingtalk_staff_id(local_user_id)
        if not staff_id:
            return "当前用户未绑定钉钉账号，无法发送消息"

        access_token = await self._get_access_token()
        if not access_token:
            return "获取钉钉access_token失败"

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": access_token},
                    json={
                        "robotCode": self.config.stream.client_id,
                        "userIds": [staff_id],
                        "msgKey": "sampleText",
                        "msgParam": json.dumps({"content": text}),
                    }
                )
                if r.status_code == 200:
                    logger.info(f"消息已发送给用户 staff_id={staff_id}")
                    return f"消息已发送: {text[:60]}"
                return f"发送消息失败: {r.text}"
        except Exception as e:
            return f"发送消息失败: {e}"

    def _conv_lock(self, conv_id: str) -> asyncio.Lock:
        """返回某 conversation 的串行锁(进程内),避免同会话消息并发执行。"""
        lock = self._conv_locks.get(conv_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conv_id] = lock
        return lock

    def _get_storage(self):
        from storage.storage import get_storage
        return get_storage()

    @staticmethod
    def _make_session(session_id: str, conversation_id: str, agent_uid: str,
                      sender_nick: str, robot_code: str, role: str,
                      sender_id: str) -> "DingTalkSession":
        session = DingTalkSession(
            session_id=session_id,
            conversation_id=conversation_id,
            sender_id=sender_id,
            sender_nick=sender_nick,
            robot_code=robot_code,
            agent_uid=agent_uid,
            role=role,
        )
        return session

    def _register_session(self, key: tuple[str, str], session: DingTalkSession) -> None:
        session._plugin = self
        self.sessions[session.session_id] = session
        self._session_by_key[key] = session.session_id

    def _persist_scope_root(self, scope_kind: str, scope_key: str,
                            root_session_id: str) -> None:
        st = self._get_storage()
        if not st:
            return
        try:
            st.upsert_dingtalk_scope_root(scope_kind, scope_key, root_session_id)
        except Exception as e:
            logger.warning(f"登记钉钉 scope 根失败 {scope_kind}:{scope_key}: {e!r}")

    def _resolve_persisted_root(self, scope_kind: str, scope_key: str,
                                agent_uid: str) -> str:
        """从 DB 取 scope 当前活跃根(跨重启续根)。返回 '' 表示无。

        - 单聊: 先查 scope 指针(保证 /new 跨重启生效), 未命中再查该 uid 最近一条
          单聊根(session_id=conversation_id 且前缀 dingtalk:{uid}:, 兼容改造前数据)
        - 群聊: 仅靠 scope→根 持久指针(cid rand 不可推导), 并校验根归属该群前缀
        """
        st = self._get_storage()
        if not st:
            return ""
        try:
            if scope_kind == "g":
                root = st.get_dingtalk_scope_root("g", scope_key) or ""
                if root.startswith(scope_key + ":"):
                    return root
                return ""
            root = st.get_dingtalk_scope_root("s", scope_key) or ""
            if root.startswith(f"dingtalk:{agent_uid}:"):
                return root
            with st.get_connection() as conn:
                row = conn.execute(
                    "SELECT session_id FROM messages "
                    "WHERE user_id = ? AND session_id = conversation_id "
                    "  AND session_id LIKE ? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (f"dingtalk:{agent_uid}", f"dingtalk:{agent_uid}:%"),
                ).fetchone()
            return row["session_id"] if row else ""
        except Exception as e:
            logger.warning(f"查询钉钉持久化根失败 {scope_kind}:{scope_key}: {e!r}")
            return ""

    def resolve_session(
        self,
        conversation_id: str,
        agent_uid: str | int,
        sender_nick: str,
        robot_code: str,
        role: str = "default",
        sender_id: str = "",
        force_new: bool = False,
        conversation_type: str = "",
    ) -> DingTalkSession:
        """按 scope 解析/复用钉钉会话根(取代旧 get_session)。

        scope：单聊=("s", agent_uid)，群聊=("g", cid 规范前缀)。
        复用顺序：内存 ``_session_by_key`` → DB 持久根(指针；单聊另兜底最近单聊根)
        → 新建。force_new(=/new) 直接开新根：单聊换 rand、群换 rand 并覆写指针。
        群共享根整群一个根，同 cid 任意成员触发均路由同一 session_id；行级
        user_id 仍按触发人。会话命名(前缀即类型)：单 dingtalk:{uid}:{rand}，
        群 dingtalk_group:{safe}:{hash}:{rand}。
        """
        agent_uid = str(agent_uid)
        is_group = is_group_conversation_id(conversation_id, conversation_type)
        scope_kind = "g" if is_group else "s"
        scope_key = (dingtalk_group_root_prefix(conversation_id)
                     if is_group else agent_uid)
        key = (scope_kind, scope_key)

        if not force_new:
            sid = self._session_by_key.get(key)
            if sid:
                session = self.sessions.get(sid)
                if session:
                    session.conversation_id = conversation_id
                    session.role = role
                    session.agent_uid = agent_uid
                    return session
                # scope 在内存有指针但对象被清理(罕见)：按 session_id 重建
                rebuilt = self._make_session(
                    sid, conversation_id, agent_uid, sender_nick,
                    robot_code, role, sender_id)
                self._register_session(key, rebuilt)
                logger.debug(f"重建钉钉Session: {sid} by {sender_nick}")
                return rebuilt

        if not force_new:
            sid = self._resolve_persisted_root(scope_kind, scope_key, agent_uid)
            if sid:
                restored = self._make_session(
                    sid, conversation_id, agent_uid, sender_nick,
                    robot_code, role, sender_id)
                self._register_session(key, restored)
                logger.debug(f"恢复钉钉会话根: {sid} by {sender_nick}")
                return restored

        rand = uuid.uuid4().hex[:8]
        session_id = (f"{scope_key}:{rand}" if is_group
                      else format_dingtalk_session_id(agent_uid, rand))
        fresh = self._make_session(
            session_id, conversation_id, agent_uid, sender_nick,
            robot_code, role, sender_id)
        self._register_session(key, fresh)
        # 群根 rand 不可推导，须登记持久指针跨重启续根；单聊亦登记以保证 /new 跨重启生效
        self._persist_scope_root(scope_kind, scope_key, session_id)
        logger.debug(f"创建新钉钉Session: {session_id} by {sender_nick} (group={is_group})")
        return fresh


class AgentChatbotHandler:
    def __init__(self, plugin: DingTalkPlugin):
        self.plugin = plugin
        self.logger = logging.getLogger("plugin.dingtalk.handler")
        self.dingtalk_client = None
        self._handler = None

    def pre_start(self):
        import dingtalk_stream
        logging.getLogger("dingtalk_stream.client").setLevel(logging.CRITICAL)
        logging.getLogger('dingtalkchatbot').setLevel(logging.WARNING)
        self._handler = dingtalk_stream.ChatbotHandler()
        self._handler.pre_start()

    def reply_text(self, content: str, incoming_message, msgtype: str = "markdown"):
        if self._handler:
            if msgtype == "markdown":
                title = content.split('\n')[0][:50] if content else "回复"
                self._handler.reply_markdown(title, content, incoming_message)
                self.logger.info(f"已回复Markdown消息: {title}")
            else:
                self._handler.reply_text(content, incoming_message)
                self.logger.info(f"已回复文本消息: {content[:50]}...")

    def reply_image(self, image_path: str, incoming_message):
        if self._handler and self.plugin._client:
            import dingtalk_stream
            try:
                media = self.plugin._client.media.upload(
                    media_type=dingtalk_stream.MediaType.IMAGE,
                    file_path=image_path,
                    conversation_id=incoming_message.conversation_id
                )
                self._handler.reply_image(media.media_id, incoming_message)
                self.logger.info(f"已回复图片: {image_path}")
            except Exception as e:
                self.logger.error(f"回复图片失败: {e!r}")

    def reply_not_provisioned(self, incoming_message):
        """回复未开户提示并返回 ACK(不创建会话、不路由)。"""
        import dingtalk_stream

        self.reply_text(NOT_PROVISIONED_REPLY, incoming_message, msgtype="text")
        self.logger.info(f"已拒绝未开通用户: {getattr(incoming_message, 'sender_staff_id', '')}")
        return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

    async def process(self, callback):
        import dingtalk_stream

        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

            content = ""
            if hasattr(incoming_message, 'text') and incoming_message.text:
                content = incoming_message.text.content.strip()

            if not content:
                self.logger.debug("Empty message, skipping")
                return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

            sender_id = incoming_message.sender_id or ""
            sender_staff_id = getattr(incoming_message, 'sender_staff_id', "") or getattr(incoming_message, 'staff_id', "") or sender_id
            sender_nick = incoming_message.sender_nick or ""
            conversation_id = incoming_message.conversation_id or ""
            conversation_type = getattr(incoming_message, "conversation_type", "") or ""
            robot_code = incoming_message.robot_code or ""

            self.logger.info(f"钉钉插件收到消息: [{sender_nick}](staff_id={sender_staff_id}) {content}...")

            # 身份解析: rbac_user_identities 绑定表(staff_id → agent 用户/角色)。
            # 未开户(无绑定)/被禁用/解析异常 → 一律拒绝并 ACK，不创建会话、不路由，
            # 杜绝以 dingtalk:{staff_id} 或 default 角色静默放行。
            try:
                from security.rbac import RBACManager, UserNotProvisionedError
                from storage.storage import get_storage
                _st = get_storage()
                if not _st:
                    raise UserNotProvisionedError(
                        "dingtalk", sender_staff_id, "storage 未初始化")
                user_info = RBACManager(_st).require_user(
                    "dingtalk", sender_staff_id, fallback_name=sender_nick)
            except UserNotProvisionedError as e:
                self.logger.info(f"拒绝未开户钉钉用户: {e}")
                return self.reply_not_provisioned(incoming_message)
            except Exception as e:
                self.logger.error(f"钉钉身份解析异常,拒绝处理: {e!r}")
                return self.reply_not_provisioned(incoming_message)

            agent_uid = str(user_info.get("user_id") or "")
            role = user_info.get("role") or "default"
            user_name = user_info.get("user_name") or sender_nick

            # 单/群判定: 优先官方 conversation_type('2'=群,'1'=单), cid 前缀仅兜底。
            # 单聊与群聊的 conversationId 均可能以 cid 开头, 前缀不可靠(曾致私聊误判为群)。
            is_group = is_group_conversation_id(conversation_id, conversation_type)

            # /new: 忽略大小写/空格, force 开新根, 回复确认, 不进 agent 处理
            if content.strip().lower() == "/new":
                self.plugin.resolve_session(
                    conversation_id=conversation_id,
                    agent_uid=agent_uid,
                    sender_nick=user_name,
                    robot_code=robot_code,
                    role=role,
                    sender_id=sender_id,
                    force_new=True,
                    conversation_type=conversation_type,
                )
                self.logger.info(f"钉钉 /new 已开启新会话: group={is_group} by {sender_nick}")
                self.reply_text("已开启新会话", incoming_message)
                return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

            session = self.plugin.resolve_session(
                conversation_id=conversation_id,
                agent_uid=agent_uid,
                sender_nick=user_name,
                robot_code=robot_code,
                role=role,
                sender_id=sender_id,
                conversation_type=conversation_type,
            )

            # 路由身份用归属 tag dingtalk:{agent_user.id}(与 web:{uid} 对齐)，
            # 不再使用 dingtalk:{staff_id}; 群聊行级审计仍记当前触发人
            user_id = f"dingtalk:{agent_uid}"

            # 本轮是否命中敏感工具(见 agent.sensitive; 子代理调用链已汇聚到顶层结果)。
            sensitive_hit = False
            if not self.plugin.plugin_manager:
                response = "执行器未注册，请稍后再试"
            else:
                router = getattr(self.plugin.plugin_manager, "router", None)
                if router:
                    result = await router.route(
                        content, channel="dingtalk",
                        session_id=session.session_id,
                        user_id=user_id, user_name=user_name,
                        role=role,
                        # 群共享上下文信号: 记忆不注入个人私有, 群内串行/群间并发见 _conv_lock
                        group_context=is_group,
                        # 返回 AgentResult, 供下方按 sensitive_hit 改道敏感出口
                        return_result=True,
                    )
                    response = result.result if hasattr(result, "result") else str(result)
                    sensitive_hit = bool(getattr(result, "sensitive_hit", False))
                else:
                    # 兜底(router 缺失, 非线上路径): 仅能取文本, 无敏感标记可判定
                    response = await self.plugin.plugin_manager.execute(
                        session_id=session.session_id,
                        content=content,
                        user_id=user_id,
                        user_name=user_name
                    )

            # Phase2 敏感改道: 群共享会话(dingtalk_group:)中命中敏感工具的最终回复
            # → 不把文本发群, 私聊(单聊回执)送达提问者(触发人), 群内仅发固定占位。
            if is_group and sensitive_hit:
                if response:
                    try:
                        dm_status = await self.plugin._send_text(response, local_user_id=user_id)
                        self.logger.info(f"群敏感结果已私聊送达 {sender_nick} "
                                         f"(staff_id={sender_staff_id}): {dm_status}")
                    except Exception as e:
                        self.logger.error(f"群敏感结果私聊发送失败: {e!r}", exc_info=True)
                self.reply_text(GROUP_SENSITIVE_PRIVATE_NOTICE, incoming_message)
            else:
                self.reply_text(response, incoming_message)

            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

        except Exception as e:
            self.logger.error(f"处理消息失败: {e!r}")
            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

    async def raw_process(self, callback_message):
        import dingtalk_stream

        ack_message = dingtalk_stream.AckMessage()
        ack_message.code = dingtalk_stream.AckMessage.STATUS_OK
        ack_message.headers.message_id = callback_message.headers.message_id
        ack_message.headers.content_type = "application/json"
        ack_message.data = {"response": "OK"}

        asyncio.create_task(self._async_process(callback_message))

        return ack_message

    async def _async_process(self, callback_message):
        # 同会话串行:快速连发时同一 conversation 的消息排队处理,
        # 避免并发跑同一个 agent 会话导致上下文/worker 竞争报错。
        conv_id = ""
        try:
            data = getattr(callback_message, "data", None) or {}
            conv_id = str(data.get("conversationId", ""))
        except Exception:
            pass
        try:
            if conv_id:
                async with self.plugin._conv_lock(conv_id):
                    await self.process(callback_message)
            else:
                await self.process(callback_message)
        except Exception as e:
            self.logger.error(f"异步处理消息失败: {e!r}")


plugin = DingTalkPlugin
