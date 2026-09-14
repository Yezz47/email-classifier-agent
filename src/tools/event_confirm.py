"""事件确认机制

高影响事项（面试/考试/付款/合同）或时间模糊/冲突/多候选的事项，
不会直接创建，而是先通过邮件发送确认请求；用户在邮件中回复
「确认」/「拒绝」（或附事项标题），系统回执解析后更新事件状态。

确认后事件状态流转：
  pending_confirm -> confirmed（创建） / declined（不创建） / discarded（超时忽略）
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from tools.send_email import _send_email_impl
from storage import calendar_store

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
CONFIRM_HOURS = 48  # 确认请求 48 小时内有效，超时视为等待中


def format_confirmation_text(event: dict) -> str:
    """把事件格式化为给用户确认的邮件正文（HTML）"""
    title = event.get("title", "未命名事项")
    start = event.get("start_time", "未确定")
    end = event.get("end_time", "未确定" if start == "未确定" else start)
    location = event.get("location") or "未指定"
    todo = event.get("todo") or "无"
    sender = event.get("sender", "未知")

    def _fmt(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return iso or "未确定"

    html = f"""<div style="font-family:sans-serif;max-width:600px;margin:auto;padding:20px;">
<h2 style="color:#1a73e8;">⏰ 待确认事项提醒</h2>
<p>检测到一封邮件包含需要安排到日历的事项，请确认是否创建提醒：</p>
<table style="border-collapse:collapse;width:100%;">
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">事项名称</td><td style="padding:6px;">{title}</td></tr>
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">开始时间</td><td style="padding:6px;">{_fmt(start)}</td></tr>
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">结束/截止</td><td style="padding:6px;">{_fmt(end)}</td></tr>
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">地点/链接</td><td style="padding:6px;">{location}</td></tr>
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">待办动作</td><td style="padding:6px;">{todo}</td></tr>
<tr><td style="padding:6px;background:#f1f3f4;font-weight:bold;">发件人</td><td style="padding:6px;">{sender}</td></tr>
</table>
<p style="margin-top:16px;">📬 <b>回复本邮件，在开头注明「确认」或「拒绝」即可返回处理结果。</b></p>
<p style="color:#666;font-size:12px;">（回复「确认」将创建日历提醒；回复「拒绝」将忽略本事项）</p>
</div>"""
    return html


def send_confirmation_request(event: dict) -> dict:
    """向用户发送待确认事件的确认邮件。

    Returns:
        {"status": "sent"/"error", "event_id": str, "message": str}
    """
    eid = event["id"]
    # 若事件尚未落库，先落库为待确认；已存在则更新状态
    existing = calendar_store.get_event(eid)
    if existing:
        calendar_store.update_status(eid, "pending_confirm")
    else:
        ev_for_store = dict(event)
        ev_for_store["status"] = "pending_confirm"
        calendar_store.create_event(ev_for_store)

    subject = f"【待确认】{event.get('title', '事项')} - 请确认是否加入日历提醒"
    content = format_confirmation_text(event)
    # 收件人：用户自己的邮箱（用于接收总结的同一账号）
    from tools.email_common import get_email_config
    try:
        cfg = get_email_config()
        to_addr = cfg["account"]
    except Exception:
        to_addr = event.get("sender", "")

    result = json.loads(_send_email_impl(to_addr, subject, content, content_type="html"))
    result["event_id"] = eid
    return result


def _match_keyword(text: str) -> str | None:
    """从回执文本中识别确认/拒绝意图"""
    t = (text or "").strip()
    if not t:
        return None
    import re
    # 精确/含关键词
    if re.search(r"(确认|同意|是的|<|要求创建|可以)", t):
        return "confirmed"
    if re.search(r"(拒绝|不要|取消|否|不需要|不考虑)", t):
        return "declined"
    return None


def respond_to_reply(event_id: str, reply_text: str) -> dict:
    """处理用户对某事件的确认回执。

    Returns:
        {"status": "confirmed"/"declined"/"unrecognized", "event": dict|None}
    """
    decision = _match_keyword(reply_text)
    if decision == "confirmed":
        ok = calendar_store.update_status(event_id, "confirmed")
        return {"status": "confirmed", "event": calendar_store.get_event(event_id) if ok else None}
    if decision == "declined":
        ok = calendar_store.update_status(event_id, "declined")
        return {"status": "declined", "event": calendar_store.get_event(event_id) if ok else None}
    return {"status": "unrecognized", "event": calendar_store.get_event(event_id)}


def confirm_event(event_id: str) -> dict:
    """直接确认事件（供对话/回执调用，状态置为 confirmed 以便创建）"""
    calendar_store.update_status(event_id, "confirmed")
    return {"status": "confirmed", "event": calendar_store.get_event(event_id)}


def decline_event(event_id: str) -> dict:
    """拒绝事件"""
    calendar_store.update_status(event_id, "declined")
    return {"status": "declined", "event": calendar_store.get_event(event_id)}


def list_pending_tasks() -> list:
    """列出所有待确认、已确认、创建中的事件，供摘要展示"""
    return calendar_store.list_events(
        status=["pending_confirm", "confirmed", "created", "failed"]
    )