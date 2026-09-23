import logging
import os
import re
from typing import Any

import env_guard
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

logger = logging.getLogger("mcp.postgres")

mcp = MCPServer("Rosiwit MCP Server")

DEFAULT_STATEMENT_TIMEOUT_MS = 10000
DEFAULT_MAX_ROWS = 200
MAX_MAX_ROWS = 1000
DSN_ENV = "PG_MCP_DSN"
DSN_PLACEHOLDER = "${PG_MCP_DSN}"
DSN_NOT_CONFIGURED = (
    "未配置连接串: 请设置环境变量 PG_MCP_DSN(如 postgresql://user:password@host:5432/dbname)后重启服务"
)

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

_ALLOWED_PREFIXES = frozenset({"select", "with", "explain", "show", "values"})

# 写操作/DDL/DCL/会话控制关键字: 在去掉注释与字符串后按整词匹配;
# 既拦截 WITH ... INSERT/UPDATE/DELETE 数据修改型 CTE, 也为只读事务兜底(纵深防御)
_FORBIDDEN_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "create", "alter", "drop", "truncate",
    "grant", "revoke", "copy", "call", "do", "vacuum", "reindex", "cluster",
    "discard", "lock", "checkpoint", "refresh", "set", "reset", "begin", "commit",
    "rollback", "savepoint", "release", "prepare", "deallocate", "execute",
    "declare", "fetch", "move", "close", "listen", "notify", "unlisten",
    "attach", "detach", "import", "into",
})

_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class PgError(Exception):
    """Postgres 只读查询失败, message 为可读中文原因。"""


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _dsn() -> str:
    """连接串 PG_MCP_DSN; 缺失/占位符/生产弱值均视为未配置(工具层可读文案)。

    DSN 含口令, 走 env_guard.require_secret 的分级语义: 生产弱值/默认值抛错(此处捕获为
    「未配置连接串」, 不崩溃), 开发仅告警放行。
    """
    value = os.getenv(DSN_ENV, "").strip()
    if not value or value == DSN_PLACEHOLDER:
        return ""
    try:
        env_guard.require_secret(DSN_ENV, value)
    except RuntimeError as exc:
        logger.error(f"PG_MCP_DSN 校验失败: {exc}")
        return ""
    return value


