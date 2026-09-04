"""邮件获取工具 - 通过 IMAP 协议从邮箱获取邮件列表"""

import imaplib
import email
import email.message
import json
import logging
from typing import Optional

from langchain.tools import tool
from cozeloop.decorator import observe
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

from tools.email_common import get_email_config, connect_imap, decode_header_value, extract_body

logger = logging.getLogger(__name__)


@observe
def _fetch_emails_impl(
    folder: str = "INBOX",
    limit: int = 10,
    unread_only: bool = False,
    search_from: str = "",
    search_subject: str = "",
) -> str:
    """邮件获取核心逻辑"""
    ctx = request_context.get() or new_context(method="fetch_emails")
    try:
        config = get_email_config()
        conn = connect_imap(config)

        try:
            # 选择邮箱文件夹
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return json.dumps({
                    "status": "error",
                    "message": f"无法打开邮箱文件夹: {folder}"
                }, ensure_ascii=False)

            # 构建搜索条件
            criteria = []
            if unread_only:
                criteria.append("UNSEEN")
            if search_from:
                criteria.append(f'FROM "{search_from}"')
            if search_subject:
                criteria.append(f'SUBJECT "{search_subject}"')

            search_query = " ".join(criteria) if criteria else "ALL"
            status, data = conn.search(None, search_query)
            if status != "OK":
                return json.dumps({
                    "status": "error",
                    "message": "邮件搜索失败"
                }, ensure_ascii=False)

            mail_ids = data[0].split()
            if not mail_ids:
                return json.dumps({
                    "status": "success",
                    "message": "没有找到符合条件的邮件",
                    "emails": [],
                    "total_count": 0
                }, ensure_ascii=False)

            # 取最新的 N 封（倒序）
            latest_ids = mail_ids[-limit:]
            latest_ids.reverse()

            emails = []
            for mid in latest_ids:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = decode_header_value(msg.get("Subject", ""))
                from_addr = decode_header_value(msg.get("From", ""))
                to_addr = decode_header_value(msg.get("To", ""))
                date_str = msg.get("Date", "")
                body = extract_body(msg)

                # 获取邮件 UID 用于后续标记
                uid = mid.decode() if isinstance(mid, bytes) else str(mid)

                emails.append({
                    "uid": uid,
                    "from": from_addr,
                    "to": to_addr,
                    "subject": subject,
                    "date": date_str,
                    "body_preview": body[:500] if body else "(无正文)"
                })

            return json.dumps({
                "status": "success",
                "folder": folder,
                "total_count": len(emails),
                "emails": emails
            }, ensure_ascii=False, indent=2)

        finally:
            conn.logout()

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP error: {e}")
        return json.dumps({
            "status": "error",
            "message": f"邮箱连接错误: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Fetch emails error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "message": f"获取邮件失败: {str(e)}"
        }, ensure_ascii=False)


@tool
def fetch_emails(
    folder: str = "INBOX",
    limit: int = 10,
    unread_only: bool = False,
    search_from: str = "",
    search_subject: str = "",
) -> str:
    """从邮箱收件箱获取最近邮件列表。

    Args:
        folder: 邮箱文件夹名称，默认 "INBOX"（收件箱）。常见值: "INBOX"(收件箱), "Sent"(已发送), "Drafts"(草稿), "[Gmail]/Spam"(垃圾邮件)
        limit: 获取邮件数量上限，默认 10，最大 50
        unread_only: 是否只获取未读邮件，默认 False
        search_from: 按发件人筛选（模糊匹配），为空则不筛选
        search_subject: 按邮件主题筛选（模糊匹配），为空则不筛选

    Returns:
        JSON 格式的邮件列表，包含每封邮件的 uid、发件人、收件人、主题、日期和正文预览
    """
    if limit > 50:
        limit = 50
    if limit < 1:
        limit = 1
    return _fetch_emails_impl(
        folder=folder,
        limit=limit,
        unread_only=unread_only,
        search_from=search_from,
        search_subject=search_subject,
    )
