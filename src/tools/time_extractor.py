"""时间信息提取模块

对"高重要程度"或"需要用户行动"的邮件，从主题和正文中提取：
- 事项名称、开始/截止时间、地点/会议链接、待办动作、建议提醒时间、提取置信度

支持会议/面试/考试/报名/付款/材料提交/任务截止等场景，
以及"明天下午""下周一""本周五前"等相对时间（结合邮件接收时间和时区转绝对时间）。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from tools.llm_call import chat_completion_text

logger = logging.getLogger(__name__)

# 用户时区（香港/北京等 UTC+8）
USER_TIMEZONE = timezone(timedelta(hours=8))

# 高影响事件类型：即使时间明确也需用户确认
HIGH_IMPACT_TYPES = {"interview", "exam", "payment", "contract"}


def _build_extract_prompt(email_data: dict, now_str: str) -> str:
    return f"""你是一个日程安排信息提取助手。请从下面这封邮件中提取日程事项信息。

【当前时间】{now_str}（时区 UTC+8）
【邮件接收时间】{email_data.get('date', '')}
【发件人】{email_data.get('from', '')}
【主题】{email_data.get('subject', '')}
【邮件正文】
{email_data.get('body_preview', '')}

请严格按以下 JSON 结构返回（不要输出其他内容）：

{{
  "needs_action": true,
  "event": {{
    "title": "事项名称",
    "start_time": "ISO8601绝对时间，如2026-09-10T14:00:00+08:00，若无明确开始时间填null",
    "end_time": "结束或截止时间ISO8601，无则填null",
    "location": "地点或会议链接，无则填null",
    "todo": "具体待办动作描述",
    "suggested_remind": ["提醒时间ISO8601数组"],
    "confidence": 0.0,
    "multi_candidate": false,
    "event_type": "meeting|exam|interview|deadline|payment|submission|other"
  }}
}}

提取规则：
1. 仅当邮件包含明确的日程/待办/截止信息时才 needs_action=true，否则 false。
2. 识别场景：会议(meeting)、面试(interview)、考试(exam)、报名(registration用submission)、付款(payment)、材料/报告提交(submission)、任务截止(deadline)。
3. 相对时间需结合邮件接收时间转换：
   - "明天下午3点" → 邮件接收日+1天 15:00
   - "下周一" → 下一个周一
   - "本周五前" → 本周五 23:59
4. confidence 为时间提取置信度(0-1)，时间模糊或无法确定时降低。
5. multi_candidate=true 当正文存在多个候选时间且难以判断。
6. 所有时间必须输出带 +08:00 时区的 ISO8601 绝对时间。
7. suggested_remind 仅为提醒建议，后面会按事件类型套用默认策略。"""


def extract_event(email_data: dict) -> dict:
    """从邮件中提取事件信息。

    输入 email_data 至少包含: subject, body_preview, date, from, message_id
    返回:
        {"needs_action": bool, "event": {...}|None, "raw": "LLM原始输出", "error": str|None}
    """
    now = datetime.now(USER_TIMEZONE)
    now_str = now.strftime("%Y年%m月%d日 %H:%M")
    try:
        prompt = _build_extract_prompt(email_data, now_str)
        raw = chat_completion_text(
            prompt=prompt,
            temperature=0.1,
            max_tokens=800,
            timeout=90,
        )
        raw = raw.strip()
        # 清理可能的 markdown 代码块包裹
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return {
            "needs_action": bool(data.get("needs_action", False)),
            "event": data.get("event"),
            "raw": raw,
            "error": None,
        }
    except json.JSONDecodeError as e:
        logger.warning(f"时间提取 LLM 返回无法解析: {e}")
        return {"needs_action": False, "event": None, "raw": "", "error": f"解析失败: {e}"}
    except Exception as e:
        logger.error(f"时间提取失败: {e}", exc_info=True)
        return {"needs_action": False, "event": None, "raw": "", "error": str(e)}


def is_high_impact(event: dict) -> bool:
    """判断是否为高影响事项（面试/考试/付款/合同等需用户确认）"""
    if not event:
        return False
    return event.get("event_type") in HIGH_IMPACT_TYPES


def select_reminders(event: dict, user_pref: dict = None) -> list:
    """根据事件类型套用默认提醒策略。

    默认策略：
    - 截止事项(deadline/submission/payment)：提前1天 + 提前1小时
    - 会议/面试(meeting/interview)：提前1天 + 提前30分钟
    - 用户已设置偏好时优先使用用户偏好
    """
    if user_pref and user_pref.get("reminders"):
        return user_pref["reminders"]

    start = event.get("start_time")
    if not start:
        return []
    try:
        start_dt = datetime.fromisoformat(start)
    except Exception:
        return []

    event_type = event.get("event_type", "other")
    if event_type in ("deadline", "submission", "payment"):
        return [
            (start_dt - timedelta(days=1)).isoformat(timespec="minutes"),
            (start_dt - timedelta(hours=1)).isoformat(timespec="minutes"),
        ]
    else:  # meeting / interview / exam / other
        return [
            (start_dt - timedelta(days=1)).isoformat(timespec="minutes"),
            (start_dt - timedelta(minutes=30)).isoformat(timespec="minutes"),
        ]