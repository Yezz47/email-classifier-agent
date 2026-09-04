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

from coze_coding_utils.runtime_ctx.context import default_headers
from storage.memory.memory_saver import get_memory_saver
from tools.email_fetcher import fetch_emails
from tools.email_labeler import label_email
from tools.send_email import send_email
from tools.daily_summary import daily_email_summary
from tools.coze_workflow import call_email_workflow

logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"

# 默认保留最近 20 轮对话 (40 条消息)
MAX_MESSAGES = 40


def _windowed_messages(old, new):
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


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
        tools=[fetch_emails, label_email, send_email, daily_email_summary, call_email_workflow],
        middleware=[handle_tool_errors],
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
    )

    return agent
