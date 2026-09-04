"""Coze 工作流调用工具 - 调用已部署的邮件分类管理工作流"""

import json
import logging
import os

import requests
from langchain.tools import tool
from coze_coding_utils.log.write_log import request_context
from coze_coding_utils.runtime_ctx.context import new_context

logger = logging.getLogger(__name__)

# 工作流部署地址（从 Coze Coding 部署页面获取）
WORKFLOW_URL = "https://4ry8bynqyy.coze.site/run"


def _call_workflow(days_to_fetch: int = 7, max_emails: int = 50) -> str:
    """调用 Coze 邮件分类管理工作流的核心逻辑"""
    ctx = request_context.get() or new_context(method="call_email_workflow")
    try:
        api_token = os.environ.get("COZE_WORKFLOW_API_TOKEN", "")
        if not api_token:
            return json.dumps({
                "status": "error",
                "message": "未配置 COZE_WORKFLOW_API_TOKEN 环境变量，无法调用工作流"
            }, ensure_ascii=False)

        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "days_to_fetch": days_to_fetch,
            "max_emails": max_emails,
        }

        logger.info(f"调用邮件分类工作流: days_to_fetch={days_to_fetch}, max_emails={max_emails}")

        response = requests.post(
            WORKFLOW_URL,
            headers=headers,
            json=payload,
            timeout=300,  # 5 分钟超时
        )

        if response.status_code == 200:
            result = response.json()
            logger.info(f"工作流调用成功: {json.dumps(result, ensure_ascii=False)[:200]}")
            return json.dumps({
                "status": "success",
                "data": result,
            }, ensure_ascii=False)
        else:
            logger.error(f"工作流调用失败: status={response.status_code}, body={response.text[:500]}")
            return json.dumps({
                "status": "error",
                "message": f"工作流调用失败 (HTTP {response.status_code}): {response.text[:500]}"
            }, ensure_ascii=False)

    except requests.exceptions.Timeout:
        logger.error("工作流调用超时")
        return json.dumps({
            "status": "error",
            "message": "工作流执行超时（超过5分钟），请减少邮件数量后重试"
        }, ensure_ascii=False)
    except requests.exceptions.ConnectionError as e:
        logger.error(f"工作流连接失败: {e}")
        return json.dumps({
            "status": "error",
            "message": f"无法连接到工作流服务: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"调用工作流异常: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "message": f"调用工作流失败: {str(e)}"
        }, ensure_ascii=False)


@tool
def call_email_workflow(
    days_to_fetch: int = 7,
    max_emails: int = 50,
) -> str:
    """调用已部署的邮件分类管理工作流。工作流会自动获取邮件、智能分类、生成报告并发送每日摘要邮件。

    Args:
        days_to_fetch: 获取最近几天的邮件，默认 7 天
        max_emails: 最多获取多少封邮件，默认 50 封

    Returns:
        JSON 格式的工作流执行结果，包含分类统计、报告内容和发送状态
    """
    if days_to_fetch < 1:
        days_to_fetch = 1
    if days_to_fetch > 30:
        days_to_fetch = 30
    if max_emails < 1:
        max_emails = 1
    if max_emails > 100:
        max_emails = 100
    return _call_workflow(days_to_fetch=days_to_fetch, max_emails=max_emails)
