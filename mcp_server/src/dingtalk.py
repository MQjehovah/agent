"""钉钉 MCP Server: 消息/通讯录/审批/待办/日程/群管理/公告/日志/钉盘/会议/日历/
互动 AI 卡片/钉钉文档工具。

钉钉开放平台权限点(开发者后台「权限管理」申请, 权限名称以控制台为准):
- 审批: 审批实例管理(发起/评论/撤回实例)、审批任务管理(同意/拒绝)、审批表单读
- 机器人消息: 机器人发送消息(含批量撤回、已读状态查询、消息文件上传)
- 群管理: 群会话管理(创建群/成员增删/群公告)
- 公告: 公告管理(创建/删除)
- 日志: 日志创建、日志读取
- 钉盘: 钉盘文件上传、钉盘文件下载
- 视频会议: 视频会议管理(创建/查询/关闭)
- 通讯录: 用户只读、部门只读、角色只读、外部联系人只读
- 日历: 日程读写
- 互动卡片: 卡片平台(模板查询、卡片实例创建/更新、卡片投递)
- 钉钉文档: 知识库读、文档读写、文档成员授权

实现约定: 统一 `_api`(v2 网关 + token, 支持 multipart 上传), 响应经 `_dump` 截断(8k);
错误透传 errcode/errmsg(HTTP>=400 抛出, 由工具转成 {"success": False, "error": ...});
少数 body/query 字段名以「线上冒烟待确认」标注(会议/外部联系人/公告/日志/钉盘/机器人文件/
卡片模板列表/文档 API/文档成员)。

参数命名约定(避免与系统 userId/工号混淆):
- `dingtalk_userid` = 钉钉用户ID(不是工号, 也不是本系统 userId); `dingtalk_userids` = 上述 ID 列表(逗号分隔);
- `dingtalk_unionid` = 钉钉 unionId(待办/日程等工具传纯数字时自动按钉钉 userId 换算, 见 `_resolve_unionid`);
  `dingtalk_unionids` = 钉钉 unionId 列表; `dingtalk_creator_unionid` = 创建人(次级 unionId 参数, 避免重名)。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
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
# 单次响应返回上限(8k; 超长截断为 truncated 标记 + 预览, 防止撑爆模型上下文)
_MAX_RESPONSE_CHARS = 8 * 1024
# 钉钉 v1.0 在「应用缺权限」时也可能统一回 HTTP 503(实测 todoTasks 连续 503),
# 与临时故障同码; 故对 503 做短退避重试, 仍失败在文案里给出权限申请入口。
_API_503_RETRIES = 2
_API_503_BACKOFF_SECONDS = (0.5, 1.0)
_API_503_PERMISSION_HINT = ("（钉钉临时故障；若持续出现，通常是应用缺少该接口权限，"
                            "请在钉钉开放平台→应用→权限管理申请）")


def _api_error_detail(resp) -> str:
    """提取错误响应中的 errcode/errmsg(新版 v1.0 网关为 code/message)透传。

    两项均无(如网关 502 HTML)时回退响应正文前 200 字符。
    """
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        parts = []
        if body.get("errcode") is not None:
            parts.append(f"errcode={body.get('errcode')}")
        if body.get("errmsg"):
            parts.append(f"errmsg={body.get('errmsg')}")
        if body.get("code"):
            parts.append(f"code={body.get('code')}")
        if body.get("message"):
            parts.append(f"message={body.get('message')}")
        if parts:
            return " ".join(parts)
    return str(getattr(resp, "text", ""))[:200]


def _api(method: str, path: str, *, params: dict | None = None,
         json_body: dict | None = None, data: dict | None = None,
         files: dict | None = None, timeout: int = 10) -> dict:
    """调用钉钉新版 OpenAPI(v2 网关), 返回解析后的 JSON body。

    统一附带 x-acs-dingtalk-access-token; 非 2xx 抛 RuntimeError(含钉钉
    errcode/errmsg), 由各工具统一转成 {"success": False, "error": ...}。
    HTTP 503 按 0.5s/1s 退避重试至多 2 次(共 3 次请求; 钉钉缺权限也可能回 503),
    持续 503 的错误文案附带权限申请提示。传 files 时走 multipart/form-data
    (data 作为普通表单字段), 此时不显式设置 Content-Type, 由 requests 生成
    boundary。测试经 monkeypatch 替换本函数注入假响应。
    """
    token = _get_access_token()
    headers = {"x-acs-dingtalk-access-token": token}
    if files is None:
        headers["Content-Type"] = "application/json"
    for attempt in range(_API_503_RETRIES + 1):
        resp = requests.request(
            method, f"{_API_BASE}{path}", headers=headers,
            params=params, json=json_body, data=data, files=files, timeout=timeout)
        if resp.status_code == 503:
            if attempt < _API_503_RETRIES:
                time.sleep(_API_503_BACKOFF_SECONDS[attempt])
                continue
            raise RuntimeError(
                f"HTTP 503: {_api_error_detail(resp)}{_API_503_PERMISSION_HINT}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {_api_error_detail(resp)}")
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


# 用户标识换算缓存: 原始值(钉钉 userId / unionId / 工号) → (userid, unionid)
_UNIONID_CACHE: dict[str, str] = {}
_USERID_CACHE: dict[str, tuple[str, str]] = {}
# 通讯录索引缓存(工号→用户 / unionid→userid / userid→unionid): TTL 懒构建;
# 构建失败不写缓存(下次重试), 调用方走原错误路径
_DIRECTORY_INDEX_TTL_SECONDS = 1800
_DIRECTORY_INDEX_MAX_PAGES = 50
_directory_index_cache: dict = {"built_at": 0.0, "data": None}


def _identity_not_found_hint(value: str) -> str:
    """用户标识换算失败的统一可执行文案。"""
    return (f"未找到该钉钉用户(userId={value}): 工号可查通讯录索引匹配, "
            "或请用当前用户画像中的钉钉 userId/unionId")


def _oapi_call(path: str, payload: dict) -> dict:
    """旧版 OAPI POST 包装: 传输/解析失败抛 RuntimeError; 返回 body(含 errcode)。"""
    body, err = _legacy_oapi_post(path, payload)
    if err:
        raise RuntimeError(err)
    return body


def _resolve_unionid(value: str) -> tuple[str, str]:
    """接受钉钉 userId(纯数字)/工号(纯数字)/unionId, 自动换算; 返回 (union_id, error)。

    纯数字: 查进程内缓存, 未命中调 topapi/v2/user/get 取 result.unionid; user/get
    失败(如该数字实为工号) → 查通讯录索引 by_job_number 兜底。非纯数字视为 unionId
    原样返回(也写入缓存)。error 非空表示失败, 调用方应直接返回错误。
    """
    raw = str(value or "").strip()
    if not raw:
        return "", "unionId/userId 不能为空"
    if not raw.isdigit():
        _UNIONID_CACHE[raw] = raw
        return raw, ""
    cached = _UNIONID_CACHE.get(raw)
    if cached:
        return cached, ""

    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}"
        resp = requests.post(url, json={"userid": raw}, timeout=10)
        body = resp.json() if resp.text else {}
    except Exception as e:
        return "", f"换算 unionId 失败(userId={raw}): {e}"
    if not isinstance(body, dict) or body.get("errcode") != 0:
        record = _directory_index()["by_job_number"].get(raw)
        if record and record.get("unionid"):
            _UNIONID_CACHE[raw] = record["unionid"]
            _USERID_CACHE.setdefault(raw, (record["userid"], record["unionid"]))
            return record["unionid"], ""
        errmsg = body.get("errmsg") if isinstance(body, dict) else ""
        return "", _identity_not_found_hint(raw) + (f"（{errmsg}）" if errmsg else "")
    union_id = str((body.get("result") or {}).get("unionid") or "").strip()
    if not union_id:
        return "", _identity_not_found_hint(raw) + "（user/get 返回缺少 unionid）"
    _UNIONID_CACHE[raw] = union_id
    return union_id, ""


def _resolve_dingtalk_userid(value: str) -> tuple[str, str, str]:
    """钉钉用户标识(钉钉 userId/工号/unionId) → (userid, unionid, error)。

    - 纯数字: 先 topapi/v2/user/get 快路径(真 userId); 失败(如该数字实为工号) →
      查通讯录索引 by_job_number 兜底, 命中返回其 userid/unionid;
    - 非数字: 调 topapi/user/getbyunionid 换 userid; 失败返回原值与错误;
    - 命中结果进程内缓存; 失败返回可执行错误(不抛)。
    """
    raw = str(value or "").strip()
    if not raw:
        return "", "", "dingtalk_userid/unionId 不能为空"
    cached = _USERID_CACHE.get(raw)
    if cached:
        return cached[0], cached[1], ""

    if raw.isdigit():
        try:
            body = _oapi_call("/topapi/v2/user/get", {"userid": raw})
        except Exception as e:
            return "", "", f"换算钉钉 userId 失败({raw}): {e}"
        if body.get("errcode") == 0:
            unionid = str((body.get("result") or {}).get("unionid") or "").strip()
            _USERID_CACHE[raw] = (raw, unionid)
            if unionid:
                _UNIONID_CACHE.setdefault(raw, unionid)
            return raw, unionid, ""
        record = _directory_index()["by_job_number"].get(raw)
        if record:
            userid, unionid = record["userid"], record.get("unionid", "")
            _USERID_CACHE[raw] = (userid, unionid)
            if unionid:
                _UNIONID_CACHE.setdefault(raw, unionid)
            return userid, unionid, ""
        return "", "", _identity_not_found_hint(raw)

    try:
        body = _oapi_call("/topapi/user/getbyunionid", {"unionid": raw})
    except Exception as e:
        return raw, "", f"换算钉钉 userId 失败({raw}): {e}"
    if body.get("errcode") == 0:
        userid = str((body.get("result") or {}).get("userid") or "").strip()
        if userid:
            _USERID_CACHE[raw] = (userid, raw)
            _UNIONID_CACHE.setdefault(raw, raw)
            return userid, raw, ""
    return raw, "", _identity_not_found_hint(raw)


def _empty_directory_index() -> dict:
    return {"by_job_number": {}, "by_userid": {}, "by_unionid": {}}


def _directory_index() -> dict:
    """通讯录索引(工号/unionid/userid → 用户); TTL 1800s 懒构建, 失败返回空索引。

    构建需读通讯录(部门全量 + 各部门用户分页); 任何失败仅告警并返回空索引,
    不抛异常(调用方自然走原错误路径)。
    """
    cached = _directory_index_cache["data"]
    now = time.time()
    if cached is not None and now - _directory_index_cache["built_at"] < _DIRECTORY_INDEX_TTL_SECONDS:
        return cached
    try:
        data = _build_directory_index()
    except Exception as e:
        logger.warning(f"通讯录索引构建失败(忽略, 走原错误路径): {e}")
        return _empty_directory_index()
    _directory_index_cache["data"] = data
    _directory_index_cache["built_at"] = now
    logger.info(f"通讯录索引构建完成: {len(data['by_userid'])} 个用户")
    return data


def _build_directory_index() -> dict:
    """枚举全部部门(含根 1)与部门用户, 构建工号/unionid/userid 索引。"""
    department_ids = _fetch_all_department_ids()
    by_userid: dict[str, dict] = {}
    for dept_id in department_ids:
        for record in _fetch_department_users(dept_id):
            by_userid[record["userid"]] = record
    by_job_number = {r["job_number"]: r for r in by_userid.values() if r.get("job_number")}
    by_unionid = {r["unionid"]: r["userid"] for r in by_userid.values() if r.get("unionid")}
    return {"by_job_number": by_job_number, "by_userid": by_userid, "by_unionid": by_unionid}


def _fetch_all_department_ids() -> list[int]:
    """复用部门 listsub 递归(fetch_child)取全部部门 id(含根部门 1)。"""
    payload = json.loads(dingtalk_get_department_list(1, True, "zh_CN"))
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError((payload or {}).get("error") or "获取部门列表失败")
    ids = {1}
    for dept in payload.get("departments") or []:
        dept_id = dept.get("dept_id")
        if isinstance(dept_id, int):
            ids.add(dept_id)
    return sorted(ids)


def _fetch_department_users(dept_id: int) -> list[dict]:
    """按部门分页(oapi v2 user/list, size=100)拉取用户; 单部门最多 50 页。"""
    records: list[dict] = []
    cursor = 0
    for _ in range(_DIRECTORY_INDEX_MAX_PAGES):
        body, err = _legacy_oapi_post(
            "/topapi/v2/user/list",
            {"dept_id": dept_id, "cursor": cursor, "size": 100})
        if err:
            raise RuntimeError(f"获取部门用户失败(dept_id={dept_id}): {err}")
        if body.get("errcode") != 0:
            raise RuntimeError(
                f"获取部门用户失败(dept_id={dept_id}): errcode={body.get('errcode')} "
                f"errmsg={body.get('errmsg') or ''}")
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        for item in result.get("list") or []:
            if not isinstance(item, dict):
                continue
            userid = str(item.get("userid") or "").strip()
            if not userid:
                continue
            records.append({
                "userid": userid,
                "unionid": str(item.get("unionid") or "").strip(),
                "job_number": str(item.get("job_number") or "").strip(),
                "name": item.get("name") or "",
            })
        if not result.get("has_more"):
            break
        next_cursor = result.get("next_cursor")
        if next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return records


def _int_arg(value, name: str, minimum: int = 0) -> tuple[int, str]:
    """校验整数参数(拒绝 bool/非数字/越界); 返回 (值, 错误串), 错误串为空表示通过。"""
    if value is None or isinstance(value, bool):
        return 0, f"{name} 必填且为整数"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0, f"{name} 需为整数"
    if number < minimum:
        return 0, f"{name} 不能小于 {minimum}"
    return number, ""


# 机器人消息文件上限: 钉钉文档为 20MB(线上冒烟待确认, 服务端另有类型限制)
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _check_upload_file(file_path: str) -> str:
    """校验待上传本地文件(必填/存在/为文件/不超过上限); 返回错误串(空=通过)。"""
    path = str(file_path or "").strip()
    if not path:
        return "file_path 必填"
    if not os.path.isfile(path):
        return f"file_path 不存在或不是文件: {path}"
    if os.path.getsize(path) > _MAX_UPLOAD_BYTES:
        return f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"
    return ""


def _upload_robot_media(file_path: str) -> str:
    """上传机器人消息媒体文件, 返回 mediaId。

    线上冒烟待确认: 上传接口 multipart 字段名 file、响应字段 mediaId。
    """
    path = str(file_path).strip()
    with open(path, "rb") as fh:
        result = _api("POST", "/v1.0/robot/messages/upload", params={"type": "file"},
                      files={"file": (os.path.basename(path), fh)}, timeout=60)
    media_id = str(result.get("mediaId", "") or "")
    if not media_id:
        raise RuntimeError(f"上传媒体文件未返回 mediaId: {result}")
    return media_id


def _recall_body(process_query_key: str, msg_id: str) -> tuple[dict, str]:
    """构造机器人消息撤回 body(processQueryKey/msgId 二选一); 返回 (body, 错误串)。"""
    body: dict = {"robotCode": ROBOT_CODE}
    key = str(process_query_key or "").strip()
    mid = str(msg_id or "").strip()
    if not key and not mid:
        return {}, "process_query_key 与 msg_id 至少提供一个"
    if key:
        body["processQueryKey"] = key
    if mid:
        body["msgId"] = mid
    return body, ""


def _first_list(data: dict, *keys: str) -> list:
    """按 key 顺序返回第一个 list 字段(响应字段名不确定时兜底), 均无则空列表。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _gen_out_track_id() -> str:
    """生成卡片实例 outTrackId(毫秒时间戳 + 随机后缀, 进程内近似唯一)。"""
    return f"mcpcard_{int(time.time() * 1000):x}{os.urandom(4).hex()}"


