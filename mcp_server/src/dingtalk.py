import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse

import requests
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.dingtalk")

mcp = MCPServer("DingTalk MCP Server")
_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_SAFE_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
# 破坏性写(不可逆): 审批同意/拒绝等会推动/终结他人流程的操作
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

APP_KEY = os.getenv("DINGTALK_APP_KEY", "")
APP_SECRET = os.getenv("DINGTALK_APP_SECRET", "")
AGENT_ID = os.getenv("DINGTALK_AGENT_ID", "")
ROBOT_CODE = os.getenv("DINGTALK_ROBOT_CODE", APP_KEY)

_access_token_cache = {"token": "", "expire_at": 0}
# v2 起同步 handler 运行在 anyio worker 线程: 并发首调需串行化, 保证只取一次 token
_token_lock = threading.Lock()


def _get_access_token() -> str:
    with _token_lock:
        if _access_token_cache["token"] and time.time() < _access_token_cache["expire_at"]:
            return _access_token_cache["token"]

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        resp = requests.post(url, json={
            "appKey": APP_KEY,
            "appSecret": APP_SECRET
        }, timeout=10)
        data = resp.json()

        if "accessToken" not in data:
            raise RuntimeError(f"获取access_token失败: {data}")

        _access_token_cache["token"] = data["accessToken"]
        _access_token_cache["expire_at"] = time.time() + data.get("expireIn", 7200) - 300
        logger.info("access_token 已刷新")
        return _access_token_cache["token"]


def _check_config() -> str:
    if not APP_KEY or not APP_SECRET:
        return "错误: 未配置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET，请在 MCP 环境变量中设置"
    return ""


_API_BASE = "https://api.dingtalk.com"
# 单次响应返回上限(超长截断为 truncated 标记 + 预览, 防止撑爆模型上下文)
_MAX_RESPONSE_CHARS = 6000