def _statement_timeout_ms() -> int:
    """语句超时(毫秒), 环境变量 PG_MCP_STATEMENT_TIMEOUT_MS, 默认 10000。"""
    return _env_int("PG_MCP_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS, minimum=100)


def _max_rows() -> int:
    """返回行数上限, 环境变量 PG_MCP_MAX_ROWS(默认 200, 硬上限 1000)。"""
    return min(_env_int("PG_MCP_MAX_ROWS", DEFAULT_MAX_ROWS), MAX_MAX_ROWS)


def _connect_options() -> str:
    """连接级强制参数: 只读事务 + 语句超时(psycopg options)。"""
    return f"-c default_transaction_read_only=on -c statement_timeout={_statement_timeout_ms()}"


def clamp_limit(limit: int, max_rows: int) -> int:
    """把请求行数夹紧到 [1, max_rows]: limit<=0/非法值取 max_rows, 超过则取 max_rows。"""
    try:
        cap = int(max_rows)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_ROWS
    cap = max(1, cap)
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return cap
    return min(value, cap)


def _scan_sql(sql: str) -> tuple[str, str] | None:
    """单遍扫描 SQL, 返回 (去注释原文, 掩码文本); 非法/未闭合时返回 None。

    - 去掉 `--` 行注释与 `/* */` 块注释(PostgreSQL 块注释支持嵌套, 同样按嵌套处理);
    - 掩码文本在去注释基础上, 把 '...' 字符串、$tag$...$tag$ 美元引用、"..."
      带引号标识符的内容替换为空格, 供关键字与分号判定使用(避免内容误判);
    - 字符串/注释未闭合时返回 None(直接拒绝, 交由数据库报语法错更不安全)。
    """
    cleaned: list[str] = []
    masked: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline == -1:
                index = length
            else:
                cleaned.append("\n")
                masked.append("\n")
                index = newline + 1
            continue
        if char == "/" and sql.startswith("/*", index):
            cursor = index
            depth = 0
            while cursor < length:
                if sql.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif sql.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                    if depth == 0:
                        break
                else:
                    cursor += 1
            if depth != 0:
                return None
            cleaned.append(" ")
            masked.append(" ")
            index = cursor
            continue
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if sql[cursor] == "'":
                    if sql.startswith("''", cursor):
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            else:
                return None
            cleaned.append(sql[index:cursor])
            masked.append(" ")
            index = cursor
            continue
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if sql[cursor] == '"':
                    if sql.startswith('""', cursor):
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            else:
                return None
            cleaned.append(sql[index:cursor])
            masked.append(" ")
            index = cursor
            continue
        if char == "$":
            match = _DOLLAR_TAG_RE.match(sql, index)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end == -1:
                    return None
                cursor = end + len(tag)
                cleaned.append(sql[index:cursor])
                masked.append(" ")
                index = cursor
                continue
        cleaned.append(char)
        masked.append(char)
        index += 1
    return "".join(cleaned), "".join(masked)


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    """纯函数: 判断 SQL 是否为允许的单条只读语句, 返回 (ok, 可读原因)。

    - 仅允许 SELECT/WITH/EXPLAIN/SHOW/VALUES 开头;
    - 注释与字符串/美元引用/带引号标识符内的内容不会造成误判或绕过;
    - 禁止多条语句(首个分号后仍有非空白内容即拒);
    - 禁止写操作关键字(含 WITH ... INSERT/UPDATE/DELETE 数据修改型 CTE 与 SELECT INTO)。
    """
    raw = str(sql or "").strip()
    if not raw:
        return False, "SQL 不能为空"
    scanned = _scan_sql(raw)
    if scanned is None:
        return False, "SQL 含未闭合的字符串/注释, 拒绝执行"
    masked = scanned[1].strip()
    if not masked:
        return False, "SQL 不能为空"
    first = re.match(r"[A-Za-z_][A-Za-z0-9_]*", masked)
    keyword = first.group(0).lower() if first else ""
    if keyword not in _ALLOWED_PREFIXES:
        return False, f"仅允许 SELECT/WITH/EXPLAIN/SHOW/VALUES 开头的只读语句, 当前: {keyword or '(无法识别)'}"
    head, separator, tail = masked.partition(";")
    if separator and tail.strip():
        return False, "禁止一次执行多条语句: 分号后仍有内容"
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head.lower()))
    banned = sorted(words & _FORBIDDEN_KEYWORDS)
    if banned:
        return False, f"只读查询禁止包含写操作关键字: {', '.join(banned)}"
    return True, ""


def _require_identifier(value: str, label: str, default: str = "") -> str:
    text = str(value or "").strip() or default
    if not text:
        raise PgError(f"{label} 不能为空")
    if not _IDENTIFIER_RE.match(text):
        raise PgError(f"非法{label}: 仅允许字母/数字/下划线/$ 且不以数字开头: {text!r}")
    return text


