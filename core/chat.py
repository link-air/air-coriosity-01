"""
聊天上下文：对话线程 + 创建者画像。

对话线程 = 类似短期记忆的上下文窗口：最近 20 条原话 + 更早的压缩概括（LLM 提炼）。
不常驻 prompt——只在「有创建者来信需要回应」时读取注入（平时她照常自主跑）。

创建者画像 = air 对创建者的认知（不是她的自我认知，她的自我认知在 L1 / 宪章里）。
聊天时读取，让她知道自己在跟谁说话。初始值由创建者手写，后续可从对话里蒸馏更新。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class ChatContext —— 对话线程 + 创建者画像：
#   段 1｜持久化 —— chat.json 的读写（原子写）
#   段 2｜对话线程 —— append 追加 / _maybe_compress 超限压缩旧对话 /
#        render 渲染（窄版/宽版，供 agent 注入 prompt）
#   段 3｜创建者画像 —— update_profile / _distill_profile（每 3 条创建者消息蒸馏一次）
# =====================================================================
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.lock import atomic_write_json, read_text_retry

CST = timezone(timedelta(hours=8))
MAX_RECENT = 20          # 最近保留原话的条数
COMPRESS_BATCH = 10      # 超过上限时一次压缩掉的最老条数
PROFILE_EVERY = 3        # 每累计 3 条创建者消息蒸馏一次画像（节流，避免每条都调 LLM）
PROFILE_CAP = 500        # 创建者画像 summary 字数上限（自动蒸馏，聊天时能看就行，不刻意维护）
# 注意力预算（2026-09-03）：【最近对话】里「我」的回复是输出方自己的话，不需要重读
# 全文——实测她回复平均 564 字、创建者消息平均 25 字，之前渲染九成是她的旧原文。
# 默认窄版：只贴最近 DEFAULT_TAIL 条、她的回复留 MY_REPLY_TRUNC 字记住"说到哪"；
# 创建者消息保留完整（上限拉高防意外长文刷屏）。
# 宽版（render(wide=True)）：agent 检测到她要回应创建者（本轮 action 是对话类工具）后，
# 下一轮注入——条数更多 + 她的回复也放宽，深度回复有完整脉络可依。
DEFAULT_TAIL = 5         # 默认渲染最近条数
WIDE_TAIL = 10           # 对话模式（wide）渲染条数
MY_REPLY_TRUNC = 200     # 「我」的回复默认保留开头字数
WIDE_REPLY_TRUNC = 400   # 对话模式「我」的回复保留字数
OWNER_MSG_CAP = 4000     # 使用者单条消息上限（防超长刷屏）
# speaker 的内部标识。**这是数据格式，不是文案**：显示时才换成 owner_name（可配）。
# 值必须是中性的——它写进 chat.json，开源出去的就是它，私人称呼不能进数据格式
#（2026-09-08 定，与 config 里「内部标识始终是 user」那句对齐）。
# 判定一律走 _is_owner（「不是我就算使用者」），所以换这个值不需要迁移历史数据：
# 老记录里残留的旧标识照样认得出来，加载时统一归一（见 _load）。
OWNER_SPEAKER = "user"
ME_SPEAKER = "我"


def _is_owner(speaker: str) -> bool:
    """这条是不是使用者说的。对话只有两方——不是「我」，就是使用者。

    刻意不比对具体标识值：历史记录里可能残留旧称呼，按值比对会把它们全判成
    「不是使用者」（那些记录就永远不参与画像蒸馏了）。按「不是我」判，新旧都认。
    """
    return speaker != ME_SPEAKER


class ChatContext:
    def __init__(self, data_dir: Path, llm=None, owner_name="human"):
        self.file = Path(data_dir) / "chat.json"
        self.llm = llm
        # 称呼（她怎么叫使用者）：可配，默认 human。只用在「显示/说出口」的地方——
        # recent 里存的 speaker 值仍然是内部标识，不动历史数据（见 OWNER_SPEAKER）。
        self.owner_name = (owner_name or "human").strip() or "human"
        # 使用者画像初始值
        self.profile = {"summary": "设计我架构的人类。", "updated": ""}
        self.recent = []      # [{"speaker": OWNER_SPEAKER|ME_SPEAKER, "text": str, "ts": str}]
        self.summary = ""     # 更早对话的压缩概括
        self._owner_since_profile = 0  # 距上次画像蒸馏累计的使用者消息数（攒够再蒸馏，控制 LLM 调用频率）
        self._load()

    # ---- 持久化 ----

    def _load(self):
        try:
            if self.file.exists():
                d = json.loads(read_text_retry(self.file))
                if isinstance(d, dict):
                    if d.get("profile"):
                        self.profile = d["profile"]
                    self.recent = d.get("recent") or []
                    self.summary = d.get("summary") or ""
                    # 历史遗留的旧标识一次性归一：非「我」的一律改成当前内部标识。
                    # 不归一也能跑（_is_owner 不看值），归一后落盘的数据格式才是干净的。
                    for r in self.recent:
                        if r.get("speaker") != ME_SPEAKER:
                            r["speaker"] = OWNER_SPEAKER
        except Exception:
            pass

    def _save(self):
        try:
            d = {"profile": self.profile, "recent": self.recent, "summary": self.summary}
            atomic_write_json(self.file, d)
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    # ---- 对话线程 ----

    def append(self, speaker: str, text: str):
        """追加一轮对话。speaker 用 OWNER_SPEAKER（使用者）或 ME_SPEAKER（她）。"""
        text = (text or "").strip()
        if not text:
            return
        self.recent.append({"speaker": speaker, "text": text, "ts": self._now()})
        if _is_owner(speaker):
            self._owner_since_profile += 1
            if self._owner_since_profile >= PROFILE_EVERY:
                self._distill_profile()
                self._owner_since_profile = 0
        self._maybe_compress()
        self._save()

    def _maybe_compress(self):
        """超过 20 条时，把最老的 10 条压缩进 summary（LLM 概括），腾出空间。"""
        if len(self.recent) <= MAX_RECENT:
            return
        old = self.recent[:COMPRESS_BATCH]
        self.recent = self.recent[COMPRESS_BATCH:]
        if self.llm:
            try:
                # 素材里 speaker 保持数据原样（可能是历史记录里的旧称呼）：
                # 概括是对当时的忠实压缩，事后统一改口反而失真；
                # 给 air 看的「现在时」渲染才换成称呼（见 render）。
                snippet = "\n".join(f"{r['speaker']}: {r['text'][:100]}" for r in old)
                out = self.llm.chat([{"role": "user", "content": (
                    f"把下面这段我和{self.owner_name}的旧对话，"
                    "概括成两三句话（聊了什么、聊到哪了），"
                    "只输出概括本身，不要别的：\n" + snippet
                )}], max_tokens=200)
                if out and out.strip():
                    self.summary = (self.summary + "；" + out.strip()).strip("；")
            except Exception:
                pass
        # 无 LLM 时：旧对话直接丢（不概括），窗口始终 ≤ 20 条

    def render(self, wide=False, chat_only=False) -> str:
        """渲染注入 prompt 的聊天上下文：创建者画像 + 更早概括 + 最近对话。

        注意力预算（2026-09-03）：「我」的回复是输出方自己的话，只留开头记住"说到哪"，
        把注意力让给创建者的输入；创建者消息完整保留。
        默认窄版（DEFAULT_TAIL 条 / MY_REPLY_TRUNC）；agent 检测到她在回应创建者
        （本轮选了对话类工具）后，下一轮注入 wide 版（WIDE_TAIL 条 / 回复放宽到
        WIDE_REPLY_TRUNC）——深度回复时让她有完整脉络可依。
        chat_only=True：只返回【最近对话】段——对话 boost 注入时用，画像/概括首轮
        已带过，不重复贴。
        对话线程本身完整持久化（recent 全量落盘），截断只发生在渲染给模型时。
        """
        parts = []
        if not chat_only:
            s = (self.profile.get("summary") or "").strip()
            if s:
                parts.append(f"【关于{self.owner_name}】{s}")
            if self.summary:
                parts.append(f"【我们更早聊过】{self.summary}")
        if self.recent:
            tail = WIDE_TAIL if wide else DEFAULT_TAIL
            reply_trunc = WIDE_REPLY_TRUNC if wide else MY_REPLY_TRUNC
            lines = []
            for r in self.recent[-tail:]:
                t = r["text"]
                # 判定用内部标识（数据格式），显示用称呼（可配）
                is_owner = _is_owner(r["speaker"])
                who = self.owner_name if is_owner else r["speaker"]
                if is_owner and len(t) > OWNER_MSG_CAP:
                    t = t[:OWNER_MSG_CAP].rstrip() + "…（消息过长已截断）"
                elif not is_owner and len(t) > reply_trunc:
                    t = t[:reply_trunc].rstrip() + "…（我的旧回复）"
                lines.append(f"- {who}：{t}")
            parts.append("【最近对话】\n" + "\n".join(lines))
        return "\n".join(parts)

    # ---- 创建者画像 ----

    def update_profile(self, summary: str):
        """更新创建者画像（自动蒸馏的产物，聊天时能看到就行，不做刻意维护）。"""
        summary = (summary or "").strip()
        if not summary:
            return
        self.profile = {"summary": summary[:PROFILE_CAP], "updated": self._now()}
        self._save()

    def _distill_profile(self):
        """从最近对话里蒸馏「使用者是谁」，更新画像（≤PROFILE_CAP 字）。无 LLM 或失败静默跳过。"""
        if not self.llm:
            return
        try:
            owner_msgs = [r["text"][:120] for r in self.recent
                          if _is_owner(r["speaker"])][-10:]
            if not owner_msgs:
                return
            out = self.llm.chat([{"role": "user", "content": (
                f"下面是我的{self.owner_name}最近对我说的话。总结「{self.owner_name}是谁」——"
                "他关心什么、是什么样的人、在和我一起做什么。只输出总结本身，"
                f"不要解释，不超过 {PROFILE_CAP} 字：\n" + "\n".join(f"- {m}" for m in owner_msgs)
            )}], max_tokens=PROFILE_CAP * 2)
            s = (out or "").strip()
            if s:
                self.update_profile(s)
        except Exception:
            pass
