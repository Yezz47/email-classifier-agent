"""邮件发送工具 - 通过 SMTP 发送邮件"""

import json
import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid

from langchain.tools import tool
from cozeloop.decorator import observe
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

from tools.email_common import get_email_config

logger = logging.getLogger(__name__)


@observe
def _send_email_impl(
    to_addrs: str,
    subject: str,
    content: str,
    cc_addrs: str = "",
    content_type: str = "html",
) -> str:
    """邮件发送核心逻辑"""
    ctx = request_context.get() or new_context(method="send_email")
    try:
        config = get_email_config()

        to_list = [addr.strip() for addr in to_addrs.split(",") if addr.strip()]
        cc_list = [addr.strip() for addr in cc_addrs.split(",") if addr.strip()] if cc_addrs else []

        msg = MIMEText(content, content_type, "utf-8")
        msg["From"] = formataddr(("邮件管理助手", config["account"]))
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        all_recipients = to_list + cc_list
        if not all_recipients:
            return json.dumps(
                {"status": "error", "message": "收件人为空"},
                ensure_ascii=False,
            )

        ctx_ssl = ssl.create_default_context()
        ctx_ssl.minimum_version = ssl.TLSVersion.TLSv1_2

        attempts = 3
        last_err = None
        for i in range(attempts):
            try:
                with smtplib.SMTP_SSL(
                    config["smtp_server"],
                    config["smtp_port"],
                    context=ctx_ssl,
                    timeout=30,
                ) as server:
                    server.ehlo()
                    server.login(config["account"], config["auth_code"])
                    server.sendmail(config["account"], all_recipients, msg.as_string())
                    server.quit()
                return json.dumps({
                    "status": "success",
                    "message": f"邮件已成功发送给 {len(to_list)} 位收件人",
                    "recipient_count": len(to_list),
                }, ensure_ascii=False)
            except (
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPConnectError,
                smtplib.SMTPDataError,
                smtplib.SMTPHeloError,
                ssl.SSLError,
                OSError,
            ) as e:
                last_err = e
                logger.warning(f"SMTP 发送尝试 {i+1} 失败: {e}")
                time.sleep(2 * (i + 1))

        error_msg = f"发送失败: {str(last_err)}" if last_err else "发送失败: 未知错误"
        return json.dumps(
            {"status": "error", "message": error_msg},
            ensure_ascii=False,
        )

    except smtplib.SMTPAuthenticationError as e:
        return json.dumps(
            {"status": "error", "message": f"SMTP 认证失败: {str(e)}"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Send email error: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "message": f"发送邮件失败: {str(e)}"},
            ensure_ascii=False,
        )


@tool
def send_email(
    to_addrs: str,
    subject: str,
    content: str,
    cc_addrs: str = "",
    content_type: str = "html",
) -> str:
    """通过 SMTP 发送邮件。

    Args:
        to_addrs: 收件人邮箱地址，多个用逗号分隔，如 "user1@example.com,user2@example.com"
        subject: 邮件主题
        content: 邮件正文内容（支持 HTML 或纯文本）
        cc_addrs: 抄送邮箱地址，多个用逗号分隔，可选
        content_type: 内容类型，"html"（默认）或 "plain"

    Returns:
        JSON 格式的发送结果
    """
    return _send_email_impl(
        to_addrs=to_addrs,
        subject=subject,
        content=content,
        cc_addrs=cc_addrs,
        content_type=content_type,
    )
