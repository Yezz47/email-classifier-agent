"""邮件分类管理工作流 - 状态定义"""

from typing import Annotated, Optional, Any
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage

# 默认保留最近 20 轮对话 (40 条消息)
MAX_MESSAGES = 40


def _windowed_messages(old, new):
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class EmailWorkflowState(MessagesState):
    """邮件分类管理工作流状态"""
    # 对话消息（带滑动窗口）
    messages: Annotated[list[AnyMessage], _windowed_messages]
    # 用户意图: "classify" | "send" | "daily_summary" | "chat"
    current_intent: str
    # 邮件获取参数（从用户消息中提取）
    fetch_params: dict
    # 获取到的邮件列表
    emails: list[dict]
    # 分类结果
    classifications: list[dict]
    # 分类统计
    category_counts: dict
    # 最终报告内容
    report: str
    # 错误信息
    error: str