def _card_data_arg(card_data) -> tuple[dict, str]:
    """解析卡片数据参数(JSON 字符串或 dict); 返回 (cardData, 错误串)。

    - 形如 {"cardParamMap": {...}} 的对象原样透传;
    - 其它非空对象视为参数键值表, 自动包一层 cardParamMap(与插件验证流程一致)。
    """
    if isinstance(card_data, str):
        text = card_data.strip()
        if not text:
            return {}, "card_data 必填(JSON 对象)"
        try:
            card_data = json.loads(text)
        except json.JSONDecodeError:
            return {}, "card_data 需为 JSON 对象字符串"
    if not isinstance(card_data, dict) or not card_data:
        return {}, "card_data 需为非空 JSON 对象"
    if isinstance(card_data.get("cardParamMap"), dict):
        return card_data, ""
    return {"cardParamMap": card_data}, ""


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
    dingtalk_userids: str,
    msg_type: str,
    msg_content: str,
    agent_id: str = ""
):
    """发送工作通知消息给指定用户（企业内部应用）。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 接收人用户ID列表，逗号分隔，例如 "user1,user2"
    - msg_type: 消息类型，支持 text / markdown / oa / action_card
    - msg_content: 消息内容JSON字符串。
        text类型: {"content":"消息内容"}
        markdown类型: {"title":"标题","text":"# Markdown内容"}
        oa类型: {"head":{"text":"标题"},"body":{"title":"正文标题","content":"正文内容"}}
    - agent_id: 应用agentId，默认使用环境变量 DINGTALK_AGENT_ID
    """
    logger.info(f"发送工作通知: type={msg_type}, users={dingtalk_userids}")

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
            "userid_list": dingtalk_userids,
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
                "message": f"工作通知已发送给 {dingtalk_userids}"
            }, ensure_ascii=False)
        else:
            logger.error(f"工作通知发送失败: {result}")
            return json.dumps({"success": False, "error": result.get("errmsg", "未知错误"), "code": result.get("errcode")}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"发送工作通知异常: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_robot_single_message(
    dingtalk_userids: list[str],
    msg_key: str,
    msg_param: str,
    robot_code: str = ""
):
    """通过机器人发送单聊消息给指定用户。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 接收人userId列表
    - msg_key: 消息类型，如 sampleText / sampleMarkdown / sampleImageMsg / sampleRichText
    - msg_param: 消息参数JSON字符串
        sampleText: {"content":"消息内容"}
        sampleMarkdown: {"title":"标题","text":"Markdown内容"}
        sampleRichText: {"richMessageParamList":[{"type":1,"textContent":"文本"}]}
    - robot_code: 机器人编码，默认使用 DINGTALK_ROBOT_CODE
    """
    logger.info(f"机器人单聊消息: type={msg_key}, users={dingtalk_userids}")

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
            "userIds": dingtalk_userids,
            "msgKey": msg_key,
            "msgParam": json.dumps(param_obj, ensure_ascii=False)
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()

        if resp.status_code == 200 and "body" not in result.get("code", ""):
            logger.info("机器人单聊消息发送成功")
            return json.dumps({"success": True, "message": f"消息已发送给 {len(dingtalk_userids)} 个用户"}, ensure_ascii=False)
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
    dingtalk_userids: list[str] | None = None,
    at_all: bool = False
):
    """通过机器人发送群聊消息。

    参数:
    - conversation_id: 群会话ID，例如 "cidXXXXXX"
    - msg_key: 消息类型，如 sampleText / sampleMarkdown / sampleActionCard / sampleInteractiveCard
    - msg_param: 消息参数JSON字符串
    - robot_code: 机器人编码，默认使用 DINGTALK_ROBOT_CODE
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; @的用户ID列表（可选）
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
            "atUserIds": dingtalk_userids or [],
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
def dingtalk_get_user_detail(dingtalk_userid: str, language: str = "zh_CN"):
    """获取用户详情。

    参数:
    - dingtalk_userid: 钉钉用户标识(必填): 钉钉 userId, 或工号(纯数字, user/get
        失败后经通讯录索引匹配换算); 不是本系统 userId
    - language: 语言，默认 zh_CN
    """
    logger.info(f"获取用户详情: {dingtalk_userid}")

    err = _check_config()
    if err:
        return err

    raw_userid = str(dingtalk_userid or "").strip()
    if not raw_userid:
        return json.dumps({"success": False, "error": "dingtalk_userid 必填"}, ensure_ascii=False)

    try:
        result = _oapi_call("/topapi/v2/user/get",
                            {"userid": raw_userid, "language": language})
        if result.get("errcode") != 0 and raw_userid.isdigit():
            # 工号兜底: user/get 失败后查通讯录索引, 命中则用真实 userid 重试
            record = _directory_index()["by_job_number"].get(raw_userid)
            if record:
                result = _oapi_call("/topapi/v2/user/get",
                                    {"userid": record["userid"], "language": language})

        if result.get("errcode") == 0:
            user = result.get("result", {})
            return json.dumps({
                "success": True,
                "user": {
                    "userid": user.get("userid"),
                    "unionid": user.get("unionid"),
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
        errmsg = result.get("errmsg", "未知错误")
        if raw_userid.isdigit():
            return json.dumps({"success": False, "error": _identity_not_found_hint(raw_userid)
                               + f"（{errmsg}）"}, ensure_ascii=False)
        return json.dumps({"success": False, "error": errmsg}, ensure_ascii=False)

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
    dingtalk_userids: list[str] | None = None,
    at_all: bool = False
):
    """发送互动卡片消息到群聊。

    参数:
    - conversation_id: 群会话ID
    - card_template_id: 卡片模板ID
    - card_data: 卡片数据JSON字符串，格式: {"cardParamMap":{"key1":"val1"},"cardMediaIdMap":{}}
    - out_track_id: 跟踪ID（可选）
    - robot_code: 机器人编码
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; @的用户ID列表（可选）
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
            "atUserIds": dingtalk_userids or [],
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
    dingtalk_userids: list[str],
    title: str,
    text: str,
    robot_code: str = ""
):
    """快捷发送Markdown单聊消息给指定用户（封装好的便捷方法）。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 接收人userId列表
    - title: 消息标题
    - text: Markdown格式正文
    - robot_code: 机器人编码
    """
    msg_param = json.dumps({"title": title, "text": text}, ensure_ascii=False)
    return dingtalk_send_robot_single_message(dingtalk_userids, "sampleMarkdown", msg_param, robot_code)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_markdown_group(
    conversation_id: str,
    title: str,
    text: str,
    robot_code: str = "",
    dingtalk_userids: list[str] | None = None,
    at_all: bool = False
):
    """快捷发送Markdown群聊消息（封装好的便捷方法）。

    参数:
    - conversation_id: 群会话ID
    - title: 消息标题
    - text: Markdown格式正文
    - robot_code: 机器人编码
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; @的用户ID列表
    - at_all: 是否@所有人
    """
    msg_param = json.dumps({"title": title, "text": text}, ensure_ascii=False)
    return dingtalk_send_robot_group_message(conversation_id, "sampleMarkdown", msg_param, robot_code, dingtalk_userids, at_all)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_text_single(
    dingtalk_userids: list[str],
    content: str,
    robot_code: str = ""
):
    """快捷发送文本单聊消息给指定用户（封装好的便捷方法）。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 接收人userId列表
    - content: 文本消息内容
    - robot_code: 机器人编码
    """
    msg_param = json.dumps({"content": content}, ensure_ascii=False)
    return dingtalk_send_robot_single_message(dingtalk_userids, "sampleText", msg_param, robot_code)


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_text_group(
    conversation_id: str,
    content: str,
    robot_code: str = "",
    dingtalk_userids: list[str] | None = None,
    at_all: bool = False
):
    """快捷发送文本群聊消息（封装好的便捷方法）。

    参数:
    - conversation_id: 群会话ID
    - content: 文本消息内容
    - robot_code: 机器人编码
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; @的用户ID列表
    - at_all: 是否@所有人
    """
    msg_param = json.dumps({"content": content}, ensure_ascii=False)
    return dingtalk_send_robot_group_message(conversation_id, "sampleText", msg_param, robot_code, dingtalk_userids, at_all)


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
    dingtalk_userid: str,
    form_values: str = "[]",
    dept_id: int = 0,
    approvers: str = "",
    dingtalk_userids: str = "",
):
    """发起审批实例(创建审批单)。

    参数:
    - process_code: 审批模板 processCode(审批后台模板唯一标识, 形如 PROC-xxxx)
    - dingtalk_userid: 钉钉用户ID(不是工号, 也不是本系统 userId); 发起人 userId(必填)
    - form_values: 表单值 JSON 字符串, 形如 [{"name":"报销金额","value":"100"}];
        name 需与模板控件名一致
    - dept_id: 发起人部门 ID(可选)
    - approvers: 指定审批人 JSON 字符串, 形如
        [{"approverUserId":"user1","actionType":"AND"}](可选)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 抄送人 userId 列表, 逗号分隔(可选)
    """
    logger.info(f"发起审批: process_code={process_code}, originator={dingtalk_userid}")

    err = _check_config()
    if err:
        return err

    if not str(process_code or "").strip():
        return _dump({"success": False, "error": "process_code 必填"})
    if not str(dingtalk_userid or "").strip():
        return _dump({"success": False, "error": "dingtalk_userid 必填"})
    originator_userid, _, resolve_err = _resolve_dingtalk_userid(dingtalk_userid)
    if resolve_err:
        return _dump({"success": False, "error": resolve_err})

    try:
        values = json.loads(form_values) if isinstance(form_values, str) else form_values
    except (json.JSONDecodeError, TypeError):
        return _dump({"success": False, "error": "form_values 需为 JSON 数组字符串"})
    if not isinstance(values, list) or any(
            not isinstance(item, dict) or not item.get("name") for item in values):
        return _dump({"success": False, "error": "form_values 需为 [{\"name\":...,\"value\":...}] JSON 数组"})

    body: dict = {
        "processCode": str(process_code).strip(),
        "originatorUserId": originator_userid,
        "formComponentValues": values,
    }
    if dept_id:
        body["deptId"] = int(dept_id)
    if approvers:
        try:
            body["approvers"] = json.loads(approvers)
        except (json.JSONDecodeError, TypeError):
            return _dump({"success": False, "error": "approvers 需为 JSON 数组字符串"})
    cc_ids = _split_ids(dingtalk_userids)
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


