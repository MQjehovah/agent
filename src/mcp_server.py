import os
import smtplib
import datetime
import logging
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

logger = logging.getLogger("mcp_server")

mcp = FastMCP("Rosiwit MCP Server")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "192.168.31.8"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "xzyz2022!"),
    "database": os.getenv("DB_NAME", "rosiwit_erp_server"),
    "charset": "utf8mb4"
}

SMTP_CONFIG = {
    "host": os.getenv("SMTP_HOST", "smtp.qiye.aliyun.com"),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "username": os.getenv("SMTP_USERNAME", "shuzizhongtai@xzrobot.com"),
    "password": os.getenv("SMTP_PASSWORD", "pm1Fw4y2pHMDTyMB"),
    "from_name": os.getenv("SMTP_FROM_NAME", "数字中台")
}

@mcp.tool()
def get_current_time():
    """获取当前时间"""
    logger.info("获取当前时间")

    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

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


@mcp.tool()
def send_email(
    to_recipients: List[str],
    subject: str,
    body: str,
    is_html: bool = False,
    cc_recipients: Optional[List[str]] = None
):
    """发送邮件
    
    参数:
    - to_recipients: 收件人邮箱列表
    - subject: 邮件主题
    - body: 邮件内容
    - is_html: 是否为HTML格式
    - cc_recipients: 抄送人邮箱列表（可选）
    """
    logger.info(f"发送邮件 - 主题: {subject}, 收件人: {to_recipients}")
    
    if not SMTP_CONFIG["username"] or not SMTP_CONFIG["password"]:
        logger.error("SMTP 未配置")
        return {"success": False, "error": "SMTP 未配置，请设置环境变量 SMTP_USERNAME 和 SMTP_PASSWORD"}
    
    if not to_recipients:
        logger.warning("收件人为空")
        return {"success": False, "error": "收件人不能为空"}
    
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = f"{SMTP_CONFIG['from_name']} <{SMTP_CONFIG['username']}>"
        msg['To'] = ', '.join(to_recipients)
        msg['Subject'] = subject
        
        if cc_recipients:
            msg['Cc'] = ', '.join(cc_recipients)
        
        mime_type = 'html' if is_html else 'plain'
        msg.attach(MIMEText(body, mime_type, 'utf-8'))
        
        all_recipients = to_recipients + (cc_recipients or [])
        
        if SMTP_CONFIG["port"] == 465:
            with smtplib.SMTP_SSL(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
                server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                server.sendmail(SMTP_CONFIG["username"], all_recipients, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
                server.starttls()
                server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                server.sendmail(SMTP_CONFIG["username"], all_recipients, msg.as_string())
        
        logger.info(f"邮件发送成功: {', '.join(to_recipients)}")
        return {
            "success": True, 
            "message": f"邮件已发送到: {', '.join(to_recipients)}"
        }
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
