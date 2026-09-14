"""日历操作模块

对外预留三个稳定接口，后续可无缝替换为真实日历服务（如 Google Calendar）：
- query_calendar_events: 查询指定时间段已有日程
- create_calendar_event: 创建日历事件
- verify_calendar_event: 回查事件是否创建成功

当前使用本地 SQLite 存储作为兜底实现（见 storage/calendar_store.py）。
"""

import logging

from storage import calendar_store

logger = logging.getLogger(__name__)


def query_calendar_events(start: str, end: str) -> list:
    """查询指定时间段的已有日程。

    Args:
        start: ISO8601 起始时间
        end: ISO8601 结束时间

    Returns:
        list[dict]: 事件列表，每项含 title/start_time/end_time/status 等
    """
    try:
        return calendar_store.list_events(start=start, end=end,
                                          status=["created", "confirmed"])
    except Exception as e:
        logger.error(f"query_calendar_events 失败: {e}", exc_info=True)
        return []


def create_calendar_event(event: dict) -> dict:
    """创建日历事件。

    Args:
        event: 事件字典，含 title/description/start_time/end_time/location/
               reminders/source/source_email_id/dedup_key/status

    Returns:
        dict: {"created": bool, "duplicate": bool, "event_id": str|None, "error": str|None}
    """
    try:
        result = calendar_store.create_event(event)
        if result["created"]:
            return {
                "created": True, "duplicate": False,
                "event_id": result["event"]["id"], "error": None,
            }
        else:
            return {
                "created": False, "duplicate": True,
                "event_id": result["event"]["id"], "error": "重复事项，跳过创建",
            }
    except Exception as e:
        logger.error(f"create_calendar_event 失败: {e}", exc_info=True)
        return {"created": False, "duplicate": False, "event_id": None, "error": str(e)}


def verify_calendar_event(event_id: str) -> dict:
    """回查事件是否创建成功。

    不仅依赖接口返回，而是重新查询存储确认记录真实存在且状态正确。

    Returns:
        dict: {"verified": bool, "event": dict|None}
    """
    try:
        event = calendar_store.get_event(event_id)
        if not event:
            return {"verified": False, "event": None}
        # 状态为 created/confirmed/pending_confirm 均视为有效建档
        verified = event["status"] in ("created", "confirmed", "pending_confirm")
        return {"verified": verified, "event": event}
    except Exception as e:
        logger.error(f"verify_calendar_event 失败: {e}", exc_info=True)
        return {"verified": False, "event": None}


def check_conflicts(start: str, end: str, exclude_id: str = None) -> list:
    """检查指定时间段是否有日程冲突"""
    try:
        return calendar_store.check_conflicts(start, end, exclude_id)
    except Exception as e:
        logger.error(f"check_conflicts 失败: {e}", exc_info=True)
        return []


def list_by_source_email(source_email_id: str) -> list:
    """按原邮件查找已建档事件，用于同一邮件去重"""
    try:
        return calendar_store.list_by_source_email(source_email_id)
    except Exception as e:
        logger.error(f"list_by_source_email 失败: {e}", exc_info=True)
        return []