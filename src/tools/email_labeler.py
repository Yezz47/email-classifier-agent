"""邮件标记工具 - 通过 IMAP 协议对邮件进行标记、归档、已读等操作"""

import imaplib
import json
import logging
from typing import Optional

from langchain.tools import tool
from coze_workload_identity import Client
from cozeloop.decorator import observe
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

logger = logging.getLogger(__name__)


def _get_email_config() -> dict:
    """获取邮件配置信息"""
    client = Client()
    email_credential = client.get_integration_credential("integration-email-imap-smtp")
    return json.loads(email_credential)


def _connect_imap(config: dict) -> imaplib.IMAP4_SSL:
    """建立 IMAP SSL 连接并登录"""
    imap_server = config["imap_server"]
    imap_port = int(config.get("imap_port", 993))
    account = config["account"]
    auth_code = config["auth_code"]

    conn = imaplib.IMAP4_SSL(imap_server, imap_port)
    conn.login(account, auth_code)
    return conn


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
        config = _get_email_config()
        conn = _connect_imap(config)

        try:
            # 选择文件夹（需要可写模式）
            status, _ = conn.select(folder)
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
                # 归档：通过删除 \\Seen 标志并 COPY 到 All Mail，然后标记删除
                # Gmail 的归档逻辑：从 INBOX 移走即可（COPY 到 [Gmail]/All Mail + DELETE from INBOX）
                archive_folder = "[Gmail]/All Mail"
                # 先尝试 COPY
                status, _ = conn.copy(uid, archive_folder)
                if status == "OK":
                    # 标记为从当前文件夹删除
                    conn.store(uid, "+FLAGS", "\\Deleted")
                    conn.expunge()
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已归档"
                    }, ensure_ascii=False)
                else:
                    # 如果不支持 COPY，尝试直接标记删除
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

                # 支持 Gmail 标签映射
                actual_folder = GMAIL_LABEL_MAP.get(target_folder.lower(), target_folder)

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
                important_folder = "[Gmail]/Important"
                status, _ = conn.copy(uid, important_folder)
                if status == "OK":
                    return json.dumps({
                        "status": "success",
                        "message": f"邮件 {uid} 已标记为重要"
                    }, ensure_ascii=False)
                else:
                    # 退而使用 Flagged 标志
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
