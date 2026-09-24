import os
import smtplib
import datetime
import logging
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
import pymysql
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.logging import RichHandler
from rich.console import Console

from env_guard import require_secret

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.mysql_query")

mcp = MCPServer("Rosiwit MCP Server")
_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

# 数据库口令由环境变量注入；改为**调用时**解析，保证缺密钥时服务仍可启动并列出工具，
# 真正查询时才报错（生产缺密钥/弱值仍在 require_secret 内拒绝）
def _db_config() -> dict:
    return {
        "host": os.getenv("DB_HOST", "192.168.31.45"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": require_secret("DB_PASSWORD", os.getenv("DB_PASSWORD", "")) or "",
        "database": os.getenv("DB_NAME", "rosiwit_erp_server"),
        "charset": "utf8mb4"
}

@mcp.tool(annotations=_READ_ANNOTATIONS)
def list_tables():
    """列出数据库中所有表"""
    logger.info("列出所有数据库表")
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor()
    cursor.execute("SHOW TABLES")
    tables = [t[0] for t in cursor.fetchall()]
    conn.close()
    logger.debug(f"找到 {len(tables)} 个表")
    return tables

@mcp.tool(annotations=_READ_ANNOTATIONS)
def describe_table(table_name: str):
    """获取表结构"""
    logger.info(f"获取表结构: {table_name}")
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(f"DESCRIBE `{table_name}`")
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"表 {table_name} 有 {len(result)} 个字段")
    return result

@mcp.tool(annotations=_READ_ANNOTATIONS)
def execute_query(query: str):
    """执行SQL查询（仅支持SELECT）"""
    query = query.strip()

    # 去掉开头的注释和空白，找到实际 SQL 起始位置
    import re
    # 反复去掉开头的单行注释(--)、多行注释(/* */)、空白和换行
    while True:
        query = query.strip()
        if query.startswith("--"):
            # 去掉单行注释
            query = re.sub(r'^--[^\n]*\n?', '', query, count=1).strip()
        elif query.startswith("/*"):
            # 去掉多行注释
            query = re.sub(r'^/\*.*?\*/', '', query, count=1, flags=re.DOTALL).strip()
        elif query.startswith("#"):
            # 去掉 MySQL 风格的单行注释
            query = re.sub(r'^#[^\n]*\n?', '', query, count=1).strip()
        else:
            break

    if not query.upper().startswith("SELECT"):
        logger.warning(f"拒绝非SELECT查询: {query[:50]}")
        return {"error": "只允许SELECT查询"}
    
    logger.info(f"执行查询: {query[:100]}...")
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(query)
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"查询返回 {len(result)} 行")
    return result

if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
