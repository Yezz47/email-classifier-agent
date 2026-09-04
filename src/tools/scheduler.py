"""定时调度器 - 全天候自动邮件管理

任务列表：
1. 每小时整点：自动获取新邮件 → 分类 → 标记 → 重要邮件即时通知
2. 每天 23:30：生成并发送当日邮件总结报告
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# 全局调度器实例
_scheduler: AsyncIOScheduler = None


def _run_auto_manage():
    """定时任务回调：每小时自动邮件管理"""
    from tools.auto_manager import auto_manage_emails

    logger.info("=== 定时任务触发：开始自动邮件管理 ===")
    try:
        result = auto_manage_emails()
        logger.info(f"自动邮件管理完成: {result[:300]}")
    except Exception as e:
        logger.error(f"自动邮件管理定时任务执行失败: {e}", exc_info=True)


def _run_daily_summary():
    """定时任务回调：每日邮件总结"""
    from tools.daily_summary import _daily_summary_impl

    logger.info("=== 定时任务触发：开始执行每日邮件总结 ===")
    try:
        result = _daily_summary_impl()
        logger.info(f"每日邮件总结完成: {result[:200]}")
    except Exception as e:
        logger.error(f"每日邮件总结定时任务执行失败: {e}", exc_info=True)


def start_scheduler() -> AsyncIOScheduler:
    """启动定时调度器，注册全天候自动邮件管理任务"""
    global _scheduler

    if _scheduler is not None:
        logger.warning("调度器已在运行中，跳过重复启动")
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 任务1：每小时整点自动管理邮件（分类 + 标记 + 重要邮件通知）
    _scheduler.add_job(
        _run_auto_manage,
        CronTrigger(hour="*", minute=0, timezone="Asia/Shanghai"),
        id="auto_manage_emails",
        name="每小时自动邮件管理",
        replace_existing=True,
        misfire_grace_time=600,  # 允许 10 分钟容错
    )

    # 任务2：每天 23:30 生成并发送当日邮件总结
    _scheduler.add_job(
        _run_daily_summary,
        CronTrigger(hour=23, minute=30, timezone="Asia/Shanghai"),
        id="daily_email_summary",
        name="每日邮件总结",
        replace_existing=True,
        misfire_grace_time=300,  # 允许 5 分钟容错
    )

    _scheduler.start()
    logger.info("定时调度器已启动: 每小时自动管理 + 每天 23:30 总结")
    return _scheduler


def stop_scheduler():
    """停止定时调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度器已停止")


def get_scheduler() -> AsyncIOScheduler:
    """获取当前调度器实例"""
    return _scheduler
