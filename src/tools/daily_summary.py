"""每日邮件总结工具 - 获取当日邮件、LLM分类统计、生成HTML报告并通过SMTP发送"""

import os
import json
import email
import logging
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from cozeloop.decorator import observe
from coze_coding_utils.runtime_ctx.context import default_headers
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

from tools.email_common import get_email_config, connect_imap, decode_header_value, extract_body
from tools.llm_call import chat_completion_text
from storage.calendar_store import init_db as _init_cal_db, list_all as _list_cal_all

logger = logging.getLogger(__name__)

# 中国标准时区
CST = timezone(timedelta(hours=8))

# 邮件分类定义
EMAIL_CATEGORIES = {
    "important": "重要",
    "secondary": "次重要",
    "unimportant": "不重要",
}


# ─────────────────── LLM 分类与总结 ───────────────────

def _classify_emails_with_llm(emails_data: list) -> dict:
    """使用 LLM 对邮件进行分类和摘要生成"""
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, "config/agent_llm_config.json")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    emails_json = json.dumps(emails_data, ensure_ascii=False, indent=2)

    prompt = f"""你是一个专业的邮件分类助手。请对以下今日收到的邮件进行分类和总结。

分类规则（三级分类）：
- important（重要，需立即关注）：课程相关（选课、调课、作业截止）、考试相关（考试时间、考场、成绩）、放假通知、黑雨/暴雨停课通知、其他紧急学术事务
- secondary（次重要，建议关注）：学校食品营养讲座、健康讲座、图书馆通知（借阅到期、新书上架）、学术讲座、奖学金/助学金申请通知
- unimportant（不重要，可忽略）：招志愿者、社团宣传/招新、商业推广广告、其他非学术类通知

邮件列表：
{emails_json}

请严格按以下 JSON 格式返回结果，不要输出任何其他内容：
{{
  "classifications": [
    {{"index": 0, "category": "work", "is_important": true, "summary": "一句话摘要"}},
    ...
  ],
  "category_counts": {{
    "important": 0,
    "secondary": 0,
    "unimportant": 0
  }},
  "important_summaries": [
    {{"index": 0, "from": "发件人", "subject": "主题", "summary": "详细摘要（2-3句话）", "message_id": "邮件的Message-ID"}}
  ]
}}"""

    content = chat_completion_text(
        prompt,
        temperature=0.3,
        max_tokens=cfg["config"].get("max_completion_tokens", 10000),
    )

    # 提取 JSON（兼容 markdown 代码块包裹）
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"LLM 返回的内容无法解析为 JSON: {content[:500]}")
        return {
            "classifications": [
                {"index": i, "category": "unimportant", "is_important": False, "summary": "分类失败"}
                for i in range(len(emails_data))
            ],
            "category_counts": {"unimportant": len(emails_data)},
            "important_summaries": [
                {"index": i, "from": emails_data[i].get("from", ""), "subject": emails_data[i].get("subject", ""),
                 "summary": "分类失败", "message_id": emails_data[i].get("message_id", "")}
                for i in range(len(emails_data))
            ],
        }


# ─────────────────── HTML 报告生成 ───────────────────

def _parse_event_dt(value):
    """解析事件时间字符串，失败返回 None"""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=CST)
        v = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except Exception:
        return None


def _fmt_dt(dt):
    """格式化时间为 HH:mm"""
    if not dt:
        return ""
    return dt.strftime("%m-%d %H:%M")


