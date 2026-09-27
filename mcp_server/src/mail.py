"""邮件 MCP（SMTP 发送 + IMAP 收发）：常见邮件能力的连接器实现。

覆盖：发送 / 抄送 / 密送 / 附件；列文件夹；列邮件（按未读、日期、发件人、主题）；
IMAP 条件检索；读单封（正文 text/html + 附件清单）；下载附件；未读数；
标记已读/未读/旗标；移动 / 复制 / 删除；新建 / 删除文件夹；回复。

环境变量（本机 stdio 或平台注入；密钥不落日志）：
  SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / SMTP_FROM_NAME  —— 发送
  IMAP_HOST / IMAP_PORT（默认 imap.qiye.aliyun.com:993）                  —— 收取
  IMAP_USERNAME / IMAP_PASSWORD（缺省复用 SMTP_USERNAME / SMTP_PASSWORD）
  MAIL_ATTACH_DIR（下载附件默认目录）
"""
from __future__ import annotations

import email
import email.encoders
import email.header
import email.utils
import imaplib
import logging
import mimetypes
import os
import re
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)],
)
logger = logging.getLogger("mcp.mail")

mcp = MCPServer("Mail (SMTP/IMAP)")

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

DEFAULT_IMAP_HOST = "imap.qiye.aliyun.com"
DEFAULT_IMAP_PORT = 993
MAX_BODY_CHARS = 20000


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _smtp_cfg() -> dict[str, Any]:
    return {
        "host": _env("SMTP_HOST", "smtp.qiye.aliyun.com"),
        "port": int(_env("SMTP_PORT", "465") or "465"),
        "username": _env("SMTP_USERNAME"),
        "password": _env("SMTP_PASSWORD"),
        "from_name": _env("SMTP_FROM_NAME"),
    }


def _imap_cfg() -> dict[str, Any]:
    smtp = _smtp_cfg()
    return {
        "host": _env("IMAP_HOST", DEFAULT_IMAP_HOST),
        "port": int(_env("IMAP_PORT", str(DEFAULT_IMAP_PORT)) or str(DEFAULT_IMAP_PORT)),
        "username": _env("IMAP_USERNAME") or smtp["username"],
        "password": _env("IMAP_PASSWORD") or smtp["password"],
    }


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:  # noqa: BLE001
        return value
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", "replace")
        else:
            out += text
    return out


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name or "attachment").strip() or "attachment"
    return name[:150]


def _imap_date(value: str) -> str:
    """接受 YYYY-MM-DD / YYYY/MM/DD / DD-Mon-YYYY，输出 IMAP 需要的 DD-Mon-YYYY。"""
    value = (value or "").strip()
    if not value:
        raise ValueError("日期为空")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return value  # 已是 DD-Mon-YYYY 之类，原样透传


def _body_and_attachments(msg: email.message.Message) -> tuple[str, str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        filename = _decode(part.get_filename() or "")
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if "attachment" in disp or (filename and ctype not in ("text/plain", "text/html")):
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "content_type": ctype,
                    "size": len(payload),
                }
            )
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            content = payload.decode("utf-8", "replace")
        if ctype == "text/html":
            html_parts.append(content)
        else:
            text_parts.append(content)
    text = "\n".join(text_parts)
    html = "\n".join(html_parts)
    if not text and html:
        text = re.sub(r"<[^>]+>", " ", html)
    return text[:MAX_BODY_CHARS], html[:MAX_BODY_CHARS], attachments


def _connect_imap(readonly: bool = True, folder: str = "INBOX"):
    cfg = _imap_cfg()
    if not cfg["username"] or not cfg["password"]:
        raise RuntimeError("未配置 IMAP 凭据（IMAP_USERNAME/IMAP_PASSWORD 或 SMTP_USERNAME/SMTP_PASSWORD）")
    client = imaplib.IMAP4_SSL(cfg["host"], int(cfg["port"]))
    client.login(cfg["username"], cfg["password"])
    client.select(folder, readonly=readonly)
    return client


def _header_summary(imap: imaplib.IMAP4_SSL, uid: bytes) -> dict[str, Any]:
    typ, data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return {"uid": uid.decode("ascii", "replace")}
    header = email.message_from_bytes(data[0][1])
    return {
        "uid": uid.decode("ascii", "replace"),
        "from": _decode(header.get("From")),
        "to": _decode(header.get("To")),
        "subject": _decode(header.get("Subject")),
        "date": _decode(header.get("Date")),
    }


