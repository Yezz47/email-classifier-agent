"""重要邮件日历提醒编排模块

在原有邮件分类流程之后，仅对重要/需行动邮件执行：
  提取时间 → 判断置信度与影响 → 冲突/重复检查 → 必要确认
  → 创建事件 → 回查 → 失败重试 → 结果写入 store（供每日摘要展示）

普通邮件不进入本流程。
"""

import json
import logging

from tools import time_extractor, calendar_manager, event_confirm
from storage import calendar_store

logger = logging.getLogger(__name__)

# 低于该置信度视为"时间模糊"，需用户确认
CONFIDENCE_THRESHOLD = 0.85

# 已建档（处于这些状态）且去重键命中视为已处理
ALREADY_HANDLED_STATUS = {"created", "confirmed", "pending_confirm"}


def _build_description(email: dict, event: dict) -> str:
    """构造 description：邮件摘要 | 待办动作 | 发件人 | 原邮件ID"""
    parts = []
    if event.get("todo"):
        parts.append(f"待办动作：{event['todo']}")
    parts.append(f"发件人：{email.get('from', '')}")
    if email.get("message_id"):
        parts.append(f"原邮件ID:{email['message_id']}")
    return " | ".join(parts) if parts else event.get("title", "")


def _get_conflicts(event: dict) -> list:
    """调用日历接口检查时间冲突"""
    start = event.get("start_time")
    end = event.get("end_time") or start
    if not start:
        return []
    try:
        return calendar_manager.check_conflicts(start, end)
    except Exception:
        return []


def _is_duplicate(email: dict, event: dict) -> bool:
    """根据 邮件ID+标题+发件人+时间 去重，避免重复创建"""
    try:
        existing = calendar_store.list_by_source_email(email.get("message_id", ""))
    except Exception:
        return False
    for ev in existing:
        if ev.get("status") in ALREADY_HANDLED_STATUS:
            # 同邮件 + 同标题 + 同发件人 + 同开始时间 → 视为已处理
            if (ev.get("title") == event.get("title")
                    and ev.get("sender") == email.get("from")
                    and ev.get("start_time") == event.get("start_time")):
                return True
    return False


def _auto_create(event: dict) -> dict:
    """自动创建日历事件并回查；失败自动重试 1 次。"""
    create_res = calendar_manager.create_calendar_event(event)
    event_id = create_res.get("event_id")
    if create_res.get("duplicate"):
        # 重复：已存在，回查其真伪
        verify = calendar_manager.verify_calendar_event(event_id) if event_id else {}
        return {"ok": True, "event_id": event_id, "duplicate": True,
                "verified": verify.get("verified", False)}

    if not event_id:
        return {"ok": False, "event_id": None, "duplicate": False,
                "verified": False, "error": create_res.get("error", "创建失败")}

    # 第一次回查
    verify = calendar_manager.verify_calendar_event(event_id)
    if verify.get("verified"):
        return {"ok": True, "event_id": event_id, "duplicate": False,
                "verified": True, "error": None}

    # 失败重试一次
    retry_res = calendar_manager.create_calendar_event(event)
    retry_id = retry_res.get("event_id") or event_id
    verify2 = calendar_manager.verify_calendar_event(retry_id)
    if verify2.get("verified"):
        return {"ok": True, "event_id": retry_id, "duplicate": False,
                "verified": True, "error": None}

    # 仍失败：标记 failed 并返回原因
    try:
        calendar_store.update_status(retry_id, "failed")
    except Exception:
        pass
    return {"ok": False, "event_id": retry_id, "duplicate": False,
            "verified": False, "error": verify2.get("event") and "回查失败" or "创建并回查失败"}


def _process_one(email: dict, user_pref: dict = None) -> str:
    """处理单封重要邮件，返回状态：created / pending_confirm / failed / skipped"""
    extracted = time_extractor.extract_event(email)
    if not extracted.get("needs_action") or not extracted.get("event"):
        return "skipped"

    event = extracted["event"]
    # 填充元数据
    event["source_email_id"] = email.get("message_id", "")
    event["source"] = "email-classifier"
    event["sender"] = email.get("from", "")
    event["description"] = _build_description(email, event)
    event["event_type"] = event.get("event_type", "other") or "other"
    event["reminders"] = time_extractor.select_reminders(event, user_pref)
    event["title"] = event.get("title") or email.get("subject", "未命名事项")

    # 去重检查
    if _is_duplicate(email, event):
        return "skipped"

    # 判定是否需用户确认
    high_impact = time_extractor.is_high_impact(event)
    confidence = float(event.get("confidence", 0) or 0)
    multi = bool(event.get("multi_candidate"))
    conflicts = _get_conflicts(event)
    needs_confirm = high_impact or multi or confidence < CONFIDENCE_THRESHOLD or bool(conflicts)

    if needs_confirm:
        # 落库为待确认 + 发送确认邮件
        store_event = dict(event)
        store_event["status"] = "pending_confirm"
        result = calendar_store.create_event(store_event)
        stored = result["event"]
        try:
            event_confirm.send_confirmation_request(stored)
        except Exception as e:
            logger.error(f"发送确认邮件失败: {e}")
        return "pending_confirm"

    # 自动创建
    store_event = dict(event)
    store_event["status"] = "created"
    res = _auto_create(store_event)
    if res.get("ok"):
        return "created"
    # 失败标记 + 通知
    ev_id = res.get("event_id")
    if ev_id:
        try:
            calendar_store.update_status(ev_id, "failed")
        except Exception:
            pass
    logger.warning(f"创建日历事件失败: {event.get('title')} - {res.get('error')}")
    return "failed"


def process_important_emails(emails_data: list, user_pref: dict = None) -> dict:
    """处理所有重要/需行动邮件的日历提醒。

    Args:
        emails_data: 重要邮件列表，每项含 subject/from/body_preview/date/message_id
        user_pref: 用户提醒偏好（可选）

    Returns:
        {"processed": int, "created": int, "pending_confirm": int,
         "failed": int, "skipped": int, "details": [str]}
    """
    calendar_store.init_db()
    summary = {
        "processed": 0, "created": 0, "pending_confirm": 0,
        "failed": 0, "skipped": 0, "details": [],
    }
    for email in (emails_data or []):
        status = _process_one(email, user_pref)
        summary["processed"] += 1
        summary[status] = summary.get(status, 0) + 1
        summary["details"].append(f"[{status}] {email.get('subject', '')}")
    return summary