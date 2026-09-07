"""运行中会话登记（跨渠道，进程级）。

Web 侧的「运行中」登记散落在 WebServer 的 worker 池登记与内存流式状态里，
只覆盖 web 渠道。钉钉/飞书/webhook/定时等经 ``MessageRouter.route`` 触发的
执行（以及池关闭时的 web）在此做一次进程内「执行开始/结束」登记，使
「运行中」API 能跨渠道回答“哪个会话正在执行、何时开始、用什么模型”。

约定与 ``WebUserWorkerPool`` 一致：登记/注销/读取全部发生在单个事件循环
线程内（web 端点 / 渠道 handler / 池清理协程），直接改 dict 即可，无需加锁。
"""
from datetime import datetime

# session_id -> {tag, channel, started_at, model}
_runs: dict[str, dict] = {}


def register_run(
    session_id: str,
    channel: str,
    user_id: str = "",
    started_at: str = "",
    model: str = "",
) -> None:
    """登记一次执行开始。tag 取自 route 的归属 user_id（``{channel}:{uid}``）。"""
    if not session_id or not channel:
        return
    _runs[session_id] = {
        "tag": user_id or f"{channel}:admin",
        "channel": channel,
        "started_at": started_at or datetime.now().isoformat(),
        "model": model or "",
    }


def unregister_run(session_id: str) -> None:
    """执行结束，移除登记。幂等：不存在的登记直接忽略。"""
    _runs.pop(session_id, None)


def running_sessions() -> list[dict]:
    """当前运行中的会话集合视图（纯同步读，无副作用，便于注入测试状态）。

    每条含 session_id/tag/uid/channel/started_at/model；与
    ``WebUserWorkerPool.running_sessions()`` 输出对齐，便于 WebServer 合并。
    """
    out = []
    for sid in sorted(_runs.keys()):
        info = _runs[sid]
        tag = info.get("tag", "")
        uid = tag.split(":", 1)[1] if ":" in tag else tag
        out.append({
            "session_id": sid,
            "tag": tag,
            "uid": uid,
            "channel": info.get("channel", ""),
            "started_at": info.get("started_at", ""),
            "model": info.get("model", ""),
        })
    return out


def reset() -> None:
    """清空全部登记（测试隔离用）。"""
    _runs.clear()
