import os
import smtplib
import datetime
import logging
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
import pymysql
from mcp.server.fastmcp import FastMCP
from rich.logging import RichHandler
from rich.console import Console

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.mysql_query")

mcp = FastMCP("Rosiwit MCP Server")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "192.168.31.45"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "xzyz2022!"),
    "database": os.getenv("DB_NAME", "rosiwit_erp_server"),
    "charset": "utf8mb4"
}

@mcp.tool()
def list_tables():
    """列出数据库中所有表"""
    logger.info("列出所有数据库表")
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SHOW TABLES")
    tables = [t[0] for t in cursor.fetchall()]
    conn.close()
    logger.debug(f"找到 {len(tables)} 个表")
    return tables

@mcp.tool()
def describe_table(table_name: str):
    """获取表结构"""
    logger.info(f"获取表结构: {table_name}")
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(f"DESCRIBE `{table_name}`")
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"表 {table_name} 有 {len(result)} 个字段")
    return result

@mcp.tool()
def execute_query(query: str):
    """执行SQL查询（仅支持SELECT）"""
    query = query.strip()
    if not query.upper().startswith("SELECT"):
        logger.warning("拒绝非SELECT查询")
        return {"error": "只允许SELECT查询"}
    
    logger.info(f"执行查询: {query[:100]}...")
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(query)
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"查询返回 {len(result)} 行")
    return result

if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