def _build_timeline_section():
    """从日历存储构建『重要时间节点』模块，返回 HTML 片段"""
    try:
        _init_cal_db()
        events = _list_cal_all() or []
        now = datetime.now(CST)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = today_start + timedelta(days=7)
        tomorrow_start = today_start + timedelta(days=1)

        created = [e for e in events if e.get("status") == "created"]
        pending = [e for e in events if e.get("status") == "pending_confirm"]
        failed = [e for e in events if e.get("status") == "failed"]

        # 预解析已创建事件的时间
        created_parsed = []
        for e in created:
            dt = _parse_event_dt(e.get("start_time"))
            if dt is not None:
                created_parsed.append((e, dt))
        # 今日待处理
        today_items = [e for e, dt in created_parsed if today_start <= dt < tomorrow_start]
        # 即将到期（未来7天内，除今日）
        upcoming = [e for e, dt in created_parsed if tomorrow_start <= dt <= week_end]

        def _row(e, tag=""):
            dt = _parse_event_dt(e.get("start_time"))
            loc = e.get("location") or ""
            return f"""<div style='background:#eef7ff;border-left:4px solid #2196f3;padding:10px;margin-bottom:6px;border-radius:4px;'>
<strong>{e.get('title','')}</strong> {tag}<br>
<span style='color:#666;font-size:13px;'>🕐 {_fmt_dt(dt)} &nbsp;|&nbsp; 📍 {loc if loc else '未指定地点'}</span>
</div>"""

        blocks = ""
        if today_items:
            rows = "".join(_row(e, "<span style='color:#d32f2f;font-size:12px;'>(今日)</span>") for e in sorted(today_items, key=lambda x: _parse_event_dt(x.get('start_time')) or now))
            blocks += f"<div style='margin-bottom:14px;'><h4 style='margin:8px 0;color:#d32f2f;'>今日待处理</h4>{rows}</div>"
        if upcoming:
            rows = "".join(_row(e) for e in sorted(upcoming, key=lambda x: _parse_event_dt(x.get('start_time')) or now))
            blocks += f"<div style='margin-bottom:14px;'><h4 style='margin:8px 0;color:#e65100;'>即将到期（7天内）</h4>{rows}</div>"
        if pending:
            rows = "".join(_row(e, "<span style='color:#795548;font-size:12px;'>(待确认)</span>") for e in pending[:10])
            blocks += f"<div style='margin-bottom:14px;'><h4 style='margin:8px 0;color:#795548;'>等待确认</h4>{rows}</div>"
        if failed:
            rows = "".join(f"<div style='background:#ffebee;border-left:4px solid #f44336;padding:10px;margin-bottom:6px;border-radius:4px;'><strong>{e.get('title','')}</strong> <span style='color:#d32f2f;font-size:12px;'>(创建失败)</span><br><span style='color:#666;font-size:13px;'>{e.get('create_error','') or '创建失败，请稍后重试'}</span></div>" for e in failed[:10])
            blocks += f"<div style='margin-bottom:14px;'><h4 style='margin:8px 0;color:#d32f2f;'>创建失败</h4>{rows}</div>"

        if not blocks:
            return ""
        return f"""<div style='margin-bottom:24px;'>
<h3 style='color:#333;'> 重要时间节点</h3>
{blocks}
</div>"""
    except Exception as e:
        logger.warning("构建重要时间节点模块失败: %s", e)
        return ""


