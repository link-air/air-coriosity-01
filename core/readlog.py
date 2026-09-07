"""
已读账本：link → 状态 + 失败退避。

两把尺子分开（2026-09-01 创建者定，见《读世界重构设计稿_2026-09-01.md》第一节）：
- 印象分（预期新奇值）= 认知尺子，实时算、随记忆库变，**不记录读没读**
- 已读 = 行为账本，持久，只记读没读

状态机：
  new     没读过（不在表里）
  partial 读到了但被截断（正文长度 ≥ 提取上限）——允许再读，但不无限卡
  done    完整读完
  dead    放弃（失败 ≥ MAX_FAIL 次 / partial ≥ MAX_PARTIAL 次）

失败退避：失败第 N 次后，退避 BACKOFF[N-1] 轮内不再重试同一条，防一个 403 死链每轮都被重试。
退避只在「new / partial」上生效；done/dead 不再出现在待读列表里。

容量：CAP 条上限，超了优先淘汰最老的 done，partial/dead/有失败记录的一律保留。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class ReadLog —— 已读账本（link → 状态 + 失败退避，读世界去重用）：
#   段 1｜持久化 —— readlog.json 读写（先裁剪再落盘）
#   段 2｜去重键 —— key()：URL 归一化（去 fragment / utm 参数）
#   段 3｜查询 —— get / due（失败退避期内不让重试同一条）
#   段 4｜记录 —— record_done / record_partial / record_fail（状态机迁移）
#   段 5｜容量 —— _trim：超 CAP 淘汰最老的 done（partial/dead 保留）
# =====================================================================
import json
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.lock import atomic_write_json, read_text_retry

BACKOFF = [1, 4, 16, 64, 256]   # 失败第 N 次的退避轮数（第 5 次失败即转 dead）
MAX_FAIL = 5
MAX_PARTIAL = 3
CAP = 3000


class ReadLog:
    def __init__(self, config):
        self.file = config.data_root / "readlog.json"
        self._log = {}
        self._load()

    # ---- 持久化 ----

    def _load(self):
        try:
            if self.file.exists():
                d = json.loads(read_text_retry(self.file))
                if isinstance(d, dict):
                    self._log = d
        except Exception:
            self._log = {}

    def _save(self):
        self._trim()
        try:
            atomic_write_json(self.file, self._log)
        except Exception:
            pass

    # ---- 去重键 ----

    @staticmethod
    def key(link: str) -> str:
        """URL 归一化：去 fragment、去 utm_* 参数（同一篇文章的追踪参数不影响去重）。"""
        try:
            parts = urlparse((link or "").strip())
            q = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
            return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params,
                               urlencode(q), ""))
        except Exception:
            return (link or "").strip()

    # ---- 查询 ----

    def get(self, link: str) -> dict | None:
        return self._log.get(self.key(link))

    def due(self, link: str, tick_sec: float = 60.0) -> bool:
        """这条现在能不能读（不在失败退避期内）。done/dead 返回 False（不该再读）。"""
        e = self.get(link)
        if not e:
            return True
        if e.get("state") in ("done", "dead"):
            return False
        fc = e.get("fail_count", 0)
        if fc == 0:
            return True
        backoff = BACKOFF[min(fc - 1, len(BACKOFF) - 1)]
        return (time.time() - float(e.get("fail_at", 0))) >= backoff * tick_sec

    # ---- 记录 ----

    def record_done(self, link: str, chars: int = 0):
        k = self.key(link)
        prev = self._log.get(k, {})
        self._log[k] = {
            "state": "done", "at": self._now(), "chars": chars,
            "fail_count": prev.get("fail_count", 0),
            "partial_count": prev.get("partial_count", 0),
            "last_kind": "", "fail_at": 0,
        }
        self._save()

    def record_partial(self, link: str, chars: int = 0) -> str:
        """读到但被截断。返回新的状态（partial 或 dead），供调用方写文案。"""
        k = self.key(link)
        prev = self._log.get(k, {})
        pc = prev.get("partial_count", 0) + 1
        state = "dead" if pc >= MAX_PARTIAL else "partial"
        self._log[k] = {
            "state": state, "at": self._now(), "chars": chars,
            "fail_count": prev.get("fail_count", 0),
            "partial_count": pc,
            "last_kind": prev.get("last_kind", ""), "fail_at": prev.get("fail_at", 0),
        }
        self._save()
        return state

    def record_fail(self, link: str, kind: str) -> str:
        """抓取/提取失败。返回新的状态（new/partial/dead），供调用方写文案。"""
        k = self.key(link)
        prev = self._log.get(k, {})
        fc = prev.get("fail_count", 0) + 1
        state = "dead" if fc >= MAX_FAIL else prev.get("state", "new")
        self._log[k] = {
            "state": state, "at": self._now(),
            "fail_count": fc,
            "partial_count": prev.get("partial_count", 0),
            "last_kind": kind, "fail_at": time.time(),
        }
        self._save()
        return state

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 容量 ----

    def _trim(self):
        if len(self._log) <= CAP:
            return
        done = [(k, v) for k, v in self._log.items() if v.get("state") == "done"]
        done.sort(key=lambda kv: kv[1].get("at", ""))
        for k, _ in done[: len(self._log) - CAP]:
            del self._log[k]
