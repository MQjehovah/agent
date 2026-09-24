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

# 鏁版嵁搴撳彛浠ょ敱鐜鍙橀噺娉ㄥ叆锛涙敼涓?*璋冪敤鏃?*瑙ｆ瀽锛屼繚璇佺己瀵嗛挜鏃舵湇鍔′粛鍙惎鍔ㄥ苟鍒楀嚭宸ュ叿锛?
# 鐪熸鏌ヨ鏃舵墠鎶ラ敊锛堢敓浜х己瀵嗛挜/寮卞€间粛鍦?require_secret 鍐呮嫆缁濓級
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
    """鍒楀嚭鏁版嵁搴撲腑鎵€鏈夎〃"""
    logger.info("鍒楀嚭鎵€鏈夋暟鎹簱琛?)
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor()
    cursor.execute("SHOW TABLES")
    tables = [t[0] for t in cursor.fetchall()]
    conn.close()
    logger.debug(f"鎵惧埌 {len(tables)} 涓〃")
    return tables

@mcp.tool(annotations=_READ_ANNOTATIONS)
def describe_table(table_name: str):
    """鑾峰彇琛ㄧ粨鏋?""
    logger.info(f"鑾峰彇琛ㄧ粨鏋? {table_name}")
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(f"DESCRIBE `{table_name}`")
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"琛?{table_name} 鏈?{len(result)} 涓瓧娈?)
    return result

@mcp.tool(annotations=_READ_ANNOTATIONS)
def execute_query(query: str):
    """鎵цSQL鏌ヨ锛堜粎鏀寔SELECT锛?""
    query = query.strip()

    # 鍘绘帀寮€澶寸殑娉ㄩ噴鍜岀┖鐧斤紝鎵惧埌瀹為檯 SQL 璧峰浣嶇疆
    import re
    # 鍙嶅鍘绘帀寮€澶寸殑鍗曡娉ㄩ噴(--)銆佸琛屾敞閲?/* */)銆佺┖鐧藉拰鎹㈣
    while True:
        query = query.strip()
        if query.startswith("--"):
            # 鍘绘帀鍗曡娉ㄩ噴
            query = re.sub(r'^--[^\n]*\n?', '', query, count=1).strip()
        elif query.startswith("/*"):
            # 鍘绘帀澶氳娉ㄩ噴
            query = re.sub(r'^/\*.*?\*/', '', query, count=1, flags=re.DOTALL).strip()
        elif query.startswith("#"):
            # 鍘绘帀 MySQL 椋庢牸鐨勫崟琛屾敞閲?
            query = re.sub(r'^#[^\n]*\n?', '', query, count=1).strip()
        else:
            break

    if not query.upper().startswith("SELECT"):
        logger.warning(f"鎷掔粷闈濻ELECT鏌ヨ: {query[:50]}")
        return {"error": "鍙厑璁窼ELECT鏌ヨ"}
    
    logger.info(f"鎵ц鏌ヨ: {query[:100]}...")
    conn = pymysql.connect(**_db_config())
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute(query)
    result = cursor.fetchall()
    conn.close()
    logger.debug(f"鏌ヨ杩斿洖 {len(result)} 琛?)
    return result

if __name__ == "__main__":
    logger.info("鍚姩 MCP Server")
    mcp.run()