def _generate_html_report(
    today_str: str,
    emails_data: list,
    classification_result: dict,
) -> str:
    """生成 HTML 格式的每日邮件总结报告"""
    total = len(emails_data)
    category_counts = classification_result.get("category_counts", {})
    important_summaries = classification_result.get("important_summaries", [])
    classifications = classification_result.get("classifications", [])

    # 分类统计行
    category_rows = ""
    for cat_key, cat_name in EMAIL_CATEGORIES.items():
        count = category_counts.get(cat_key, 0)
        if count > 0:
            category_rows += f"<tr><td style='padding:8px;border:1px solid #e0e0e0;'>{cat_name}</td><td style='padding:8px;border:1px solid #e0e0e0;text-align:center;'>{count}</td></tr>\n"

    # 邮件明细行
    email_rows = ""
    for i, em in enumerate(emails_data):
        cat = "other"
        is_important = False
        summary_text = ""
        for c in classifications:
            if c.get("index") == i:
                cat = c.get("category", "other")
                is_important = c.get("is_important", False)
                summary_text = c.get("summary", "")
                break

        cat_name = EMAIL_CATEGORIES.get(cat, "其他")
        badge = (
            "⭐ "
            if is_important
            else ""
        )
        # 重要邮件主题添加 Gmail 跳转链接
        message_id = em.get("message_id", "")
        subject_display = em.get('subject', '')
        if is_important and message_id:
            gmail_link = f'https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id}'
            subject_display = f"<a href='{gmail_link}' target='_blank' style='color:#1a73e8;text-decoration:none;'>{em.get('subject', '')}</a>"
        email_rows += f"""<tr>
<td style='padding:8px;border:1px solid #e0e0e0;'>{em.get('from', '')}</td>
<td style='padding:8px;border:1px solid #e0e0e0;'>{subject_display}</td>
<td style='padding:8px;border:1px solid #e0e0e0;text-align:center;'>{badge}{cat_name}</td>
<td style='padding:8px;border:1px solid #e0e0e0;'>{summary_text}</td>
</tr>\n"""

    # 重要邮件摘要（带 Gmail 跳转链接）
    important_section = ""
    if important_summaries:
        important_items = ""
        for imp in important_summaries:
            message_id = imp.get("message_id", "")
            # 构造 Gmail 跳转链接
            gmail_link = ""
            if message_id:
                gmail_link = f'https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id}'
            link_html = ""
            if gmail_link:
                link_html = f"""<a href='{gmail_link}' target='_blank' style='color:#1a73e8;text-decoration:none;font-size:13px;margin-top:4px;display:inline-block;'> 在 Gmail 中打开 →</a>"""

            important_items += f"""
<div style='background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin-bottom:8px;border-radius:4px;'>
<strong> {imp.get('subject', '')}</strong><br>
<span style='color:#666;font-size:13px;'>来自: {imp.get('from', '')}</span><br>
<span style='margin-top:4px;display:block;'>{imp.get('summary', '')}</span>
{link_html}
</div>"""
        important_section = f"""
<div style='margin:24px 0;'>
<h3 style='color:#d32f2f;'>🔔 重要邮件摘要</h3>
{important_items}
</div>"""

    # 重要时间节点模块
    timeline_section = _build_timeline_section()

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style='font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width:800px; margin:0 auto; padding:20px; color:#333;'>
<div style='background:linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding:24px; border-radius:12px; color:white; margin-bottom:24px;'>
<h2 style='margin:0;'>📧 每日邮件整理</h2>
<p style='margin:8px 0 0; opacity:0.9;'>{today_str}</p>
</div>

<div style='background:#f8f9fa; padding:16px; border-radius:8px; margin-bottom:24px;'>
<p style='margin:0; font-size:18px;'>今日共收到 <strong style='color:#667eea; font-size:24px;'>{total}</strong> 封邮件</p>
</div>

<div style='margin-bottom:24px;'>
<h3 style='color:#333;'>📊 分类统计</h3>
<table style='width:100%; border-collapse:collapse;'>
<tr style='background:#f0f0f0;'><th style='padding:8px;border:1px solid #e0e0e0;'>分类</th><th style='padding:8px;border:1px solid #e0e0e0;'>数量</th></tr>
{category_rows}
</table>
</div>

{important_section}

{timeline_section}

<div style='margin-bottom:24px;'>
<h3 style='color:#333;'>📋 邮件明细</h3>
<table style='width:100%; border-collapse:collapse; font-size:14px;'>
<tr style='background:#f0f0f0;'>
<th style='padding:8px;border:1px solid #e0e0e0;'>发件人</th>
<th style='padding:8px;border:1px solid #e0e0e0;'>主题</th>
<th style='padding:8px;border:1px solid #e0e0e0;'>分类</th>
<th style='padding:8px;border:1px solid #e0e0e0;'>摘要</th>
</tr>
{email_rows}
</table>
</div>

