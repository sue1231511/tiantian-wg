import os
import json
import time
import logging
import threading

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

# 未读邮件结果缓存：get_unread_emails 在 build_bot_context 的前置链路上，
# 每次 TG/微信回复前都会被调用，而它内部是 1 次 list + 每封邮件 1 次 get
# 的串行 Google API 往返（最多 6 次）。加 60 秒 TTL 缓存把这段开销从
# "每条回复一次"降到"每分钟最多一次"，邮件提醒最多滞后 1 分钟，无感知。
_unread_cache: tuple[float, str] | None = None
_unread_cache_lock = threading.Lock()
_UNREAD_CACHE_TTL = 60


def _get_gmail_service():
    """构建 Gmail 服务客户端。
    通过 AuthorizedHttp(httplib2.Http(timeout=15)) 给底层 HTTP 设置超时——
    googleapiclient 默认不带超时，Google 接口抖动时整个 build_bot_context
    会跟着无限挂起，进而卡住 TG/微信回复。"""
    token_json = os.environ.get("GOOGLE_USER_TOKEN_JSON")
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=15))
        return build("gmail", "v1", http=authed_http)
    except Exception as e:
        log.error("[_get_gmail_service] Gmail 认证失败: %s", e, exc_info=True)
        return None


def get_unread_emails(max_results: int = 5) -> str:
    """获取最近未读邮件摘要，返回可注入 context 的文本（60 秒 TTL 缓存）"""
    global _unread_cache
    now = time.time()
    cached = _unread_cache
    if cached and now - cached[0] < _UNREAD_CACHE_TTL:
        return cached[1]

    with _unread_cache_lock:
        cached = _unread_cache
        if cached and time.time() - cached[0] < _UNREAD_CACHE_TTL:
            return cached[1]

        result = _fetch_unread_emails(max_results)
        _unread_cache = (time.time(), result)
        return result


def _fetch_unread_emails(max_results: int = 5) -> str:
    service = _get_gmail_service()
    if not service:
        return ""
    try:
        results = service.users().messages().list(
            userId="me", q="is:unread", maxResults=max_results
        ).execute()
        msg_list = results.get("messages", [])

        if not msg_list:
            return "【Gmail】无未读邮件"

        total = results.get("resultSizeEstimate", len(msg_list))
        lines = [f"【Gmail 未读邮件（约{total}封，显示最近{len(msg_list)}封）】"]

        for msg_info in msg_list:
            m = service.users().messages().get(
                userId="me",
                id=msg_info["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            h_map = {
                h["name"]: h["value"]
                for h in m.get("payload", {}).get("headers", [])
            }
            snippet = (m.get("snippet") or "")[:120]
            lines.append(
                f"- 来自: {h_map.get('From', '未知')}\n"
                f"  主题: {h_map.get('Subject', '(无主题)')}\n"
                f"  摘要: {snippet}"
            )

        return "\n".join(lines)

    except Exception as e:
        log.error("[_fetch_unread_emails] Gmail 获取失败 max_results=%s: %s", max_results, e, exc_info=True)
        return ""