def _jsonable(value: Any) -> Any:
    """把 psycopg 返回值转为可 JSON 序列化的形式(Decimal/日期等转字符串)。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _connect(dsn: str):
    """建立只读连接; 测试可 monkeypatch 本函数注入假连接, 不真连 DB。"""
    try:
        import psycopg
    except ImportError as exc:
        raise PgError('缺少依赖 psycopg: 请安装 "psycopg[binary]>=3.1" 后重启服务') from exc
    return psycopg.connect(dsn, options=_connect_options())


def _execute_rows(conn, sql: str, params: tuple | None, cap: int) -> tuple[list[str], list[list[Any]], bool]:
    """执行查询并最多取 cap+1 行, 返回 (列名, 行, 是否截断)。"""
    with conn, conn.cursor() as cur:
        cur.execute(sql, params)
        description = cur.description
        columns = [getattr(column, "name", str(column)) for column in (description or [])]
        fetched = cur.fetchmany(cap + 1) if description is not None else []
    truncated = len(fetched) > cap
    rows = [[_jsonable(value) for value in row] for row in fetched[:cap]]
    return columns, rows, truncated


def _query_error(exc: Exception) -> dict:
    logger.error(f"查询失败: {type(exc).__name__}: {exc}")
    return {"ok": False, "error": f"查询失败: {type(exc).__name__}: {exc}"}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def pg_query(sql: str, limit: int = 0) -> dict:
    """执行单条只读 SQL 查询, 返回列名与行数据(只读, 会连数据库)。

    安全约束:
    - 仅允许单条 SELECT/WITH/EXPLAIN/SHOW/VALUES(注释/字符串安全剥离后判定),
      禁止分号拼接多语句与 INSERT/UPDATE/DELETE/DDL 等写操作关键字;
    - 连接强制 default_transaction_read_only=on 与 statement_timeout;
    - 返回行数上限 PG_MCP_MAX_ROWS(默认 200, 上限 1000), limit 只能把上限调得更小。

    参数:
    - sql: 单条只读 SQL
    - limit: 期望返回的最大行数, <=0 表示使用 PG_MCP_MAX_ROWS

    返回 ok/columns/rows/row_count/truncated/limit; 未配置 PG_MCP_DSN、
    SQL 非只读或执行失败时返回 {"ok": false, "error": "可读原因"}, 不抛异常。
    """
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": DSN_NOT_CONFIGURED}
    ok, reason = is_read_only_sql(sql)
    if not ok:
        logger.warning(f"拒绝非只读 SQL: {reason}")
        return {"ok": False, "error": reason}
    cap = clamp_limit(limit, _max_rows())
    logger.info(f"pg_query: {str(sql or '').strip()[:120]!r} limit={cap}")
    try:
        conn = _connect(dsn)
        columns, rows, truncated = _execute_rows(conn, str(sql or "").strip(), None, cap)
    except PgError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _query_error(exc)
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "limit": cap,
    }


@mcp.tool(annotations=_READ_ANNOTATIONS)
def pg_list_tables(schema: str = "public") -> dict:
    """列出指定 schema 下的表/视图(只读)。

    参数:
    - schema: schema 名(仅字母/数字/下划线/$), 默认 public

    返回 ok/schema/columns(table_schema/table_name/table_type)/rows/row_count/truncated。
    未配置 PG_MCP_DSN 或执行失败返回 {"ok": false, "error": "..."}。
    """
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": DSN_NOT_CONFIGURED}
    try:
        schema_name = _require_identifier(schema, "schema", default="public")
    except PgError as exc:
        return {"ok": False, "error": str(exc)}
    logger.info(f"pg_list_tables: schema={schema_name}")
    sql = (
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name"
    )
    try:
        conn = _connect(dsn)
        columns, rows, truncated = _execute_rows(conn, sql, (schema_name,), _max_rows())
    except PgError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _query_error(exc)
    return {
        "ok": True,
        "schema": schema_name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


@mcp.tool(annotations=_READ_ANNOTATIONS)
def pg_describe_table(table: str, schema: str = "public") -> dict:
    """查看表结构(只读)。

    参数:
    - table: 表名(仅字母/数字/下划线/$)
    - schema: schema 名, 默认 public

    返回 ok/schema/table/columns(column_name/data_type/is_nullable/column_default)/
    rows/row_count/truncated。未配置 PG_MCP_DSN 或执行失败返回 {"ok": false, "error": "..."}。
    """
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": DSN_NOT_CONFIGURED}
    try:
        schema_name = _require_identifier(schema, "schema", default="public")
        table_name = _require_identifier(table, "table")
    except PgError as exc:
        return {"ok": False, "error": str(exc)}
    logger.info(f"pg_describe_table: {schema_name}.{table_name}")
    sql = (
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
    )
    try:
        conn = _connect(dsn)
        columns, rows, truncated = _execute_rows(conn, sql, (schema_name, table_name), _max_rows())
    except PgError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _query_error(exc)
    return {
        "ok": True,
        "schema": schema_name,
        "table": table_name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
