import os
import asyncio
import json
import logging
import uuid
import time
import requests

import websockets
from websockets.exceptions import ConnectionClosed

from bg_executor import track_task

log = logging.getLogger(__name__)

NAPCAT_WS_URL    = os.environ.get("NAPCAT_WS_URL", "ws://napcat.zeabur.internal:3001")
NAPCAT_WS_TOKEN  = os.environ.get("NAPCAT_WS_TOKEN", "")
PUSHPLUS_TOKEN   = os.environ.get("PUSHPLUS_TOKEN", "")

_last_notify_time: float = 0.0
_NOTIFY_COOLDOWN  = 1800  # 30 分钟内只推一次


def _notify_disconnect(reason: str = ""):
    """断线时推送微信通知，10 分钟冷却"""
    global _last_notify_time
    if not PUSHPLUS_TOKEN:
        return
    now = time.time()
    if now - _last_notify_time < _NOTIFY_COOLDOWN:
        return
    _last_notify_time = now
    try:
        now_str = time.strftime("%H:%M:%S")
        content = f"NapCat QQ 连接已断开，请前往 NapCat 网页重新登录。<br>原因：{reason or '未知'}<br>时间：{now_str}"
        requests.get(
            "http://www.pushplus.plus/send",
            params={"token": PUSHPLUS_TOKEN, "title": "⚠️ QQ 机器人掉线了", "content": content, "template": "html"},
            timeout=8,
        )
        print("📱 已发送断线微信通知")
    except Exception as e:
        log.warning("[_notify_disconnect] 推送通知失败 reason=%s: %s", reason, e, exc_info=True)

_ws_conn  = None
_ws_loop: asyncio.AbstractEventLoop | None = None
_send_lock = asyncio.Lock()
_pending_replies: dict[str, asyncio.Future] = {}


def send_qq_msg_threadsafe(target_type: str, target_id: int, message, timeout: float = 15.0) -> bool:
    """线程安全版 send_qq_msg，给跑在普通线程里的工具函数
    （send_qq_sticker / send_qq_voice 等）使用。

    背景：send_qq_msg 内部的 _send_lock（asyncio.Lock）和 _WSAdapter 包装的
    ASGI send 都绑定在 NapCat WebSocket 所在的主事件循环上。之前工具函数在
    asyncio.to_thread 的子线程里用 asyncio.run() 新开临时循环去调 send_qq_msg，
    Python 3.10+ 的 asyncio 原语跨循环使用会直接抛
    RuntimeError: ... is bound to a different event loop ——
    这就是表情包/语音"偶尔能发偶尔失败"的根因。
    这里用 run_coroutine_threadsafe 把整个发送协程调度回它所属的主循环执行，
    在调用线程里同步等待结果。返回 True 表示已成功交给 NapCat 发送。"""
    loop = _ws_loop
    if loop is None or loop.is_closed() or _ws_conn is None:
        log.error(
            "[send_qq_msg_threadsafe] NapCat 未连接（loop存在=%s conn存在=%s），无法发送 target=%s/%s",
            loop is not None, _ws_conn is not None, target_type, target_id,
        )
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(send_qq_msg(target_type, target_id, message), loop)
        fut.result(timeout=timeout)
        return True
    except Exception as e:
        log.error(
            "[send_qq_msg_threadsafe] 发送失败 target=%s/%s: %s",
            target_type, target_id, e, exc_info=True,
        )
        return False


async def _send_action(action: str, params: dict):
    global _ws_conn
    if not _ws_conn:
        log.warning("[_send_action] QQ WS 未连接，无法发送 action=%s params=%s", action, params)
        return
    payload = json.dumps({"action": action, "params": params, "echo": str(uuid.uuid4())})
    async with _send_lock:
        try:
            await _ws_conn.send(payload)
            if action not in ("send_private_msg", "send_group_msg"):
                print(f"📤 发送 action={action} params={params}")
        except Exception as e:
            log.error("[_send_action] QQ 发送失败 action=%s params=%s: %s", action, params, e, exc_info=True)


async def _send_action_and_wait(action: str, params: dict, timeout: float = 5.0) -> dict | None:
    """发送 action 并等待 NapCat 响应"""
    global _ws_conn
    if not _ws_conn:
        return None
    echo = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending_replies[echo] = fut
    payload = json.dumps({"action": action, "params": params, "echo": echo})
    async with _send_lock:
        try:
            await _ws_conn.send(payload)
        except Exception as e:
            log.error(
                "[_send_action_and_wait] QQ 发送失败 action=%s params=%s: %s",
                action, params, e, exc_info=True,
            )
            _pending_replies.pop(echo, None)
            return None
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        _pending_replies.pop(echo, None)
        return None


async def get_msg(msg_id: int) -> dict | None:
    """获取消息详情"""
    result = await _send_action_and_wait("get_msg", {"message_id": msg_id})
    if result and result.get("status") == "ok":
        return result.get("data")
    return None


async def get_group_member_info(group_id: int, user_id: int) -> dict | None:
    """获取群成员信息"""
    result = await _send_action_and_wait(
        "get_group_member_info",
        {"group_id": group_id, "user_id": user_id, "no_cache": False}
    )
    if result and result.get("status") == "ok":
        return result.get("data")
    return None


async def get_group_info(group_id: int) -> dict | None:
    """获取群信息"""
    result = await _send_action_and_wait(
        "get_group_info",
        {"group_id": group_id, "no_cache": False}
    )
    if result and result.get("status") == "ok":
        return result.get("data")
    return None


