"""
语义向量服务（移植自 air1.0 her/core/embedding.py，2026-08 落地）。

接 OpenAI 兼容的 /embeddings 端点（Ollama 本地 /v1 端点也可），或本地
sentence-transformers 模型（endpoint 以 local:// 开头，如 local://BAAI/bge-small-zh-v1.5）。

设计原则（照搬 air1.0）：
  - 未配置、或调用失败时优雅降级 —— 返回 None，调用方回退字符重叠检索，air 不会崩。
  - 存储时就在条目上算好向量（有服务则算，无则留缺省）；检索时只对新查询算一次向量。
  - novelty：embedding_novelty(query_vec, ref_vecs) = 1 − max 余弦，对应设计文档 v_new。

远程模式用 urllib.request（纯标准库，与 rss.py 同风格，零第三方依赖）；
local:// 模式才需要 sentence-transformers（可选，懒加载）。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜EmbeddingService 类 —— 语义向量服务封装（本文件主体）
#        embed() / embed_one()：给文本批量/单个算向量。
#        没配置或调用失败 → 返回 None（air 降级字符重叠，不崩）；
#        degraded 标记当前是否处于降级，read_state 会把它暴露给 air。
#   段 2｜cosine / embedding_novelty —— 两个纯函数
#        余弦相似度 + 内容新奇值 v_new（双驱螺旋熵增账的原料）。
# =====================================================================
import json
import ssl
import urllib.request

# 与 fetch.py 同口径：语义向量服务是只读低危操作，放宽证书校验换可用性。
# embedding 挂了 air 会原地挂机（AGENTS.md 定），一个自签名/证书链不完整的端点
# 不该把她卡死——宁可放宽校验保证可用（2026-09-01 创建者拍板补上，此前只 webreader 有）。
_SSL_CTX = ssl._create_unverified_context()


# ---- 段 1：EmbeddingService（向量服务封装）----

class EmbeddingService:
    def __init__(self, endpoint: str = "", api_key: str = "",
                 model: str = "text-embedding-3-small", timeout: float = 20.0):
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        # 本地模式：endpoint 以 local:// 开头，后缀 sentence-transformers 模型名
        self.is_local = self.endpoint.startswith("local://")
        self.local_model = self.endpoint[len("local://"):] if self.is_local else ""
        if self.is_local:
            self.available = bool(self.local_model)
        else:
            self.available = bool(self.endpoint and self.api_key)
        self._local_model = None     # 懒加载的 SentenceTransformer
        self._local_loaded = False   # 避免反复重试加载
        self.degraded = False        # 最近一次调用是否失败（当前处于字符重叠降级）

    def _get_local_model(self):
        """懒加载本地 sentence-transformers 模型；失败则降级返回 None。"""
        if self._local_loaded:
            return self._local_model
        self._local_loaded = True
        try:
            from sentence_transformers import SentenceTransformer
            self._local_model = SentenceTransformer(self.local_model)
            print(f"[embedding] 本地模型已加载: {self.local_model}")
        except Exception as e:
            print(f"[embedding] 本地模型加载失败（降级字符重叠）: {e}")
            self._local_model = None
        return self._local_model

    def embed(self, texts: list) -> list | None:
        """批量取向量；不可用或失败返回 None（调用方降级）。

        成功置 degraded=False，失败置 degraded=True——read_state 会把它暴露给 air，
        让她知道自己正处在字符重叠降级（新奇值/相关度失真），别拿降级结果当真归因。
        """
        if not self.available or not texts:
            return None
        try:
            if self.is_local:
                model = self._get_local_model()
                if model is None:
                    self.degraded = True
                    return None
                # normalize_embeddings=True → 余弦≈点积，与 cosine() 兼容
                vecs = model.encode(texts, normalize_embeddings=True)
                self.degraded = False
                return [v.tolist() for v in vecs]
            req = urllib.request.Request(
                f"{self.endpoint}/embeddings",
                data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = [item["embedding"] for item in data.get("data", [])]
            ok = len(out) == len(texts)
            self.degraded = not ok
            return out if ok else None
        except Exception as e:
            self.degraded = True
            print(f"[embedding] 调用失败（降级字符重叠）: {e}")
            return None

    def embed_one(self, text: str) -> list | None:
        r = self.embed([text])
        return r[0] if r else None


# ---- 段 2：纯函数（余弦相似度 / 内容新奇值）----

def cosine(a: list, b: list) -> float:
    """余弦相似度；维度不一致或任一为空返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embedding_novelty(query_vec: list, ref_vecs: list) -> float:
    """内容新奇值 v_new = 1 − 与已有记忆的最大余弦相似度（离得越远越新奇）。

    对应双驱螺旋设计思路：v_new 是 1 − p 在内容点上的体现（事后实测）。
    ref_vecs 空 → 全新奇（没有任何参照）。返回值 0~1。
    """
    if not query_vec or not ref_vecs:
        return 1.0
    best = 0.0
    for rv in ref_vecs:
        c = cosine(query_vec, rv)
        if c > best:
            best = c
    return 1.0 - best