# 旧版 OAPI 审批接口回退: v1.0 对「应用缺权限」统一回 503, 而旧版接口会明确
# 返回 errcode 88/sub_code 60011 + 缺失 scope + 申请链接, 便于给出可执行指引。
# 旧版按 process_code 查询, 故先 listbyuserid 列出用户可发起的审批模板, 再逐模板
# 查实例 ID(两段式; 单模板失败跳过)。
_LEGACY_SCOPE_RE = re.compile(r"scope[=:\s]+([A-Za-z][A-Za-z0-9_.-]*)", re.IGNORECASE)
_LEGACY_APPLY_URL_RE = re.compile(r"https://open-dev\.dingtalk\.com/appscope/apply[^\s\"']*")
_LEGACY_TEMPLATE_LIMIT = 50
_LEGACY_STATUS_LIST = {0: "RUNNING", 1: "COMPLETED"}
_LEGACY_APPROVAL_NOTE = ("旧版接口仅返回审批实例ID；详情请用 "
                         "dingtalk_approval_instance(process_instance_id=...)")
# listids 必须带 start_time/end_time(毫秒, 缺省报 errcode=40); 取近 90 天
_LEGACY_LISTIDS_WINDOW_DAYS = 90
# 判定「缺权限」的文本兜底: errcode 88 / sub_code 60011(避免匹配到 188/88001 等)
_LEGACY_PERMISSION_RE = re.compile(r"(?<!\d)(88|60011)(?!\d)")


def _permission_apply_hint(body: dict) -> str:
    """从旧版错误响应解析缺失 scope 与申请链接, 生成可执行中文文案。"""
    text = " ".join(str(body.get(key) or "") for key in ("sub_msg", "errmsg"))
    match = _LEGACY_SCOPE_RE.search(text)
    scope = f" [{match.group(1)}]" if match else ""
    hint = (f"应用缺少钉钉权限{scope}（OA审批）。"
            "请到钉钉开放平台→应用→权限管理申请开通")
    url = _LEGACY_APPLY_URL_RE.search(text)
    if url:
        hint += f"；申请链接: {url.group(0)}"
    return hint


def _legacy_oapi_post(path: str, payload: dict) -> tuple[dict, str]:
    """旧版 OAPI POST(带 token); 返回 (body, error): error 为空表示拿到 dict body。"""
    try:
        token = _get_access_token()
        url = f"https://oapi.dingtalk.com{path}?access_token={token}"
        resp = requests.post(url, json=payload, timeout=10)
        body = resp.json() if resp.text else {}
    except Exception as e:
        return {}, f"请求异常: {e}"
    if not isinstance(body, dict):
        return {}, f"响应异常: {str(body)[:200]}"
    return body, ""


def _legacy_result_list(body: dict) -> list:
    """容忍旧版 result 为 dict(list 键) / list 两种形态, 取实例 ID 列表。"""
    result = body.get("result")
    if isinstance(result, dict):
        value = result.get("list")
    elif isinstance(result, list):
        value = result
    else:
        value = None
    return list(value) if isinstance(value, list) else []


def _is_permission_failure(text: str, body: dict) -> bool:
    """旧版错误是否缺权限: errcode/sub_code 结构优先, 兜底 first_error 文本 88/60011。"""
    errcode = str(body.get("errcode") or "")
    sub_code = str(body.get("sub_code") or "")
    if errcode in ("88", "60011") or sub_code == "60011":
        return True
    return bool(_LEGACY_PERMISSION_RE.search(text))


