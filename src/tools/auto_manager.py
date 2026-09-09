"""自动邮件管理 - 后台自动分类、标记、通知，无需用户触发"""

import json
import logging
import smtplib
import ssl
import time
import email
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from tools.email_common import get_email_config, connect_imap, decode_header_value, extract_body, resolve_folder

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

EMAIL_CATEGORIES = {
    "important": "重要",
    "secondary": "次重要",
    "unimportant": "不重要",
}

# 自动标记规则：分类 → (action, target_folder)
AUTO_LABEL_RULES = {
    "important": ("mark_flagged", ""),    # 重要 → 标记星标 + 发送通知
    "secondary": ("mark_flagged", ""),    # 次重要 → 标记星标
    "unimportant": ("archive", ""),       # 不重要 → 标记已读 + 归档
}


def _get_llm():
    """获取 LLM 实例"""
    import os
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, "config/agent_llm_config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")
    return ChatOpenAI(
        model=cfg["config"].get("model"),
        api_key=api_key,
        base_url=base_url,
        temperature=0.2,
        timeout=cfg["config"].get("timeout", 300),
        max_tokens=3000,
        extra_body={"thinking": {"type": cfg["config"].get("thinking", "disabled")}},
    )


def _classify_emails_llm(emails_data: list) -> list:
    """LLM 批量分类邮件，返回每封邮件的分类结果"""
    llm = _get_llm()
    emails_json = json.dumps([
        {"index": i, "from": e["from"], "subject": e["subject"], "body": e["body_preview"]}
        for i, e in enumerate(emails_data)
    ], ensure_ascii=False)

    prompt = f"""你是学校邮件分类助手。对以下邮件进行三级分类，严格按 JSON 数组返回：
[{{"index": 0, "category": "important|secondary|unimportant"}}]

分类规则：
- important（重要，需立即关注）：课程相关（选课、调课、作业截止）、考试相关（考试时间、考场、成绩）、放假通知、黑雨/暴雨停课通知、其他紧急学术事务
- secondary（次重要，建议关注）：学校食品营养讲座、健康讲座、图书馆通知（借阅到期、新书上架）、学术讲座、奖学金/助学金申请通知
- unimportant（不重要，可忽略）：招志愿者、社团宣传/招新、商业推广广告、其他非学术类通知

邮件：{emails_json}"""

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content
    if isinstance(raw, list):
        raw = " ".join(item if isinstance(item, str) else item.get("text", "") for item in raw).strip()
    else:
        raw = str(raw).strip()

    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"LLM 分类结果解析失败: {raw[:300]}")
        return [{"index": i, "category": "other", "is_important": False} for i in range(len(emails_data))]


def _auto_label_email(conn, uid: str, action: str, folder: str = "INBOX") -> bool:
    """对单封邮件执行自动标记操作"""
    try:
        if action == "mark_read":
            conn.store(uid, "+FLAGS", "\\Seen")
        elif action == "mark_flagged":
            conn.store(uid, "+FLAGS", "\\Flagged")
        elif action == "archive":
            all_mail = resolve_folder(conn, "all_mail")
            conn.copy(uid, all_mail)
            conn.store(uid, "+FLAGS", "\\Deleted")
            conn.expunge()
        return True
    except Exception as e:
        logger.warning(f"自动标记邮件 {uid} ({action}) 失败: {e}")
        return False


def _send_notification(config: dict, subject: str, html_content: str) -> bool:
    """发送通知邮件"""
    msg = MIMEText(html_content, "html", "utf-8")
    msg["From"] = formataddr(("邮件管理助手", config["account"]))
    msg["To"] = config["account"]
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    for i in range(3):
        try:
            with smtplib.SMTP_SSL(config["smtp_server"], config["smtp_port"], context=ctx, timeout=30) as server:
                server.ehlo()
                server.login(config["account"], config["auth_code"])
                server.sendmail(config["account"], [config["account"]], msg.as_string())
            return True
        except Exception as e:
            logger.warning(f"通知邮件发送尝试 {i+1} 失败: {e}")
            time.sleep(2 * (i + 1))
    return False