def _search_uids(imap: imaplib.IMAP4_SSL, criteria: list[str]) -> list[bytes]:
    typ, data = imap.uid("search", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


# ------------------------------------------------------------------ SMTP 发送


def _smtp_send(
    to_recipients: List[str],
    subject: str,
    body: str,
    is_html: bool = False,
    cc_recipients: Optional[List[str]] = None,
    bcc_recipients: Optional[List[str]] = None,
    attachments: Optional[List[str]] = None,
    reply_to: str = "",
) -> dict[str, Any]:
    cfg = _smtp_cfg()
    if not cfg["username"] or not cfg["password"]:
        return {"success": False, "error": "SMTP 未配置：请设置 SMTP_USERNAME / SMTP_PASSWORD"}
    to_recipients = [x.strip() for x in (to_recipients or []) if x and x.strip()]
    if not to_recipients:
        return {"success": False, "error": "收件人不能为空"}
    cc = [x.strip() for x in (cc_recipients or []) if x and x.strip()]
    bcc = [x.strip() for x in (bcc_recipients or []) if x and x.strip()]

    attachments = attachments or []
    if attachments:
        msg: email.message.Message = MIMEMultipart()
        msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))
        for path in attachments:
            if not os.path.isfile(path):
                return {"success": False, "error": f"附件不存在: {path}"}
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(path, "rb") as fh:
                part = MIMEBase(maintype, subtype)
                part.set_payload(fh.read())
            email.encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=("utf-8", "", os.path.basename(path))
            )
            msg.attach(part)
    else:
        msg = MIMEText(body, "html" if is_html else "plain", "utf-8")

    msg["From"] = (
        f"{email.header.Header(cfg['from_name'], 'utf-8').encode()} <{cfg['username']}>"
        if cfg["from_name"]
        else cfg["username"]
    )
    msg["To"] = ", ".join(to_recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)

    recipients = to_recipients + cc + bcc
    try:
        if int(cfg["port"]) == 465:
            with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), timeout=30) as server:
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["username"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["username"], recipients, msg.as_string())
    except Exception as exc:  # noqa: BLE001
        logger.error("发送邮件失败: %s", exc)
        return {"success": False, "error": str(exc)}
    logger.info("邮件已发送: %s", ", ".join(recipients))
    return {"success": True, "message": "邮件已发送", "to": recipients}


@mcp.tool(annotations=_WRITE)
def send_email(
    to_recipients: List[str],
    subject: str,
    body: str,
    is_html: bool = False,
    cc_recipients: Optional[List[str]] = None,
    bcc_recipients: Optional[List[str]] = None,
    attachments: Optional[List[str]] = None,
    reply_to: str = "",
):
    """发送邮件（SMTP）。

    参数:
    - to_recipients: 收件人邮箱列表
    - subject: 主题
    - body: 正文
    - is_html: 是否 HTML
    - cc_recipients / bcc_recipients: 抄送 / 密送（可选）
    - attachments: 本机附件文件路径列表（可选）
    - reply_to: 回复地址（可选）
    """
    return _smtp_send(
        to_recipients, subject, body, is_html, cc_recipients, bcc_recipients, attachments, reply_to
    )


# ------------------------------------------------------------------ IMAP 收取


@mcp.tool(annotations=_READ)
def list_folders():
    """列出邮箱所有文件夹（含收件箱、已发送、草稿等）。"""
    try:
        imap = _connect_imap()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    try:
        typ, data = imap.list()
        folders = []
        if typ == "OK":
            for line in data or []:
                if not line:
                    continue
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                m = re.search(r'"([^"]*)"\s*$', text)
                name = m.group(1) if m else text.split(" ")[-1].strip('"')
                folders.append(name)
        return {"success": True, "folders": folders}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_READ)
