"""邮件分类管理工作流 - 意图路由节点

使用 LLM 分析用户消息，判断用户意图并提取参数。
"""

import os
import json
import logging

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage

from coze_coding_utils.runtime_ctx.context import default_headers
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context
from graphs.state import EmailWorkflowState

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """你是一个邮件管理系统的意图路由器。分析用户的消息，判断用户的意图。

你可以返回以下意图之一：
1. "classify" - 用户想要获取并分类邮件（如"帮我分类邮件"、"查看未读邮件"、"整理收件箱"）
2. "send" - 用户想要发送邮件（如"帮我发一封邮件"、"回复邮件"）
3. "daily_summary" - 用户想要生成每日邮件总结（如"今日邮件总结"、"今天的邮件概况"）
4. "chat" - 一般性对话，不属于以上任何类别

对于 "classify" 意图，你还需要提取以下参数（如果用户提到了的话）：
- folder: 邮箱文件夹（默认 "INBOX"）
- limit: 获取数量上限（默认 10）
- unread_only: 是否只获取未读邮件（默认 false）
- search_from: 按发件人筛选
- search_subject: 按主题筛选

对于 "send" 意图，提取：
- to_addrs: 收件人
- subject: 主题
- content: 内容

请严格按以下 JSON 格式返回，不要输出其他内容：
{
  "intent": "classify" | "send" | "daily_summary" | "chat",
  "params": { ... }
}"""


def _get_llm(ctx=None):
    """获取 LLM 实例"""
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
        temperature=0.1,  # 路由用低温度，保证稳定性
        timeout=cfg["config"].get("timeout", 300),
        max_tokens=500,
        extra_body={
            "thinking": {
                "type": cfg["config"].get("thinking", "disabled")
            }
        },
        default_headers=default_headers(ctx) if ctx else {},
    )


def _parse_router_response(content: str) -> dict:
    """解析 LLM 路由响应"""
    # 兼容 markdown 代码块包裹
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(content)
        intent = result.get("intent", "chat")
        if intent not in ("classify", "send", "daily_summary", "chat"):
            intent = "chat"
        return {
            "intent": intent,
            "params": result.get("params", {}),
        }
    except json.JSONDecodeError:
        logger.warning(f"Router response not JSON: {content[:200]}")
        return {"intent": "chat", "params": {}}


async def router_node(state: EmailWorkflowState, ctx=None) -> dict:
    """意图路由节点：分析用户消息，判断意图"""
    ctx = request_context.get() or new_context(method="router_node")
    llm = _get_llm(ctx)

    # 获取最新的用户消息
    messages = state.get("messages", [])
    user_message = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content
            break

    if not user_message:
        return {
            "current_intent": "chat",
            "fetch_params": {},
        }

    # 调用 LLM 进行意图识别
    response = await llm.ainvoke([
        HumanMessage(content=ROUTER_PROMPT),
        HumanMessage(content=f"用户消息: {user_message}"),
    ])

    raw_content = response.content
    if isinstance(raw_content, list):
        content = " ".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in raw_content
        ).strip()
    else:
        content = str(raw_content).strip()

    result = _parse_router_response(content)
    logger.info(f"Router result: intent={result['intent']}, params={result['params']}")

    return {
        "current_intent": result["intent"],
        "fetch_params": result["params"],
    }


# 用于同步测试的包装
def router_node_sync(state: dict, ctx=None) -> dict:
    """同步版本的路由节点（用于测试）"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果已有事件循环在运行，创建新线程
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, router_node(state, ctx)).result()
                return result
        else:
            return loop.run_until_complete(router_node(state, ctx))
    except RuntimeError:
        return asyncio.run(router_node(state, ctx))
