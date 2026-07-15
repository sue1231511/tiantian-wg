"""
后台进程（Process B）入口：跑所有自主/周期性任务——主动思考（TG+微信）、
提醒检查、凌晨总结（日/周/月/年）、自由活动、全平台压缩轮询、全平台摘要
整理。不起 HTTP 服务，不处理 QQ/TG/微信的实时消息收发——那部分继续由
main.py（消息进程，Process A）负责。

2026-07-14 消息进程/后台进程拆分记录：
原来这些任务和 QQ/TG/微信实时消息处理挤在同一个进程、同一个后台事件
循环里（main.py 的 start_background_tasks），消息进程越来越重，一个自由
活动或压缩任务卡住理论上都可能间接影响到消息处理的资源。拆开后两个进程
各自独立、互不阻塞，一个卡住/崩溃不影响另一个。

两个进程完全不共享内存，只共享 Supabase——所以这里用到的历史消息读取都
是直查 Supabase 的版本（context.get_chat_history_messages_db /
get_wx_history_messages_db），不是 Process A 那套内存缓存版本；微信主动
思考的 context_token 也改成每次现查 Supabase（wx_workers._fetch_valid_
context_token_db），不依赖 Process A 内存里的 _context_token_cache。

部署方式：同一个容器内，entrypoint.sh 同时拉起 main.py 和本文件两个独立
操作系统进程，任意一个退出就把另一个也杀掉、整个容器退出，交给 Zeabur
的重启策略统一处理，不会出现"一个进程还活着、另一个早死了没人知道"的
半死不活状态。不需要额外的 Zeabur 服务/端口/环境变量配置。
"""
import asyncio
import logging

# 独立进程，必须自己调用一次 basicConfig，否则 log.info(...) 会被 Python
# logging 内置的 lastResort handler（级别 WARNING）静默丢弃，根本不会出现
# 在 Zeabur 日志里——跟 main.py 顶部那段配置的背景说明是同一个坑。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)

log = logging.getLogger(__name__)


async def _main():
    from workers import (
        async_proactive_thinking,
        async_reminder_checker,
        async_nightly_summary,
        async_free_activity,
        async_platform_compress_poller,
        async_platform_summary_maintenance,
    )
    from wx_workers import async_wx_proactive_thinking

    coros = [
        async_proactive_thinking(),
        async_reminder_checker(),
        async_nightly_summary(),
        async_free_activity(),
        async_wx_proactive_thinking(),
        async_platform_compress_poller(),
        async_platform_summary_maintenance(),
    ]
    log.info(
        "🚀 [后台进程] 启动，%d 个常驻任务：主动思考(TG)、提醒检查、凌晨总结、"
        "自由活动、主动思考(微信)、全平台压缩轮询、全平台摘要整理",
        len(coros),
    )
    await asyncio.gather(*coros, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(_main())
