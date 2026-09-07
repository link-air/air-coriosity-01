"""
短期记忆（air-curiosity-01，2026-08-30 行为系统重构；2026-08-31 目标清单化）：
  长期方向   （direction，set_goal 覆盖式维护，跨 tick 保留）
  目标清单   （最近 TITLES_KEEP+1 个 tick 的目标 + 完成状态，确定性拼装，零 LLM）

2026-08-31 创建者定（见《行为系统设计思路.md》第七节）：
  滚动概括（summary / compress / _parse_summary / llm_fn）整体退役——
  概括层是旧「并行线」机制的残留，信息质量低（与 goal 重叠、含 id 碎片），
  换成她每 tick 亲手定的目标 + 系统记录的 done 状态，更权威、零 LLM 成本。
  短期记忆因此退化为两层：长期方向 + 目标清单。

2026-09-01 创建者定（结构化结果渲染）：
  最近一次 tick 不再只给一行 60 字摘要，而是紧凑明细——头部 + 目标 + **一行**
  动作明细「动作名 ✓/✗ 摘要；…」，同类动作合并计数（×N），8 个 nothing
  不再占 8 行。结果摘要 = 第一行 + 全角括号反馈行（变化/查重/印证的 L1
  没找到/这步没成…），正文（小说/文章/代码/记忆条目）按规则滤掉，零 LLM、
  纯确定性。tick 记录从此带成败字段（ok）——她看得到自己每一步成没成，
  卡住（连续 ✗）自然浮现，系统不判断。

纯视图层：不写记忆库；record_tick/折叠/render 全部确定性，零 LLM。

时间：tick 记录带墙钟（MM-DD HH:MM）。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Narrative —— 短期记忆（视图层，不写记忆库，零 LLM）：
#   段 1｜常量 —— 目标清单保留条数 / 渲染预算上限等
#   段 2｜持久化 —— narrative.json 完全落盘，重启恢复
#   段 3｜tick 记录 —— record_tick：每 tick 合成目标 + 动作成败明细；
#       更早的压成标题行（_fold_tick），滚动丢弃回调给 L5 冷存
#   段 4｜渲染 —— render()：拼成注入 prompt 的短期记忆文本
# =====================================================================

from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ---- 常量 ----
TITLES_KEEP = 9         # 更早 tick 标题行数上限（+ 最近一次 = 目标清单共展示 10 个 tick）
BUDGET_CHARS = 2000     # render 总长硬上限（超了从最旧目标丢起）
TICK_SUMMARY_MAX = 500  # tick 记录的结果摘要上限
LIST_GOAL_MAX = 60      # 目标清单里目标显示上限
LIST_BRIEF_MAX = 60     # 目标清单里「做了」显示上限
ACTIONS_KEEP = 12       # tick 记录保留的动作条数上限（2026-09-03 创建者定，8→12）


class Narrative:
    def __init__(self, on_discard=None):
        self.on_discard = on_discard  # 滚动丢弃回调（旧 tick 记录 → 冷存），None 则不归档
        self.direction = ""           # 长期方向：接下来很长一段时间要干嘛（set_goal 覆盖式维护，跨 tick 保留）
        self.last_tick = None         # 最近一次 tick 记录 {t, goal, done, summary, ts, actions?}
        self.titles = []              # 更早 tick 标题行 [{t, goal, done, brief, ts}]
        self.reflection = ""          # 上个 tick 新写的自省摘要（下个 tick 显示一次；没写就空着）

    # ---- 持久化（完全落盘，重启恢复；见设计文档）----

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "last_tick": self.last_tick,
            "titles": self.titles,
            "reflection": self.reflection,
        }

    def from_dict(self, d: dict | None):
        d = d or {}
        self.direction = (d.get("direction") or "")
        lt = d.get("last_tick")
        self.last_tick = lt if isinstance(lt, dict) else None
        self.titles = list(d.get("titles") or [])[-TITLES_KEEP:]
        self.reflection = d.get("reflection") or ""
        # 旧数据里的 read_log（小说已读）已退役：已读状态改由文件名【已读】承载
        # （2026-09-01 创建者定 A 方案），这里不读也不写，旧字段自然留在 json 里不影响。

    @property
    def last(self) -> dict | None:
        """最近一次 tick 记录（兼容状态快照 main.py）。"""
        return self.last_tick

    def _discard(self, items: list):
        """已结束的行为 → 回调（滚进 L5 冷存，不真丢）。失败静默。"""
        if not items or not self.on_discard:
            return
        try:
            self.on_discard(items)
        except Exception:
            pass

    # ---- tick 记录（确定性，零 LLM）----

    def record_tick(self, t, goal, summary, done=False, actions=None):
        """tick 结束合成一条记录（目标 + 完成状态 + 做了什么 + 成败明细）。上一次的滚进标题层 + 冷存。
        actions：动作快照列表（{action, result, ok}），渲染最近一次时用；不传（旧调用/旧数据）则无明细。"""
        goal = (goal or "").strip()
        summary = (summary or "").strip()[:TICK_SUMMARY_MAX]
        if self.last_tick:
            self._fold_tick(self.last_tick)
            self._discard([self.last_tick])
        self.last_tick = {"t": int(t), "goal": goal, "summary": summary,
                          "done": bool(done), "ts": self._now()}
        if actions:
            self.last_tick["actions"] = list(actions)[:ACTIONS_KEEP]
        if len(self.titles) > TITLES_KEEP:
            # 只截断概览：被截内容已在上一次 record_tick 滚进冷存，这里不重复滚
            self.titles = self.titles[-TITLES_KEEP:]

    def _fold_tick(self, e: dict):
        """把一条 tick 记录压成标题行（保留目标 + 完成状态，供目标清单渲染）。
        有 actions 时 brief 用动作名序列（read_local；write_memory）——四轮同样动作的重复一眼可见；
        没有（旧数据）退回用 summary。"""
        brief = self._actions_brief(e)
        if not brief:
            brief = (e.get("summary") or "")[:40].replace("\n", " ")
        self.titles.append({
            "t": e.get("t"), "goal": (e.get("goal") or "")[:LIST_GOAL_MAX],
            "done": bool(e.get("done", False)), "brief": brief,
            "ts": e.get("ts") or "",
        })

    @staticmethod
    def _now() -> str:
        """墙钟（短期记忆显示用短格式 MM-DD HH:MM，跨年不跨——短期记忆本来就不该超过一天尺度）。"""
        return datetime.now(CST).strftime("%m-%d %H:%M")

    @staticmethod
    def _head(e: dict) -> str:
        """头部：tick 号 + 墙钟 + 完成状态（✓/✗）。旧残留条目（无 done）不带标记。"""
        head = f"[t{e['t']}]" if e.get("t") is not None else ""
        ts = e.get("ts") or ""
        if ts:
            head += f"[{ts}]"
        if "done" in e:
            head += " ✓" if bool(e.get("done")) else " ✗"
        return head

    @staticmethod
    def _goal_row(e: dict) -> str:
        """目标清单一行：头部 + 目标 + 做了啥。✓=已完成 ✗=未完成。
        旧残留条目（无 goal/done）：直接显示 brief，不编造目标与状态。"""
        head = Narrative._head(e)
        goal = (e.get("goal") or "").strip().replace("\n", " ")[:LIST_GOAL_MAX]
        brief = (e.get("brief") or e.get("summary") or "").strip().replace("\n", " ")[:LIST_BRIEF_MAX]
        if goal:
            body = goal
            if brief:
                body += f"｜做了：{brief}"
        else:
            body = brief or "（无目标）"
        return f"{head} {body}".strip()

    @staticmethod
    def _summarize(result: str, limit: int = 200) -> str:
        """工具返回 → 一行摘要：第一行 + 全角括号反馈行，其余（正文/条目）丢弃。

        limit 默认 200（2026-09-03 创建者定，100→200）：100 会把 read_branch 那种
        单行框架句 + 失败反馈硬切在半截（如「（印证」），200 给完整句留出余量。

        系统反馈全包在全角括号里（（变化：…）（查重：…）（印证的 L1 没找到：…）
        （这步没成…）），正文（小说/文章/代码/记忆条目）都是大块无括号文本，
        正好被这条规则滤掉——t94 那种「摘要被小说正文占满」不再发生。
        零 LLM、纯确定性，与「纯视图层」定位一致。"""
        lines = [(ln or "").strip() for ln in (result or "").split("\n")]
        keep = []
        if lines and lines[0]:
            keep.append(lines[0])
        for ln in lines[1:]:
            if ln.startswith("（"):
                keep.append(ln)
        return "；".join(keep)[:limit]

    @staticmethod
    def _merge_actions(acts: list) -> list:
        """动作合并计数：[name, 次数, 是否全部 ok, 首个动作快照]，保留首次出现顺序。
        8 个 nothing 压成 nothing×8；同名动作一成一败 → ok_all=False（标 ✗）。"""
        merged = []
        for a in acts or []:
            name = (a.get("action") or "").strip()[:16] or "?"
            m = next((x for x in merged if x[0] == name), None)
            if m:
                m[1] += 1
                m[2] = m[2] and bool(a.get("ok"))
            else:
                merged.append([name, 1, bool(a.get("ok")), a])
        return merged

    @staticmethod
    def _actions_brief(e: dict) -> str:
        """动作名序列（标题行 brief）：read_local；write_memory×2。同类合并计数，
        8 个 nothing 显示成 nothing×8 而不是 8 个重复。没 actions 返回空串。"""
        acts = e.get("actions")
        if not acts:
            return ""
        out = "；".join(name + (f"×{cnt}" if cnt > 1 else "")
                       for name, cnt, _, _ in Narrative._merge_actions(acts))
        return out[:LIST_BRIEF_MAX]

    def _tick_detail(self, e: dict) -> str:
        """最近一次 tick 的紧凑渲染（2026-09-01 创建者定）：
        头部 + 目标 + **一行**动作明细「动作名 ✓ 摘要；…」——
        同类动作合并计数（×N），8 个 nothing 不再占 8 行。
        ok=False 或同名动作里有失败 → ✗（这步没成）。摘要取第一条。
        旧数据（无 actions 字段）退化为一行标题行。"""
        acts = e.get("actions")
        if not acts:
            return self._goal_row(e)
        goal = (e.get("goal") or "").strip().replace("\n", " ")[:LIST_GOAL_MAX] or "（无目标）"
        parts = []
        for name, cnt, ok_all, a in Narrative._merge_actions(acts[:ACTIONS_KEEP]):
            mark = "✓" if ok_all else "✗"
            name_s = name + (f"×{cnt}" if cnt > 1 else "")
            parts.append(f"{name_s} {mark} {self._summarize(a.get('result') or '')}")
        return f"{self._head(e)} {goal} 做了：{'；'.join(parts)}"

    # ---- 渲染（注入 prompt）----

    def render(self) -> str:
        """短期记忆注入 prompt：长期方向 + 目标清单（最近 TITLES_KEEP+1 个 tick，最新在前）
        + 上个 tick 的自省（有才贴，2026-09-03 创建者定）。
        预算硬上限：超了从最旧目标丢起（长期方向/最近一次/自省永不丢）。"""
        def _join(ts: list) -> str:
            lines = []
            if self.direction:
                lines.append("【长期方向】" + self.direction)
            elif self.last_tick or ts:
                # 有活动痕迹但长期方向空：报事实不提醒（方案 C，创建者 2026-08-31 定）。
                # 完全空白的第一轮仍走下方兜底「（还没有…）」，不显示（未设定）。
                lines.append("【长期方向】（未设定）")
            if self.last_tick or ts:
                lines.append("【目标清单】")
                rows = []
                if self.last_tick:
                    rows.append(self._tick_detail(self.last_tick))
                rows.extend(self._goal_row(ti) for ti in reversed(ts))
                lines.extend(rows)
            if self.reflection:
                lines.append("【上个tick的自省】" + self.reflection)
            return "\n".join(lines)

        titles = list(self.titles)
        text = _join(titles)
        while len(text) > BUDGET_CHARS and titles:
            titles.pop(0)   # 从最旧目标丢起（自省/长期方向/最近一次永不丢）
            text = _join(titles)
        return text if text else "（还没有，这是我醒来后的第一轮）"
