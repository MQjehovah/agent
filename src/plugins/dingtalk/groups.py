"""钉钉群目录: 群消息触发时回写 (cid, 群名, robot_code, 最近触发人, last_active)。

共享文件路径由环境变量 ``DINGTALK_GROUPS_FILE`` 指定(默认
``/app/shared/dingtalk_groups.json``); 部署时 agent 侧 rw 挂载、market 侧 ro
挂载同一宿主目录。写出统一「tmp + os.replace」原子替换; 读取对缺失/空/损坏/
BOM 一律按空目录容错; 最多保留按 last_active 最新 ``MAX_GROUPS`` 个群。

文件格式(版本 1):
    {"version": 1, "groups": {"cidXXXX": {"name": "群名", "robot_code": "...",
      "sender": "最近触发人", "last_active": "2026-09-29T13:00:00+08:00"}}}
"""
import contextlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("plugin.dingtalk.groups")

DEFAULT_GROUPS_FILE = "/app/shared/dingtalk_groups.json"
GROUPS_FILE_ENV = "DINGTALK_GROUPS_FILE"
GROUPS_VERSION = 1
MAX_GROUPS = 200

_CST = timezone(timedelta(hours=8))
# 进程内串行: 并发 upsert 时防丢更新(tmp+replace 只保证单次写原子, 不保证读改写串行)
_GROUPS_LOCK = threading.Lock()


def groups_file_path() -> str:
    """群目录文件路径: 环境变量优先, 缺省 ``/app/shared/dingtalk_groups.json``。"""
    return os.environ.get(GROUPS_FILE_ENV, "").strip() or DEFAULT_GROUPS_FILE


def _now_iso() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def load_groups(path: str = "") -> dict:
    """读取群目录; 缺失/空/损坏/BOM 一律按空目录返回(不抛异常)。

    返回 ``{"version": 1, "groups": {cid: entry}}``; entry 非 dict 的条目丢弃。
    """
    target = path or groups_file_path()
    try:
        with open(target, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return {"version": GROUPS_VERSION, "groups": {}}
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        return {"version": GROUPS_VERSION, "groups": {}}
    groups = {str(cid): entry for cid, entry in data["groups"].items()
              if isinstance(entry, dict)}
    return {"version": GROUPS_VERSION, "groups": groups}


def _prune_groups(groups: dict, limit: int = MAX_GROUPS) -> dict:
    """仅保留 last_active 最新的 limit 个群(空 last_active 视为最旧)。"""
    if len(groups) <= limit:
        return groups
    ordered = sorted(
        groups.items(),
        key=lambda item: str(item[1].get("last_active") or ""),
        reverse=True,
    )
    return dict(ordered[:limit])


def _atomic_write(path: str, payload: dict) -> None:
    """tmp + os.replace 原子替换; 失败清理 tmp 后抛出。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".dingtalk_groups_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    # mkstemp 产物默认 0600, 改为 0644 供 market 容器(不同 UID, ro 挂载)读取;
    # chmod 语义受限平台(如 Windows)失败不影响写入结果, 仅忽略。
    with contextlib.suppress(Exception):
        os.chmod(path, 0o644)


def upsert_group_entry(path: str, cid: str, name: str,
                       robot_code: str = "", sender: str = "") -> bool:
    """新增/更新一个群目录条目(同步, 进程内加锁)。

    - cid 为空直接返回 False; 群名传入空串时保留原值(避免 SDK 无标题时清空);
    - last_active 更新为当前北京时间(秒级 ISO8601);
    - 失败仅告警返回 False, 不抛异常(调用方据此打一条 WARNING 即可)。
    """
    cid = str(cid or "").strip()
    if not cid:
        return False
    target = path or groups_file_path()
    try:
        with _GROUPS_LOCK:
            groups = load_groups(target)["groups"]
            entry = groups.get(cid)
            if not isinstance(entry, dict):
                entry = {}
            if name:
                entry["name"] = str(name)
            else:
                entry.setdefault("name", "")
            entry["robot_code"] = str(robot_code or "")
            entry["sender"] = str(sender or "")
            entry["last_active"] = _now_iso()
            groups[cid] = entry
            _atomic_write(target, {
                "version": GROUPS_VERSION,
                "groups": _prune_groups(groups),
            })
        return True
    except Exception as e:
        logger.warning(f"回写钉钉群目录失败(path={target}, cid={cid}): {e!r}")
        return False