def _legacy_approval_listids(dingtalk_userid: str, status: int = 0) -> dict:
    """旧版 OAPI 两段式回退: 列模板(listbyuserid) → 逐模板查实例 ID(listids)。

    仅主 v1.0 接口缺权限回 503 时使用; status 0→RUNNING(待办) / 1→COMPLETED(已办)。
    listids 必带 start_time/end_time(近 90 天毫秒时间戳, 否则 errcode=40)。
    返回 {success, source, instance_ids, process_count, failed_processes, first_error?, note}
    或 {success: False, permission_error?, error}。
    """
    body, err = _legacy_oapi_post(
        "/topapi/process/listbyuserid",
        {"userid": dingtalk_userid, "offset": 0, "size": 100})
    if err:
        return {"success": False, "error": f"旧版审批回退失败(列模板): {err}"}
    errcode = body.get("errcode")
    sub_code = str(body.get("sub_code") or "")
    if str(errcode) in ("88", "60011") or sub_code == "60011":
        return {"success": False, "permission_error": True,
                "error": _permission_apply_hint(body)}
    if errcode != 0:
        detail = (f"errcode={errcode} errmsg={body.get('errmsg') or ''} "
                  f"sub_code={sub_code} sub_msg={_clip(body.get('sub_msg'), 200)}")
        return {"success": False, "error": f"旧版审批回退失败(列模板): {detail.strip()}"}

    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    raw_processes = result.get("process_list") or result.get("list") or []
    processes = [item for item in raw_processes if isinstance(item, dict)] \
        if isinstance(raw_processes, list) else []
    attempted = processes[:_LEGACY_TEMPLATE_LIMIT]
    status_list = _LEGACY_STATUS_LIST.get(status, "RUNNING")
    now_ms = int(time.time() * 1000)
    start_ms = int((time.time() - _LEGACY_LISTIDS_WINDOW_DAYS * 86400) * 1000)

    instance_ids: list[str] = []
    failed = 0
    first_error = ""
    first_failure: dict = {}
    for item in attempted:
        process_code = str(item.get("process_code") or item.get("processCode") or "").strip()
        if not process_code:
            failed += 1
            if not first_error:
                first_error = "process=? 缺少 process_code"
            continue
        list_body, list_err = _legacy_oapi_post(
            "/topapi/processinstance/listids",
            {"process_code": process_code, "userid": dingtalk_userid, "status_list": status_list,
             "start_time": start_ms, "end_time": now_ms})
        if list_err or list_body.get("errcode") != 0:
            failed += 1
            detail = list_err or (
                f"errcode={list_body.get('errcode')} errmsg={list_body.get('errmsg') or ''} "
                f"sub_code={list_body.get('sub_code') or ''} "
                f"sub_msg={_clip(list_body.get('sub_msg'), 200)}")
            if not first_error:
                first_error = f"process={process_code} {detail}"
                first_failure = list_body if not list_err else {}
            logger.warning(f"旧版审批 listids 模板失败: {process_code}: "
                           f"{list_err or _clip(list_body.get('errmsg'), 120)}")
            continue
        instance_ids.extend(str(item_id) for item_id in _legacy_result_list(list_body))

    # 全部模板失败且首错即缺权限: 直接给可执行权限文案(优先于泛化失败信息)
    if attempted and failed == len(attempted) and first_failure \
            and _is_permission_failure(first_error, first_failure):
        return {"success": False, "permission_error": True,
                "error": _permission_apply_hint(first_failure)}

    result_out = {
        "success": True,
        "source": "legacy",
        "instance_ids": instance_ids,
        "process_count": len(attempted),
        "failed_processes": failed,
        "note": _LEGACY_APPROVAL_NOTE,
    }
    if first_error:
        result_out["first_error"] = first_error
    return result_out


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_approval_tasks(
    dingtalk_userid: str,
    status: int = 0,
    max_results: int = 20,
    next_token: int = 0,
):
    """查询某用户的审批待办/已办任务列表。

    参数:
    - dingtalk_userid: 钉钉用户标识(必填): 钉钉 userId, 或工号/unionId(自动换算,
        见 _resolve_dingtalk_userid; 不是本系统 userId)
    - status: 任务状态, 0=待办(默认), 1=已办
    - max_results: 单页条数(1~100, 默认 20)
    - next_token: 分页游标, 首页传 0
    """
    logger.info(f"查询审批任务: user={dingtalk_userid}, status={status}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_userid or "").strip():
        return _dump({"success": False, "error": "dingtalk_userid 必填"})
    dingtalk_userid, _, resolve_err = _resolve_dingtalk_userid(dingtalk_userid)
    if resolve_err:
        return _dump({"success": False, "error": resolve_err})

    if isinstance(status, bool) or status not in (0, 1):
        return _dump({"success": False, "error": "status 仅支持 0(待办)/1(已办)"})
    size = max(1, min(int(max_results or 20), 100))
    token = int(next_token or 0)

    try:
        resp = _api("GET", "/v1.0/workflow/workRecords/todoTasks",
                    params={"userId": dingtalk_userid, "status": status,
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
        error_text = str(e)
        logger.error(f"查询审批任务异常: {e}")
        if "503" not in error_text:
            return _dump({"success": False, "error": error_text})
        # v1.0 对「应用缺权限」也可能统一回 503: 待办/已办均走旧版两段式回退
        legacy = _legacy_approval_listids(str(dingtalk_userid).strip(), status)
        if legacy.get("success"):
            out = {
                "success": True,
                "source": legacy.get("source", "legacy"),
                "instance_ids": legacy.get("instance_ids", []),
                "process_count": legacy.get("process_count", 0),
                "failed_processes": legacy.get("failed_processes", 0),
                "note": legacy.get("note", _LEGACY_APPROVAL_NOTE),
            }
            if legacy.get("first_error"):
                out["first_error"] = legacy["first_error"]
            return _dump(out)
        if legacy.get("permission_error"):
            return _dump({"success": False, "error": legacy.get("error", "权限不足")})
        logger.error(f"旧版审批回退也失败: {legacy.get('error')}")
        return _dump({"success": False,
                      "error": f"{error_text}；{legacy.get('error', '旧版审批回退失败')}"})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_approval_action(
    task_id: int,
    result: str,
    remark: str = "",
    process_instance_id: str = "",
    dingtalk_userid: str = "",
):
    """同意或拒绝审批任务(不可逆, 会推动/终结他人审批流程)。

    参数:
    - task_id: 审批任务 ID(taskId, 来自实例详情或待办列表)
    - result: 操作结果, agree=同意 / refuse=拒绝
    - remark: 审批意见(可选)
    - process_instance_id: 审批实例 ID(OpenAPI 必填, 建议随 task 一并传入)
    - dingtalk_userid: 钉钉用户标识(可选): 钉钉 userId, 或工号/unionId(自动换算);
        操作人(服务端代操作时必填, 不是本系统 userId; 缺省由钉钉按应用身份处理)
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
    if dingtalk_userid:
        actioner_userid, _, resolve_err = _resolve_dingtalk_userid(dingtalk_userid)
        if resolve_err:
            return _dump({"success": False, "error": resolve_err})
        body["actionerUserId"] = actioner_userid

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
    dingtalk_unionid: str,
    subject: str,
    dingtalk_userids: str,
    dingtalk_creator_unionid: str = "",
    description: str = "",
    due_time_ms: int = 0,
    priority: int = 0,
    detail_url: str = "",
    source_id: str = "",
):
    """创建待办任务。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 传纯数字时按钉钉 userId 自动换算(见 _resolve_unionid);
        待办归属用户(必填, 钉钉待办接口以用户维度鉴权)
    - subject: 待办标题(必填)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 执行人(必填)
    - dingtalk_creator_unionid: 钉钉 unionId(可选, 纯数字自动换算); 创建人,
        缺省为 dingtalk_unionid 对应用户
    - description: 待办描述(可选)
    - due_time_ms: 截止时间毫秒时间戳(可选, 0=不设置)
    - priority: 优先级数值(可选, 0=不设置)
    - detail_url: 详情跳转链接(可选)
    - source_id: 业务来源 ID(可选; 同 source_id 幂等, 便于重复创建去重)
    """
    logger.info(f"创建待办: dingtalk_unionid={dingtalk_unionid}, subject={subject}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})
    if not str(subject or "").strip():
        return _dump({"success": False, "error": "subject 必填"})
    executors = _split_ids(dingtalk_userids)
    if not executors:
        return _dump({"success": False, "error": "dingtalk_userids 必填(逗号分隔钉钉用户ID)"})

    body: dict = {"subject": str(subject).strip(), "executorIds": executors}
    if dingtalk_creator_unionid:
        creator_union, creator_err = _resolve_unionid(dingtalk_creator_unionid)
        if creator_err:
            return _dump({"success": False, "error": creator_err})
        body["creatorId"] = creator_union
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
        result = _api("POST", f"/v1.0/todo/users/{dingtalk_unionid}/tasks", json_body=body)
        return _dump({"success": True, "task_id": result.get("id", "")})
    except Exception as e:
        logger.error(f"创建待办异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_todo_update(
    dingtalk_unionid: str,
    task_id: str,
    done: bool | None = None,
    subject: str = "",
    description: str = "",
    due_time_ms: int = 0,
    dingtalk_userids: str = "",
):
    """更新待办任务(状态/描述/标题/截止时间/执行人)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 待办归属用户 unionId 或钉钉 userId(纯数字自动换算; 必填)
    - task_id: 待办任务 ID(必填)
    - done: 是否完成 true=完成 false=恢复未完成(可选)
    - subject: 新标题(可选)
    - description: 新描述(可选)
    - due_time_ms: 新截止时间毫秒时间戳(可选, 0=不变)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 新执行人 userId 列表, 逗号分隔(可选)
    """
    logger.info(f"更新待办: dingtalk_unionid={dingtalk_unionid}, task_id={task_id}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})
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
    executors = _split_ids(dingtalk_userids)
    if executors:
        body["executorIds"] = executors
    if not body:
        return _dump({"success": False, "error": "至少提供一项待更新字段(done/subject/description/due_time_ms/dingtalk_userids)"})

    try:
        result = _api("PUT", f"/v1.0/todo/users/{dingtalk_unionid}/tasks/{task_id}", json_body=body)
        ok = result.get("result", True) is not False
        return _dump({"success": bool(ok), "task_id": str(task_id),
                      "error": "" if ok else "更新未成功"})
    except Exception as e:
        logger.error(f"更新待办异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_todo_list(
    dingtalk_unionid: str,
    is_done: bool | None = None,
    next_token: str = "",
):
    """查询某用户的待办任务列表(按完成状态过滤, 游标分页)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 待办归属用户 unionId 或钉钉 userId(纯数字自动换算; 必填)
    - is_done: 完成状态过滤 true=已完成 / false=未完成(可选, 缺省不过滤)
    - next_token: 分页游标(上次返回的 next_token, 首页留空)
    """
    logger.info(f"查询待办列表: dingtalk_unionid={dingtalk_unionid}, is_done={is_done}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})

    body: dict = {}
    if is_done is not None:
        body["isDone"] = bool(is_done)
    if next_token:
        body["nextToken"] = str(next_token)

    try:
        result = _api("POST", f"/v1.0/todo/users/{dingtalk_unionid}/tasks/list", json_body=body)
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
    dingtalk_unionid: str,
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    dingtalk_userids: str = "",
    is_all_day: bool = False,
    time_zone: str = "Asia/Shanghai",
):
    """创建日程(会议)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 日程归属用户 unionId 或钉钉 userId(纯数字自动换算; 必填)
    - summary: 日程标题(必填)
    - start_time: 开始时间, ISO8601 形如 2026-09-24T10:00:00+08:00;
        全天日程传日期 2026-09-24(必填)
    - end_time: 结束时间, 格式同 start_time(必填)
    - calendar_id: 日历 ID, 主日历为 primary(默认)
    - description: 日程描述(可选)
    - location: 地点/会议室名称(可选)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 参与人 userId 列表, 逗号分隔(可选)
    - is_all_day: 是否全天日程(默认否)
    - time_zone: 时区(默认 Asia/Shanghai)
    """
    logger.info(f"创建日程: user={dingtalk_unionid}, summary={summary}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})
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
    attendee_ids = _split_ids(dingtalk_userids)
    if attendee_ids:
        body["attendees"] = [{"id": uid} for uid in attendee_ids]

    try:
        result = _api("POST", f"/v1.0/calendar/users/{dingtalk_unionid}/calendars/{calendar_id}/events",
                      json_body=body)
        return _dump({"success": True, "event_id": result.get("id", "")})
    except Exception as e:
        logger.error(f"创建日程异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_calendar_list_events(
    dingtalk_unionid: str,
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 20,
    next_token: str = "",
):
    """查询日程列表(时间范围内)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 日程归属用户 unionId 或钉钉 userId(纯数字自动换算; 必填)
    - calendar_id: 日历 ID, 主日历为 primary(默认)
    - time_min: 起始时间(UTC, yyyy-MM-ddTHH:mmZ, 可选)
    - time_max: 结束时间(UTC, yyyy-MM-ddTHH:mmZ, 可选)
    - max_results: 单页条数(1~100, 默认 20)
    - next_token: 分页游标(可选)
    """
    logger.info(f"查询日程列表: user={dingtalk_unionid}, calendar={calendar_id}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})

    params: dict = {"maxResults": max(1, min(int(max_results or 20), 100))}
    if time_min:
        params["timeMin"] = str(time_min)
    if time_max:
        params["timeMax"] = str(time_max)
    if next_token:
        params["nextToken"] = str(next_token)

    try:
        result = _api("GET", f"/v1.0/calendar/users/{dingtalk_unionid}/calendars/{calendar_id}/events",
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
    dingtalk_unionid: str,
    dingtalk_unionids: str,
    start_time: str,
    end_time: str,
):
    """查询用户忙闲(会议时间段)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 操作者 unionId 或钉钉 userId(纯数字自动换算; 必填)
    - dingtalk_unionids: 钉钉 unionId 列表; 被查询用户 unionId 列表, 逗号分隔(必填)
    - start_time: 起始时间(UTC, yyyy-MM-ddTHH:mmZ, 必填)
    - end_time: 结束时间(UTC, yyyy-MM-ddTHH:mmZ, 必填)
    """
    logger.info(f"查询忙闲: user={dingtalk_unionid}, targets={dingtalk_unionids}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    dingtalk_unionid, union_err = _resolve_unionid(dingtalk_unionid)
    if union_err:
        return _dump({"success": False, "error": union_err})
    targets = _split_ids(dingtalk_unionids)
    if not targets:
        return _dump({"success": False, "error": "dingtalk_unionids 必填(逗号分隔 unionId)"})
    if not str(start_time or "").strip() or not str(end_time or "").strip():
        return _dump({"success": False, "error": "start_time/end_time 必填"})

    try:
        result = _api("POST", f"/v1.0/calendar/users/{dingtalk_unionid}/querySchedule",
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


# ── 审批补全(P0) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_approval_comment(
    process_instance_id: str,
    text: str,
    dingtalk_userid: str = "",
    file: dict | None = None,
):
    """评论审批实例(可附附件)。

    参数:
    - process_instance_id: 审批实例 ID(必填)
    - text: 评论内容(必填)
    - dingtalk_userid: 钉钉用户标识(可选): 钉钉 userId, 或工号/unionId(自动换算);
        评论人(不是本系统 userId; 缺省由钉钉按应用身份处理)
    - file: 附件对象或 JSON 字符串(可选), 形如
        {"fileId":"xxx","fileName":"xxx.pdf","fileSize":1024,"fileType":"pdf"}
    """
    logger.info(f"审批评论: instance={_clip(process_instance_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(process_instance_id or "").strip():
        return _dump({"success": False, "error": "process_instance_id 必填"})
    if not str(text or "").strip():
        return _dump({"success": False, "error": "text 必填"})

    body: dict = {"processInstanceId": str(process_instance_id).strip(), "text": str(text)}
    if dingtalk_userid:
        comment_userid, _, resolve_err = _resolve_dingtalk_userid(dingtalk_userid)
        if resolve_err:
            return _dump({"success": False, "error": resolve_err})
        body["commentUserId"] = comment_userid
    if file is not None:
        if isinstance(file, str):
            try:
                file = json.loads(file)
            except json.JSONDecodeError:
                return _dump({"success": False, "error": "file 需为 JSON 对象"})
        if not isinstance(file, dict):
            return _dump({"success": False, "error": "file 需为 JSON 对象"})
        body["file"] = file

    try:
        result = _api("POST", "/v1.0/workflow/processInstances/comments", json_body=body)
        return _dump({"success": True, "comment_id": result.get("commentId", ""),
                      "message": "审批评论已提交"})
    except Exception as e:
        logger.error(f"审批评论异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_approval_revoke(process_instance_id: str, comment: str = ""):
    """撤销/终止审批实例(不可逆, 会终结他人流程)。

    参数:
    - process_instance_id: 审批实例 ID(必填)
    - comment: 撤销说明(可选)
    """
    logger.info(f"撤销审批实例: instance={_clip(process_instance_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(process_instance_id or "").strip():
        return _dump({"success": False, "error": "process_instance_id 必填"})

    body: dict = {"processInstanceId": str(process_instance_id).strip()}
    if comment:
        body["comment"] = str(comment)

    try:
        result = _api("POST", "/v1.0/workflow/processInstances/terminate", json_body=body)
        ok = bool(result.get("success", True)) and result.get("result", True) is not False
        if not ok:
            return _dump({"success": False, "error": result.get("message", "撤销未成功")})
        return _dump({"success": True, "message": "审批实例已撤销",
                      "process_instance_id": body["processInstanceId"]})
    except Exception as e:
        logger.error(f"撤销审批实例异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_approval_schema(process_code: str):
    """查询审批表单 schema(控件定义, 发起审批前可先用它校对控件名)。

    参数:
    - process_code: 审批模板 processCode(必填, 形如 PROC-xxxx)
    """
    logger.info(f"查询审批表单 schema: process_code={_clip(process_code, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(process_code or "").strip():
        return _dump({"success": False, "error": "process_code 必填"})

    try:
        result = _api("GET", "/v1.0/workflow/forms/schemas/processCodes",
                      params={"processCode": str(process_code).strip()})
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "schema": data})
    except Exception as e:
        logger.error(f"查询审批表单 schema 异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 消息治理(P0) ──


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_recall_single(process_query_key: str = "", msg_id: str = ""):
    """批量撤回单聊机器人消息(不可逆; process_query_key 与 msg_id 二选一)。

    参数:
    - process_query_key: 消息发送时返回的 processQueryKey(可选)
    - msg_id: 消息 ID(可选)
    """
    logger.info("撤回单聊机器人消息")

    err = _check_config()
    if err:
        return err

    body, problem = _recall_body(process_query_key, msg_id)
    if problem:
        return _dump({"success": False, "error": problem})

    try:
        result = _api("POST", "/v1.0/robot/oToMessages/batchRecall", json_body=body)
        return _dump({"success": True, "message": "撤回请求已提交", "result": result})
    except Exception as e:
        logger.error(f"撤回单聊消息异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_recall_group(process_query_key: str = "", msg_id: str = ""):
    """批量撤回群聊机器人消息(不可逆; process_query_key 与 msg_id 二选一)。

    参数:
    - process_query_key: 消息发送时返回的 processQueryKey(可选)
    - msg_id: 消息 ID(可选)
    """
    logger.info("撤回群聊机器人消息")

    err = _check_config()
    if err:
        return err

    body, problem = _recall_body(process_query_key, msg_id)
    if problem:
        return _dump({"success": False, "error": problem})

    try:
        result = _api("POST", "/v1.0/robot/groupMessages/batchRecall", json_body=body)
        return _dump({"success": True, "message": "撤回请求已提交", "result": result})
    except Exception as e:
        logger.error(f"撤回群聊消息异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_message_read_status(process_query_key: str, scene: str = "single"):
    """查询机器人消息已读状态。

    参数:
    - process_query_key: 消息发送时返回的 processQueryKey(必填)
    - scene: 会话场景, single=单聊(默认) / group=群聊
    """
    logger.info(f"查询消息已读状态: scene={scene}")

    err = _check_config()
    if err:
        return err

    key = str(process_query_key or "").strip()
    if not key:
        return _dump({"success": False, "error": "process_query_key 必填"})
    scene_norm = str(scene or "single").strip().lower()
    if scene_norm not in ("single", "group"):
        return _dump({"success": False, "error": "scene 仅支持 single/group"})
    path = ("/v1.0/robot/oToMessages/readStatus" if scene_norm == "single"
            else "/v1.0/robot/groupMessages/readStatus")

    try:
        result = _api("GET", path,
                      params={"robotCode": ROBOT_CODE, "processQueryKey": key})
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "scene": scene_norm, "read_status": data})
    except Exception as e:
        logger.error(f"查询消息已读状态异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 群管理(P0) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_create_group(
    name: str,
    dingtalk_userid: str = "",
    dingtalk_userids: str = "",
    icon: str = "",
    only_admin_can_invite: bool = False,
):
    """创建群聊(内部群)。

    参数:
    - name: 群名称(必填, 不超过 100 字符)
    - dingtalk_userid: 钉钉用户ID(不是工号, 也不是本系统 userId); 群主 userId(可选)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 初始成员 userId 列表, 逗号分隔(可选)
    - icon: 群头像 mediaId(可选)
    - only_admin_can_invite: 是否仅群主/管理员可邀请(默认否)
    """
    logger.info(f"创建群聊: name={_clip(name, 64)}")

    err = _check_config()
    if err:
        return err

    group_name = str(name or "").strip()
    if not group_name:
        return _dump({"success": False, "error": "name 必填"})
    if len(group_name) > 100:
        return _dump({"success": False, "error": "name 长度不能超过 100 字符"})

    body: dict = {"name": group_name}
    if dingtalk_userid:
        body["owner"] = str(dingtalk_userid).strip()
    members = _split_ids(dingtalk_userids)
    if members:
        body["memberUserIds"] = members
    if icon:
        body["icon"] = str(icon).strip()
    if only_admin_can_invite:
        body["onlyAdminCanInvite"] = True

    try:
        result = _api("POST", "/v1.0/im/chatGroups", json_body=body)
        return _dump({"success": True, "chat_id": result.get("chatId", ""),
                      "open_conversation_id": result.get("openConversationId", "")})
    except Exception as e:
        logger.error(f"创建群聊异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_group_add_members(chat_id: str, dingtalk_userids: str):
    """添加群成员。

    参数:
    - chat_id: 群会话 ID(必填)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 待添加 userId 列表, 逗号分隔(必填)
    """
    logger.info(f"添加群成员: chat={_clip(chat_id, 64)}")

    err = _check_config()
    if err:
        return err

    chat = str(chat_id or "").strip()
    if not chat:
        return _dump({"success": False, "error": "chat_id 必填"})
    members = _split_ids(dingtalk_userids)
    if not members:
        return _dump({"success": False, "error": "dingtalk_userids 必填(逗号分隔钉钉用户ID)"})

    try:
        result = _api("POST", f"/v1.0/im/chatGroups/{chat}/members",
                      json_body={"userIds": members})
        return _dump({"success": True, "added": len(members),
                      "message": f"已添加 {len(members)} 名群成员", "result": result})
    except Exception as e:
        logger.error(f"添加群成员异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_group_remove_members(chat_id: str, dingtalk_userids: str):
    """移除群成员(不可逆, 成员将被移出群聊)。

    参数:
    - chat_id: 群会话 ID(必填)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 待移除 userId 列表, 逗号分隔(必填)
    """
    logger.info(f"移除群成员: chat={_clip(chat_id, 64)}")

    err = _check_config()
    if err:
        return err

    chat = str(chat_id or "").strip()
    if not chat:
        return _dump({"success": False, "error": "chat_id 必填"})
    members = _split_ids(dingtalk_userids)
    if not members:
        return _dump({"success": False, "error": "dingtalk_userids 必填(逗号分隔钉钉用户ID)"})

    try:
        result = _api("DELETE", f"/v1.0/im/chatGroups/{chat}/members",
                      json_body={"userIds": members})
        return _dump({"success": True, "removed": len(members),
                      "message": f"已移除 {len(members)} 名群成员", "result": result})
    except Exception as e:
        logger.error(f"移除群成员异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_group_set_notice(chat_id: str, notice: str):
    """设置群公告。

    参数:
    - chat_id: 群会话 ID(必填)
    - notice: 群公告内容(必填)
    """
    logger.info(f"设置群公告: chat={_clip(chat_id, 64)}")

    err = _check_config()
    if err:
        return err

    chat = str(chat_id or "").strip()
    if not chat:
        return _dump({"success": False, "error": "chat_id 必填"})
    if not str(notice or "").strip():
        return _dump({"success": False, "error": "notice 必填"})

    try:
        result = _api("PUT", f"/v1.0/im/chatGroups/{chat}",
                      json_body={"notice": str(notice)})
        return _dump({"success": True, "message": "群公告已更新", "result": result})
    except Exception as e:
        logger.error(f"设置群公告异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 公告(P1) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_announcement_create(
    title: str,
    content: str,
    dingtalk_userid: str = "",
    dingtalk_userids: str = "",
):
    """创建公告。

    参数:
    - title: 公告标题(必填)
    - content: 公告正文(必填)
    - dingtalk_userid: 钉钉用户ID(不是工号, 也不是本系统 userId); 发布人 userId(可选)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 送达 userId 列表, 逗号分隔(可选, 缺省不传)

    线上冒烟待确认: body 字段名按钉钉惯例 title/content/author/sendTo,
    响应取 boardId/id。
    """
    logger.info(f"创建公告: title={_clip(title, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(title or "").strip():
        return _dump({"success": False, "error": "title 必填"})
    if not str(content or "").strip():
        return _dump({"success": False, "error": "content 必填"})

    body: dict = {"title": str(title).strip(), "content": str(content)}
    if dingtalk_userid:
        body["author"] = str(dingtalk_userid).strip()
    receivers = _split_ids(dingtalk_userids)
    if receivers:
        body["sendTo"] = receivers

    try:
        result = _api("POST", "/v1.0/blackboard/blackboards", json_body=body)
        return _dump({"success": True,
                      "board_id": result.get("boardId") or result.get("id", ""),
                      "message": "公告已创建"})
    except Exception as e:
        logger.error(f"创建公告异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_announcement_delete(board_id: str):
    """删除公告(不可逆)。

    参数:
    - board_id: 公告 ID(必填)
    """
    logger.info(f"删除公告: board_id={_clip(board_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(board_id or "").strip():
        return _dump({"success": False, "error": "board_id 必填"})

    try:
        result = _api("DELETE", f"/v1.0/blackboard/blackboards/{str(board_id).strip()}")
        return _dump({"success": True, "message": "公告已删除", "result": result})
    except Exception as e:
        logger.error(f"删除公告异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 钉钉日志(P1) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_report_submit(
    template_name: str,
    content: str,
    dingtalk_userids: str = "",
    to_chat: bool = False,
):
    """提交钉钉日志。

    参数:
    - template_name: 日志模板名称(必填, 可用 dingtalk_report_templates 查询)
    - content: 日志内容(必填)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 日志发送对象 userId 列表, 逗号分隔(可选)
    - to_chat: 是否同步到日志群(默认否)
    """
    logger.info(f"提交日志: template={_clip(template_name, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(template_name or "").strip():
        return _dump({"success": False, "error": "template_name 必填"})
    if not str(content or "").strip():
        return _dump({"success": False, "error": "content 必填"})

    body: dict = {"templateName": str(template_name).strip(), "content": str(content)}
    receivers = _split_ids(dingtalk_userids)
    if receivers:
        body["userIds"] = receivers
    if to_chat:
        body["toChat"] = True

    try:
        result = _api("POST", "/v1.0/report/entries", json_body=body)
        return _dump({"success": True,
                      "entry_id": result.get("id") or result.get("entryId", ""),
                      "message": "日志已提交"})
    except Exception as e:
        logger.error(f"提交日志异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_report_list(
    dingtalk_userids: str = "",
    start_time: int = 0,
    end_time: int = 0,
    template_name: str = "",
    size: int = 20,
    cursor: int = 0,
):
    """查询日志列表(按时间/模板/用户过滤, 游标分页)。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 查询用户 userId 列表, 逗号分隔(可选)
    - start_time: 起始时间毫秒时间戳(可选, 0=不限)
    - end_time: 结束时间毫秒时间戳(可选, 0=不限)
    - template_name: 日志模板名称(可选)
    - size: 单页条数(1~100, 默认 20)
    - cursor: 分页游标, 首页传 0

    线上冒烟待确认: query 参数名 userIds/startTime/endTime。
    """
    logger.info("查询日志列表")

    err = _check_config()
    if err:
        return err

    page_size, problem = _int_arg(size if size is not None else 20, "size", 1)
    if problem:
        return _dump({"success": False, "error": problem})
    page_cursor, problem = _int_arg(cursor if cursor is not None else 0, "cursor", 0)
    if problem:
        return _dump({"success": False, "error": problem})

    params: dict = {"size": min(page_size, 100), "cursor": page_cursor}
    receivers = _split_ids(dingtalk_userids)
    if receivers:
        params["userIds"] = receivers
    if start_time:
        start_ms, problem = _int_arg(start_time, "start_time", 1)
        if problem:
            return _dump({"success": False, "error": problem})
        params["startTime"] = start_ms
    if end_time:
        end_ms, problem = _int_arg(end_time, "end_time", 1)
        if problem:
            return _dump({"success": False, "error": problem})
        params["endTime"] = end_ms
    if template_name:
        params["templateName"] = str(template_name).strip()

    try:
        result = _api("GET", "/v1.0/report/entries", params=params)
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        entries = data.get("entries")
        if not isinstance(entries, list):
            entries = data.get("list") if isinstance(data.get("list"), list) else []
        return _dump({"success": True, "entries": entries, "count": len(entries),
                      "has_more": data.get("hasMore", False),
                      "next_cursor": data.get("nextCursor", 0)})
    except Exception as e:
        logger.error(f"查询日志列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_report_templates():
    """查询当前企业可用的日志模板列表。"""
    logger.info("查询日志模板列表")

    err = _check_config()
    if err:
        return err

    try:
        result = _api("GET", "/v1.0/report/templates")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        templates = data.get("templates")
        if not isinstance(templates, list):
            templates = data.get("templateList") if isinstance(data.get("templateList"), list) else []
        return _dump({"success": True, "templates": templates, "count": len(templates)})
    except Exception as e:
        logger.error(f"查询日志模板列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 钉盘/文件(P1) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_drive_upload(
    space_id: str,
    parent_dentry_id: str,
    file_path: str,
    name: str = "",
    conflict_policy: str = "AUTO_RENAME",
):
    """上传本地文件到钉盘。

    参数:
    - space_id: 钉盘空间 ID(必填)
    - parent_dentry_id: 父目录 dentryId(必填)
    - file_path: 本地文件路径(必填)
    - name: 钉盘中的文件名(可选, 缺省用本地文件名)
    - conflict_policy: 同名冲突策略, 默认 AUTO_RENAME(可选 OVERWRITE 等)

    线上冒烟待确认: multipart 字段名 file/parentDentryId/conflictPolicy、
    响应 dentry.id/name。
    """
    logger.info(f"上传钉盘文件: space={_clip(space_id, 64)}")

    err = _check_config()
    if err:
        return err

    space = str(space_id or "").strip()
    if not space:
        return _dump({"success": False, "error": "space_id 必填"})
    parent = str(parent_dentry_id or "").strip()
    if not parent:
        return _dump({"success": False, "error": "parent_dentry_id 必填"})
    problem = _check_upload_file(file_path)
    if problem:
        return _dump({"success": False, "error": problem})
    policy = str(conflict_policy or "AUTO_RENAME").strip().upper()
    if not policy:
        return _dump({"success": False, "error": "conflict_policy 必填"})

    path = str(file_path).strip()
    upload_name = str(name or "").strip() or os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            result = _api("POST", f"/v1.0/storage/spaces/{space}/files/upload",
                          data={"parentDentryId": parent, "conflictPolicy": policy},
                          files={"file": (upload_name, fh)}, timeout=120)
        dentry = result.get("dentry") if isinstance(result.get("dentry"), dict) else {}
        return _dump({"success": True,
                      "dentry_id": dentry.get("id", ""),
                      "name": dentry.get("name", upload_name),
                      "message": "文件已上传到钉盘"})
    except Exception as e:
        logger.error(f"上传钉盘文件异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_drive_download_url(space_id: str, dentry_id: str):
    """获取钉盘文件下载链接。

    参数:
    - space_id: 钉盘空间 ID(必填)
    - dentry_id: 文件 dentryId(必填)
    """
    logger.info(f"获取钉盘下载链接: space={_clip(space_id, 64)}")

    err = _check_config()
    if err:
        return err

    space = str(space_id or "").strip()
    if not space:
        return _dump({"success": False, "error": "space_id 必填"})
    dentry = str(dentry_id or "").strip()
    if not dentry:
        return _dump({"success": False, "error": "dentry_id 必填"})

    try:
        result = _api("GET", f"/v1.0/storage/spaces/{space}/dentries/{dentry}/downloadInfos")
        return _dump({"success": True,
                      "download_url": result.get("resourceUrl") or result.get("url", ""),
                      "result": result})
    except Exception as e:
        logger.error(f"获取钉盘下载链接异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_file_single(dingtalk_userids: str, file_path: str):
    """上传本地文件并发送单聊文件消息。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 接收人 userId 列表, 逗号分隔(必填)
    - file_path: 本地文件路径(必填, 不超过 20MB)

    流程: 先上传媒体得 mediaId, 再以 msgKey=sampleFile 发送
    (线上冒烟待确认: 上传响应 mediaId、msgKey/msgParam 字段)。
    """
    logger.info(f"发送单聊文件消息: users={_clip(dingtalk_userids, 64)}")

    err = _check_config()
    if err:
        return err

    receivers = _split_ids(dingtalk_userids)
    if not receivers:
        return _dump({"success": False, "error": "dingtalk_userids 必填(逗号分隔钉钉用户ID)"})
    problem = _check_upload_file(file_path)
    if problem:
        return _dump({"success": False, "error": problem})

    path = str(file_path).strip()
    file_name = os.path.basename(path)
    try:
        media_id = _upload_robot_media(path)
        msg_param = json.dumps({
            "mediaId": media_id, "fileName": file_name,
            "fileType": os.path.splitext(file_name)[1].lstrip(".") or "file",
        }, ensure_ascii=False)
        result = _api("POST", "/v1.0/robot/oToMessages/batchSend",
                      json_body={"robotCode": ROBOT_CODE, "userIds": receivers,
                                 "msgKey": "sampleFile", "msgParam": msg_param})
        return _dump({"success": True, "media_id": media_id,
                      "message": f"文件消息已发送给 {len(receivers)} 个用户",
                      "result": result})
    except Exception as e:
        logger.error(f"发送单聊文件消息异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_send_file_group(open_conversation_id: str, file_path: str):
    """上传本地文件并发送群文件消息。

    参数:
    - open_conversation_id: 群 openConversationId(必填)
    - file_path: 本地文件路径(必填, 不超过 20MB)

    线上冒烟待确认: msgKey=sampleFile 及 msgParam 字段。
    """
    logger.info(f"发送群文件消息: conversation={_clip(open_conversation_id, 64)}")

    err = _check_config()
    if err:
        return err

    conversation = str(open_conversation_id or "").strip()
    if not conversation:
        return _dump({"success": False, "error": "open_conversation_id 必填"})
    problem = _check_upload_file(file_path)
    if problem:
        return _dump({"success": False, "error": problem})

    path = str(file_path).strip()
    file_name = os.path.basename(path)
    try:
        media_id = _upload_robot_media(path)
        msg_param = json.dumps({
            "mediaId": media_id, "fileName": file_name,
            "fileType": os.path.splitext(file_name)[1].lstrip(".") or "file",
        }, ensure_ascii=False)
        result = _api("POST", "/v1.0/robot/groupMessages/send",
                      json_body={"robotCode": ROBOT_CODE, "conversationId": conversation,
                                 "msgKey": "sampleFile", "msgParam": msg_param})
        return _dump({"success": True, "media_id": media_id,
                      "message": "群文件消息已发送",
                      "process_query_key": result.get("processQueryKey", "")})
    except Exception as e:
        logger.error(f"发送群文件消息异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 视频会议(P1) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_conference_create(
    title: str,
    start_time: int,
    end_time: int,
    dingtalk_userids: str = "",
):
    """创建视频会议。

    参数:
    - title: 会议标题(必填)
    - start_time: 开始时间毫秒时间戳(必填)
    - end_time: 结束时间毫秒时间戳(必填, 需大于 start_time)
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 参会人 userId 列表, 逗号分隔(可选)

    线上冒烟待确认: body 参会人字段名 memberUserIds(亦可能为 memberUnionIds)。
    """
    logger.info(f"创建视频会议: title={_clip(title, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(title or "").strip():
        return _dump({"success": False, "error": "title 必填"})
    start_ms, problem = _int_arg(start_time, "start_time", 1)
    if problem:
        return _dump({"success": False, "error": problem})
    end_ms, problem = _int_arg(end_time, "end_time", 1)
    if problem:
        return _dump({"success": False, "error": problem})
    if end_ms <= start_ms:
        return _dump({"success": False, "error": "end_time 需大于 start_time"})

    body: dict = {"title": str(title).strip(), "startTime": start_ms, "endTime": end_ms}
    members = _split_ids(dingtalk_userids)
    if members:
        body["memberUserIds"] = members

    try:
        result = _api("POST", "/v1.0/conference/videoConferences", json_body=body)
        return _dump({"success": True,
                      "conference_id": result.get("conferenceId") or result.get("id", ""),
                      "message": "视频会议已创建"})
    except Exception as e:
        logger.error(f"创建视频会议异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_conference_query(conference_id: str):
    """查询视频会议详情。

    参数:
    - conference_id: 会议 ID(必填)
    """
    logger.info(f"查询视频会议: conference={_clip(conference_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(conference_id or "").strip():
        return _dump({"success": False, "error": "conference_id 必填"})

    try:
        result = _api("GET", f"/v1.0/conference/videoConferences/{str(conference_id).strip()}")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "conference": data})
    except Exception as e:
        logger.error(f"查询视频会议异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_conference_close(conference_id: str):
    """关闭视频会议(不可逆, 会议立即结束)。

    参数:
    - conference_id: 会议 ID(必填)
    """
    logger.info(f"关闭视频会议: conference={_clip(conference_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(conference_id or "").strip():
        return _dump({"success": False, "error": "conference_id 必填"})

    try:
        result = _api("PUT", f"/v1.0/conference/videoConferences/{str(conference_id).strip()}/close")
        return _dump({"success": True, "message": "视频会议已关闭", "result": result})
    except Exception as e:
        logger.error(f"关闭视频会议异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 通讯录进阶(P1) ──


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_user_by_unionid(dingtalk_unionid: str):
    """根据 unionId 查询用户详情(新版 v1.0 通讯录)。

    参数:
    - dingtalk_unionid: 钉钉 unionId(必填); 按 unionId 查询用户, 本工具不做纯数字换算
    """
    logger.info(f"根据 unionId 查询用户: {_clip(dingtalk_unionid, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})

    try:
        result = _api("GET", f"/v1.0/contact/users/{str(dingtalk_unionid).strip()}")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "user": data})
    except Exception as e:
        logger.error(f"根据 unionId 查询用户异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_department_detail(dept_id: int):
    """查询部门详情(新版 v1.0 通讯录)。

    参数:
    - dept_id: 部门 ID(必填, 根部门为 1)
    """
    logger.info(f"查询部门详情: dept={dept_id}")

    err = _check_config()
    if err:
        return err

    dept, problem = _int_arg(dept_id, "dept_id", 1)
    if problem:
        return _dump({"success": False, "error": problem})

    try:
        result = _api("GET", f"/v1.0/contact/departments/{dept}")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "department": data})
    except Exception as e:
        logger.error(f"查询部门详情异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_role_list():
    """查询角色列表(新版 v1.0 通讯录)。"""
    logger.info("查询角色列表")

    err = _check_config()
    if err:
        return err

    try:
        result = _api("GET", "/v1.0/contact/roles")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        roles = data.get("roles")
        if not isinstance(roles, list):
            roles = data.get("list") if isinstance(data.get("list"), list) else []
        return _dump({"success": True, "roles": roles, "count": len(roles)})
    except Exception as e:
        logger.error(f"查询角色列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_external_contacts(dingtalk_userids: str = "", size: int = 20, cursor: int = 0):
    """查询外部联系人列表。

    参数:
    - dingtalk_userids: 钉钉用户ID列表(不是工号, 也不是本系统 userId), 逗号分隔; 员工 userId 列表, 逗号分隔(可选)
    - size: 单页条数(1~100, 默认 20)
    - cursor: 分页游标, 首页传 0

    线上冒烟待确认: query 参数名 userIds/size/cursor 与响应字段。
    """
    logger.info("查询外部联系人列表")

    err = _check_config()
    if err:
        return err

    page_size, problem = _int_arg(size if size is not None else 20, "size", 1)
    if problem:
        return _dump({"success": False, "error": problem})
    page_cursor, problem = _int_arg(cursor if cursor is not None else 0, "cursor", 0)
    if problem:
        return _dump({"success": False, "error": problem})

    params: dict = {"size": min(page_size, 100), "cursor": page_cursor}
    owners = _split_ids(dingtalk_userids)
    if owners:
        params["userIds"] = owners

    try:
        result = _api("GET", "/v1.0/contact/empExtContacts", params=params)
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        contacts = data.get("contacts")
        if not isinstance(contacts, list):
            contacts = data.get("list") if isinstance(data.get("list"), list) else []
        return _dump({"success": True, "contacts": contacts, "count": len(contacts),
                      "next_cursor": data.get("nextCursor", 0)})
    except Exception as e:
        logger.error(f"查询外部联系人列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 日历补全(P1) ──


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_calendar_update_event(
    dingtalk_unionid: str,
    calendar_id: str,
    event_id: str,
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
):
    """更新日程(仅提交非空字段)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 日程归属用户 unionId(必填)
    - calendar_id: 日历 ID, 主日历为 primary(必填)
    - event_id: 日程事件 ID(必填)
    - summary: 新标题(可选)
    - start: 新开始时间 ISO8601, 形如 2026-09-24T10:00:00+08:00(可选)
    - end: 新结束时间 ISO8601(可选)
    - description: 新描述(可选)
    """
    logger.info(f"更新日程: user={_clip(dingtalk_unionid, 64)}, event={_clip(event_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    if not str(calendar_id or "").strip():
        return _dump({"success": False, "error": "calendar_id 必填"})
    if not str(event_id or "").strip():
        return _dump({"success": False, "error": "event_id 必填"})

    body: dict = {}
    if summary:
        body["summary"] = str(summary)
    if start:
        body["start"] = {"dateTime": str(start).strip(), "timeZone": "Asia/Shanghai"}
    if end:
        body["end"] = {"dateTime": str(end).strip(), "timeZone": "Asia/Shanghai"}
    if description:
        body["description"] = str(description)
    if not body:
        return _dump({"success": False, "error": "至少提供一项待更新字段(summary/start/end/description)"})

    uid = str(dingtalk_unionid).strip()
    cal = str(calendar_id).strip()
    event = str(event_id).strip()
    try:
        result = _api("PUT", f"/v1.0/calendar/users/{uid}/calendars/{cal}/events/{event}",
                      json_body=body)
        ok = result.get("result", True) is not False
        return _dump({"success": bool(ok), "event_id": event,
                      "error": "" if ok else "更新未成功"})
    except Exception as e:
        logger.error(f"更新日程异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def dingtalk_calendar_delete_event(dingtalk_unionid: str, calendar_id: str, event_id: str):
    """删除日程(不可逆)。

    参数:
    - dingtalk_unionid: 钉钉 unionId; 日程归属用户 unionId(必填)
    - calendar_id: 日历 ID, 主日历为 primary(必填)
    - event_id: 日程事件 ID(必填)
    """
    logger.info(f"删除日程: user={_clip(dingtalk_unionid, 64)}, event={_clip(event_id, 64)}")

    err = _check_config()
    if err:
        return err

    if not str(dingtalk_unionid or "").strip():
        return _dump({"success": False, "error": "dingtalk_unionid 必填"})
    if not str(calendar_id or "").strip():
        return _dump({"success": False, "error": "calendar_id 必填"})
    if not str(event_id or "").strip():
        return _dump({"success": False, "error": "event_id 必填"})

    uid = str(dingtalk_unionid).strip()
    cal = str(calendar_id).strip()
    event = str(event_id).strip()
    try:
        result = _api("DELETE", f"/v1.0/calendar/users/{uid}/calendars/{cal}/events/{event}")
        return _dump({"success": True, "message": "日程已删除", "result": result})
    except Exception as e:
        logger.error(f"删除日程异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 互动/AI 卡片(卡片平台) ──


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_card_template_list():
    """查询互动卡片/AI 卡片模板列表(取发卡所需的模板 ID)。

    返回要点: templates(模板数组)与 count。

    线上冒烟待确认: GET /v1.0/card/templates 的 query 参数(是否需要分页)与
    响应字段名(当前兼容 templates/templateList/data)。
    """
    logger.info("查询卡片模板列表")

    err = _check_config()
    if err:
        return err

    try:
        result = _api("GET", "/v1.0/card/templates")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        templates = _first_list(data, "templates", "templateList", "data")
        return _dump({"success": True, "templates": templates, "count": len(templates)})
    except Exception as e:
        logger.error(f"查询卡片模板列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_ai_card_send(
    template_id: str,
    card_data: str,
    dingtalk_userid: str = "",
    open_conversation_id: str = "",
    out_track_id: str = "",
):
    """创建并投递互动/AI 卡片(STREAM 回调, 可用于后续 dingtalk_card_instance_update)。

    参数:
    - template_id: 卡片模板 ID(必填, 可先用 dingtalk_card_template_list 查询)
    - card_data: 卡片数据 JSON 字符串(必填), 支持
        {"cardParamMap":{"key":"val"}} 形态或直接传参数键值表(自动包 cardParamMap)
    - dingtalk_userid: 钉钉用户ID(不是工号, 也不是本系统 userId); 接收人
        (投递到私聊场域 dtv1.card//IM_ROBOT.{userId})
    - open_conversation_id: 群 openConversationId(投递到群场域 dtv1.card//IM_GROUP.{id})
        dingtalk_userid 与 open_conversation_id 至少提供一个, 两者都给则逐一投递
    - out_track_id: 卡片实例 ID(可选, 缺省自动生成 mcpcard_ 前缀 ID)

    返回要点: out_track_id(更新卡片时使用)、delivered(各场域投递结果列表)。

    流程与插件验证一致: 创建实例(POST /v1.0/card/instances, callbackType=STREAM)
    → 投递(POST /v1.0/card/instances/deliver, userIdType=1)。
    线上冒烟待确认: callbackType 插件验证在 body, 此处 body 与 query 双带;
    群场域 imGroupOpenDeliverModel.robotCode 字段。
    """
    logger.info(f"发送 AI 卡片: template={_clip(template_id, 64)}")

    err = _check_config()
    if err:
        return err

    template = str(template_id or "").strip()
    if not template:
        return _dump({"success": False, "error": "template_id 必填"})
    data_obj, problem = _card_data_arg(card_data)
    if problem:
        return _dump({"success": False, "error": problem})

    targets = []
    if str(dingtalk_userid or "").strip():
        targets.append(("IM_ROBOT", f"dtv1.card//IM_ROBOT.{str(dingtalk_userid).strip()}"))
    if str(open_conversation_id or "").strip():
        targets.append(("IM_GROUP", f"dtv1.card//IM_GROUP.{str(open_conversation_id).strip()}"))
    if not targets:
        return _dump({"success": False, "error": "dingtalk_userid 与 open_conversation_id 至少提供一个"})

    track = str(out_track_id or "").strip() or _gen_out_track_id()
    try:
        _api("POST", "/v1.0/card/instances", params={"callbackType": "STREAM"},
             json_body={
                 "cardTemplateId": template,
                 "outTrackId": track,
                 "cardData": data_obj,
                 "callbackType": "STREAM",
                 "imGroupOpenSpaceModel": {"supportForward": False},
                 "imRobotOpenSpaceModel": {"supportForward": False},
             })
        delivered = []
        for space_type, open_space_id in targets:
            body: dict = {"outTrackId": track, "openSpaceId": open_space_id, "userIdType": 1}
            if space_type == "IM_ROBOT":
                body["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}
            else:
                body["imGroupOpenDeliverModel"] = {"robotCode": ROBOT_CODE}
            result = _api("POST", "/v1.0/card/instances/deliver", json_body=body)
            delivered.append({"space_type": space_type, "open_space_id": open_space_id,
                              "result": result})
        return _dump({"success": True, "out_track_id": track,
                      "message": f"AI 卡片已投递({len(delivered)} 个场域)",
                      "delivered": delivered})
    except Exception as e:
        logger.error(f"发送 AI 卡片异常: {e}")
        return _dump({"success": False, "out_track_id": track, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_card_instance_update(out_track_id: str, card_data: str):
    """更新互动卡片实例数据(按 key 合并, 未提交的卡片字段保持不变)。

    参数:
    - out_track_id: 卡片实例 ID(必填, 发卡时返回)
    - card_data: 卡片数据 JSON 字符串(必填), 支持
        {"cardParamMap":{"key":"val"}} 形态或直接传参数键值表(自动包 cardParamMap)

    返回要点: success 与透传结果 result。
    流程与插件验证一致: PUT /v1.0/card/instances,
    cardUpdateOptions.updateCardDataByKey=true(仅更新提交的 key)。
    """
    logger.info(f"更新卡片实例: out_track_id={_clip(out_track_id, 64)}")

    err = _check_config()
    if err:
        return err

    track = str(out_track_id or "").strip()
    if not track:
        return _dump({"success": False, "error": "out_track_id 必填"})
    data_obj, problem = _card_data_arg(card_data)
    if problem:
        return _dump({"success": False, "error": problem})

    try:
        result = _api("PUT", "/v1.0/card/instances", json_body={
            "outTrackId": track,
            "cardData": data_obj,
            "cardUpdateOptions": {"updateCardDataByKey": True},
        })
        ok = result.get("success", True) is not False
        return _dump({"success": bool(ok), "out_track_id": track,
                      "error": "" if ok else "更新未成功", "result": result})
    except Exception as e:
        logger.error(f"更新卡片实例异常: {e}")
        return _dump({"success": False, "error": str(e)})


# ── 钉钉文档/知识库 ──


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_doc_workspaces():
    """查询知识库/团队空间列表。

    返回要点: workspaces(知识库数组)与 count。

    线上冒烟待确认: GET /v1.0/doc/workspaces 的 query 参数(是否需要分页)与
    响应字段名(当前兼容 workspaces/workspaceList/data)。
    """
    logger.info("查询知识库列表")

    err = _check_config()
    if err:
        return err

    try:
        result = _api("GET", "/v1.0/doc/workspaces")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        workspaces = _first_list(data, "workspaces", "workspaceList", "data")
        return _dump({"success": True, "workspaces": workspaces, "count": len(workspaces)})
    except Exception as e:
        logger.error(f"查询知识库列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_doc_list(
    workspace_id: str,
    parent_id: str = "",
    page_size: int = 50,
    next_token: str = "",
):
    """查询知识库中文档列表(可按父目录过滤, 游标分页)。

    参数:
    - workspace_id: 知识库/团队空间 ID(必填)
    - parent_id: 父目录 ID(可选, 缺省查根目录)
    - page_size: 单页条数(1~100, 默认 50)
    - next_token: 分页游标(上次返回的 next_token, 首页留空)

    返回要点: docs(文档数组)、count 与 next_token(无更多时为空)。
    线上冒烟待确认: query 参数名 parentId/maxResults/nextToken 与响应字段名
    (当前兼容 docs/docList/data)。
    """
    logger.info(f"查询文档列表: workspace={_clip(workspace_id, 64)}")

    err = _check_config()
    if err:
        return err

    workspace = str(workspace_id or "").strip()
    if not workspace:
        return _dump({"success": False, "error": "workspace_id 必填"})
    size, problem = _int_arg(page_size if page_size is not None else 50, "page_size", 1)
    if problem:
        return _dump({"success": False, "error": problem})

    params: dict = {"maxResults": min(size, 100)}
    if str(parent_id or "").strip():
        params["parentId"] = str(parent_id).strip()
    if str(next_token or "").strip():
        params["nextToken"] = str(next_token).strip()

    try:
        result = _api("GET", f"/v1.0/doc/workspaces/{workspace}/docs", params=params)
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        docs = _first_list(data, "docs", "docList", "data")
        return _dump({"success": True, "docs": docs, "count": len(docs),
                      "next_token": data.get("nextToken", "")})
    except Exception as e:
        logger.error(f"查询文档列表异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_READ_ANNOTATIONS)
def dingtalk_doc_info(workspace_id: str, doc_id: str):
    """查询文档元信息(名称/类型/创建人/更新时间等)。

    参数:
    - workspace_id: 知识库/团队空间 ID(必填)
    - doc_id: 文档 ID(必填)

    返回要点: doc(原始响应体, 字段名以线上为准)。
    线上冒烟待确认: 路径参数 docId 与响应字段。
    """
    logger.info(f"查询文档信息: workspace={_clip(workspace_id, 64)}, doc={_clip(doc_id, 64)}")

    err = _check_config()
    if err:
        return err

    workspace = str(workspace_id or "").strip()
    if not workspace:
        return _dump({"success": False, "error": "workspace_id 必填"})
    doc = str(doc_id or "").strip()
    if not doc:
        return _dump({"success": False, "error": "doc_id 必填"})

    try:
        result = _api("GET", f"/v1.0/doc/workspaces/{workspace}/docs/{doc}")
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True, "doc": data})
    except Exception as e:
        logger.error(f"查询文档信息异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_doc_create(
    workspace_id: str,
    name: str,
    doc_type: str = "DOC",
    parent_id: str = "",
):
    """在知识库中创建文档。

    参数:
    - workspace_id: 知识库/团队空间 ID(必填)
    - name: 文档名称(必填)
    - doc_type: 文档类型, 默认 DOC(常见 DOC/SHEET, 大小写不敏感)
    - parent_id: 父目录 ID(可选, 缺省创建在根目录)

    返回要点: doc_id 与 url(若服务端返回)。
    线上冒烟待确认: body 字段名 name/docType/parentId、doc_type 取值与
    响应字段(docId/id/url)。
    """
    logger.info(f"创建文档: workspace={_clip(workspace_id, 64)}, name={_clip(name, 64)}")

    err = _check_config()
    if err:
        return err

    workspace = str(workspace_id or "").strip()
    if not workspace:
        return _dump({"success": False, "error": "workspace_id 必填"})
    if not str(name or "").strip():
        return _dump({"success": False, "error": "name 必填"})
    kind = str(doc_type or "").strip().upper()
    if not kind:
        return _dump({"success": False, "error": "doc_type 必填"})

    body: dict = {"name": str(name).strip(), "docType": kind}
    if str(parent_id or "").strip():
        body["parentId"] = str(parent_id).strip()

    try:
        result = _api("POST", f"/v1.0/doc/workspaces/{workspace}/docs", json_body=body)
        data = result.get("result") if isinstance(result.get("result"), dict) else result
        return _dump({"success": True,
                      "doc_id": data.get("docId") or data.get("id", ""),
                      "url": data.get("url") or data.get("docUrl", ""),
                      "message": "文档已创建"})
    except Exception as e:
        logger.error(f"创建文档异常: {e}")
        return _dump({"success": False, "error": str(e)})


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def dingtalk_doc_add_member(
    workspace_id: str,
    doc_id: str,
    dingtalk_userid: str,
    role: str = "READER",
):
    """为文档添加成员授权。

    参数:
    - workspace_id: 知识库/团队空间 ID(必填)
    - doc_id: 文档 ID(必填)
    - dingtalk_userid: 钉钉用户ID(不是工号, 也不是本系统 userId); 被授权用户 userId(必填)
    - role: 成员角色, 默认 READER(常见 READER/EDITOR/OWNER, 大小写不敏感)

    返回要点: success 与透传结果 result。
    线上冒烟待确认: 成员接口 body 形态与 role 取值; 当前按
    {"members":[{"memberId":...,"memberType":"USER","role":...}]} 提交,
    若线上要求扁平 {"userId":...,"role":...} 需按冒烟结果调整。
    """
    logger.info(f"添加文档成员: workspace={_clip(workspace_id, 64)}, doc={_clip(doc_id, 64)}")

    err = _check_config()
    if err:
        return err

    workspace = str(workspace_id or "").strip()
    if not workspace:
        return _dump({"success": False, "error": "workspace_id 必填"})
    doc = str(doc_id or "").strip()
    if not doc:
        return _dump({"success": False, "error": "doc_id 必填"})
    member = str(dingtalk_userid or "").strip()
    if not member:
        return _dump({"success": False, "error": "dingtalk_userid 必填"})
    member_role = str(role or "").strip().upper()
    if not member_role:
        return _dump({"success": False, "error": "role 必填"})

    body = {"members": [{"memberId": member, "memberType": "USER", "role": member_role}]}

    try:
        result = _api("POST", f"/v1.0/doc/workspaces/{workspace}/docs/{doc}/members",
                      json_body=body)
        return _dump({"success": True, "message": "文档成员已授权",
                      "role": member_role, "result": result})
    except Exception as e:
        logger.error(f"添加文档成员异常: {e}")
        return _dump({"success": False, "error": str(e)})


if __name__ == "__main__":
    logger.info("启动 DingTalk MCP Server")
    mcp.run()