def _build_important_notification_html(today_str: str, important_emails: list) -> str:
    """构建重要邮件通知 HTML"""
    items = ""
    for em in important_emails:
        message_id = em.get("message_id", "")
        gmail_link = f'https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id}' if message_id else ""
        link_html = f"<a href='{gmail_link}' target='_blank' style='color:#1a73e8;'>🔗 打开邮件</a>" if gmail_link else ""
        items += f"""
<div style='background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin-bottom:8px;border-radius:4px;'>
<strong>{em.get('subject', '')}</strong><br>
<span style='color:#666;font-size:13px;'>来自: {em.get('from', '')} | {em.get('time', '')}</span><br>
<span style='margin-top:4px;display:block;'>{em.get('summary', '')}</span>
{link_html}
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;'>
<div style='background:linear-gradient(135deg,#d32f2f 0%,#b71c1c 100%);padding:24px;border-radius:12px;color:white;'>
<h2 style='margin:0;'>🔔 重要邮件提醒</h2>
<p style='margin:8px 0 0;opacity:0.9;'>{today_str}</p>
</div>
<div style='margin-top:16px;'>
<p>检测到 <strong style='color:#d32f2f;'>{len(important_emails)}</strong> 封重要邮件，已自动标记星标：</p>
{items}
</div>
<div style='text-align:center;color:#999;font-size:12px;margin-top:24px;'>邮件管理助手 · 自动托管</div>
</body></html>"""


def auto_manage_emails() -> str:
    """
    自动邮件管理核心逻辑（定时任务调用）：
    1. 获取最近 1 小时的新邮件
    2. LLM 自动分类
    3. 按规则自动标记（促销归档、重要星标等）
    4. 如有重要邮件，立即发送通知
    """
    logger.info("=== 自动邮件管理任务开始 ===")
    try:
        config = get_email_config()
        conn = connect_imap(config)

        try:
            status, _ = conn.select("INBOX")
            if status != "OK":
                return json.dumps({"status": "error", "message": "无法打开收件箱"}, ensure_ascii=False)

            # 获取最近 1 小时的邮件
            one_hour_ago = datetime.now(CST) - timedelta(hours=1)
            since_str = one_hour_ago.strftime("%d-%b-%Y %H:%M")
            search_query = f'(SINCE "{since_str}")'
            status, data = conn.search(None, search_query)
            if status != "OK":
                return json.dumps({"status": "success", "message": "最近1小时无新邮件"}, ensure_ascii=False)

            mail_ids = data[0].split()
            if not mail_ids:
                return json.dumps({"status": "success", "message": "最近1小时无新邮件", "processed": 0}, ensure_ascii=False)

            # 获取邮件内容
            emails_data = []
            for mid in mail_ids:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                message_id = msg.get("Message-ID", "").strip("<>")
                emails_data.append({
                    "uid": mid.decode() if isinstance(mid, bytes) else str(mid),
                    "from": decode_header_value(msg.get("From", "")),
                    "subject": decode_header_value(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "body_preview": extract_body(msg, max_length=500),
                    "message_id": message_id,
                })

            logger.info(f"获取到 {len(emails_data)} 封最近1小时邮件")

            # LLM 分类
            classifications = _classify_emails_llm(emails_data)

            # 自动标记 + 收集重要邮件
            important_emails = []
            stats = {"important": 0, "secondary": 0, "unimportant": 0}
            labeled_count = 0

            for cls in classifications:
                idx = cls.get("index", 0)
                cat = cls.get("category", "other")
                is_important = cls.get("is_important", False)
                if idx < len(emails_data):
                    em = emails_data[idx]
                    stats[cat] = stats.get(cat, 0) + 1

                    # 自动标记
                    if cat in AUTO_LABEL_RULES:
                        action, target = AUTO_LABEL_RULES[cat]
                        if _auto_label_email(conn, em["uid"], action):
                            labeled_count += 1

                    # 收集重要和次重要邮件用于通知
                    if cat in ("important", "secondary"):
                        important_emails.append({
                            "from": em["from"],
                            "subject": em["subject"],
                            "time": em["date"],
                            "message_id": em["message_id"],
                            "summary": em["body_preview"][:200],
                        })

            result = {
                "status": "success",
                "processed": len(emails_data),
                "labeled": labeled_count,
                "important_count": len(important_emails),
                "stats": stats,
            }

            # 有重要邮件 → 立即发送通知
            if important_emails:
                today_str = datetime.now(CST).strftime("%Y年%m月%d日 %H:%M")
                html = _build_important_notification_html(today_str, important_emails)
                sent = _send_notification(config, f"🔔 重要邮件提醒 - {today_str}", html)
                result["notification_sent"] = sent
                logger.info(f"重要邮件通知已发送: {len(important_emails)} 封")

            logger.info(f"自动邮件管理完成: {json.dumps(result, ensure_ascii=False)}")
            return json.dumps(result, ensure_ascii=False)

        finally:
            conn.logout()

    except Exception as e:
        logger.error(f"自动邮件管理失败: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
