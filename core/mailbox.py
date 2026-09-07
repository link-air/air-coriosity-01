"""
信箱：和创建者的通信。

inbox.jsonl  = 创建者发来的（仪表盘写入，agent 轮询）
outbox.jsonl = 她回复的（agent 写入，仪表盘轮询）

已读状态用 read_ids.json 持久化（消息指纹去重），重启后不会把旧消息当成新消息重读。

每条带墙钟时间戳（CST，%Y-%m-%d %H:%M），她看得到"创建者什么时候说的、自己什么时候回的"。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Mailbox —— 信箱（inbox/outbox，air 与创建者的通信通道）：
#   段 1｜已读持久化 —— read_ids.json（消息指纹去重，重启不重读旧消息）
#   段 2｜收（创建者发来的）—— poll() 取新消息 / mark_all_read() 标已读
#   段 3｜发（她回创建者）—— send()：追加写 outbox.jsonl
# =====================================================================
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.lock import atomic_write_json, read_text_retry

CST = timezone(timedelta(hours=8))


class Mailbox:
    def __init__(self, config):
        self.dir = config.data_root / "mailbox"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.inbox = self.dir / "inbox.jsonl"
        self.outbox = self.dir / "outbox.jsonl"
        self.read_ids_file = self.dir / "read_ids.json"
        self._read_ids = set()
        self._load_read_ids()

    # ---- 已读持久化 ----

    def _load_read_ids(self):
        try:
            if self.read_ids_file.exists():
                self._read_ids = set(json.loads(read_text_retry(self.read_ids_file)))
        except Exception:
            self._read_ids = set()

    def _save_read_ids(self):
        try:
            atomic_write_json(self.read_ids_file, sorted(self._read_ids))
        except Exception:
            pass

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]

    # ---- 收（创建者发来的）----

    def poll(self) -> list:
        """返回尚未读过的新消息（read_ids 去重，重启不丢）。不自动标记，处理完调 mark_all_read。

        顺手记一份「poll 那一刻 inbox 的行指纹快照」到 _snapshot_ids：mark_all_read 用这份
        快照收缩，而不是重新读此刻的 inbox——否则 poll 与 mark 之间新进来的消息（创建者在我
        处理上一批时发来的）会被 mark 一并标成已读，下一轮 poll 再也读不到，消息就丢了。
        """
        lines = self._read_lines(self.inbox)
        self._snapshot_ids = {self._fingerprint(ln) for ln in lines}
        out = []
        for line in lines:
            if self._fingerprint(line) in self._read_ids:
                continue
            out.append(self._parse(line))
        return out

    def mark_all_read(self):
        """把 poll 时刻见过的消息标记为已读（持久化，重启不重读）。

        用 poll 的快照指纹收缩而非读此刻的 inbox：处理消息期间（命令工作流要跑好几轮 LLM，
        可能几十秒）新到的来信不在快照里，不会被我误标成已读丢掉。快照大小恒等于 poll 时
        inbox 的行数，不随历史只增不删地慢性增长。
        """
        fps = getattr(self, "_snapshot_ids", None)
        if fps is None:
            return   # 没 poll 过就调用，没快照可标（防御，正常流程 poll 先跑）
        if fps != self._read_ids:
            self._read_ids = fps
            self._save_read_ids()

    @staticmethod
    def _read_lines(path: Path) -> list:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    @staticmethod
    def _parse(line: str) -> str:
        """解析一行；带 ts 则拼时间前缀；旧数据无 ts 原样返回。"""
        try:
            d = json.loads(line)
            text = d.get("text", "")
            ts = d.get("ts", "")
            return f"[{ts}] {text}" if ts else text
        except Exception:
            return line

    # ---- 发（她回创建者）----

    def send(self, text: str):
        with open(self.outbox, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text, "ts": self._now()}, ensure_ascii=False) + "\n")

    @staticmethod
    def _now() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M")