async def send_qq_msg(target_type: str, target_id: int, message):
    """发送QQ消息。target_type: 'private' | 'group'
    message 可以是纯字符串，也可以是 OneBot v11 消息段数组
    （比如 [{"type":"reply","data":{"id":"123"}}, {"type":"at","data":{"qq":"456"}}, {"type":"text","data":{"text":"..."}}]），
    用于真实引用/真实@，两种格式 NapCat/SnowLuma 都原生支持，这里原样透传不用做转换。"""
    if target_type == "private":
        await _send_action("send_private_msg", {"user_id": target_id, "message": message})
    else:
        await _send_action("send_group_msg", {"group_id": target_id, "message": message})


async def send_poke(user_id: int, group_id: int | None = None):
    """戳一戳某人。群聊传 group_id，私聊不传"""
    if group_id:
        await _send_action("group_poke", {"group_id": group_id, "user_id": user_id})
    else:
        await _send_action("friend_poke", {"user_id": user_id})


RECONNECT_INITIAL_DELAY = 2
RECONNECT_MAX_DELAY     = 60
RECONNECT_BACKOFF       = 2

_last_heartbeat_time:   float = 0.0
_last_offline_log_time: float = 0.0
_OFFLINE_LOG_INTERVAL         = 300  # 离线日志5分钟打一次，避免刷屏


async def handle_napcat_ws_forward(scope, receive, send):
    """正向WS模式：NapCat主动连进来"""
    global _ws_conn, _ws_loop, _last_heartbeat_time

    # 验证token
    headers_dict = {k.decode("utf-8").lower(): v.decode("utf-8") for k, v in scope.get("headers", [])}
    auth = headers_dict.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    if NAPCAT_WS_TOKEN and auth != NAPCAT_WS_TOKEN:
        await send({"type": "websocket.close", "code": 1008})
        return

    await send({"type": "websocket.accept"})
    print("✅ NapCat 正向WS 已连接！")

    class _WSAdapter:
        async def send(self, text):
            await send({"type": "websocket.send", "text": text})

    _ws_conn = _WSAdapter()
    # 记录当前（uvicorn 主）事件循环，供 send_qq_msg_threadsafe 从工具线程
    # 把发送协程调度回这个循环执行。
    _ws_loop = asyncio.get_running_loop()
    _last_heartbeat_time = time.time()

    from qq_workers import handle_qq_event

    try:
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                print("⚠️ NapCat 断开连接")
                _notify_disconnect("NapCat WebSocket 主动断开")
                break
            if msg["type"] != "websocket.receive":
                continue
            raw = msg.get("text", "")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception as e:
                log.warning(
                    "[handle_napcat_ws_forward] 解析 NapCat 消息 JSON 失败: %s | raw=%r",
                    e, raw[:200], exc_info=True,
                )
                continue

            if data.get("post_type") == "meta_event" and data.get("meta_event_type") == "heartbeat":
                _last_heartbeat_time = time.time()
                if not data.get("status", {}).get("online", True):
                    global _last_offline_log_time
                    _now = time.time()
                    if _now - _last_offline_log_time >= _OFFLINE_LOG_INTERVAL:
                        _last_offline_log_time = _now
                        print("⚠️ [Heartbeat] QQ 账号已离线！")
                        _notify_disconnect("心跳检测到 QQ 账号离线")
                continue

            if "KickedOffLine" in raw or (
                data.get("post_type") == "meta_event"
                and data.get("sub_type") in ("disable", "offline")
            ):
                _notify_disconnect("QQ 账号被踢下线")
                continue

            echo = data.get("echo")
            if echo and echo in _pending_replies:
                fut = _pending_replies.pop(echo)
                if not fut.done():
                    fut.set_result(data)
                continue

            if data.get("post_type"):
                track_task(asyncio.create_task(handle_qq_event(data)))
    except Exception as e:
        log.error("[handle_napcat_ws_forward] NapCat 正向WS 错误: %s", e, exc_info=True)
        _notify_disconnect(f"NapCat WS 异常断开: {e}")
    finally:
        _ws_conn = None
        for fut in list(_pending_replies.values()):
            if not fut.done():
                fut.cancel()
        _pending_replies.clear()
        print("NapCat 连接已关闭")


async def async_qq_bot():
    """正向WS模式：监控心跳超时，检测僵死连接"""
    print("🔌 QQ Bot 正向WS模式，等待 NapCat 连入 /qq-ws ...")

    from qq_workers import _restore_member_names
    await asyncio.to_thread(_restore_member_names)

    HEARTBEAT_TIMEOUT = 90  # 90秒无心跳视为僵死
    CHECK_INTERVAL = 30
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        if _ws_conn is None:
            # 之前连接过但现在彻底断了，定期提醒（受 _notify_disconnect 内部冷却控制）
            if _last_heartbeat_time > 0:
                _notify_disconnect("NapCat 长时间未重连，QQ 机器人仍处于离线状态")
            continue
        if _last_heartbeat_time <= 0:
            continue
        now = time.time()
        if now - _last_heartbeat_time > HEARTBEAT_TIMEOUT:
            print(f"⚠️ [Heartbeat] 超过{HEARTBEAT_TIMEOUT}秒未收到心跳，连接可能已僵死")
            _notify_disconnect(f"超过{HEARTBEAT_TIMEOUT}秒未收到 NapCat 心跳，连接可能已僵死")
