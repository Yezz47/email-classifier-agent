"""邮件分类管理智能体 - 自动获取、分类、标记 Gmail 邮件"""

import os
import json
import logging
from typing import Annotated

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langchain.agents.middleware import wrap_tool_call
from langchain.messages import ToolMessage
from langchain.tools import tool

from coze_coding_utils.runtime_ctx.context import default_headers
from storage.memory.memory_saver import get_memory_saver
from tools.email_fetcher import fetch_emails
from tools.email_labeler import label_email
from tools.send_email import send_email
from tools.daily_summary import daily_email_summary
from tools.coze_workflow import call_email_workflow
from tools import calendar_manager as _cm

logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"

# 默认保留最近 20 轮对话 (40 条消息)
MAX_MESSAGES = 40


def _windowed_messages(old, new):
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


@tool
def query_calendar_events(start_time: str, end_time: str) -> str:
    """查询指定时间段内的已有日历日程，用于检查日程冲突或了解已有安排。
    start_time/end_time 为 ISO 格式时间字符串，如 '2026-09-10T00:00:00+08:00'。"""
    try:
        result = _cm.query_calendar_events(start_time, end_time)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"查询日历失败: {e}"}, ensure_ascii=False)


@tool
def create_calendar_event(title: str, start_time: str, end_time: str, location: str = "",
                          description: str = "", reminder_minutes: int = 30) -> str:
    """创建一条日历提醒事件。title为事项名称，start_time/end_time为ISO时间，location为地点或会议链接，
    description为描述，reminder_minutes为提前提醒分钟数。返回创建结果。"""
    try:
        event = {
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "description": description,
            "reminders": [f"-{reminder_minutes}min"],
            "source": "email-classifier",
            "source_email_id": "",
        }
        result = _cm.create_calendar_event(event)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"创建日历事件失败: {e}"}, ensure_ascii=False)


@tool
def verify_calendar_event(event_id: str) -> str:
    """回查日历事件是否创建成功。event_id为创建时返回的事件ID。"""
    try:
        result = _cm.verify_calendar_event(event_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"回查日历事件失败: {e}"}, ensure_ascii=False)


@wrap_tool_call
def handle_tool_errors(request, handler):
    """Handle tool execution errors with custom messages."""
    try:
        return handler(request)
    except Exception as e:
        logger.error(f"Tool execution error: {e}", exc_info=True)
        return ToolMessage(
            content=f"工具执行出错: {str(e)}，请检查输入参数后重试。",
            tool_call_id=request.tool_call["id"],
        )


def build_agent(ctx=None):
    """构建邮件分类管理智能体"""
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    llm = ChatOpenAI(
        model=cfg["config"].get("model"),
        api_key=api_key,
        base_url=base_url,
        temperature=cfg["config"].get("temperature", 0.3),
        streaming=True,
        timeout=cfg["config"].get("timeout", 300),
        extra_body={
            "thinking": {
                "type": cfg["config"].get("thinking", "disabled")
            }
        },
        default_headers=default_headers(ctx) if ctx else {},
    )

    agent = create_agent(
        model=llm,
        system_prompt=cfg.get("sp"),
        tools=[fetch_emails, label_email, send_email, daily_email_summary, call_email_workflow,
               query_calendar_events, create_calendar_event, verify_calendar_event],
        middleware=[handle_tool_errors],
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
    )

    return agent
