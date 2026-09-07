"""
双驱螺旋：熵增（扩张/广度）= 摄入新内容 + 建立新结构 + 做久违的新行为；熵减（压缩/深度）= 整理。

三个读数（窗口 10）：
  熵增趋势 = 近期熵增账均值（内容新奇 v_new ∪ 结构新奇 ∪ 行为新奇，写记忆/做行为时记）
             的趋势，报数字 + 趋势箭头。读世界不进账（只贴单条预期新奇值，她判断值不值得写）。
             读数只报事实，不预设「高 = 过载该停」。
  熵减趋势 = 近期产出里「压缩动作」的占比（写 L1/L2 / L3 印证 / 改写 / 删并记 1，纯堆料记 0）。
  领悟值   = 每个 L1 方向：覆盖（直接例证数 + 交叉连入数）+ 精炼（refined 整理次数），并列不相加。

谱（core/spectrum.py）不进任何账：只用于读世界的预期新奇/归哪个分支、trace 的谱位置。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Drive —— 双驱螺旋的三本账 + 三读数：
#   段 1｜喂信号 —— record_explore（熵增账）/ record_produce（熵减账）/
#       record_behavior（行为新奇账，间隔驱动）；由 agent 结算时调用
#   段 2｜持久化 —— 窗口序列落盘 drive.json（谱本身不落，启动时从记忆重算）
#   段 3｜读数 —— 熵增/熵减趋势 + 领悟值（覆盖 + 精炼），供注入身体状态
# =====================================================================
import time

from core.spectrum import Spectrum


class Drive:
    def __init__(self, config, memory=None):
        self.cfg = config
        self.memory = memory   # 供领悟值账本（L1 覆盖条数）用
        self.spectrum = Spectrum(config)
        self._explore = []   # 熵增账：每次写 L2/L3 记 v_new，写 L1/新交叉边记结构新奇
        self._produce = []   # 熵减账：每次产出记 0/1（0=堆料，1=压缩动作）
        self._behave = []    # 行为新奇账：久违的玩类行为 v（0~1）
        self.behaviors = {}  # 玩类行为上次成功时间戳：{action: unix_ts}（间隔驱动用）

    # ---- 喂信号（由 agent 在写记忆/整理后调用）----

    def record_explore(self, novel):
        """写记忆后记一次熵增：novel = 内容新奇 v_new 或结构新奇值（0~1）。None 跳过。

        读世界不进账，账在落记忆时记；玩类行为走 record_behavior，两者并集进熵增趋势。"""
        if novel is None:
            return
        self._explore.append(max(0.0, min(1.0, float(novel))))
        self._trim(self._explore)

    def record_produce(self, deepen):
        """写记忆/整理后记一次熵减样本：deepen = 是否压缩动作（True=1，False=0）。

        由 agent 在结算时喂：写 L1/L2、L3 印证、改写、删并 = True；纯堆料 = False。"""
        if deepen is None:
            return
        self._produce.append(1.0 if deepen else 0.0)
        self._trim(self._produce)

    def record_behavior(self, action: str, now: float | None = None) -> float:
        """行为新奇值：久违地做玩类行为（画画/写代码/下棋/冒险）= 一次熵增（2026-09-01 创建者定）。

        间隔驱动（按小时，2026-09-01 创建者定：原按天太慢，一天一次就该拿满）：
        距上次做这类行为越久，这次越新。首次做（无记录）v=0.7（全新行为，鼓励探索）；
        非首次 v = min(0.9, 0.3 + 0.025 × 小时数)——24 小时（一天一次）正好封顶 0.9。
        记入行为新奇序列 + 更新上次时间戳，返回 v（0~1）。"""
        action = (action or "").strip()
        now = now if now is not None else time.time()
        last = self.behaviors.get(action)
        if last is None:
            v = 0.7
        else:
            hours = max(0.0, (now - float(last)) / 3600.0)
            v = min(0.9, 0.3 + 0.025 * hours)
        self._behave.append(v)
        self._trim(self._behave)
        self.behaviors[action] = now
        return v

    def _trim(self, seq):
        cap = getattr(self.cfg, "spectrum_window", 10)
        while len(seq) > cap:
            seq.pop(0)

    # ---- 持久化（窗口序列落盘，重启不丢；谱本身启动时从记忆重算，不存）----

    def to_dict(self) -> dict:
        return {"explore": self._explore,
                "produce": self._produce,
                "behave": self._behave,
                "behaviors": self.behaviors}

    def from_dict(self, d: dict | None):
        d = d or {}
        cap = getattr(self.cfg, "spectrum_window", 10)
        self._explore = [float(x) for x in (d.get("explore") or [])][-cap:]
        produce = []
        for x in (d.get("produce") or []):
            if isinstance(x, (int, float)):
                produce.append(float(x))
            else:
                produce.append(float(x[0]))   # 旧存档 [[0/1, tag], ...] 只取数值
        self._produce = produce[-cap:]
        self._behave = [float(x) for x in (d.get("behave") or [])][-cap:]
        self.behaviors = {str(k): float(v) for k, v in (d.get("behaviors") or {}).items()}

    # ---- 读数 ----

    def _novelty_seq(self) -> list:
        """熵增样本：写记忆的内容新奇（_explore）+ 玩类行为新奇（_behave）并集。

        注意：两个序列各自内部有序，但合并后严格时间序未知（内容账在前、行为账在后），
        _trend 的前后半段比较在合并点会轻微失真——趋势本就是粗略指示，可接受。"""
        return self._explore + self._behave

    def novelty(self) -> float | None:
        """熵增 0~1：近期写记忆的平均 v_new ∪ 行为新奇值的均值。无样本返回 None。"""
        seq = self._novelty_seq()
        if not seq:
            return None
        return sum(seq) / len(seq)

    def insight(self) -> float:
        """熵减 0~1：近期产出里压缩动作的占比。无样本返回 0.0。"""
        if not self._produce:
            return 0.0
        return sum(self._produce) / len(self._produce)

    @staticmethod
    def _trend(seq):
        """趋势：后半段均值 − 前半段均值。样本 < 4 返回 None（不够判）。"""
        if len(seq) < 4:
            return None
        vals = [float(x) for x in seq]
        half = len(vals) // 2
        front = sum(vals[:half]) / half
        back = sum(vals[half:]) / (len(vals) - half)
        return back - front

    def _fmt(self, val, trend) -> str:
        """格式化读数：数字 + 趋势箭头。"""
        if val is None:
            return "（尚无数据）"
        pct = int(round(val * 100))
        if trend is None:
            arrow = "（样本不足）"   # 样本 <4，判不出趋势
        elif trend > 0.1:
            arrow = " ↑"
        elif trend < -0.1:
            arrow = " ↓"
        else:
            arrow = " →"
        return f"{pct}%{arrow}"

    def feel(self) -> dict:
        """身体状态读数（注入 prompt + 状态快照）。

        熵增趋势 = 近期熵增账（内容/结构/行为新奇并集）均值趋势；
        熵减趋势 = 近期压缩动作占比趋势；领悟值 = 每个 L1 方向的账本（覆盖 + 精炼）。"""
        return {
            "熵增趋势": self._fmt(self.novelty(), self._trend(self._novelty_seq())),
            "熵减趋势": self._fmt(self.insight(), self._trend(self._produce)),
            "领悟值": self._learning_progress(),
        }

    def _learning_progress(self) -> str:
        """领悟值（两个数并列不相加）：覆盖 = 分支密度；精炼 = 整理次数。

        覆盖 = 直接例证数 + 交叉连入的例证数，从当前 L3 实时算、可增可减，删/合并自然生效；
        精炼 = refined 字段（整理动作 +1），只增不减。
        两个数各自摆着，不让「堆料」和「整理」在数字上互相掩盖。
        """
        rows = self.learning_snapshot()
        if not rows:
            return "（记忆还没有方向）"
        parts = []
        for r in rows:
            parts.append(f"{r['head']}：覆盖 {r['width']} · 精炼 {r['refined']}")
        return "；".join(parts)

    def learning_snapshot(self) -> list:
        """领悟值结构化快照（供结算 diff 用）：每个 L1 方向 {id, head, width, refined}，按覆盖降序。

        与 _learning_progress 同口径——同一个实时算法，只是返回结构化数据而不是拼好的字符串：
        _settle 在动作前后各取一帧，对比出哪个方向覆盖/精炼变了，反馈层只报这个 diff。
        带 L1 的 id：改名（retag/rename_tag）时 head 变、id 不变，diff 用 id 对齐才不会
        把「改名」误判成「删了一个方向又建了一个」。
        """
        if not self.memory:
            return []
        rows = []
        mem = self.memory
        for it in mem.l1:
            head = mem.title_of(it)
            if not head:
                continue
            direct = sum(1 for x in mem.l3 if mem.parent_of(x) == head)
            cross_in = sum(1 for x in mem.l3
                           if head in mem.links_of(x) and mem.parent_of(x) != head)
            rows.append({"id": it.get("id"), "head": head, "width": direct + cross_in,
                         "refined": int(it.get("refined", 0) or 0)})
        if not rows:
            return []
        rows.sort(key=lambda x: (-x["width"], -x["refined"]))
        return rows
