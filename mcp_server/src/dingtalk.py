import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse

import requests
from mcp.server.fastmcp import FastMCP
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

mcp = FastMCP("DingTalk MCP Server")

APP_KEY = os.getenv("DINGTALK_APP_KEY", "")
APP_SECRET = os.getenv("DINGTALK_APP_SECRET", "")
AGENT_ID = os.getenv("DINGTALK_AGENT_ID", "")
ROBOT_CODE = os.getenv("DINGTALK_ROBOT_CODE", APP_KEY)

_access_token_cache = {"token": "", "expire_at": 0}


def _get_access_token() -> str:
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


if __name__ == "__main__":
    logger.info("启动 DingTalk MCP Server")
    mcp.run()