def list_messages(
    folder: str = "INBOX",
    limit: int = 20,
    unread_only: bool = False,
    since: str = "",
    before: str = "",
    sender: str = "",
    subject: str = "",
):
    """列出文件夹内最近的邮件（按 UID 倒序，即最新在前）。

    - limit: 返回条数上限（默认 20）
    - unread_only: 仅未读
    - since/before: 日期过滤（YYYY-MM-DD）
    - sender/subject: 发件人 / 主题包含匹配（IMAP 检索，建议英文关键词）
    """
    try:
        imap = _connect_imap(folder=folder)
        criteria = ["ALL"]
        if unread_only:
            criteria = ["UNSEEN"]
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if before:
            criteria += ["BEFORE", _imap_date(before)]
        if sender:
            criteria += ["FROM", sender]
        if subject:
            criteria += ["SUBJECT", subject]
        uids = _search_uids(imap, criteria)
        uids = uids[-max(1, int(limit)):][::-1]
        messages = [_header_summary(imap, uid) for uid in uids]
        return {"success": True, "folder": folder, "count": len(messages), "messages": messages}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_READ)
def search_messages(
    folder: str = "INBOX",
    from_addr: str = "",
    to_addr: str = "",
    subject: str = "",
    body: str = "",
    since: str = "",
    before: str = "",
    unseen: bool = False,
    seen: bool = False,
    flagged: bool = False,
    limit: int = 50,
):
    """按条件检索邮件（IMAP SEARCH），返回命中的邮件概要。"""
    try:
        imap = _connect_imap(folder=folder)
        criteria: list[str] = []
        if unseen:
            criteria.append("UNSEEN")
        if seen:
            criteria.append("SEEN")
        if flagged:
            criteria.append("FLAGGED")
        if from_addr:
            criteria += ["FROM", from_addr]
        if to_addr:
            criteria += ["TO", to_addr]
        if subject:
            criteria += ["SUBJECT", subject]
        if body:
            criteria += ["BODY", body]
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if before:
            criteria += ["BEFORE", _imap_date(before)]
        if not criteria:
            criteria = ["ALL"]
        uids = _search_uids(imap, criteria)
        uids = uids[-max(1, int(limit)):][::-1]
        messages = [_header_summary(imap, uid) for uid in uids]
        return {"success": True, "folder": folder, "count": len(messages), "messages": messages}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_READ)
def read_message(uid: str, folder: str = "INBOX", mark_seen: bool = False):
    """读取单封邮件：返回收件人/发件人/主题/日期、正文（text/html）与附件清单。

    uid 来自 list_messages / search_messages。mark_seen=True 时标记为已读。
    """
    try:
        imap = _connect_imap(folder=folder, readonly=not mark_seen)
        typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return {"success": False, "error": f"未找到邮件 uid={uid}"}
        msg = email.message_from_bytes(data[0][1])
        text, html, attachments = _body_and_attachments(msg)
        if mark_seen:
            imap.uid("store", str(uid), "+FLAGS", "(\\Seen)")
        return {
            "success": True,
            "uid": str(uid),
            "folder": folder,
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "cc": _decode(msg.get("Cc")),
            "subject": _decode(msg.get("Subject")),
            "date": _decode(msg.get("Date")),
            "text": text,
            "html": html,
            "attachments": attachments,
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def download_attachments(
    uid: str,
    folder: str = "INBOX",
    save_dir: str = "",
    attachment_index: int = -1,
):
    """下载某封邮件的附件到本机目录（默认 MAIL_ATTACH_DIR 或当前目录 _mail_attachments）。

    attachment_index >=0 时只下载指定序号的附件（序号见 read_message 的 attachments）。
    """
    out_dir = save_dir or _env("MAIL_ATTACH_DIR") or os.path.join(os.getcwd(), "_mail_attachments")
    try:
        os.makedirs(out_dir, exist_ok=True)
        imap = _connect_imap(folder=folder)
        typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return {"success": False, "error": f"未找到邮件 uid={uid}"}
        msg = email.message_from_bytes(data[0][1])
        saved = []
        idx = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            filename = _decode(part.get_filename() or "")
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if "attachment" not in disp and not (filename and part.get_content_type() not in ("text/plain", "text/html")):
                continue
            if attachment_index >= 0 and idx != attachment_index:
                idx += 1
                continue
            idx += 1
            name = _safe_name(filename)
            target = os.path.join(out_dir, name)
            counter = 1
            base, ext = os.path.splitext(target)
            while os.path.exists(target):
                target = f"{base}({counter}){ext}"
                counter += 1
            with open(target, "wb") as fh:
                fh.write(payload)
            saved.append(target)
        return {"success": True, "uid": str(uid), "saved": saved, "dir": out_dir}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_READ)
