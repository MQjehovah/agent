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

logger = logging.getLogger("mcp.default")

mcp = FastMCP("Rosiwit MCP Server")

SMTP_CONFIG = {
    "host": os.getenv("SMTP_HOST", "smtp.qiye.aliyun.com"),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "username": os.getenv("SMTP_USERNAME", "shuzizhongtai@xzrobot.com"),
    "password": os.getenv("SMTP_PASSWORD", "pm1Fw4y2pHMDTyMB"),
    "from_name": os.getenv("SMTP_FROM_NAME", "数字中台")
}

@mcp.tool()
def get_current_time():
    """获取服务器当前的本地时间。

    无需任何参数。返回一个包含多种常用格式的字典，便于不同场景使用：
    - datetime: 标准可读格式 "YYYY-MM-DD HH:MM:SS"
    - iso: ISO 8601 格式（带本地时区偏移，便于跨系统解析）
    - date: 仅日期 "YYYY-MM-DD"
    - time: 仅时间 "HH:MM:SS"
    - weekday: 星期几（中文，如 "星期三"）
    - timezone: 本地时区名称与 UTC 偏移（如 "UTC+08:00"）

    适用场景：日志记录、定时任务判断、报表时间戳、为用户展示当前时间等。
    注意：返回的是服务器所在系统的本地时间，非调用方时间。
    """
    logger.info("获取当前时间")

    now = datetime.datetime.now().astimezone()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0
    tz_str = f"UTC{'+' if offset_hours >= 0 else ''}{offset_hours:g}:00"

    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekdays[now.weekday()],
        "timezone": tz_str,
    }

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
        msg['From'] = f"{Header(SMTP_CONFIG['from_name'], 'utf-8').encode()} <{SMTP_CONFIG['username']}>"
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
