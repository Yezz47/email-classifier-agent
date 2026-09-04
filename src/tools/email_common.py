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
