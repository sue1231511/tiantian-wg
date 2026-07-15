"""
Mem0 + Pinecone 混合长期记忆客户端
写入采用双写：Mem0 和 Pinecone 各自独立尝试，互不依赖，任一成功即记录成功。
检索仍是 Mem0 优先，失败时降级到 Pinecone 向量检索。
"""
import os
import uuid
import logging
import requests

log = logging.getLogger("mem0_client")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] mem0_client: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

MEM0_API_KEY = os.environ.get("MEM0_API_KEY", "").strip()
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "").strip()
DOUBAO_EMBEDDING_EP = os.environ.get("DOUBAO_EMBEDDING_EP", "").strip()
SILICON_API_KEY = os.environ.get("SILICON_API_KEY", "").strip()
SILICONFLOW_EMBEDDING_MODEL = os.environ.get("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3").strip()
MEM0_USER_ID = os.environ.get("MEM0_USER_ID", "user")

_http = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=2)
_http.mount("http://", _adapter)
_http.mount("https://", _adapter)


def _get_embedding(text: str) -> list:
    # 优先硅基流动
    if SILICON_API_KEY:
        try:
            resp = _http.post(
                "https://api.siliconflow.cn/v1/embeddings",
                json={"model": SILICONFLOW_EMBEDDING_MODEL, "input": text},
                headers={"Authorization": f"Bearer {SILICON_API_KEY}", "Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"❌ [Embedding/SiliconFlow] HTTP {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            embedding_data = data.get("data")
            if isinstance(embedding_data, list) and embedding_data:
                vec = [float(x) for x in embedding_data[0].get("embedding", [])]
                if vec:
                    print(f"✅ [Embedding/SiliconFlow] 成功，model={SILICONFLOW_EMBEDDING_MODEL}，维度={len(vec)}")
                    return vec
            print(f"❌ [Embedding/SiliconFlow] 响应格式异常: {str(data)[:200]}")
        except Exception as e:
            print(f"❌ [Embedding/SiliconFlow] 请求异常: {e}")
        return []

    # fallback 豆包
    if not DOUBAO_API_KEY or not DOUBAO_EMBEDDING_EP:
        print("⚠️ [Embedding] SILICONFLOW_API_KEY 与 DOUBAO_API_KEY 均未配置，跳过")
        return []
    try:
        resp = _http.post(
            "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal",
            json={"model": DOUBAO_EMBEDDING_EP, "input": [{"type": "text", "text": text}]},
            headers={"Authorization": f"Bearer {DOUBAO_API_KEY}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"❌ [Embedding/Doubao] HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        embedding_data = data.get("data")
        vec = []
        if isinstance(embedding_data, list) and embedding_data:
            vec = [float(x) for x in embedding_data[0].get("embedding", [])]
        elif isinstance(embedding_data, dict):
            vec = [float(x) for x in embedding_data.get("embedding", [])]
        if vec:
            print(f"✅ [Embedding/Doubao] 成功，维度={len(vec)}")
            return vec
        print(f"❌ [Embedding/Doubao] 响应格式异常: {str(data)[:200]}")
    except Exception as e:
        print(f"❌ [Embedding/Doubao] 请求异常: {e}")
    return []


class HybridMemoryClient:
    def __init__(self):
        self.mem0 = None
        self.index = None

        if MEM0_API_KEY:
            try:
                from mem0 import MemoryClient
                self.mem0 = MemoryClient(api_key=MEM0_API_KEY)
                log.info("[Mem0] 初始化成功")
            except Exception as e:
                log.error("[Mem0] 初始化失败: %s", e, exc_info=True)
        else:
            log.warning("[Mem0] MEM0_API_KEY 未配置，本进程生命周期内 self.mem0 将一直是 None")

        if PINECONE_KEY:
            try:
                from pinecone import Pinecone
                pc = Pinecone(api_key=PINECONE_KEY)
                idx_name = os.environ.get("PINECONE_INDEX_NAME", "notion-brain")
                self.index = pc.Index(idx_name)
                log.info("[Pinecone] 初始化成功 index=%s", idx_name)
            except Exception as e:
                log.error("[Pinecone] 初始化失败: %s", e, exc_info=True)
        else:
            log.warning("[Pinecone] PINECONE_API_KEY 未配置，本进程生命周期内 self.index 将一直是 None")

    @property
    def available(self) -> bool:
        return self.mem0 is not None or self.index is not None

    _LEGACY_USER_IDS: list[str] = []

    def search(self, query: str, user_id: str = None, limit: int = 3) -> list:
        uid = user_id or MEM0_USER_ID
        if not query or not query.strip():
            return []

        if self.mem0:
            try:
                all_items = []
                seen_ids = set()
                for search_uid in [uid] + [lid for lid in self._LEGACY_USER_IDS if lid != uid]:
                    res = self.mem0.search(query=query, filters={"user_id": search_uid}, limit=limit)
                    items = res.get("results", res) if isinstance(res, dict) else res
                    if isinstance(items, list):
                        for item in items:
                            mid = item.get("id", id(item))
                            if mid not in seen_ids:
                                seen_ids.add(mid)
                                all_items.append(item)
                if all_items:
                    all_items.sort(key=lambda x: x.get("score", 0), reverse=True)
                    return all_items[:limit]
            except Exception as e:
                log.error("[Mem0] 搜索降级 query=%r uid=%s: %s", query[:80], uid, e)
        else:
            log.info("[Mem0] self.mem0 为 None，跳过 Mem0 搜索，尝试 Pinecone query=%r", query[:80])

        if self.index:
            try:
                vec = _get_embedding(query)
                if vec:
                    print(f"🔍 [Pinecone] 开始查询，uid={uid}")
                    res = self.index.query(vector=vec, top_k=limit, include_metadata=True)
                    results = [{"memory": m.metadata.get("text", ""), "id": m.id}
                            for m in res.matches if m.metadata]
                    print(f"✅ [Pinecone] 查询完成，命中={len(results)}")
                    return results
                else:
                    log.warning("[Pinecone] embedding 为空，跳过查询 query=%r", query[:80])
            except Exception as e:
                log.error("[Pinecone] 搜索失败 query=%r uid=%s: %s", query[:80], uid, e)
        else:
            log.error("[Memory] self.mem0 与 self.index 均为 None，搜索彻底没有结果 query=%r", query[:80])
        return []

    def add(self, messages: list, user_id: str = None) -> bool:
        """双写：Mem0 和 Pinecone 各自独立尝试写入，互不影响、互不依赖。
        任意一边成功即视为这一轮对话被记录下来了。"""
        uid = user_id or MEM0_USER_ID
        msg_preview = " | ".join(
            f"{m.get('role')}:{str(m.get('content'))[:40]}" for m in messages if isinstance(m, dict)
        )

        mem0_ok = False
        if self.mem0:
            try:
                self.mem0.add(messages, user_id=uid)
                mem0_ok = True
                log.info("[Mem0] 写入成功 uid=%s preview=%r", uid, msg_preview)
            except Exception as e:
                log.error("[Mem0] 写入失败 uid=%s preview=%r: %s", uid, msg_preview, e)
        else:
            log.info("[Mem0] self.mem0 为 None，跳过 Mem0 写入 uid=%s preview=%r", uid, msg_preview)

        pinecone_ok = False
        if self.index:
            try:
                text = " | ".join(
                    f"{m.get('role')}: {m.get('content')}"
                    for m in messages if isinstance(m, dict)
                )
                vec = _get_embedding(text)
                if vec:
                    print(f"💾 [Pinecone] 开始写入，uid={uid}")
                    self.index.upsert(vectors=[{
                        "id": str(uuid.uuid4()),
                        "values": vec,
                        "metadata": {"text": text, "user_id": uid},
                    }])
                    pinecone_ok = True
                    print("✅ [Pinecone] 写入成功")
                else:
                    log.warning("[Pinecone] embedding 为空，跳过写入 uid=%s preview=%r", uid, msg_preview)
            except Exception as e:
                log.error("[Pinecone] 写入失败 uid=%s preview=%r: %s", uid, msg_preview, e)
        else:
            log.info("[Pinecone] self.index 为 None，跳过 Pinecone 写入 uid=%s preview=%r", uid, msg_preview)

        if not mem0_ok and not pinecone_ok:
            log.error(
                "[Memory] Mem0 和 Pinecone 都没写入成功，这一轮对话彻底没有被记录到任何地方！uid=%s preview=%r",
                uid, msg_preview,
            )
        return mem0_ok or pinecone_ok

    def get_all(self, user_id: str = None) -> list:
        uid = user_id or MEM0_USER_ID
        if self.mem0:
            try:
                return self.mem0.get_all(user_id=uid)
            except Exception:
                pass
        return []

    def delete(self, memory_id: str):
        if self.mem0:
            try:
                self.mem0.delete(memory_id)
            except Exception:
                pass
        if self.index:
            try:
                self.index.delete(ids=[memory_id])
            except Exception:
                pass


def search_mem0_context(query: str, limit: int = 3) -> str:
    """用 query 检索 Mem0，返回可直接注入 context 的文本"""
    items = mem0.search(query, limit=limit)
    if not items:
        return ""
    lines = []
    for m in items:
        text = m.get("memory", str(m)) if isinstance(m, dict) else str(m)
        lines.append(f"- {text}")
    return "【深层记忆（Mem0 语义检索）】\n" + "\n".join(lines)


def write_mem0_chat(user_text: str, assistant_text: str):
    """把一轮对话写入 Mem0 形成长期记忆"""
    log.info(
        "[write_mem0_chat] 进入函数 available=%s self.mem0存在=%s self.index存在=%s user_text_len=%s",
        mem0.available, mem0.mem0 is not None, mem0.index is not None, len(user_text or ""),
    )
    if not mem0.available:
        log.error(
            "[write_mem0_chat] 静默退出：self.mem0 和 self.index 都是 None，本轮对话没有任何地方可以记录！"
            " user_text=%r assistant_text=%r",
            (user_text or "")[:80], (assistant_text or "")[:80],
        )
        return
    if not user_text:
        log.warning("[write_mem0_chat] user_text 为空，跳过写入")
        return
    try:
        ok = mem0.add([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ])
        log.info("[write_mem0_chat] mem0.add 执行完毕，返回值=%s", ok)
    except Exception as e:
        log.error(
            "[write_mem0_chat] mem0.add 抛出了未被内部捕获的异常 user_text=%r: %s",
            (user_text or "")[:80], e,
        )


# 全局单例
mem0 = HybridMemoryClient()
