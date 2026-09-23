import datetime
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.console import Console
from rich.logging import RichHandler

# 无密钥依赖: 不使用 env_guard(该守卫用于 PG_MCP_DSN/DB_PASSWORD 等密钥类 server)

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.time")

mcp = MCPServer("Rosiwit MCP Server")

DEFAULT_TIMEZONE = "Asia/Shanghai"
MAX_TIMEZONE_RESULTS = 50
WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _parse_timezone(name: str) -> tuple[ZoneInfo | None, str | None]:
    """解析 IANA 时区名, 返回 (ZoneInfo, None) 或 (None, 可读错误)。"""
    try:
        return ZoneInfo(str(name or "").strip()), None
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return None, (
            f"非法时区: {name!r}。请使用 IANA 时区名(如 Asia/Shanghai), 可用 list_timezones 按关键词查询"
        )


def _format_offset(offset: datetime.timedelta | None) -> str:
    """把 UTC 偏移格式化为 UTC+08:00 风格。"""
    total = int((offset or datetime.timedelta()).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _humanize_seconds(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts) or "0 分钟"


def _describe_moment(moment: datetime.datetime) -> dict:
    """把带时区的时间点整理为结构化字段。"""
    return {
        "iso": moment.isoformat(),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS[moment.weekday()],
        "utc_offset": _format_offset(moment.utcoffset()),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_current_time(timezone: str = DEFAULT_TIMEZONE) -> dict:
    """获取指定时区的当前时间(纯离线, 不触网)。

    参数:
    - timezone: IANA 时区名, 默认 Asia/Shanghai

    返回:
    - iso: ISO 8601 时间(带时区偏移)
    - date: 日期 YYYY-MM-DD
    - time: 时间 HH:MM:SS
    - weekday: 中文星期(如 "星期三")
    - timezone: 请求的时区名
    - utc_offset: UTC 偏移(如 "UTC+08:00")

    时区非法时返回 {"error": "可读原因"}, 不抛异常。
    """
    logger.info(f"获取当前时间: {timezone}")
    tz, error = _parse_timezone(timezone)
    if error:
        return {"error": error}
    payload = _describe_moment(datetime.datetime.now(tz))
    payload["timezone"] = timezone
    return payload


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def convert_time(value: str, from_timezone: str, to_timezone: str) -> dict:
    """把 ISO8601 时间从 from_timezone 换算到 to_timezone(纯离线)。

    参数:
    - value: ISO8601 时间, 含时区(2026-09-23T12:00:00+08:00 / ...Z)或不含(2026-09-23T12:00:00)
    - from_timezone: value 不含时区时的解释时区, 如 Asia/Shanghai
    - to_timezone: 目标时区, 如 America/New_York

    返回换算后的 iso/date/time/weekday/utc_offset、from_iso(实际解释出的时间)与
    note(两地时差说明, 已按换算时刻的夏令时规则计算)。
    时区或时间非法时返回 {"error": "可读原因"}, 不抛异常。
    """
    logger.info(f"时间换算: {value} {from_timezone} -> {to_timezone}")
    from_tz, error = _parse_timezone(from_timezone)
    if error:
        return {"error": error}
    to_tz, error = _parse_timezone(to_timezone)
    if error:
        return {"error": error}

    raw = str(value or "").strip()
    if not raw:
        return {"error": "value 不能为空, 需为 ISO8601 时间字符串(如 2026-09-23T12:00:00 或 2026-09-23T12:00:00+08:00)"}
    normalized = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return {"error": f"无法解析时间: {value!r}, 需为 ISO8601 格式(如 2026-09-23T12:00:00)"}

    has_timezone = parsed.tzinfo is not None
    if not has_timezone:
        parsed = parsed.replace(tzinfo=from_tz)
    converted = parsed.astimezone(to_tz)

    diff_seconds = int((converted.utcoffset() - parsed.utcoffset()).total_seconds())
    if diff_seconds == 0:
        note = f"{to_timezone} 与 {from_timezone} 当前无时差"
    else:
        direction = "快" if diff_seconds > 0 else "慢"
        note = f"{to_timezone} 比 {from_timezone} {direction} {_humanize_seconds(abs(diff_seconds))}"

    payload = _describe_moment(converted)
    payload.update({
        "input": value,
        "input_has_timezone": has_timezone,
        "from_timezone": from_timezone,
        "from_iso": parsed.isoformat(),
        "to_timezone": to_timezone,
        "note": note,
    })
    return payload


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def list_timezones(keyword: str = "") -> dict:
    """按关键词列出 IANA 时区名(纯离线, 最多 50 条)。

    参数:
    - keyword: 过滤关键词(不区分大小写, 匹配时区名子串), 留空返回全部的前 50 条

    返回 matched(匹配总数)/returned(实际返回数)/limit/timezones。
    """
    logger.info(f"列出时区: keyword={keyword!r}")
    key = str(keyword or "").strip().lower()
    names = sorted(available_timezones())
    if key:
        names = [name for name in names if key in name.lower()]
    returned = names[:MAX_TIMEZONE_RESULTS]
    return {
        "keyword": keyword,
        "matched": len(names),
        "returned": len(returned),
        "limit": MAX_TIMEZONE_RESULTS,
        "timezones": returned,
    }


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
