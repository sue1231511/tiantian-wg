"""
全局共享、大小固定的后台线程池。

背景（[Errno 11] Resource temporarily unavailable 问题的根因）：
之前 main.py / context.py / workers.py / qq_workers.py / wx_workers.py 里
大量使用"来一个任务就 threading.Thread(target=fn, args=..., daemon=True).start()"
的写法做 fire-and-forget 持久化（写 Supabase / Mem0 / Pinecone）。这种写法每次
都会新建一个全新的 OS 线程，完全没有上限。一旦 Supabase/Mem0/Pinecone 响应变慢，
这些线程就会堆积不退出；TG私聊+群聊+QQ+微信+8个常驻后台协程同时活跃时，线程数
很容易在短时间内滚雪球式增长，撞上容器的进程/线程数上限（pids-limit），导致
后续任何 threading.Thread().start() 直接抛出
    OSError: [Errno 11] Resource temporarily unavailable

而 main.py 里这类调用发生在 /v1/chat/completions 的流式响应已经发送过
http.response.start 之后，且完全没有 try/except 保护：一旦这里抛出异常，
会被外层 except 兜底、尝试对同一个连接二次发送 response.start，触发 ASGI
协议冲突，导致这次响应既不能正常关闭也不会返回错误信息——Rikkahub 这类客户端
只能一直等数据，界面停在"加载中"。

解决方式：用一个大小固定的 ThreadPoolExecutor 复用线程。不管任务提交多快、
单个任务卡多久，线程数都不会再新增，只会在池子里排队等待，从根源上避免线程数失控。
所有原来的 threading.Thread(target=fn, args=..., daemon=True).start() 调用点
都应该改成 submit_background(fn, *args, **kwargs)。
"""
import logging
import concurrent.futures

log = logging.getLogger(__name__)

# 线程池大小：覆盖 Supabase/Mem0/Pinecone 等持久化任务的正常并发即可。
# 线程池的意义就在于"有限"——多出来的任务会排队而不是新开 OS 线程。
_MAX_WORKERS = 20

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_WORKERS,
    thread_name_prefix="bg-worker",
)


def _run_with_log(fn, args, kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as e:
        log.error(
            "[bg_executor] 后台任务执行异常 fn=%s args=%r kwargs=%r: %s",
            getattr(fn, "__name__", repr(fn)), args, kwargs, e,
            exc_info=True,
        )


def submit_background(fn, *args, **kwargs) -> None:
    """
    提交一个后台任务到共享线程池，fire-and-forget。

    - 不会像 threading.Thread(...).start() 那样无限新建 OS 线程；
      并发线程数固定为 _MAX_WORKERS，多出来的任务在线程池内部排队执行。
    - 任务内部抛出的异常会被捕获并完整记录（位置 + 异常栈 + 关键参数），
      不会静默丢失，也不会让调用方感知到异常。
    - submit 这一步本身理论上也可能失败，这里同样兜底只记日志、不向上抛异常：
      调用方几乎都是 fire-and-forget 场景（比如聊天记录持久化），不应该因为
      一次提交失败就影响到调用方的主流程（例如影响一个已经在发送中的
      HTTP 响应的正常收尾）。
    """
    try:
        _executor.submit(_run_with_log, fn, args, kwargs)
    except Exception as e:
        log.error(
            "[bg_executor] 提交后台任务失败 fn=%s args=%r kwargs=%r: %s",
            getattr(fn, "__name__", repr(fn)), args, kwargs, e,
            exc_info=True,
        )


# ── asyncio 后台任务防 GC 集合 ────────────────────────────────
# asyncio 官方文档明确警告：事件循环只持有任务的弱引用，
# asyncio.create_task 之后如果调用方不保存返回的 Task 引用，任务可能在
# 执行中途被垃圾回收，表现为"某条消息莫名其妙没被处理"。
# 所有 fire-and-forget 的 create_task 都应该经过 track_task 挂一份强引用，
# 任务结束后由 done_callback 自动移除，不会泄漏。
_bg_tasks: set = set()


def track_task(task) -> None:
    """给 fire-and-forget 的 asyncio.Task 挂一份强引用，防止执行中途被 GC。"""
    try:
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except Exception as e:
        log.error("[bg_executor] track_task 失败 task=%r: %s", task, e, exc_info=True)
