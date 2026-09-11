"""LLM 文本生成助手

适配基于 integration.coze.cn/api/v3 的 SSE-only 流式代理端点：
该端点无论是否请求流式都以 SSE 分块返回，不能直接用非流式的
ChatOpenAI.invoke()（会把流式文本解析成 str 导致报错），
因此这里统一用 openai SDK stream=True 手工消费并拼接文本。
"""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


def _load_llm_config() -> dict:
    """读取 LLM 配置（模型 ID 等）"""
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, "config/agent_llm_config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def chat_completion_text(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 3000,
    timeout: Optional[float] = 300,
) -> str:
    """通过流式接口调用 LLM 生成文本，返回拼接后的完整内容。

    Args:
        prompt: 发给模型的用户提示词。
        temperature: 采样温度。
        max_tokens: 生成的最大 token 数。
        timeout: 请求超时秒数。

    Returns:
        模型返回的文本内容。
    """
    cfg = _load_llm_config()["config"]
    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    thinking = cfg.get("thinking", "disabled")
    stream = client.chat.completions.create(
        model=cfg.get("model"),
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        extra_body={"thinking": {"type": thinking}},
    )

    parts = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            parts.append(delta.content)
    return "".join(parts)