<div style='text-align:center; color:#999; font-size:12px; margin-top:32px; padding-top:16px; border-top:1px solid #eee;'>
此邮件由邮件管理智能体自动生成
</div>
</body>
</html>"""
    return html


# ─────────────────── SMTP 发送 ───────────────────

def _send_summary_email(config: dict, to_addr: str, subject: str, html_content: str) -> dict:
    """通过 SMTP 发送 HTML 格式的总结邮件"""
    msg = MIMEText(html_content, "html", "utf-8")
    msg["From"] = formataddr(("邮件管理助手", config["account"]))
    msg["To"] = to_addr
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    attempts = 3
    last_err = None
    for i in range(attempts):
        try:
            with smtplib.SMTP_SSL(
                config["smtp_server"], config["smtp_port"], context=ctx, timeout=30
            ) as server:
                server.ehlo()
                server.login(config["account"], config["auth_code"])
                server.sendmail(config["account"], [to_addr], msg.as_string())
                server.quit()
            return {"status": "success", "message": f"总结邮件已发送至 {to_addr}"}
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
    return {"status": "error", "message": error_msg}


# ─────────────────── 核心流程 ───────────────────

@observe
def _daily_summary_impl() -> str:
    """每日邮件总结核心逻辑"""
    ctx = request_context.get() or new_context(method="daily_email_summary")
    try:
        config = get_email_config()
        conn = connect_imap(config)

        try:
            status, _ = conn.select("INBOX", readonly=True)
            if status != "OK":
                return json.dumps(
                    {"status": "error", "message": "无法打开收件箱"},
                    ensure_ascii=False,
                )

            # 获取今天的日期（中国时区）
            today = datetime.now(CST).date()
            date_str = today.strftime("%d-%b-%Y")

            # 按日期搜索今日邮件
            search_query = f'(SINCE {date_str})'
            status, data = conn.search(None, search_query)
            if status != "OK":
                return json.dumps(
                    {"status": "error", "message": "邮件搜索失败"},
                    ensure_ascii=False,
                )

            mail_ids = data[0].split()
            if not mail_ids:
                # 即使没有邮件也发送一封通知
                today_str = today.strftime("%Y年%m月%d日")
                html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;'>
<div style='background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:24px;border-radius:12px;color:white;'>
<h2 style='margin:0;'>📧 每日邮件整理</h2>
<p style='margin:8px 0 0;opacity:0.9;'>{today_str}</p>
</div>
<div style='background:#f8f9fa;padding:24px;border-radius:8px;margin-top:24px;text-align:center;'>
<p style='font-size:18px;color:#666;'>🎉 今日没有收到新邮件</p>
</div>
</body></html>"""
                subject = f"{today_str} 邮件整理"
                send_result = _send_summary_email(config, config["account"], subject, html)
                return json.dumps(
                    {"status": "success", "message": "今日无新邮件，已发送空报告通知"},
                    ensure_ascii=False,
                )

            # 获取每封邮件的详细内容
            emails_data = []
            for mid in mail_ids:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = decode_header_value(msg.get("Subject", ""))
                from_addr = decode_header_value(msg.get("From", ""))
                to_addr = decode_header_value(msg.get("To", ""))
                date_str_email = msg.get("Date", "")
                body = extract_body(msg, max_length=800)

                # 获取 Message-ID 用于构造 Gmail 跳转链接
                message_id = msg.get("Message-ID", "").strip()
                # 去掉尖括号，用于 URL 拼接
                message_id_clean = message_id.strip("<>") if message_id else ""

                emails_data.append({
                    "from": from_addr,
                    "to": to_addr,
                    "subject": subject,
                    "date": date_str_email,
                    "body_preview": body[:500] if body else "(无正文)",
                    "message_id": message_id_clean,
                })

            logger.info(f"获取到 {len(emails_data)} 封今日邮件，开始 LLM 分类")

            # LLM 分类
            classification_result = _classify_emails_with_llm(emails_data)

            # 生成 HTML 报告
            today_str = today.strftime("%Y年%m月%d日")
            html_report = _generate_html_report(today_str, emails_data, classification_result)

            # 发送总结邮件
            subject = f"{today_str} 邮件整理"
            send_result = _send_summary_email(config, config["account"], subject, html_report)

            return json.dumps({
                "status": "success",
                "message": f"每日邮件总结已完成，共处理 {len(emails_data)} 封邮件",
                "email_count": len(emails_data),
                "send_result": send_result,
            }, ensure_ascii=False)

        finally:
            conn.logout()

    except Exception as e:
        logger.error(f"Daily summary error: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "message": f"每日邮件总结失败: {str(e)}"},
            ensure_ascii=False,
        )


@tool
def daily_email_summary() -> str:
    """生成并发送每日邮件总结报告。自动获取当日所有邮件，通过AI进行分类统计和重要邮件摘要，
    生成HTML格式报告并发送到邮箱。主题格式为「YYYY年MM月DD日 邮件整理」。

    Returns:
        JSON 格式的执行结果
    """
    return _daily_summary_impl()