def get_unread_count(folder: str = "INBOX"):
    """获取文件夹未读邮件数。"""
    try:
        imap = _connect_imap(folder=folder)
        typ, data = imap.uid("search", None, "UNSEEN")
        count = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        return {"success": True, "folder": folder, "unread": count}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def set_message_flags(
    uid: str,
    folder: str = "INBOX",
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
):
    """设置邮件标记：add/remove 取值为 Seen / Flagged / Answered / Deleted（可带反斜杠）。"""
    def _norm(flags: Optional[List[str]]) -> str:
        items = []
        for f in flags or []:
            f = (f or "").strip()
            if not f:
                continue
            items.append(f if f.startswith("\\") else "\\" + f)
        return "(" + " ".join(items) + ")" if items else ""

    try:
        imap = _connect_imap(folder=folder, readonly=False)
        if add:
            imap.uid("store", str(uid), "+FLAGS", _norm(add))
        if remove:
            imap.uid("store", str(uid), "-FLAGS", _norm(remove))
        return {"success": True, "uid": str(uid), "folder": folder}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def move_message(uid: str, target_folder: str, folder: str = "INBOX"):
    """把邮件移动到目标文件夹。"""
    try:
        imap = _connect_imap(folder=folder, readonly=False)
        typ, data = imap.uid("copy", str(uid), target_folder)
        if typ != "OK":
            return {"success": False, "error": f"复制到 {target_folder} 失败: {data}"}
        imap.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        imap.expunge()
        return {"success": True, "uid": str(uid), "from": folder, "to": target_folder}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def copy_message(uid: str, target_folder: str, folder: str = "INBOX"):
    """把邮件复制到目标文件夹（保留原邮件）。"""
    try:
        imap = _connect_imap(folder=folder, readonly=False)
        typ, data = imap.uid("copy", str(uid), target_folder)
        if typ != "OK":
            return {"success": False, "error": f"复制到 {target_folder} 失败: {data}"}
        return {"success": True, "uid": str(uid), "from": folder, "to": target_folder}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_message(uid: str, folder: str = "INBOX", permanently: bool = False):
    """删除邮件。permanently=False 仅标记删除（可在客户端恢复）；True 立即彻底删除。"""
    try:
        imap = _connect_imap(folder=folder, readonly=False)
        imap.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        if permanently:
            imap.expunge()
        return {"success": True, "uid": str(uid), "folder": folder, "permanent": permanently}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def create_folder(name: str):
    """新建文件夹。"""
    try:
        imap = _connect_imap()
        typ, data = imap.create(name)
        return {"success": typ == "OK", "folder": name, "detail": str(data)}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_folder(name: str):
    """删除文件夹（内部邮件可能一并删除，谨慎）。"""
    try:
        imap = _connect_imap()
        typ, data = imap.delete(name)
        return {"success": typ == "OK", "folder": name, "detail": str(data)}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool(annotations=_WRITE)
def reply_email(
    uid: str,
    body: str,
    folder: str = "INBOX",
    is_html: bool = False,
    reply_all: bool = False,
):
    """回复某封邮件（自动带上 Re: 主题与收件人）。"""
    try:
        imap = _connect_imap(folder=folder)
        typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return {"success": False, "error": f"未找到邮件 uid={uid}"}
        header = email.message_from_bytes(data[0][1])
        subject = _decode(header.get("Subject"))
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        to = _decode(header.get("Reply-To") or header.get("From"))
        cc: list[str] = []
        if reply_all:
            addr = email.utils.getaddresses([header.get("To") or ""])
            cfg = _smtp_cfg()
            cc = [a for _, a in addr if a and a != cfg["username"]]
        return _smtp_send(
            to_recipients=[a.strip() for a in to.split(",") if a.strip()],
            subject=subject,
            body=body,
            is_html=is_html,
            cc_recipients=cc,
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    logger.info("启动 Mail MCP Server (SMTP/IMAP)")
    mcp.run()
