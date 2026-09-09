"""邮件标记工具 - 通过 IMAP 协议对邮件进行标记、归档、已读等操作"""

import imaplib
import json
import logging
from typing import Optional

from langchain.tools import tool
from cozeloop.decorator import observe
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

from tools.email_common import get_email_config, connect_imap, resolve_folder

logger = logging.getLogger(__name__)


# Gmail 标签到 IMAP 文件夹的映射
GMAIL_LABEL_MAP = {
    "important": "[Gmail]/Important",
    "starred": "[Gmail]/Starred",
    "spam": "[Gmail]/Spam",
    "trash": "[Gmail]/Trash",
    "drafts": "[Gmail]/Drafts",
    "sent": "[Gmail]/Sent Mail",
    "all_mail": "[Gmail]/All Mail",
    "inbox": "INBOX",
}

# 标准邮件标记标志
FLAG_MAP = {
    "read": "\\Seen",
    "unread": "\\Seen",
    "flagged": "\\Flagged",
    "starred": "\\Flagged",
    "answered": "\\Answered",
    "deleted": "\\Deleted",
    "draft": "\\Draft",
}


@observe
def _label_email_impl(
    uid: str,
    action: str = "mark_read",
    folder: str = "INBOX",
    target_folder: str = "",
) -> str:
    """邮件标记核心逻辑"""
    ctx = request_context.get() or new_context(method="label_email")
    try:
        config = get_email_config()
        conn = connect_imap(config)

        try:
            # 选择文件夹（需要可写模式）：优先解析标签名，兼容中文界面下的 modified UTF-7 名称
            actual_folder = resolve_folder(conn, folder)
            status, _ = conn.select(actual_folder)
            if status != "OK":
                return json.dumps({
                    "status": "error",
                    "message": f"无法打开邮箱文件夹: {folder}"
                }, ensure_ascii=False)

            if action == "mark_read":
                # 标记为已读
                status, _ = conn.store(uid, "+FLAGS", "\\Seen")
                if status == "OK":
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为已读"
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "message": f"标记邮件 {uid} 为已读失败"
                    }, ensure_ascii=False)

            elif action == "mark_unread":
                # 标记为未读
                status, _ = conn.store(uid, "-FLAGS", "\\Seen")
                if status == "OK":
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为未读"
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "message": f"标记邮件 {uid} 为未读失败"
                    }, ensure_ascii=False)

            elif action == "mark_flagged":
                # 标记为重要/星标
                status, _ = conn.store(uid, "+FLAGS", "\\Flagged")
                if status == "OK":
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为重要（星标）"
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "message": f"标记邮件 {uid} 为重要失败"
                    }, ensure_ascii=False)

            elif action == "archive":
                # 归档：COPY 到 All Mail + DELETE from INBOX
                archive_folder = resolve_folder(conn, "all_mail")
                status, _ = conn.copy(uid, archive_folder)
                if status == "OK":
                    conn.store(uid, "+FLAGS", "\\Deleted")
                    conn.expunge()
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已归档"
                    }, ensure_ascii=False)
                else:
                    conn.store(uid, "+FLAGS", "\\Deleted")
                    conn.expunge()
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已从收件箱移除（归档）"
                    }, ensure_ascii=False)

            elif action == "move":
                # 移动到指定文件夹
                if not target_folder:
                    return json.dumps({
                        "status": "error",
                        "message": "移动邮件需要指定 target_folder 参数"
                    }, ensure_ascii=False)

                actual_folder = resolve_folder(conn, target_folder)

                status, _ = conn.copy(uid, actual_folder)
                if status == "OK":
                    conn.store(uid, "+FLAGS", "\\Deleted")
                    conn.expunge()
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已移动到 {actual_folder}"
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "message": f"移动邮件 {uid} 到 {actual_folder} 失败，文件夹可能不存在"
                    }, ensure_ascii=False)

            elif action == "mark_important":
                # 标记为重要（Gmail 特有）
                important_folder = resolve_folder(conn, "important")
                status, _ = conn.copy(uid, important_folder)
                if status == "OK":
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为重要"
                    }, ensure_ascii=False)
                else:
                    conn.store(uid, "+FLAGS", "\\Flagged")
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为星标（Important 文件夹不可用）"
                    }, ensure_ascii=False)

            else:
                return json.dumps({
                    "status": "error",
                    "message": f"不支持的操作: {action}。支持的操作: mark_read, mark_unread, mark_flagged, archive, move, mark_important"
                }, ensure_ascii=False)

        finally:
            conn.logout()

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP error: {e}")
        return json.dumps({
            "status": "error",
            "message": f"邮箱连接错误: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Label email error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "message": f"标记邮件失败: {str(e)}"
        }, ensure_ascii=False)


@tool
def label_email(
    uid: str,
    action: str = "mark_read",
    folder: str = "INBOX",
    target_folder: str = "",
) -> str:
    """对指定邮件执行标记操作（已读/未读/星标/归档/移动/重要）。

    Args:
        uid: 邮件的唯一标识符（从 fetch_emails 返回的 uid 字段获取）
        action: 要执行的操作，可选值:
            - "mark_read": 标记为已读（默认）
            - "mark_unread": 标记为未读
            - "mark_flagged": 标记为星标/重要
            - "archive": 归档邮件
            - "move": 移动到指定文件夹（需配合 target_folder）
            - "mark_important": 标记为重要
        folder: 邮件当前所在文件夹，默认 "INBOX"
        target_folder: 目标文件夹（仅 action="move" 时需要）。支持: "important", "starred", "spam", "trash", "inbox", 或自定义文件夹名

    Returns:
        JSON 格式的操作结果
    """
    return _label_email_impl(
        uid=uid,
        action=action,
        folder=folder,
        target_folder=target_folder,
    )
