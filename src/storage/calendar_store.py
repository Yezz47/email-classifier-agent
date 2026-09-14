"""日历事件本地存储（SQLite 兜底实现）

作为 query_calendar_events / create_calendar_event / verify_calendar_event
三个预留接口的真实后端。后续替换为真实日历服务时，只需改 calendar_manager，
本存储可作为本地缓存/审计保留。

事件默认去重键：source_email_id + title + sender + start_time
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

WORKSPACE = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
DB_PATH = os.path.join(WORKSPACE, "assets", "calendar.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    start_time TEXT,
    end_time TEXT,
    location TEXT,
    reminders TEXT,
    source TEXT,
    source_email_id TEXT,
    sender TEXT,
    dedup_key TEXT,
    status TEXT,
    event_type TEXT,
    todo TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cal_dedup ON calendar_events(dedup_key);
CREATE INDEX IF NOT EXISTS idx_cal_time ON calendar_events(start_time);
CREATE INDEX IF NOT EXISTS idx_cal_email ON calendar_events(source_email_id);
"""


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """初始化数据库表（幂等）"""
    conn = _conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row_to_event(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["reminders"] = json.loads(d["reminders"]) if d.get("reminders") else []
    except Exception:
        d["reminders"] = []
    return d


def _build_dedup_key(event: dict) -> str:
    email_id = event.get("source_email_id", "")
    title = event.get("title", "")
    sender = event.get("sender", "")
    start = event.get("start_time", "")
    return f"{email_id}|{title}|{sender}|{start}"


def create_event(event: dict) -> dict:
    """创建事件。若 dedup_key 已存在则返回已存在的事件。

    Returns:
        {"created": bool, "event": dict}
    """
    init_db()
    dedup_key = event.get("dedup_key") or _build_dedup_key(event)
    event["dedup_key"] = dedup_key

    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT * FROM calendar_events WHERE dedup_key=?",
            (dedup_key,),
        ).fetchone()
        if existing:
            return {"created": False, "event": _row_to_event(existing)}

        eid = event.get("id") or str(uuid.uuid4())
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO calendar_events
               (id,title,description,start_time,end_time,location,reminders,source,
                source_email_id,sender,dedup_key,status,event_type,todo,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                eid,
                event.get("title", ""),
                event.get("description", ""),
                event.get("start_time"),
                event.get("end_time"),
                event.get("location"),
                json.dumps(event.get("reminders", []), ensure_ascii=False),
                event.get("source", "email-classifier"),
                event.get("source_email_id", ""),
                event.get("sender", ""),
                dedup_key,
                event.get("status", "created"),
                event.get("event_type", "other"),
                event.get("todo", ""),
                now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM calendar_events WHERE id=?", (eid,)).fetchone()
        return {"created": True, "event": _row_to_event(row)}
    finally:
        conn.close()


def get_event(event_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM calendar_events WHERE id=?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


def list_events(start: str = None, end: str = None, status: list = None) -> list:
    conn = _conn()
    try:
        sql = "SELECT * FROM calendar_events WHERE 1=1"
        params = []
        if start:
            sql += " AND end_time >= ?"
            params.append(start)
        if end:
            sql += " AND start_time <= ?"
            params.append(end)
        if status:
            placeholders = ",".join("?" for _ in status)
            sql += f" AND status IN ({placeholders})"
            params.extend(status)
        sql += " ORDER BY start_time"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def list_by_source_email(source_email_id: str) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE source_email_id=?",
            (source_email_id,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def check_conflicts(start: str, end: str, exclude_id: str = None) -> list:
    """检查时间区间内是否存在已建档事件冲突"""
    try:
        st = datetime.fromisoformat(start)
    except Exception:
        return []
    try:
        et_datetime = datetime.fromisoformat(end)
        et = et_datetime if et_datetime > st else st + timedelta(hours=1)
    except Exception:
        et = st + timedelta(hours=1)

    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT * FROM calendar_events
               WHERE status IN ('created','confirmed')
                 AND start_time < ? AND end_time > ?""",
            (et.isoformat(timespec="minutes"), st.isoformat(timespec="minutes")),
        ).fetchall()
        result = []
        for r in rows:
            ev = _row_to_event(r)
            if exclude_id and ev["id"] == exclude_id:
                continue
            try:
                est = datetime.fromisoformat(ev["start_time"])
                eet = datetime.fromisoformat(ev["end_time"])
                overlap = (min(et, eet) - max(st, est)).total_seconds() / 60
                result.append({
                    "event_id": ev["id"],
                    "title": ev["title"],
                    "start": ev["start_time"],
                    "end": ev["end_time"],
                    "overlap_minutes": round(max(overlap, 0), 1),
                })
            except Exception:
                result.append({
                    "event_id": ev["id"], "title": ev["title"],
                    "start": ev["start_time"], "end": ev["end_time"],
                    "overlap_minutes": 0,
                })
        return result
    finally:
        conn.close()


def update_status(event_id: str, status: str) -> bool:
    conn = _conn()
    try:
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        cur = conn.execute(
            "UPDATE calendar_events SET status=?, updated_at=? WHERE id=?",
            (status, now, event_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_all() -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()