def _api(method: str, path: str, *, params: dict | None = None,
         json_body: dict | None = None, timeout: int = 10) -> dict:
    """调用钉钉新版 OpenAPI(v2 网关), 返回解析后的 JSON body。

    统一附带 x-acs-dingtalk-access-token; 非 2xx 抛 RuntimeError, 由各工具统一
    转成 {"success": False, "error": ...}。测试经 monkeypatch 替换本函数注入假响应。
    """
    token = _get_access_token()
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
    resp = requests.request(
        method, f"{_API_BASE}{path}", headers=headers,
        params=params, json=json_body, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else {}


def _clip(value, limit: int = 300) -> str:
    text = str(value or "")
    return text[:limit] + "…" if len(text) > limit else text


def _dump(obj: dict) -> str:
    """序列化响应; 超长时返回 truncated 标记 + 预览(保持 JSON 可解析)。"""
    text = json.dumps(obj, ensure_ascii=False)
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return json.dumps({
        "success": obj.get("success", True),
        "truncated": True,
        "preview": text[: _MAX_RESPONSE_CHARS - 200],
    }, ensure_ascii=False)


def _split_ids(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_access_token():
    """获取钉钉应用的 access_token。调用任何钉钉 API 前需要先获取 token。"""
    logger.info("获取 access_token")
    err = _check_config()
    if err:
        return err
    try:
        token = _get_access_token()
        return json.dumps({"success": True, "access_token": token}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"获取access_token失败: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_work_notification(
    user_ids: str,
    msg_type: str,
    msg_content: str,
    agent_id: str = ""
):
    """发送工作通知消息给指定用户（企业内部应用）。

    参数:
    - user_ids: 接收人用户ID列表，逗号分隔，例如 "user1,user2"
    - msg_type: 消息类型，支持 text / markdown / oa / action_card
    - msg_content: 消息内容JSON字符串。
        text类型: {"content":"消息内容"}
        markdown类型: {"title":"标题","text":"# Markdown内容"}
        oa类型: {"head":{"text":"标题"},"body":{"title":"正文标题","content":"正文内容"}}
    - agent_id: 应用agentId，默认使用环境变量 DINGTALK_AGENT_ID
    """
    logger.info(f"发送工作通知: type={msg_type}, users={user_ids}")

    err = _check_config()
    if err:
        return err

    aid = agent_id or AGENT_ID
    if not aid:
        return "错误: 未配置 DINGTALK_AGENT_ID，请设置或传入 agent_id 参数"

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?access_token={token}"

        try:
            content_obj = json.loads(msg_content) if isinstance(msg_content, str) else msg_content
        except json.JSONDecodeError:
            content_obj = {"content": msg_content}

        payload = {
            "agent_id": int(aid),
            "userid_list": user_ids,
            "msg": {
                "msgtype": msg_type,
                msg_type: content_obj
            }
        }

        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            logger.info(f"工作通知发送成功: task_id={result.get('task_id')}")
            return json.dumps({
                "success": True,
                "task_id": result.get("task_id"),
                "message": f"工作通知已发送给 {user_ids}"
            }, ensure_ascii=False)
        else:
            logger.error(f"工作通知发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误"), "code": result.get("errcode")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"发送工作通知异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_robot_single_message(
    user_ids: list[str],
    msg_key: str,
    msg_param: str,
    robot_code: str = ""
):
    """通过机器人发送单聊消息给指定用户。

    参数:
    - user_ids: 接收人userId列表
    - msg_key: 消息类型，如 sampleText / sampleMarkdown / sampleImageMsg / sampleRichText
    - msg_param: 消息参数JSON字符串
        sampleText: {"content":"消息内容"}
        sampleMarkdown: {"title":"标题","text":"Markdown内容"}
        sampleRichText: {"richMessageParamList":[{"type":1,"textContent":"文本"}]}
    - robot_code: 机器人编码，默认使用 DINGTALK_ROBOT_CODE
    """
    logger.info(f"机器人单聊消息: type={msg_key}, users={user_ids}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}

        try:
            param_obj = json.loads(msg_param) if isinstance(msg_param, str) else msg_param
        except json.JSONDecodeError:
            param_obj = {"content": msg_param}

        payload = {
            "robotCode": robot_code or ROBOT_CODE,
            "userIds": user_ids,
            "msgKey": msg_key,
            "msgParam": json.dumps(param_obj, ensure_ascii=False)
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()

        if resp.status_code == 200 and "body" not in result.get("code", ""):
            logger.info("机器人单聊消息发送成功")
            return json.dumps({"success": True, "message": f"消息已发送给 {len(user_ids)} 个用户"}, ensure_ascii=False)
        else:
            logger.error(f"机器人单聊消息发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("message", str(result))}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"机器人单聊消息异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_robot_group_message(
    conversation_id: str,
    msg_key: str,
    msg_param: str,
    robot_code: str = "",
    at_user_ids: list[str] | None = None,
    at_all: bool = False
):
    """通过机器人发送群聊消息。

    参数:
    - conversation_id: 群会话ID，例如 "cidXXXXXX"
    - msg_key: 消息类型，如 sampleText / sampleMarkdown / sampleActionCard / sampleInteractiveCard
    - msg_param: 消息参数JSON字符串
    - robot_code: 机器人编码，默认使用 DINGTALK_ROBOT_CODE
    - at_user_ids: @的用户ID列表（可选）
    - at_all: 是否@所有人（默认否）
    """
    logger.info(f"机器人群聊消息: conversation={conversation_id}, type={msg_key}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}

        try:
            param_obj = json.loads(msg_param) if isinstance(msg_param, str) else msg_param
        except json.JSONDecodeError:
            param_obj = {"content": msg_param}

        payload = {
            "robotCode": robot_code or ROBOT_CODE,
            "conversationId": conversation_id,
            "msgKey": msg_key,
            "msgParam": json.dumps(param_obj, ensure_ascii=False),
            "atUserIds": at_user_ids or [],
            "isAtAll": at_all
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()

        if resp.status_code == 200:
            logger.info("机器人群聊消息发送成功")
            return json.dumps({"success": True, "message": "群消息已发送", "process_query_key": result.get("processQueryKey", "")}, ensure_ascii=False)
        else:
            logger.error(f"机器人群聊消息发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("message", str(result))}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"机器人群聊消息异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_webhook_message(
    webhook_url: str,
    msg_type: str,
    content: str,
    at_all: bool = False,
    at_mobiles: list[str] | None = None,
    secret: str = ""
):
    """通过自定义机器人Webhook发送群消息。

    参数:
    - webhook_url: Webhook地址，例如 "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    - msg_type: 消息类型，支持 text / markdown / actionCard / feedCard
    - content: 消息内容。
        text类型: 纯文本字符串
        markdown类型: Markdown格式字符串（标题取第一行的 # 标题）
        actionCard类型: JSON字符串 {"title":"标题","text":"内容","btnOrientation":"0","singleTitle":"按钮标题","singleURL":"链接"}
    - at_all: 是否@所有人（默认否）
    - at_mobiles: @指定手机号列表（可选）
    - secret: 机器人的加签密钥（如果配置了加签安全设置）
    """
    logger.info(f"Webhook消息: type={msg_type}")

    try:
        url = webhook_url
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}timestamp={timestamp}&sign={sign}"

        at_body = {"atAll": at_all}
        if at_mobiles:
            at_body["atMobiles"] = at_mobiles

        if msg_type == "text":
            payload = {
                "msgtype": "text",
                "text": {"content": content},
                "at": at_body
            }
        elif msg_type == "markdown":
            title = ""
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("#"):
                    title = line.lstrip("# ").strip()
                    break
            if not title:
                title = content[:50]
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": content},
                "at": at_body
            }
        elif msg_type == "actionCard":
            try:
                card_data = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                card_data = {"title": content, "text": content}
            payload = {
                "msgtype": "actionCard",
                "actionCard": card_data
            }
        else:
            try:
                extra_data = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                extra_data = {"content": content}
            payload = {
                "msgtype": msg_type,
                msg_type: extra_data,
                "at": at_body
            }

        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            logger.info("Webhook消息发送成功")
            return json.dumps({"success": True, "message": "Webhook消息已发送"}, ensure_ascii=False)
        else:
            logger.error(f"Webhook消息发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Webhook消息异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_department_list(
    dept_id: int = 1,
    fetch_child: bool = False,
    language: str = "zh_CN"
):
    """获取子部门列表。

    参数:
    - dept_id: 父部门ID，根部门传 1
    - fetch_child: 是否递归获取所有子部门
    - language: 语言，默认 zh_CN
    """
    logger.info(f"获取部门列表: parent={dept_id}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/v2/department/listsub?access_token={token}"

        payload = {"dept_id": dept_id, "language": language}
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            departments = result.get("result", [])
            dept_list = []
            for dept in departments:
                dept_list.append({
                    "dept_id": dept.get("dept_id"),
                    "name": dept.get("name"),
                    "parent_id": dept.get("parent_id"),
                    "create_dept_group": dept.get("create_dept_group"),
                    "auto_add_user": dept.get("auto_add_user")
                })

            if fetch_child and dept_list:
                all_depts = list(dept_list)
                for d in dept_list:
                    child_result = dingtalk_get_department_list(d["dept_id"], True, language)
                    try:
                        child_data = json.loads(child_result)
                        if child_data.get("success"):
                            all_depts.extend(child_data.get("departments", []))
                    except (json.JSONDecodeError, TypeError):
                        pass
                dept_list = all_depts

            return json.dumps({
                "success": True,
                "departments": dept_list,
                "count": len(dept_list)
            }, ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"获取部门列表异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_department_users(
    dept_id: int,
    cursor: int = 0,
    size: int = 100,
    language: str = "zh_CN"
):
    """获取部门用户详情列表。

    参数:
    - dept_id: 部门ID
    - cursor: 分页游标，首页传 0
    - size: 分页大小，最大100
    - language: 语言，默认 zh_CN
    """
    logger.info(f"获取部门用户: dept={dept_id}, cursor={cursor}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/v2/user/list?access_token={token}"

        payload = {
            "dept_id": dept_id,
            "cursor": cursor,
            "size": min(size, 100),
            "language": language
        }
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            data = result.get("result", {})
            users = []
            for u in data.get("list", []):
                users.append({
                    "userid": u.get("userid"),
                    "name": u.get("name"),
                    "mobile": u.get("mobile"),
                    "title": u.get("title"),
                    "dept_id_list": u.get("dept_id_list")
                })

            return json.dumps({
                "success": True,
                "users": users,
                "count": len(users),
                "has_more": data.get("has_more", False),
                "next_cursor": data.get("next_cursor", 0)
            }, ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"获取部门用户异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_user_detail(userid: str, language: str = "zh_CN"):
    """获取用户详情。

    参数:
    - userid: 用户ID
    - language: 语言，默认 zh_CN
    """
    logger.info(f"获取用户详情: {userid}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}"

        payload = {"userid": userid, "language": language}
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            user = result.get("result", {})
            return json.dumps({
                "success": True,
                "user": {
                    "userid": user.get("userid"),
                    "name": user.get("name"),
                    "mobile": user.get("mobile"),
                    "email": user.get("email"),
                    "title": user.get("title"),
                    "dept_id_list": user.get("dept_id_list"),
                    "avatar": user.get("avatar"),
                    "hired_date": user.get("hired_date"),
                    "job_number": user.get("job_number"),
                    "org_email": user.get("org_email"),
                    "state_code": user.get("state_code")
                }
            }, ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"获取用户详情异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_user_by_mobile(mobile: str):
    """根据手机号获取用户ID。

    参数:
    - mobile: 手机号码
    """
    logger.info(f"根据手机号获取用户: {mobile}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/v2/user/getbymobile?access_token={token}"

        payload = {"mobile": mobile}
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            user = result.get("result", {})
            return json.dumps({
                "success": True,
                "userid": user.get("userid")
            }, ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"根据手机号获取用户异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_interactive_card(
    conversation_id: str,
    card_template_id: str,
    card_data: str,
    out_track_id: str = "",
    robot_code: str = "",
    at_user_ids: list[str] | None = None,
    at_all: bool = False
):
    """发送互动卡片消息到群聊。

    参数:
    - conversation_id: 群会话ID
    - card_template_id: 卡片模板ID
    - card_data: 卡片数据JSON字符串，格式: {"cardParamMap":{"key1":"val1"},"cardMediaIdMap":{}}
    - out_track_id: 跟踪ID（可选）
    - robot_code: 机器人编码
    - at_user_ids: @的用户ID列表（可选）
    - at_all: 是否@所有人
    """
    logger.info(f"发送互动卡片: conversation={conversation_id}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = "https://oapi.dingtalk.com/v1.0/card/instances"
        headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}

        try:
            data_obj = json.loads(card_data) if isinstance(card_data, str) else card_data
        except json.JSONDecodeError:
            data_obj = {"cardParamMap": {"content": card_data}}

        payload = {
            "cardTemplateId": card_template_id,
            "conversationId": conversation_id,
            "robotCode": robot_code or ROBOT_CODE,
            "cardData": data_obj,
            "outTrackId": out_track_id or f"mcp_card_{int(time.time())}",
            "atUserIds": at_user_ids or [],
            "isAtAll": at_all
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)

        if resp.status_code == 200:
            logger.info("互动卡片发送成功")
            return json.dumps({"success": True, "message": "互动卡片已发送"}, ensure_ascii=False)
        else:
            result = resp.json()
            logger.error(f"互动卡片发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("message", str(result))}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"互动卡片发送异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_markdown_single(
    user_ids: list[str],
    title: str,
    text: str,
    robot_code: str = ""
):
    """快捷发送Markdown单聊消息给指定用户（封装好的便捷方法）。

    参数:
    - user_ids: 接收人userId列表
    - title: 消息标题
    - text: Markdown格式正文
    - robot_code: 机器人编码
    """
    msg_param = json.dumps({"title": title, "text": text}, ensure_ascii=False)
    return dingtalk_send_robot_single_message(user_ids, "sampleMarkdown", msg_param, robot_code)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_markdown_group(
    conversation_id: str,
    title: str,
    text: str,
    robot_code: str = "",
    at_user_ids: list[str] | None = None,
    at_all: bool = False
):
    """快捷发送Markdown群聊消息（封装好的便捷方法）。

    参数:
    - conversation_id: 群会话ID
    - title: 消息标题
    - text: Markdown格式正文
    - robot_code: 机器人编码
    - at_user_ids: @的用户ID列表
    - at_all: 是否@所有人
    """
    msg_param = json.dumps({"title": title, "text": text}, ensure_ascii=False)
    return dingtalk_send_robot_group_message(conversation_id, "sampleMarkdown", msg_param, robot_code, at_user_ids, at_all)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_text_single(
    user_ids: list[str],
    content: str,
    robot_code: str = ""
):
    """快捷发送文本单聊消息给指定用户（封装好的便捷方法）。

    参数:
    - user_ids: 接收人userId列表
    - content: 文本消息内容
    - robot_code: 机器人编码
    """
    msg_param = json.dumps({"content": content}, ensure_ascii=False)
    return dingtalk_send_robot_single_message(user_ids, "sampleText", msg_param, robot_code)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_text_group(
    conversation_id: str,
    content: str,
    robot_code: str = "",
    at_user_ids: list[str] | None = None,
    at_all: bool = False
):
    """快捷发送文本群聊消息（封装好的便捷方法）。

    参数:
    - conversation_id: 群会话ID
    - content: 文本消息内容
    - robot_code: 机器人编码
    - at_user_ids: @的用户ID列表
    - at_all: 是否@所有人
    """
    msg_param = json.dumps({"content": content}, ensure_ascii=False)
    return dingtalk_send_robot_group_message(conversation_id, "sampleText", msg_param, robot_code, at_user_ids, at_all)


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_get_conversation(conversation_id: str):
    """获取群会话信息。

    参数:
    - conversation_id: 群会话ID
    """
    logger.info(f"获取群会话信息: {conversation_id}")

    err = _check_config()
    if err:
        return err

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/im/chat/get?access_token={token}"
        payload = {"chatId": conversation_id}
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            chat_info = result.get("result", {})
            return json.dumps({
                "success": True,
                "chat": {
                    "chat_id": chat_info.get("chat_id"),
                    "name": chat_info.get("name"),
                    "owner_userid": chat_info.get("owner_userid"),
                    "member_count": chat_info.get("member_count"),
                    "notice": chat_info.get("notice"),
                    "admin_ids": chat_info.get("admin_ids")
                }
            }, ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"获取群会话信息异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_approval_start(
    process_code: str,
    originator_user_id: str,
    form_values: str = "[]",
    dept_id: int = 0,
    approvers: str = "",
    cc_list: str = "",
):
    """发起审批实例(创建审批单)。

    参数:
    - process_code: 审批模板 processCode(审批后台模板唯一标识, 形如 PROC-xxxx)
    - originator_user_id: 发起人 userId(必填)
    - form_values: 表单值 JSON 字符串, 形如 [{"name":"报销金额","value":"100"}];
        name 需与模板控件名一致
    - dept_id: 发起人部门 ID(可选)
    - approvers: 指定审批人 JSON 字符串, 形如
        [{"approverUserId":"user1","actionType":"AND"}](可选)
    - cc_list: 抄送人 userId 列表, 逗号分隔(可选)
    """
    logger.info(f"发起审批: process_code={process_code}, originator={originator_user_id}")

    err = _check_config()
    if err:
        return err

    if not str(process_code or "").strip():
        return _dump({"success": False, "error": "process_code 必填"})
    if not str(originator_user_id or "").strip():
        return _dump({"success": False, "error": "originator_user_id 必填"})

    try:
        values = json.loads(form_values) if isinstance(form_values, str) else form_values
    except (json.JSONDecodeError, TypeError):
        return _dump({"success": False, "error": "form_values 需为 JSON 数组字符串"})
    if not isinstance(values, list) or any(
            not isinstance(item, dict) or not item.get("name") for item in values):
        return _dump({"success": False, "error": "form_values 需为 [{\"name\":...,\"value\":...}] JSON 数组"})

    body: dict = {
        "processCode": str(process_code).strip(),
        "originatorUserId": str(originator_user_id).strip(),
        "formComponentValues": values,
    }
    if dept_id:
        body["deptId"] = int(dept_id)
    if approvers:
        try:
            body["approvers"] = json.loads(approvers)
        except (json.JSONDecodeError, TypeError):
            return _dump({"success": False, "error": "approvers 需为 JSON 数组字符串"})
    cc_ids = _split_ids(cc_list)
    if cc_ids:
        body["ccList"] = cc_ids

    try:
        result = _api("POST", "/v1.0/workflow/processInstances", json_body=body)
        return _dump({"success": True, "process_instance_id": result.get("instanceId", "")})
    except Exception as e:
        logger.error(f"发起审批异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_approval_instance(process_instance_id: str):
    """查询审批实例详情(标题/状态/结果/发起人/表单/任务节点)。

    参数:
    - process_instance_id: 审批实例 ID(发起审批或待办列表返回)
    """
    logger.info(f"查询审批实例: {process_instance_id}")

    err = _check_config()
    if err:
        return err

    if not str(process_instance_id or "").strip():
        return _dump({"success": False, "error": "process_instance_id 必填"})

    try:
        resp = _api("GET", "/v1.0/workflow/processInstances",
                    params={"processInstanceId": str(process_instance_id).strip()})
        data = resp.get("result") if isinstance(resp.get("result"), dict) else resp
        tasks = []
        for task in data.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tasks.append({
                "task_id": task.get("taskId"),
                "activity_id": task.get("activityId"),
                "status": task.get("status"),
                "result": task.get("result"),
            })
        forms = []
        for form in data.get("formComponentValues") or []:
            if not isinstance(form, dict):
                continue
            forms.append({"name": form.get("name"), "value": _clip(form.get("value"), 500)})
        return _dump({"success": True, "instance": {
            "process_instance_id": process_instance_id,
            "title": data.get("title"),
            "status": data.get("status"),
            "result": data.get("result"),
            "originator_user_id": data.get("originatorUserId"),
            "originator_dept_name": data.get("originatorDeptName"),
            "create_time": data.get("createTime"),
            "finish_time": data.get("finishTime"),
            "tasks": tasks,
            "form_values": forms,
        }})
    except Exception as e:
        logger.error(f"查询审批实例异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_approval_tasks(
    user_id: str,
    status: int = 0,
    max_results: int = 20,
    next_token: int = 0,
):
    """查询某用户的审批待办/已办任务列表。

    参数:
    - user_id: 用户 userId(必填)
    - status: 任务状态, 0=待办(默认), 1=已办
    - max_results: 单页条数(1~100, 默认 20)
    - next_token: 分页游标, 首页传 0
    """
    logger.info(f"查询审批任务: user={user_id}, status={status}")

    err = _check_config()
    if err:
        return err

    if not str(user_id or "").strip():
        return _dump({"success": False, "error": "user_id 必填"})

    if isinstance(status, bool) or status not in (0, 1):
        return _dump({"success": False, "error": "status 仅支持 0(待办)/1(已办)"})
    size = max(1, min(int(max_results or 20), 100))
    token = int(next_token or 0)

    try:
        resp = _api("GET", "/v1.0/workflow/workRecords/todoTasks",
                    params={"userId": str(user_id).strip(), "status": status,
                            "maxResults": size, "nextToken": token})
        data = resp.get("result") if isinstance(resp.get("result"), dict) else resp
        tasks = []
        for item in data.get("list") or []:
            if not isinstance(item, dict):
                continue
            forms = []
            for form in item.get("forms") or []:
                if not isinstance(form, dict):
                    continue
                forms.append({"title": form.get("title"), "content": _clip(form.get("content"), 300)})
            tasks.append({
                "task_id": item.get("taskId"),
                "instance_id": item.get("instanceId"),
                "title": item.get("title"),
                "url": item.get("url"),
                "forms": forms,
            })
        return _dump({"success": True, "tasks": tasks,
                      "next_token": data.get("nextToken", 0)})
    except Exception as e:
        logger.error(f"查询审批任务异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_approval_action(
    task_id: int,
    result: str,
    remark: str = "",
    process_instance_id: str = "",
    actioner_user_id: str = "",
):
    """同意或拒绝审批任务(不可逆, 会推动/终结他人审批流程)。

    参数:
    - task_id: 审批任务 ID(taskId, 来自实例详情或待办列表)
    - result: 操作结果, agree=同意 / refuse=拒绝
    - remark: 审批意见(可选)
    - process_instance_id: 审批实例 ID(OpenAPI 必填, 建议随 task 一并传入)
    - actioner_user_id: 操作人 userId(服务端代操作时必填; 缺省由钉钉按应用身份处理)
    """
    logger.info(f"审批操作: task_id={task_id}, result={result}")

    err = _check_config()
    if err:
        return err

    if task_id is None or isinstance(task_id, bool):
        return _dump({"success": False, "error": "task_id 必填且为整数"})
    action = str(result or "").strip().lower()
    action_aliases = {"agree": "agree", "同意": "agree", "refuse": "refuse", "reject": "refuse", "拒绝": "refuse"}
    if action not in action_aliases:
        return _dump({"success": False, "error": "result 仅支持 agree(同意)/refuse(拒绝)"})

    body: dict = {"taskId": int(task_id), "result": action_aliases[action]}
    if remark:
        body["remark"] = str(remark)
    if process_instance_id:
        body["processInstanceId"] = str(process_instance_id).strip()
    if actioner_user_id:
        body["actionerUserId"] = str(actioner_user_id).strip()

    try:
        resp = _api("POST", "/v1.0/workflow/processInstances/execute", json_body=body)
        ok = bool(resp.get("success", True)) and resp.get("result", True) is not False
        if not ok:
            return _dump({"success": False, "error": resp.get("message", "审批操作未成功")})
        return _dump({"success": True, "message": "审批操作已提交",
                      "task_id": int(task_id), "result": action_aliases[action]})
    except Exception as e:
        logger.error(f"审批操作异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_todo_create(
    union_id: str,
    subject: str,
    executor_ids: str,
    creator_id: str = "",
    description: str = "",
    due_time_ms: int = 0,
    priority: int = 0,
    detail_url: str = "",
    source_id: str = "",
):
    """创建待办任务。

    参数:
    - union_id: 待办归属用户 unionId(必填, 钉钉待办接口以用户维度鉴权)
    - subject: 待办标题(必填)
    - executor_ids: 执行人 userId 列表, 逗号分隔(必填)
    - creator_id: 创建人 unionId(可选; 缺省为 union_id 对应用户)
    - description: 待办描述(可选)
    - due_time_ms: 截止时间毫秒时间戳(可选, 0=不设置)
    - priority: 优先级数值(可选, 0=不设置)
    - detail_url: 详情跳转链接(可选)
    - source_id: 业务来源 ID(可选; 同 source_id 幂等, 便于重复创建去重)
    """
    logger.info(f"创建待办: union_id={union_id}, subject={subject}")

    err = _check_config()
    if err:
        return err

    if not str(union_id or "").strip():
        return _dump({"success": False, "error": "union_id 必填"})
    if not str(subject or "").strip():
        return _dump({"success": False, "error": "subject 必填"})
    executors = _split_ids(executor_ids)
    if not executors:
        return _dump({"success": False, "error": "executor_ids 必填(逗号分隔 userId)"})

    body: dict = {"subject": str(subject).strip(), "executorIds": executors}
    if creator_id:
        body["creatorId"] = str(creator_id).strip()
    if description:
        body["description"] = str(description)
    if due_time_ms:
        body["dueTime"] = int(due_time_ms)
    if priority:
        body["priority"] = int(priority)
    if detail_url:
        body["detailUrl"] = {"pcUrl": str(detail_url), "appUrl": str(detail_url)}
    if source_id:
        body["sourceId"] = str(source_id)

    try:
        result = _api("POST", f"/v1.0/todo/users/{union_id}/tasks", json_body=body)
        return _dump({"success": True, "task_id": result.get("id", "")})
    except Exception as e:
        logger.error(f"创建待办异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_todo_update(
    union_id: str,
    task_id: str,
    done: bool | None = None,
    subject: str = "",
    description: str = "",
    due_time_ms: int = 0,
    executor_ids: str = "",
):
    """更新待办任务(状态/描述/标题/截止时间/执行人)。

    参数:
    - union_id: 待办归属用户 unionId(必填)
    - task_id: 待办任务 ID(必填)
    - done: 是否完成 true=完成 false=恢复未完成(可选)
    - subject: 新标题(可选)
    - description: 新描述(可选)
    - due_time_ms: 新截止时间毫秒时间戳(可选, 0=不变)
    - executor_ids: 新执行人 userId 列表, 逗号分隔(可选)
    """
    logger.info(f"更新待办: union_id={union_id}, task_id={task_id}")

    err = _check_config()
    if err:
        return err

    if not str(union_id or "").strip():
        return _dump({"success": False, "error": "union_id 必填"})
    if not str(task_id or "").strip():
        return _dump({"success": False, "error": "task_id 必填"})

    body: dict = {}
    if done is not None:
        body["done"] = bool(done)
    if subject:
        body["subject"] = str(subject)
    if description:
        body["description"] = str(description)
    if due_time_ms:
        body["dueTime"] = int(due_time_ms)
    executors = _split_ids(executor_ids)
    if executors:
        body["executorIds"] = executors
    if not body:
        return _dump({"success": False, "error": "至少提供一项待更新字段(done/subject/description/due_time_ms/executor_ids)"})

    try:
        result = _api("PUT", f"/v1.0/todo/users/{union_id}/tasks/{task_id}", json_body=body)
        ok = result.get("result", True) is not False
        return _dump({"success": bool(ok), "task_id": str(task_id),
                      "error": "" if ok else "更新未成功"})
    except Exception as e:
        logger.error(f"更新待办异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_todo_list(
    union_id: str,
    is_done: bool | None = None,
    next_token: str = "",
):
    """查询某用户的待办任务列表(按完成状态过滤, 游标分页)。

    参数:
    - union_id: 待办归属用户 unionId(必填)
    - is_done: 完成状态过滤 true=已完成 / false=未完成(可选, 缺省不过滤)
    - next_token: 分页游标(上次返回的 next_token, 首页留空)
    """
    logger.info(f"查询待办列表: union_id={union_id}, is_done={is_done}")

    err = _check_config()
    if err:
        return err

    if not str(union_id or "").strip():
        return _dump({"success": False, "error": "union_id 必填"})

    body: dict = {}
    if is_done is not None:
        body["isDone"] = bool(is_done)
    if next_token:
        body["nextToken"] = str(next_token)

    try:
        result = _api("POST", f"/v1.0/todo/users/{union_id}/tasks/list", json_body=body)
        todos = []
        for card in result.get("todoCards") or []:
            if not isinstance(card, dict):
                continue
            todos.append({
                "task_id": card.get("taskId"),
                "subject": card.get("subject"),
                "is_done": card.get("isDone"),
                "todo_status": card.get("todoStatus"),
                "priority": card.get("priority"),
                "due_time": card.get("dueTime"),
                "creator_id": card.get("creatorId"),
                "created_time": card.get("createdTime"),
                "modified_time": card.get("modifiedTime"),
            })
        return _dump({"success": True, "todos": todos,
                      "next_token": result.get("nextToken", ""),
                      "total_count": result.get("totalCount")})
    except Exception as e:
        logger.error(f"查询待办列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_calendar_create_event(
    user_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: str = "",
    is_all_day: bool = False,
    time_zone: str = "Asia/Shanghai",
):
    """创建日程(会议)。

    参数:
    - user_id: 日程归属用户 unionId(必填)
    - summary: 日程标题(必填)
    - start_time: 开始时间, ISO8601 形如 2026-09-24T10:00:00+08:00;
        全天日程传日期 2026-09-24(必填)
    - end_time: 结束时间, 格式同 start_time(必填)
    - calendar_id: 日历 ID, 主日历为 primary(默认)
    - description: 日程描述(可选)
    - location: 地点/会议室名称(可选)
    - attendees: 参与人 userId 列表, 逗号分隔(可选)
    - is_all_day: 是否全天日程(默认否)
    - time_zone: 时区(默认 Asia/Shanghai)
    """
    logger.info(f"创建日程: user={user_id}, summary={summary}")

    err = _check_config()
    if err:
        return err

    if not str(user_id or "").strip():
        return _dump({"success": False, "error": "user_id 必填"})
    if not str(summary or "").strip():
        return _dump({"success": False, "error": "summary 必填"})
    if not str(start_time or "").strip() or not str(end_time or "").strip():
        return _dump({"success": False, "error": "start_time/end_time 必填"})

    def _moment(value: str) -> dict:
        value = str(value).strip()
        if is_all_day:
            return {"date": value[:10], "timeZone": time_zone}
        return {"dateTime": value, "timeZone": time_zone}

    body: dict = {"summary": str(summary).strip(),
                  "start": _moment(start_time), "end": _moment(end_time),
                  "isAllDay": bool(is_all_day)}
    if description:
        body["description"] = str(description)
    if location:
        body["location"] = {"displayName": str(location)}
    attendee_ids = _split_ids(attendees)
    if attendee_ids:
        body["attendees"] = [{"id": uid} for uid in attendee_ids]

    try:
        result = _api("POST", f"/v1.0/calendar/users/{user_id}/calendars/{calendar_id}/events",
                      json_body=body)
        return _dump({"success": True, "event_id": result.get("id", "")})
    except Exception as e:
        logger.error(f"创建日程异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_calendar_list_events(
    user_id: str,
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 20,
    next_token: str = "",
):
    """查询日程列表(时间范围内)。

    参数:
    - user_id: 日程归属用户 unionId(必填)
    - calendar_id: 日历 ID, 主日历为 primary(默认)
    - time_min: 起始时间(UTC, yyyy-MM-ddTHH:mmZ, 可选)
    - time_max: 结束时间(UTC, yyyy-MM-ddTHH:mmZ, 可选)
    - max_results: 单页条数(1~100, 默认 20)
    - next_token: 分页游标(可选)
    """
    logger.info(f"查询日程列表: user={user_id}, calendar={calendar_id}")

    err = _check_config()
    if err:
        return err

    if not str(user_id or "").strip():
        return _dump({"success": False, "error": "user_id 必填"})

    params: dict = {"maxResults": max(1, min(int(max_results or 20), 100))}
    if time_min:
        params["timeMin"] = str(time_min)
    if time_max:
        params["timeMax"] = str(time_max)
    if next_token:
        params["nextToken"] = str(next_token)

    try:
        result = _api("GET", f"/v1.0/calendar/users/{user_id}/calendars/{calendar_id}/events",
                      params=params)
        events = []
        for event in result.get("events") or []:
            if not isinstance(event, dict):
                continue
            events.append({
                "event_id": event.get("id"),
                "summary": event.get("summary"),
                "status": event.get("status"),
                "start": event.get("start"),
                "end": event.get("end"),
                "location": event.get("location"),
            })
        return _dump({"success": True, "events": events,
                      "next_token": result.get("nextToken", "")})
    except Exception as e:
        logger.error(f"查询日程列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_calendar_freebusy(
    user_id: str,
    user_ids: str,
    start_time: str,
    end_time: str,
):
    """查询用户忙闲(会议时间段)。

    参数:
    - user_id: 操作者 unionId(必填)
    - user_ids: 被查询用户 unionId 列表, 逗号分隔(必填)
    - start_time: 起始时间(UTC, yyyy-MM-ddTHH:mmZ, 必填)
    - end_time: 结束时间(UTC, yyyy-MM-ddTHH:mmZ, 必填)
    """
    logger.info(f"查询忙闲: user={user_id}, targets={user_ids}")

    err = _check_config()
    if err:
        return err

    if not str(user_id or "").strip():
        return _dump({"success": False, "error": "user_id 必填"})
    targets = _split_ids(user_ids)
    if not targets:
        return _dump({"success": False, "error": "user_ids 必填(逗号分隔 unionId)"})
    if not str(start_time or "").strip() or not str(end_time or "").strip():
        return _dump({"success": False, "error": "start_time/end_time 必填"})

    try:
        result = _api("POST", f"/v1.0/calendar/users/{user_id}/querySchedule",
                      json_body={"userIds": targets,
                                 "startTime": str(start_time).strip(),
                                 "endTime": str(end_time).strip()})
        schedule = []
        for info in result.get("scheduleInformation") or []:
            if not isinstance(info, dict):
                continue
            items = []
            for item in info.get("scheduleItems") or []:
                if not isinstance(item, dict):
                    continue
                items.append({"start": item.get("start"), "end": item.get("end"),
                              "status": item.get("status")})
            schedule.append({"user_id": info.get("userId"), "error": info.get("error"),
                             "items": items})
        return _dump({"success": True, "schedule": schedule})
    except Exception as e:
        logger.error(f"查询忙闲异常: {e}")
        return _dump({"success": False, "error": str(e)})


if __name__ == "__main__":
    logger.info("启动 DingTalk MCP Server")
    mcp.run()
