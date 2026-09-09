"""邮件公共模块 - 共享的邮件配置、IMAP连接、邮件解码等工具函数"""

import imaplib
import email
import email.message
import json
import re
import logging
from email.header import decode_header
from typing import Optional, Any

from coze_workload_identity import Client

logger = logging.getLogger(__name__)


def get_email_config() -> dict:
    """获取邮件配置信息（IMAP/SMTP 通用）"""
    client = Client()
    email_credential = client.get_integration_credential("integration-email-imap-smtp")
    return json.loads(email_credential)


def connect_imap(config: dict) -> imaplib.IMAP4_SSL:
    """建立 IMAP SSL 连接并登录"""
    imap_server = config["imap_server"]
    imap_port = int(config.get("imap_port", 993))
    account = config["account"]
    auth_code = config["auth_code"]

    conn = imaplib.IMAP4_SSL(imap_server, imap_port)
    conn.login(account, auth_code)
    return conn


def decode_header_value(value: Optional[str]) -> str:
    """解码邮件头字段（支持 RFC 2047 编码）"""
    if not value:
        return ""
    decoded_parts = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return " ".join(decoded_parts)


def extract_body(msg: email.message.Message, max_length: int = 1500) -> str:
    """提取邮件正文（纯文本优先，其次 HTML 去标签）"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                payload: Any = part.get_payload(decode=True)
                if payload and isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
            elif content_type == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload and isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    body = re.sub(r"<[^>]+>", " ", html)
                    body = re.sub(r"\s+", " ", body).strip()
    else:
        payload = msg.get_payload(decode=True)
        if payload and isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    if len(body) > max_length:
        body = body[:max_length] + "...[truncated]"
    return body.strip()


# Gmail 标签 → 期望的 IMAP 文件夹属性（根据属性动态解析，兼容不同界面语言的 modified UTF-7 命名）
_LABEL_TO_ATTR = {
    "important": "\\Important",
    "starred": "\\Flagged",
    "spam": "\\Junk",
    "trash": "\\Trash",
    "drafts": "\\Drafts",
    "sent": "\\Sent",
    "all_mail": "\\All",
}

# Gmail 标签 → 英文默认文件夹名（动态解析失败时的回退项）
_LABEL_TO_DEFAULT = {
    "important": "[Gmail]/Important",
    "starred": "[Gmail]/Starred",
    "spam": "[Gmail]/Spam",
    "trash": "[Gmail]/Trash",
    "drafts": "[Gmail]/Drafts",
    "sent": "[Gmail]/Sent Mail",
    "all_mail": "[Gmail]/All Mail",
}


def _parse_list_line(line: bytes):
    """解析 IMAP LIST 返回行，返回 (attributes_list, folder_name)。"""
    text = line.decode("utf-8", errors="replace").strip()
    m = re.match(r'\((?P<attrs>[^)]*)\)\s+("[^"]*"|\S+)\s+(?P<name>"[^"]*"|\S+)$', text)
    if not m:
        return [], text
    attrs = [a.strip() for a in m.group("attrs").split() if a.strip()]
    name = m.group("name")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return attrs, name


def _quoted_folder(name: str) -> str:
    """IMAP 文件夹名用引号包裹，兼容含空格/方括号等特殊字符的名称。"""
    if name.startswith('"') and name.endswith('"'):
        return name
    return f'"{name}"'


def resolve_folder(conn: imaplib.IMAP4_SSL, label: str) -> str:
    """将标签解析为当前账号实际可用的 IMAP 文件夹名（带引号）。

    支持标签：inbox / important / starred / spam / trash / drafts / sent / all_mail，
    也支持直接传入自定义文件夹名。优先按文件夹属性动态匹配（例如 Gmail 中文界面下
    文件夹名可能是 "[Gmail]/&YkBnCZCuTvY-"），自动兼容不同界面语言；找不到时回退英文默认名。
    """
    key = (label or "").lower()
    if key in ("", "inbox"):
        return "INBOX"
    want_attr = _LABEL_TO_ATTR.get(key)
    default = _LABEL_TO_DEFAULT.get(key, label)
    try:
        status, data = conn.list()
        if status == "OK":
            for item in data:
                attrs, name = _parse_list_line(item)
                if name.upper() == "INBOX" or "\\Noselect" in attrs:
                    continue
                if want_attr and want_attr in attrs:
                    return _quoted_folder(name)
    except Exception as e:  # noqa: BLE001
        logger.warning("解析邮箱文件夹失败，回退默认名: %s", e)
    return _quoted_folder(default)
