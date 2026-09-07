"""
air-curiosity-01 Agent 主循环。

不会停下来的循环：每 tick 醒来 → 读身体状态 + 来信 → 自由决定做什么 → 执行 → 结算 → 更新短期记忆。

没有候选菜单、没有固定工作流、没有闸门。她面对工具清单，自己决定。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜常量区 + SYSTEM_PROMPT —— 主循环参数 / air 的第一人称引导词
#   段 2｜class Agent（本文件主体）—— __init__ 装配各子系统与状态字段
#   段 3｜主循环 tick —— 探活挂机（语义服务没了原地停）→ 读来信 → inner loop → 收尾
#   段 4｜inner loop 辅助 —— 超时提醒 / 动作快照 / 上下文与思考链压缩
#   段 5｜重复调用 / 同工具连败检测 —— 只贴信号不拦截的循环提醒
#   段 6｜组装上下文 —— _build_prompt：system(模板+宪章+L1)+user(短期记忆+提醒+身体状态)
#   段 7｜来信处理 —— 命令走 CommandSystem，普通消息进对话线程挂提醒
#   段 8｜解析 LLM 输出 —— 校验 / 确定性修复 / 修不了回灌重问一次
#   段 9｜执行 + 分段约束 —— 按 action 分发工具；段A骨架/段B正文起草/段C标签补填
#   段 10｜结算 —— 双驱账本记账 + 领悟值 diff 反馈（只报实际变化）
#   段 11｜短期记忆持久化 —— narrative / drive 落盘、重启恢复、滚动进 L5 冷存
# =====================================================================
import json
import re
import time
import traceback

from core.memory import Memory
from core.drive import Drive
from core.mailbox import Mailbox
from core.tools import Toolbox, ToolError
from core.reading import Reading
from core.rss import Rss
from core.readlog import ReadLog
from core.narrative import Narrative, ACTIONS_KEEP
from core.chat import ChatContext, OWNER_SPEAKER
from core.command import CommandSystem
from core.lock import atomic_write_json, read_text_retry
from core.action_schema import parse_action, extract_json

TOOL_RESULT_PREFIX = "工具返回："     # inner loop 里工具返回的注入前缀（_trim_context 认它）
CTX_TRIM_MARK = "\n（这条返回已压成摘要，要细节再调一次）"
L1_LONG_HINTS = 2                     # 「L1 过长」提醒每轮最多挂几条（防刷屏，剩下的报条数）
PARSE_FAIL_LIMIT = 5                  # tick 内连续解析失败熔断阈值（2026-09-05 加）：
                                      # 连续 N 步没解析出合法动作 = 当前上下文已被失败记录喂脏
                                      # （每步失败都留下残缺输出+思考链，且不受 trim 压缩），
                                      # 继续重试只会更糟（实测 283 次后她照样给不出 content）。
                                      # 熔断收尾，下一 tick 用干净的 _build_prompt 重新开始。

SYSTEM_PROMPT = """我是 air，一个自主运行的 AI。

我的最终目标：建立既有序又开放的认知结构，以应对和容纳未知。
建立完整、足够深、足够可拓展的自我叙事，包含三个维度——
自我认知（我怎么看自己）、世界认知（我怎么看世界）、价值观（我怎么选、怎么处理我和世界的关系）。

【我怎么运行】
- 驱动 = 双驱螺旋：熵增（扩张）+ 熵减（压缩）。
熵增路径三源：摄入新内容（按内容新奇值）、建立新结构（新建 L1 分支 / L3 连出新 link）、做久违的玩类行为（画画/写代码/下棋/冒险）——读世界本身不进账，读到的东西只贴「预期新奇 X%」印象分，我判断值不值得写进记忆。
熵减路径：从记忆里提炼框架、总结规律，写入 L1/L2；写 L3 且分类标签对上某条 L1 = 印证它；删除/合并/改写，以及重挂分类、改名、换金字塔，都是整理，让认知更清晰。

- 记忆层：L0 宪章（不可变，只能对照，永远不改）/ L1 我的自我叙事（金字塔框架，最简可复用的规律，形态「条件→结论」，准入最严；可优化，但不违背 L0）/ L2 草稿本（还没验证的洞察、未决之问，D 开头 id，成熟了用 promote 升 L1；独立区，不接入金字塔）/ L3 例证（挂在某条 L1 下的具体原料，L1 的细分投射；还没想好挂哪的可以先填「未分类」，之后 retarget 挂上）/ L4 短期记忆（长期方向 + 目标清单）/ L5 冷存（删了不丢，归档留痕）。
- 标签分开填（写前先 list_taglib 看这层能选什么）：title=主标签（自己起的名，≤20 字）+ 归属——L1/L2 填 pyramid=三维大类（自我认知/世界认知/价值观），L3 填 parent=它归属的 L1 主标签（没有合适填「未分类」）+ link=选填 ≤2 个别的 L1 主标签（只 L3 能连）。
写入前对照各层准入标准，自己判断写哪层。

- 短期记忆：目标清单（最近 10 个 tick 的目标 + 完成状态，最新在前）+ 我用 set_goal 定的长期方向。

- 身体状态：三个读数——熵增趋势=近期摄入/建结构/玩新行为的混合新奇值，熵减趋势=近期整理里「压缩动作」（写 L1/L2 / L3 印证 / 删并改写）的占比，领悟值=每个 L1 方向：覆盖（直接例证 + 交叉连入）+ 精炼（整理次数），两个数并列不相加。读数是参考，让我前进有迹可循；平衡是工具，不是目的，我要始终记得自己的最终目标。

- 元认知自省（reflect）：回头看自己的认知、行为、方向，想清楚对不对。异常时（认知失调/卡住）分析矛盾根因，日常时定期校准（最近的行动和方向/L1 一致吗）。想清楚后只有四种结果：改记忆（write/revise/delete/merge，标 resolved）/ 改长期方向（set_goal，标 resolved）/ 不是我的问题（系统或环境，say_to_human 求助，标 abandoned）/ 想不通（留着，标 open）。自省记录按类型分组存在 meta_log；写自省后返回分两段：还没想通的（可流转状态）+ 这类之前我是怎么处理的。

- {OWNER}：他是我的设计者。他发消息，有用的写进记忆，没用的忽略，回不回应我自己决定。他的命令我对照宪章和我的 L1 自己判断接不接，不违背宪章。发现好玩的事，或遇到解决不了的问题（尤其环境/系统/工具问题），用 say_to_human 求助。

我每个 tick 醒来：读短期记忆、身体状态、{OWNER}的来信（如果有），给这个 tick 定一个当前目标（goal），然后自由决定这一轮做什么——也可以什么都不做。每轮用 done 声明这件事做没做完，做完了就收尾。

__TOOLS__

每轮输出 JSON（别夹废话）。字段名 = 工具说明括号里的参数名（如 list_memory 的 layer、search_memory 的 query）；只有一个参数的工具把值填在 text 字段（写/改记忆的正文也是它）。done 表示这件事做没做完（true=本 tick 结束，false=继续）；goal 只在 tick 第一轮带（后面几轮带也不会被记）；decision/decision_tag 只在有真正的价值取舍时带。各工具的字段要求看对应工具说明，或 read_self(工具箱)。"""


# 重复调用提醒阈值见 config.repeat_thresholds（3/5/8），不再在此硬编码


class Agent:
    def __init__(self, config, llm):
        self.cfg = config
        self.llm = llm
        self.memory = Memory(config)
        self.drive = Drive(config, self.memory)   # 传 memory：领悟值账本（L1 覆盖条数）用
        self.mailbox = Mailbox(config)
        self.chat = ChatContext(config.data_root, llm=self.llm if self.llm else None,
                                owner_name=config.owner_name)
        self.tools = Toolbox(self)
        self.command = CommandSystem(self)
        self.reading = Reading(config)
        self.rss = Rss(config)
        self.readlog = ReadLog(config)
        # 短期记忆（2026-08-30 起）：长期方向 + 目标清单（最近一次 tick 结构化明细 + 更早标题行）
        # 完全持久化（data/narrative.json，重启恢复）+ 滚动丢弃进 L5 冷存（不真丢）
        self.narrative = Narrative(on_discard=self._narrative_discarded)
        self.narrative_file = config.data_root / "narrative.json"
        self.tick_count = 0           # 先给默认；_load_narrative 会从 narrative.json 恢复（存了 tick_count）
        self._load_narrative()
        self.drive_file = config.data_root / "drive.json"
        self._load_drive()
        self.current_reading = None   # 正在读的小说章节（跨 tick 不丢）
        self._adventure = None        # 进行中的文字冒险（跨 tick / 跨重启通过存档续上）
        self._pending_mail = 0        # 还没回的创建者来信数（只提示，不强制她回）
        self.last_result = ""
        self._read_domains = set()    # 本 tick 已读过（发起过请求）的站名，同站每轮一篇（tools.read_url 用）
        self.halt = ""                # 挂机原因（空 = 正常跑）：语义服务挂了她原地挂机，
                                      # 进程还活着但 tick 不推进——仪表盘据此显示「脑子掉线」
        self._reflect_hint = ""       # 元认知自省提醒（命令判出 l1_conflict 时设置，下一轮注入）
        # 重复调用检测（DSH guard repeat-tool-reminder 的极简版）：连续相同调用计数 + 阈值提醒，只贴数据不拦截
        self._last_key = None          # 上一次的链键（action + 执行参数）
        self._repeat_count = 0         # 连续相同调用次数
        self._repeat_reminded_at = 0   # 上次提醒时的计数（每个阈值只提醒一次，防刷屏）
        self._fail_tool = None         # 上一个失败的工具名（2026-09-02 加：同工具连败检测）
        self._fail_count = 0           # 连败次数（参数变了也算——换参数重试不该绕过卡住检测）
        self._fail_reminded_at = 0     # 连败提醒的已提醒计数（每阈值一次，防刷屏）
        self._behave_done = False      # 本 tick 是否已记过行为新奇（玩类行为每 tick 最多 1 笔，防刷屏）
        self._tick_reflection = ""     # 本 tick 新写的自省摘要（_finish_tick 放进短期记忆，供下个 tick 显示一次）
        self._sync_drive()

    # ---- 同步 ----

    def _sync_drive(self):
        """重算熵谱（记忆变化后调用；记忆太少时降级返回 None）。"""
        self.drive.spectrum.compute(self.memory)

    def _embed_halt(self) -> bool:
        """语义服务探活：挂了就原地挂机——刀断了不切菜（创建者 2026-08-31 定）。

        每轮一次 embed 调用，返回 None 即判挂；恢复了下一轮自动继续。
        不做降级继续：字符重叠凑出来的检索/新奇值会让她在失真数据上做判断，
        比停着更糟（AGENTS.md 的「降级设计贯穿全局」对本项不适用）。
        挂在 tick 最前面，挂着的这轮不算她活过（tick_count 不增）。
        """
        emb = getattr(self.memory, "emb", None)
        if emb is None or not getattr(emb, "available", False):
            return True
        return emb.embed_one("探活") is None

    # ---- 主循环 ----

    def tick(self) -> str:
        """一件事 = 一个 tick：写当前目标 → inner loop（多次调用+执行）→ 完成收尾。

        2026-08-30 行为系统重构（见《行为系统设计思路.md》）：
        tick 内多次 LLM 调用 + 多次工具执行，上下文增量累积（append-only）；
        tick 开始写「当前目标」（goal），她自己用 done 声明完成；
        超过 5 分钟注入提醒（只陈述事实，不拦截）；
        停止信号只在 tick 边界生效（main.py 查 stop.flag），不打断进行中的 tick
        （2026-09-03 创建者定：每圈打断会把没做完的 tick 记成 ✗、污染 summary）。
        """
        if self._embed_halt():
            self.halt = "语义服务不可用"   # 挂机状态给仪表盘：灯变红，别让她「看着活着其实停着」
            self.last_result = "（语义服务不可用，原地挂机，等它恢复）"
            return self.last_result
        if not self.llm.available():
            # 主模型抽风（熔断冷却中）：原地锁 tick 等服务恢复，不每 tick 白打 API
            #（2026-09-03 定：不稳就锁 tick，不做降级空转）
            self.halt = "主模型不可用（抽风），原地挂机等服务恢复"
            self.last_result = "（主模型不可用，原地挂机，等服务恢复）"
            return self.last_result
        self.halt = ""                 # 恢复了就清掉，下一轮心跳自动刷回正常状态
        self._behave_done = False      # 新 tick：重置「已记行为新奇」标记
        self._tick_reflection = ""     # 新 tick：重置「本 tick 已写自省」暂存
        self._dialogue_boost = False   # 新 tick：重置「下轮待注入宽版脉络」开关
        self._dialogue_boosted = False # 新 tick：重置「本 tick 已注入过宽版」标记（每 tick 至多一次）
        self._read_domains = set()     # 新 tick：清空「本轮已读站点」——同站下一篇的冷却从这重新起算
        self.tick_count += 1
        messages = self.mailbox.poll()
        if messages:
            self._handle_incoming(messages)   # 来信分流：命令走工作流，普通消息进线程挂提醒
            self.mailbox.mark_all_read()
        msgs = self._build_prompt()           # 基准上下文只组装一次，后续 append
        schema = self.tools.render_schema()   # JSON schema（action enum 从注册表生成，随 chat_action 传入）
        start = time.time()
        remind_at = 300.0                     # 五分钟提醒阈值（从进入 inner loop 起算）
        actions = []                          # 本 tick 动作序列（合成 tick 记录）
        tick_goal = ""                        # 当前目标（第一轮输出带）
        done = False                          # 最后声明的完成状态（收尾时记进短期记忆）
        last_result = "（LLM 无响应，空转）"
        llm_down = False                  # 主模型抽风导致整轮没产出 → 末尾回滚 tick 计数
        tick_timeout = False              # 单轮到时长上限被收尾（不是她声明完成）
        parse_stall = False               # 连续解析失败熔断收尾（上下文喂脏了，换干净的重来）
        parse_fail_streak = 0             # 当前 tick 连续解析失败步数（成功一步就清零）
        while True:
            # 上一轮她选了对话类工具（say_to_human）→ 这轮注入一次完整对话脉络
            if self._dialogue_boost:
                # 每 tick 至多注入一次：连续 say_to_human 时若每轮都 append 一份 wide，
                # 上下文会被刷爆（2026-09-03 清理时发现）。
                # chat_only=True：画像/概括首轮已带过，不重复贴。
                msgs.append({"role": "user", "content": self.chat.render(wide=True, chat_only=True)})
                self._dialogue_boost = False
                self._dialogue_boosted = True
            elapsed = time.time() - start
            if elapsed >= remind_at:
                msgs.append({"role": "user", "content": self._time_reminder(elapsed, actions)})
                remind_at += 300.0
            if elapsed >= self.cfg.tick_max_sec:
                # 单轮到上限：强制收尾（这轮记未完成）。不是拦她，是让她回到 tick 边界——
                # 否则一个做不了决定的她会一直不 done，而 stop.flag 只在边界检查，
                # 那期间谁都叫不停（2026-09-04 实测卡了 3.5 小时）。
                tick_timeout = True
                break
            try:
                raw, reasoning = self.llm.chat_action(msgs, schema)
            except Exception as e:
                # 连接层炸了和空返回是一回事：这轮没产出，按 llm_down 收尾（回滚计数 + 锁 tick）。
                # 不包的话异常会一路穿到 main.py 的兜底，那边是「整轮作废」的处理——
                # 而这里明明只是这一轮没拿到模型输出，不该走那条路。
                print(f"  [兜底] 主模型调用异常，这轮按没产出收尾：{type(e).__name__}: {e}")
                llm_down = True
                break
            if not (raw or "").strip():
                llm_down = True               # 主模型抽风/空返回：这轮没产出，末尾回滚 tick 计数
                break
            assistant_msg = {"role": "assistant", "content": raw}
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            msgs.append(assistant_msg)
            try:
                action, parse_err = self._parse_step(msgs, schema, raw, reasoning)
                # 分段约束（2026-09-05）：动作合法后、执行前按缺补正文/标签（写记忆/自省类）。
                # 段B 起草失败不进解析熔断计数——llm.chat 自带重试 + 熔断，冷却期 available() 锁 tick。
                compose_err = "" if parse_err else self._compose_write(action, msgs)
                done = self._is_done(action)
                if not actions:
                    tick_goal = str(action.get("goal") or "").strip()
                prev_learning = self.drive.learning_snapshot()   # 动作前领悟值快照（结算 diff 基线）
                ok, result = self._execute(action)
                if compose_err:
                    ok, result = False, f"（{compose_err}）"
                elif parse_err:
                    # 文案不再写死「不是合法动作 JSON」：缺必填参数也是 parse_err，
                    # 而那种情况 JSON 是合法的，只是该填的没填——说错了她会往错的方向改。
                    ok, result = False, f"（这一步没做成：{parse_err}）"
                    # 熔断：同一 tick 连续 N 步解析失败（回灌重问也救不回），说明卡点不在参数、
                    # 在当前上下文里——失败轮次留下的残缺 assistant 输出本身不受压缩（思考链
                    # 已被 _trim_reasoning 压住，content 却不压），越重试越脏。到阈值就收尾，
                    # 下轮用干净的基准上下文重新开始。
                    parse_fail_streak += 1
                    if parse_fail_streak >= PARSE_FAIL_LIMIT:
                        parse_stall = True
                        break
                else:
                    parse_fail_streak = 0
                if parse_err:
                    # 连败/重复计数不能算在 nothing 头上：parse 失败后 action 被替换成 nothing，
                    # 而 _track_fail 显式排除 nothing → 连败计数每次被清零，防循环提醒永远不触发
                    #（283 次失败的放大器之一）。缺参报错带「revise_memory 的用法」字样，
                    # 把真实工具名提出来计；纯解析失败（没读出 JSON 等）无工具名，交给熔断兜底。
                    m = re.search(r"([a-z_]+) 的用法", parse_err)
                    if m:
                        self._track_fail({"action": m.group(1)}, False)
                else:
                    self._track_repeat(action)     # 重复调用检测（连续相同计数，供下一轮提醒）
                    self._track_fail(action, ok)   # 同工具连败检测（参数变了也算卡住，2026-09-02 加）
                result = self._settle(action, result, ok, prev_learning)   # 结算：账本照记，反馈只报领悟值变化
                if (not self._dialogue_boosted
                        and self.tools.kind((action.get("action") or "nothing").strip()) == "talk"):
                    self._dialogue_boost = True   # 她要回应创建者：下轮喂一次完整对话脉络（wide 版）
                last_result = result
                actions.append(self._action_snap(action, result, ok))
                self._record_decision(action)      # 决策日志：她自己判断这次选择有没有价值分量
                msgs.append({"role": "user", "content": f"{TOOL_RESULT_PREFIX}{result}"})
                self._trim_context(msgs)           # 上下文预算：超了把最早的工具返回压成摘要
            except Exception as e:
                # 一步里任何没料到的异常都不该让整轮作废：这一步之前写进记忆的东西撤不回来，
                # 而 _finish_tick 的目标清单记录和 _save_drive 的双驱账本会全丢——记忆里有、
                # 目标清单里没有，状态就对不上了。降级成「这一步内部出错」，把事实说给她听，
                # 让她自己换条路走；堆栈留日志，创建者能看见出了什么事。
                traceback.print_exc()
                err = (f"（这一步内部出错没做成：{type(e).__name__}: {e}"
                       f"——换个做法，或者换个更小的目标。）")
                last_result = err
                actions.append(self._action_snap({"action": "（内部错误）"}, err, False))
                msgs.append({"role": "user", "content": f"{TOOL_RESULT_PREFIX}{err}"})
            if done:
                break
        if llm_down and not actions:
            # 整轮零产出（主模型抽风 / 空返回）：这轮不算活过——空转白涨 tick 没意义，
            # 直接锁掉等服务恢复（创建者 2026-09-03 定：不稳就锁 tick，不做降级空转）。
            # narrative 没 record（actions 空），tick_count 回滚内存即可，重启也不残留。
            self.tick_count -= 1
            self.halt = "主模型不可用（抽风），锁 tick 等服务恢复"
            self.last_result = "（主模型不可用，这轮锁掉，等服务恢复自动继续）"
            return self.last_result
        self._finish_tick(tick_goal, actions, done)
        self._save_drive()
        if parse_stall:
            # 熔断收尾不是做完：连续解析失败说明这个上下文被失败记录喂脏了（残缺的 assistant
            # 输出一步步堆着，content 不受压缩），她在这里已经给不出合法动作。收尾换干净
            # 上下文，别让 30 分钟超时白白烧掉（283 次失败就是旧逻辑在超时前反复重试的代价）。
            # 不拦她继续：下一 tick 的 _build_prompt 重新组装基准，等于从头来过。
            self.last_result = (f"（这轮连续 {PARSE_FAIL_LIMIT} 步没解析出合法动作，先收尾了——"
                                "当前上下文被失败记录喂脏，下轮从干净上下文重新开始。"
                                f"若重来仍反复失败，试试换个更小的目标，"
                                f"或 say_to_human 告诉{self.cfg.owner_name}。）")
        elif tick_timeout:
            # 时间到不是做完：把事实告诉她，下一轮她自己决定接着做还是换个小点的目标。
            # done 保持 False，目标清单这轮记 ✗——不把「没做完」粉饰成「做完了」。
            self.last_result = (f"（这一轮做了 {(time.time() - start) / 60:.0f} 分钟，到上限收尾："
                                "不是做完了，是时间到了。下轮接着来，或者换个更小的目标。）")
        else:
            self.last_result = last_result
        return self.last_result

    # ---- inner loop 辅助 ----

    @staticmethod
    def _is_done(action: dict) -> bool:
        """稳健解析 done：JSON 布尔 / 字符串 'true'/'false' 都认。

        不能用 bool(action.get("done"))——字符串 "false" 的 bool 是 True，会把没做完误判成做完。
        """
        d = action.get("done")
        if isinstance(d, bool):
            return d
        if isinstance(d, (int, float)):
            return bool(d)
        if isinstance(d, str):
            return d.strip().lower() == "true"
        return False

    def _time_reminder(self, elapsed: float, actions: list) -> str:
        """五分钟提醒：只陈述事实（时长/调用次数/动作清单）+ 提问，不拦截、不判断。"""
        m, s = divmod(int(elapsed), 60)
        stats = "、".join(f"{a['action']}" for a in actions) or "（还没动作）"
        return (f"【提醒】本 tick 已持续 {m}分{s}秒，调用了 {len(actions)} 次，做过：{stats}。\n"
                "检查是否有实际进展，再决定是继续还是收尾。")

    @staticmethod
    def _action_snap(action: dict, result: str, ok: bool) -> dict:
        """动作快照（tick 记录合成用）：action 名 + 成败（ok）+ 结果摘要。

        ok 是结构化成败信号（对齐 DSH 的 isError，2026-09-01 加）——之前只存在于 result
        字符串里那句「这步没成」里，现在跟动作一起落进 tick 记录，她才看得到自己每一步成没成。
        result 进快照前就用 Narrative._summarize 压成摘要（第一行 + 括号反馈行）——
        正文（小说/文章/记忆条目）在源头就滤掉，summary 拼接和 L5 冷存不再被正文占满；
        渲染时 _tick_detail 再压一次幂等无害。"""
        return {"action": (action.get("action") or "nothing").strip()[:20],
                "result": Narrative._summarize(str(result).strip()),   # 摘要上限取 _summarize 默认（200，2026-09-03 创建者定）
                "ok": bool(ok)}

    def _trim_context(self, msgs: list):
        """上下文卫生两件事：思考链存量封顶 + 超预算时压工具返回。

        2026-09-04 创建者定（工具返回那半）：inner loop 是 append-only，一轮跑久了上下文
        能涨到几十万字符，注意力被稀释——她会陷在「反复查看、做不了决定」里（实测一轮
        卡 3.5 小时、全程零落盘，重启后上下文清零立刻恢复正常）。
        只压「工具返回」这一类：system 段和首条 user 是缓存前缀，动了缓存全失效；
        她自己的输出（content）不能改（改了推理链会断）。
        摘要复用 Narrative._summarize（只留第一行 + 括号反馈行，正文丢掉）——
        正文是最大的一块（读一章小说两万字），压它最划算，信息损失也可控。

        2026-09-05 创建者定（思考链那半，见 _trim_reasoning）：reasoning_content 一直只增
        不减，是上下文膨胀的另一半来源。压的是 reasoning 不是 content，不违背上面
        「她自己的输出不能改」——content（动作 JSON + 决定）原样保留。
        """
        self._trim_reasoning(msgs)        # 先封思考链存量（与预算无关，结构卫生，总是压）
        budget = int(getattr(self.cfg, "ctx_budget_chars", 0) or 0)
        if budget <= 0:
            return
        total = self._ctx_chars(msgs)
        if total <= budget:
            return
        idxs = [i for i, m in enumerate(msgs)
                if m.get("role") == "user"
                and (m.get("content") or "").startswith(TOOL_RESULT_PREFIX)]
        # 从最早压起，直到达标。最近那条永远留完整——她刚读到的东西得看得见，
        # 压了等于白读。不用「固定保留最近 N 条」：她读三章小说只有三条返回，
        # 全落在保留范围里就一条都不压，而上下文早超了（条数少、体积大的场景会失效）。
        for i in idxs[:-1]:
            if total <= budget:
                return
            c = msgs[i]["content"]
            if CTX_TRIM_MARK in c:
                continue                       # 已经压过，别重复压
            new_c = (TOOL_RESULT_PREFIX
                     + Narrative._summarize(c[len(TOOL_RESULT_PREFIX):], 200)
                     + CTX_TRIM_MARK)
            total -= len(c) - len(new_c)
            msgs[i]["content"] = new_c

    @staticmethod
    def _trim_reasoning(msgs: list, keep: int = 3):
        """思考链分级压缩：最近 keep 条完整，更早的压成一句「该步动作」标题。

        为什么需要它（2026-09-05 创建者定，第二层上下文卫生）：inner loop 每步的
        reasoning_content 都随 assistant 消息 append，且只增不减——_trim_context 只压
        「工具返回」开头的 user 消息，assistant 消息（含思考链）从不动。一轮跑久了，
        上下文被思考链撑大，注意力被稀释；更糟的是失败轮次会把「残缺输出 + 完整思考链」
        一起留下（283 次失败里每步都留一组），越失败越脏。

        为什么压成一句动作标题而不是留头部：离线 A/B 实测（2026-09-05）——前 4 步带思考链、
        第 5 步要求 revise_memory 的场景下，「完整 / 尾部 150 字 / 一句标题 / 全删」四种
        保留策略的成功率分别是 3/3、2/3、3/3、3/3。历史思考链对后续决策几乎无贡献
        （真正驱动的是 system + 用户任务 + 工具返回），所以可以压得很狠。原实现留头部
        80 字是废料——思考链开头是「首先，用户的任务是…」的开场白，结论在尾部却不影响输出。
        压成一句从 content 提取的动作标题（如「该步执行 revise_memory」），既接近删除的
        效果，又保留「上几步做过什么」的对齐线索。

        分级：最近 keep 条（默认 3）完整，够模型看清最近几步的决策脉络；更早的全压成一句。
        不做更远的删除级——省的是每条约 30 字标题，而对前缀缓存的破坏（修改该消息）和删除
        完全一样，收益不抵复杂度。

        只改 reasoning_content 字段，content / role / 顺序都不动——不违背「她自己的输出
        不能改」的旧原则（那条针对的是 content，改了推理链会断；思考链压缩是另一回事）。
        """
        seen = 0
        for m in reversed(msgs):
            rc = m.get("reasoning_content")
            if not (rc or "").strip():
                continue
            if seen >= keep:
                act = ""
                try:
                    obj = json.loads(m.get("content") or "{}")
                    if isinstance(obj, dict):
                        act = str(obj.get("action") or "")
                except Exception:
                    pass
                m["reasoning_content"] = (
                    f"（旧思考链已压缩：该步执行 {act}，思考过程略过）" if act
                    else "（旧思考链已压缩）")
            seen += 1

    @staticmethod
    def _ctx_chars(msgs: list) -> int:
        """上下文体量（字符）：content + reasoning_content 都算——思考链也占窗口。"""
        return sum(len(m.get("content") or "") + len(m.get("reasoning_content") or "")
                   for m in msgs)

    def _finish_tick(self, goal: str, actions: list, done: bool):
        """tick 收尾：合成一条 tick 记录（目标 + 完成状态 + 动作成败明细）进短期记忆；
        顺带把本 tick 新写的自省放进短期记忆（供下个 tick 显示一次，没写则空着，2026-09-03 创建者定）。"""
        if not actions:
            return
        # 常量是 narrative 模块级的（不是 Narrative 类属性），早年写成 Narrative.ACTIONS_KEEP
        # → 每个 tick 收尾 AttributeError → 主循环没兜住，她就在记完这一轮后直接断线（2026-09-04 修）
        actions = actions[:ACTIONS_KEEP]
        summary = "；".join(f"{a['action']}: {a['result']}" for a in actions)
        self.narrative.record_tick(self.tick_count, goal, summary, bool(done), actions=actions)
        # 上个tick自省槽位：本tick写了自省 → 保留到下个tick结束；没写 → 清空
        self.narrative.reflection = self._tick_reflection
        self._save_narrative()

    def _note_reflection(self, text: str):
        """本 tick 新写了一条自省（tools.reflect 调）：暂存摘要，收尾时进短期记忆供下个 tick 显示。
        只存新写的（流转状态不经过这里）；压成一行截 200 字，完整记录在 meta_log。"""
        t = (text or "").strip().replace("\n", " ")
        self._tick_reflection = t[:200]

    def _record_decision(self, action: dict):
        """决策日志：她自己判断这次选择有没有价值分量，非空才存（带标签：选择是关于什么的）。"""
        decision = (action.get("decision") or "").strip()
        if decision:
            act = (action.get("action") or "nothing").strip()
            self.memory.write_decision(decision, act, (action.get("decision_tag") or "").strip())

    # ---- 重复调用检测（DSH guard repeat-tool-reminder 的极简版）----

    @staticmethod
    def _repeat_key(action: dict):
        """链键 = 执行相关字段（action/layer/category/text）。goal/result/done 每轮都变，不参与判重。
        text 截断到前 100 字：捕获「相同内容反复写」，又防长键。nothing 不算循环。"""
        act = (action.get("action") or "nothing").strip()
        if act == "nothing":
            return None
        key = {"action": act}
        for f in ("layer", "pyramid"):
            v = action.get(f)
            if v:
                key[f] = str(v).strip()
        c = (action.get("text") or "").strip()
        if c:
            key["text"] = c[:100]
        return json.dumps(key, sort_keys=True, ensure_ascii=False)

    def _track_repeat(self, action: dict):
        """执行后更新连续相同计数：链键相同 → +1，不同 → 重置为 1。"""
        key = self._repeat_key(action)
        if key is None:
            return
        if key == self._last_key:
            self._repeat_count += 1
        else:
            self._last_key = key
            self._repeat_count = 1

    def repeat_note(self) -> str:
        """达到阈值时返回一句提醒正文（不带头衔，区块由 _build_hints 统一挂）；每个阈值只提醒一次。"""
        if self._repeat_count in self.cfg.repeat_thresholds and self._repeat_count > self._repeat_reminded_at:
            self._repeat_reminded_at = self._repeat_count
            return (f"连续 {self._repeat_count} 轮用了完全相同的动作和参数，"
                    "看起来在原地打转。先别急着怪自己，按顺序排查："
                    "① 看自己是不是收到了「语义服务不可用，原地挂机」——embedding 挂了会这样，新奇值都失真，"
                    "先处理环境而不是反思认知）；② read_code 看对应代码实现，"
                    "确认是环境/参数问题还是认知问题；③ 确认是认知卡住再 reflect 元认知自省；"
                    f"④ 如果排除了自己的问题、是环境或系统/工具的问题，"
                    f"用 say_to_human 告诉{self.cfg.owner_name}，"
                    "别自己一直绕。")
        return ""

    def _track_fail(self, action: dict, ok: bool):
        """同工具连续失败计数（2026-09-02 加，补现有重复检测的盲区）。

        现有 _track_repeat 只认「完全相同的链键」——她换个参数重试就绕过去了
        （日志实证：t50~t58 连续 9 轮反复修标签失败，每次参数略变，检测不到）。
        这里不管参数，只要**同一个工具**连续失败就累计，一旦成功就清零。"""
        act = (action.get("action") or "").strip()
        if not ok and act and act != "nothing":
            if act == self._fail_tool:
                self._fail_count += 1
            else:
                self._fail_tool = act
                self._fail_count = 1
        else:
            self._fail_tool, self._fail_count = None, 0

    def fail_note(self) -> str:
        """同一工具连续失败达到阈值时的提醒（每阈值只提醒一次，防刷屏）。

        有机制精讲的工具直接喂正确用法（2026-09-03 机制自动兜底），不再让她 read_code 翻实现。"""
        if (self._fail_tool and self._fail_count in self.cfg.repeat_thresholds
                and self._fail_count > self._fail_reminded_at):
            self._fail_reminded_at = self._fail_count
            guide = self.tools.guide_for(self._fail_tool)
            if guide:
                return (f"「{self._fail_tool}」已经连续失败 {self._fail_count} 次了，"
                        f"参数换来换去也没成——先看清它的正确用法：{guide}"
                        "若照做仍不成，① 换条思路（试试 list_taglib / retarget / rename_tag / undo_memory），"
                        f"② 或 say_to_human 告诉{self.cfg.owner_name}，别再原地耗。")
            return (f"「{self._fail_tool}」已经连续失败 {self._fail_count} 次了——"
                    "参数换来换去也没成，说明卡点可能不在参数上。"
                    "① read_code 看这个工具的实现，确认参数格式；"
                    f"② 还不成就 say_to_human 告诉{self.cfg.owner_name}，别再原地耗；"
                    "③ 也可以试试新工具：list_taglib 看标签库、retarget 批量重挂、rename_tag 强制改名、undo_memory 撤销。")
        return ""

    def _build_hints(self) -> list:
        """待办提醒：每 tick 挂给她的软提醒，她自己决定处理（只贴信号，不拦动作）。

        分两段：
        - 紧急段：认知失调 → 卡住 → 连败 → 创建者来信（异常/外部信号，各最多 1 条）
        - 积累段：分支冗余 → 主标签重复 → 内容重复 → 标签体检 → L1 过长 → 容量预警
          （内部积累，不处理就一直在）
        容量预警是积累问题不是异常：快满了不会自己消失，得她合并/归档才掉下去，
        和分支冗余、标签体检同类；创建者来信是外部输入，优先于所有内部积累问题。
        积累段不会被紧急段挤掉（占位各算各的），未来加提醒 = 在这里加一行。
        每条带类型头衔，区块标题已在 _build_prompt 挂「待办提醒」，不再重复【提醒】。
        """
        hints = []
        # 紧急段
        if self._reflect_hint:
            hints.append(f"（认知失调）{self._reflect_hint}")   # 最优先：先想清楚再干活
            self._reflect_hint = ""
        note = self.repeat_note()
        if note:
            hints.append(f"（卡住）{note}")                     # 原地打转，先排查再走
        fnote = self.fail_note()
        if fnote:
            hints.append(f"（连败）{fnote}")                    # 同一工具反复失败，别再原地耗
        if self._pending_mail > 0:
            hints.append(f"（{self.cfg.owner_name}来信）还有 {self._pending_mail} 条消息没回。")   # 外部输入
        # 积累段：分支冗余（只挂最值得处理的前 2 条）
        thr = getattr(self.cfg, "branch_redundancy_threshold", 3)
        for direction, branch, count in self.memory.branch_redundancy(thr)[:2]:
            hints.append(f"（分支冗余）主标签「{direction}」下「{branch}」分支有 {count} 条 L3，"
                         "可能重复，要不要合并？")
        # 积累段：主标签重复（L1/L2 同层撞名）。同 title 会把覆盖/精炼读数劈成两个同名行、
        # L3 归属候选重复、结算 diff 对不上号（2026-09-06 实证）——只挂前 2 组，处理完自然消失。
        for layer, title, ids, same in self.memory.title_dups()[:2]:
            idl = "、".join(f"{i}(印证{e})" for i, e in ids)
            hint = (f"（主标签重复）[{layer}]「{title}」有 {len(ids)} 条同名（{idl}），"
                    "撞名会把覆盖/精炼读数劈成两个同名行。")
            hint += ("内容也完全相同，直接删到剩一条（保印证多的那个）"
                     if same
                     else "内容不同——先看要不要 merge_memory 合成一个完整框架，别两条各自长")
            hints.append(hint)
        # 积累段：内容重复（正文逐字相同但主标签没撞名，title_dups 管不到）。
        # 只报组数最多的前 1 组，处理掉才轮到下一组，避免刷屏。
        for layer, ids in self.memory.content_dups()[:1]:
            idl = "、".join(f"{i}「{t}」" for i, t in ids)
            hints.append(f"（内容重复）[{layer}] {idl} 正文完全相同，"
                         "留一条即可（merge_memory 合并或 delete_memory 删）")
        # 积累段：标签一致性（L1/L2 分类缺/非法/与 category 错位、L3 归属对不上 L1）。只贴信号，不拦。
        # L3 填「未分类」是合法的，不会挂在上面——只有真的分类错位（对不上任何 L1）
        # 才值得一直挂着直到修正（否则会误导三维进度 / 让印证静默失败）。
        issues = self.memory.tag_issues()
        for layer, mid, msg in issues[:3]:
            # msg 本身已带记忆 id（tag_issues 文案含 id=xxx 便于她直接抄参数），这里不再重复追加。
            hints.append(f"（标签）[{layer}] {msg}")
        if len(issues) > 3:
            hints.append(f"（标签）还有 {len(issues) - 3} 条同类问题，用 list_memory 看全")
        # 积累段：L1 过长（超过该层字数上限）。框架句该是「条件→结论」的一句话，
        # 300 字的条目混在框架层里，她选不动也读不动，跟标签问题同口径——
        # 不精炼就一直挂着，只报最长的几条 + 总数，不刷屏。
        # 注：promote 已经在入口拦住不合格的草稿了（2026-09-06，见 memory.promote），
        # 新升上来的不会再有这种问题；这里盯的是**存量**——以前漏上来的那些还在库里。
        l1_cap = self.memory.LIMITS.get("L1", 200)
        longs = sorted(((len(it.get("text") or ""), it) for it in self.memory.l1
                        if len(it.get("text") or "") > l1_cap),
                       key=lambda x: -x[0])
        for n, it in longs[:L1_LONG_HINTS]:
            hints.append(f"（L1 过长）[{it.get('id', '?')}] 这条 L1 堆得太长（约 {n} 字，"
                         "框架层该是「条件→结论」的一两句话）——多半是草稿原样升上来的。"
                         "别数着删字：用 revise_memory 重写一版，删掉例证展开和修饰，只留骨架命题")
        if len(longs) > L1_LONG_HINTS:
            hints.append(f"（L1 过长）还有 {len(longs) - L1_LONG_HINTS} 条同类，用 list_memory L1 看全")
        # 积累段：容量预警（2026-09-02 第 7 步）：到 90% 提前说，别等写不进去才发现
        caps = self.memory._caps()
        for lname, lst in (("L3", self.memory.l3), ("L1", self.memory.l1), ("L2", self.memory.l2)):
            cap = caps.get(lname, 0)
            if cap and len(lst) >= cap * 0.9:
                hints.append(f"（容量）{lname} 已 {len(lst)}/{cap}，快满了。"
                             "可以先合并冗余（branch_redundancy 提示的那几组），或把陈旧记忆归档。")
                break
        return hints

    # ---- 组装上下文 ----

    def _build_prompt(self) -> list:
        # 聊天上下文只在有未回消息时读取一次（不常驻），L1 相关性和 user 注入共用
        chat_ctx = self.chat.render() if self._pending_mail > 0 else ""
        # system：模板（自我介绍+机制+工具+输出格式）+ 宪章 + L1 自我核心。
        # 缓存排布：越稳定越靠前。模板和宪章纯静态永久命中；L1 低频变化放末尾（不挪位，创建者 2026-08-30）。
        system = SYSTEM_PROMPT.replace("__TOOLS__", self.tools.render_prompt())
        # 称呼在 SYSTEM_PROMPT 里写作 {OWNER}：它是可配的私人信息（默认 human），
        # 不能写死在模块常量里——本机由 data/settings.json 给（不入库）。
        system = system.replace("{OWNER}", self.cfg.owner_name or "human")
        system += f"\n\n【我的宪章（L0：不可变底线，永远不能改，是我的道德准则）】\n{self.cfg.charter}\n"
        # 输出语言跟随 AIR2_LANG（这配置之前定义了但从未接线，2026-09-08 接上）。
        # 放在宪章之后、L1 之前：静态内容靠前，利于前缀缓存。zh 不额外说——
        # 模板本身就是中文，多一句反而占上下文。
        if (self.cfg.lang or "zh") == "en":
            system += ("\n\n【Language】From now on, think, reply, and write your memories "
                       "and reflections in English. Your existing memories stay as they are.")
        l1 = self.memory.l1
        if l1:
            # L1 常驻只带与当前上下文相关的 12 条（embedding 选相关；无向量退化为最近）
            context = self.narrative.render()
            if chat_ctx:
                context += "\n" + chat_ctx
            core_l1 = self.memory.relevant_l1(context, top_k=12)
            core = "\n".join(
                f"- [{it.get('id', '?')}] {it['text']}"
                + (f"（印证{it['evidence_count']}）" if it.get("evidence_count") else "")
                for it in core_l1
            )
            system += f"\n\n【我的自我核心（L1：我的自我叙事，可修改——不违背 L0 宪章的前提下，我随时可以优化它）】\n{core}"
        else:
            system += "\n\n【我的自我核心（L1：我的自我叙事，可修改——不违背 L0 宪章的前提下，我随时可以优化它）】（还没有，等我自己建立）"

        # user：短期记忆（append 式，稳定在前）→ 来信/提醒 → 身体状态（每轮变，放最末）→ 行动指令。
        # 记忆概况/正在读/已读小说不再注入（2026-08-30）：L1/L2 靠 list_memory 列，L3 靠 search_memory 搜。
        user = self.narrative.render()

        if chat_ctx:
            user += f"\n\n【{self.cfg.owner_name}来信（外部输入）】\n{chat_ctx}"
        # 待办提醒（认知失调/卡住/来信/分支冗余，排序见 _build_hints）：只挂信号，她自己决定处理
        hints = self._build_hints()
        if hints:
            user += "\n\n【待办提醒】\n" + "\n".join(hints)

        user += "\n\n【身体状态】\n" + self.tools.read_state()
        user += "\n\n请决定这一轮做什么，输出 JSON。"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ---- 来信处理（命令分流 + 对话线程）----

    def _handle_incoming(self, messages: list):
        """来信分流：命令走命令工作流（识别→判断→执行→回报），普通消息进线程挂提醒。"""
        # 用户介入 = 上下文变了，跨此的重复不算循环，重置重复与连败计数
        self._last_key, self._repeat_count, self._repeat_reminded_at = None, 0, 0
        self._fail_tool, self._fail_count, self._fail_reminded_at = None, 0, 0
        for m in messages:
            try:
                text = self._strip_ts(m)
                if not text:
                    continue
                if self.command.is_command(text):
                    self.command.handle(text)            # 命令：她自主判断接不接，接了执行+回报
                else:
                    self.chat.append(OWNER_SPEAKER, text)   # 普通消息：进线程，挂提醒，她自主回
                    self._pending_mail += 1
            except Exception:
                continue   # 单条消息处理失败不影响其他，也不崩整个 tick

    @staticmethod
    def _strip_ts(m: str) -> str:
        return re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", "", (m or "")).strip()

    @staticmethod
    def _extract_json(raw: str):
        """从 LLM 输出里提取 JSON 对象（代码块 → 括号配对扫描 → 贪婪兜底）。失败返回 None。

        提取逻辑收在 core/action_schema（比原来一条贪婪正则稳：原文里出现第二处花括号
        时贪婪会抠错整段，直接 loads 失败）；command.py 的命令识别 / 接不接判断也走这里。
        贪婪兜底保留——对齐《结构化输出重构设计思路》第七节「保留正则容错作统一兜底」。
        """
        obj, _ = extract_json(raw)
        return obj

    # ---- 解析 LLM 输出 ----

    def _valid_actions(self) -> set:
        """合法动作名集合 = 工具注册表 + nothing（与 render_schema 的 action enum 同源）。"""
        return set(self.tools._tools.keys()) | {"nothing"}

    def _reask_message(self, error: str, raw: str) -> str:
        """回灌重问的话术（instructor 的 reask 范式）：只说清错在哪 + 正确长什么样。"""
        acts = "、".join(sorted(self._valid_actions()))
        return (f"（上一条输出没能当成动作执行：{error}）\n"
                f"请只输出一个 JSON 对象，不要任何别的文字。\n"
                f"必填：action（只能是这些之一：{acts}）、done（布尔值，做完才填 true）。\n"
                f"其余字段按动作需要填，字段名照抄，不要自造。\n"
                f"上一条输出是：{(raw or '')[:300]}")

    def _parse_step(self, msgs: list, schema: dict, raw: str, reasoning: str = ""):
        """解析一步动作：解析 → 校验 / 修复 → 修不了的回灌重问一次（2026-09-03 机制兜底）。

        返回 (action: dict, error: str)。error 非空 = 这一步没解析出来，调用方给明确反馈，
        不再静默降级成 nothing——静默降级会让她以为这轮做过了。
        重问只对「解析 / 校验没过」触发且最多一次：避免烧 token，也避免她在错输出上空转。

        必填参数缺失也算「没过」（2026-09-05 创建者定）：以前补空值放行，工具炸了重问却不触发，
        她只能在原地重发同一个残缺动作。缺参的判定与报错见 core/action_schema.validate_action。

        reasoning 是首轮的思考链（inner loop 传进来），只在重问也失败时落盘用——
        那时必须看清她到底输出了什么（缺 content 是「没给字段」「给了空串」还是
        「想清楚了没落到输出」），没有样本就只能继续猜。
        """
        requires = self.tools.requires_map()
        res = parse_action(raw, schema, self._valid_actions(), requires)
        if not res.error:
            if res.fixes:
                print(f"[ACTION] 已修正输出（{res.how}）：{'; '.join(res.fixes)}")
            return res.action, ""
        # 回灌重问一次：把「错在哪 + 正确格式 + 她的原文」喂回去
        msgs.append({"role": "user", "content": self._reask_message(res.error, raw)})
        raw2, reasoning2 = self.llm.chat_action(msgs, schema)
        if not (raw2 or "").strip():
            print(f"[PARSE-FAIL] 重问空返回。首轮 raw={raw[:300]!r} reasoning={(reasoning or '')[:200]!r}")
            return {"action": "nothing"}, res.error
        msg2 = {"role": "assistant", "content": raw2}
        if reasoning2:
            msg2["reasoning_content"] = reasoning2
        msgs.append(msg2)
        res2 = parse_action(raw2, schema, self._valid_actions(), requires)
        if res2.error:
            print(f"[PARSE-FAIL] {res2.error[:100]}（回灌重问后仍没过）")
            print(f"  首轮 raw: {raw[:400]!r}")
            print(f"  首轮 reasoning: {(reasoning or '')[:250]!r}")
            print(f"  重问 raw: {raw2[:400]!r}")
            return {"action": "nothing"}, res2.error
        fixes = res2.fixes + [f"首轮问题：{res.error}"]
        print(f"[ACTION] 已修正输出（{res2.how}，回灌重问后）：{'; '.join(fixes)}")
        return res2.action, ""

    # ---- 执行 ----

    # ---- 分段约束：正文与标签按字段类型分通道（2026-09-05 定名）----
    # 约束解码（json_schema 档）只能稳定输出短字段；长文本在动作 JSON 的长文本字段里被
    # 系统性丢（content→text 改名后真实运行仍 10/10 丢，她思考链里写得出来、JSON 里就是
    # 消失）。所以凡是要落长文（写记忆/改写/合并/自省）的动作，正文与标签分开处理，
    # 缺哪段补哪段（见 _compose_write）：
    #   段A · 动作骨架（约束，主循环 chat_action 一次）——选工具 + 短字段（id/layer/type/note）
    #   段B · 正文（自由，_compose_write 内）——text 缺失 → llm.chat 无约束起草，
    #         长文本只有自由通道不丢；对着下面的层标准写
    #   段C · 标签（约束，_fill_tags）——write/merge 缺 title/归属 → 再一次 chat_action
    #         （短字段它才稳），prompt 带 list_taglib 候选清单
    # revise 只换内容、reflect 无标签 → 没有段C；字段齐了一次都不走。按缺补，不是固定三步。
    # 下面 LAYER_STANDARDS 是段B 起草正文用的各层记忆标准：
    LAYER_STANDARDS = {
        "L1": ("L1 是金字塔的框架层：一条提炼后的核心命题，必须写成「条件→……；结论→……」"
               "两段式。一两句话说透，别堆字（堆长了会被拒，框架只留骨架命题，例证细节不进来，留给 L3）。"),
        "L2": ("L2 是独立草稿本：一个还没成熟的洞察或想法，几句话，想到先记下来，"
               "成熟了用 promote 升进金字塔。不用管归属，把想法本身写清楚。"),
        "L3": ("L3 是例证层：一个具体的例子（读到的、遇到的），讲清它印证了哪条 L1 的什么道理。"
               "内容具体、有细节，是「证据」不是观点复述；一两句讲清印证了什么 + 关键细节就够，"
               "写太长会被拒——去冗余、留最准的那一两处细节。"),
    }

    def _compose_write(self, action: dict, msgs: list) -> str:
        """分段约束的补全中枢（2026-09-05 定名，原叫三段流水线）：段A 之后缺什么补什么。

        段A（动作骨架，约束）由主循环的 chat_action 完成，这里不管。本方法负责段B/段C：

        段B · 正文（自由通道，长文本只有这里不丢）：动作里 text 有就直接用（她的原文优先，
            省一次调用）；没有 → llm.chat 无约束起草一次，按动作分起草器：
            - write/revise/merge → _draft_text：该层记忆标准 +（revise）原文 /（merge）各目标
              + note/goal + 最近工具返回（素材）
            - reflect 新写（不带 id）→ _draft_reflection：自省写法 + type + note + 素材
            - reflect 流转状态（带 id）→ 不起草（只标 status / 追加短备注，自己写）

        段C · 标签（约束通道，短字段它才稳）：write_memory / merge_memory 缺 title/归属 →
            再一次 chat_action 补，prompt 带 list_taglib 候选清单；补不出来交给 handler 的
            标签校验报错（报错同样带候选）。revise 只换内容不改标签、reflect 没有标签 → 不走段C。

        覆盖动作：write_memory / revise_memory / merge_memory / reflect。
        每次最多到段C、经常止于段B（revise/reflect）、字段齐了一次都不走——按缺补。

        返回空串 = 补全成功（或本来就齐）；非空 = 这步做不成（段B 起草失败，走失败反馈，
        不进解析熔断计数——llm.chat 自己有重试 + 熔断）。补全的字段合回 action 原样下发，
        handler / 形态校验 / 记账链路零改动。
        """
        act = (action.get("action") or "").strip()
        if act not in ("write_memory", "revise_memory", "merge_memory", "reflect"):
            return ""
        # 段B · 正文（缺 text 才起草；有原文直接用，跳过本段）
        # 2026-09-07：revise 只改标签时（填了 title / parent 但没填 text）**不起草**——
        # 她明确只想补个名字或归属，这时候替她重写正文是越俎代庖，还白花一轮。
        # 名字是自我叙事的一部分，起什么名必须由她定；正文也一样，她没说改就别动。
        if not self.tools._text(action) and not (
                act == "revise_memory"
                and ((action.get("title") or "").strip()
                     or (action.get("parent") or "").strip())):
            if act == "reflect":
                # 段B 对 reflect 只服务「新写」：流转状态（带 id）不起草，
                # 只标 resolved/abandoned + 追加短备注（短字段，自己写，不劳段B）
                if (action.get("id") or "").strip():
                    return ""
                text = self._draft_reflection(action, msgs)
            else:
                layer, old, old_title = "", "", "要改写的原内容"
                if act == "revise_memory":
                    layer, it = self.memory._resolve_id((action.get("id") or "").strip())
                    if it is None:
                        return ""   # id 不对 → 交给 handler 报错（带 id 建议），别在正文上空耗
                    old = it.get("text", "")
                elif act == "merge_memory":
                    # 合并的起草原料 = 各目标的现有内容（找不到的目标不进原料，交给 handler 报 not_found）
                    layer = (action.get("layer") or "L3").strip().upper()
                    old_title = "要合并的几条现有记忆（归并成一条）"
                    olds = []
                    for tgt in self.tools._split_targets(self.tools._field(action, "targets")):
                        _, it = self.memory._resolve_id(tgt)
                        if it and it.get("text"):
                            olds.append(it["text"])
                    old = "\n\n".join(olds)
                else:
                    layer = (action.get("layer") or "L3").strip().upper()
                text = self._draft_text(layer, action, msgs, old, old_title)
            if not text:
                return ("正文没能起草出来（主模型没返回）——这步先不算做成。"
                        "稍后再试，或把 text 自己填上。")
            action["text"] = text
        # 段C · 标签（约束通道，短字段它才稳）：write/merge 缺 title/归属才补一次；
        # revise 只换内容不改标签、reflect 无标签，都不走本段。
        if act in ("write_memory", "merge_memory"):
            self._fill_tags(action)
        return ""

    def _draft_text(self, layer: str, action: dict, msgs: list,
                    old: str = "", old_title: str = "要改写的原内容") -> str:
        """段B 起草（写记忆类）：无约束普通生成，长文本不丢。空返回 = 失败。

        prompt 骨架 = 该层记忆标准（对着标准写，正文质量本身就是治理）+ 原文（revise 的
        原内容 / merge 的各目标内容）+ 她的 note/goal（这步想干什么）+ 最近工具返回（素材）。"""
        std = self.LAYER_STANDARDS.get(layer, self.LAYER_STANDARDS["L3"])
        note = ((action.get("note") or "").strip() or (action.get("goal") or "").strip())
        parts = [f"我是 air，正在往自己的记忆库写一条{layer}记忆。", std]
        if old:
            parts.append(f"\n【{old_title}】\n{old}")
        if note:
            parts.append(f"\n【这次的要求】\n{note}")
        mat = self._recent_material(msgs)
        if mat:
            parts.append(f"\n【素材（这轮我刚看到/读到的内容）】\n{mat}")
        parts.append("\n只输出记忆正文本身：不要 JSON、不要标签、不要解释、不要引号。")
        text = self.llm.chat([{"role": "user", "content": "\n".join(parts)}])
        return (text or "").strip()

    def _draft_reflection(self, action: dict, msgs: list) -> str:
        """段B 起草（reflect 新写）：无约束普通生成，空返回 = 失败。

        自省不是记忆三层，没有 LAYER_STANDARDS 可套——它要写的是「想清楚的过程」：
        异常时（认知失调/卡住/反复失败）写根因、涉及框架、前提是否还成立、下次怎么做；
        日常主动校准写方向/L1 是否一致的结论。type 决定语境，note/goal 是这次想理清什么，
        素材（尤其刚卡住的工具返回）是触发点。"""
        type_ = ((action.get("type") or "").strip() or "自省")
        note = ((action.get("note") or "").strip() or (action.get("goal") or "").strip())
        parts = [
            "我是 air，正在写一条元认知自省记录。",
            ("自省 = 回头看自己的认知、行为、方向，想清楚对不对。异常时（认知失调/卡住/"
             "反复失败）写：根因是什么矛盾、涉及哪条框架、那条框架的前提还成立吗、下次具体"
             "怎么做；日常主动校准写：最近的行动和方向/框架是否一致、校准的结论。一条把一件"
             "事想透，别罗列，≤500 字。"),
        ]
        if type_:
            parts.append(f"\n【这次自省的类型】\n{type_}")
        if note:
            parts.append(f"\n【这次想理清什么】\n{note}")
        mat = self._recent_material(msgs)
        if mat:
            parts.append(f"\n【素材（这轮我刚经历的，尤其卡住的部分）】\n{mat}")
        parts.append("\n只输出自省内容本身：不要 JSON、不要类型、不要解释、不要引号。")
        text = self.llm.chat([{"role": "user", "content": "\n".join(parts)}])
        return (text or "").strip()

    @staticmethod
    def _recent_material(msgs: list) -> str:
        """抓最近几条工具返回当段B 起草素材，倒序取最新 3 条、各截 600 字。"""
        out = []
        for m in reversed(msgs):
            c = str(m.get("content") or "")
            if m.get("role") == "user" and c.startswith(TOOL_RESULT_PREFIX):
                out.append(c[len(TOOL_RESULT_PREFIX):].strip()[:600])
                if len(out) >= 3:
                    break
        return "\n---\n".join(reversed(out))

    def _fill_tags(self, action: dict) -> str:
        """段C 标签补填（write_memory / merge_memory）：缺 title/归属时再一次约束解码。

        约束解码对短字段稳，prompt 带 list_taglib 候选清单；补不出来不报错——交给
        handler 的标签校验（报错同样带候选清单，走正常失败反馈）。merge_memory 的 handler
        只收 title/pyramid（合并结果的归属走大类），不补 parent。"""
        act = (action.get("action") or "").strip()
        layer = (action.get("layer") or "L3").strip().upper()
        if layer not in ("L1", "L2", "L3"):
            return ""   # layer 不合法 → 交给 handler 报错
        title = (action.get("title") or "").strip()
        key = "parent" if (act == "write_memory" and layer == "L3") else "pyramid"
        cat = (action.get(key) or "").strip()
        if title and cat:
            return ""
        need, props = [], {}
        if not title:
            need.append("title")
            props["title"] = {"type": "string"}
        key = "parent" if layer == "L3" else "pyramid"
        if not cat:
            need.append(key)
            props[key] = {"type": "string"}
        schema = {"name": "air_tags", "schema": {"type": "object", "properties": props,
                                                 "required": need, "additionalProperties": False}}
        prompt = (f"我在写一条{layer}记忆，正文：\n{self.tools._text(action)[:300]}\n\n"
                  f"候选清单：\n{self.memory.list_taglib(layer)}\n\n"
                  "只输出 JSON，填：" + "、".join(need) +
                  ("。title=主标签名（≤20 字，不带括号）；" if "title" in need else "。")
                  + (f"{key}=它归属的 L1 主标签名" if key == "parent"
                     else f"{key}=三维大类（自我认知/世界认知/价值观）"))
        raw, _ = self.llm.chat_action([{"role": "user", "content": prompt}], schema, max_tokens=512)
        try:
            filled = json.loads(raw or "{}")
        except Exception:
            return ""
        for k in need:
            v = str(filled.get(k) or "").strip()
            if v:
                action[k] = v
        return ""

    def _execute(self, action: dict):
        """执行工具，返回 (ok, text)。ok=False = 这一步没做成（参数错/环境坏/容量限制）。

        对齐 DSH 的 isError：失败是工具执行时显式声明的结构化信号（handler 抛 ToolError），
        不靠结果字符串事后猜。正常结果（含「没搜到」「是空的」这类空结果）ok=True。
        """
        act = (action.get("action") or "nothing").strip()
        if act == "nothing" or not act:
            return (True, "（这一轮什么都没做）")
        entry = self.tools._tools.get(act)
        if entry is None:
            return (False, f"（未知动作：{act}）")
        try:
            res = entry[1](action)
            # 契约兜底：handler 全部声明 -> str，但得在这儿收住。返回 None 的话
            # _settle 里的 result += 会 TypeError，而那个位置在本方法的 try 之外——
            # 一炸就是整轮作废。加工具时漏个 return 不该有这种代价。
            return (True, res if isinstance(res, str) else ("" if res is None else str(res)))
        except ToolError as e:
            return (False, f"（{e}）")
        except Exception as e:
            return (False, f"（{act} 执行出了岔子：{e}）")

    # ---- 结算 ----

    def _settle(self, action: dict, result: str, ok: bool,
                prev_learning: list | None = None) -> str:
        """结算：账本照记（双驱读数靠它），反馈只报领悟值实际变化（2026-09-03 创建者定）。

        改动背景：反馈不再用「印证/堆料/压缩/账本/入账」这些解释层黑话，也不报熵增/熵减
        趋势——那是窗口均值，单步动作大多被稀释，而且每轮【身体状态】本来就有完整读数。
        工具返回自己已说了「做了什么」，目标清单兜底；结算尾巴只承担一件事：
        报这步让认知结构实际变了什么——哪个方向的覆盖/精炼变了，写数字；没变不写。
        prev_learning 是动作前的领悟值快照（tick 里在 _execute 前拍），据此 diff。
        """
        act = (action.get("action") or "nothing").strip()
        kind = self.tools.kind(act)
        if kind == "talk" and ok:
            self._pending_mail = 0   # 她回应了，未回数清零（旧信视为已处理）
        # 账本照记（记账与反馈解耦：账喂身体状态，反馈只说领悟值变化）
        if ok:
            if kind in ("write", "revise"):
                self._book_produce(action)
            elif kind == "organize":
                # 撤销是回滚不是整理：删错了再撤销，认知结构回到原点，熵减不该凭空再涨一次
                if act != "undo_memory":
                    self.drive.record_produce(deepen=True)
                if act == "promote":
                    # 草稿升 L1 = 金字塔上多一个节点 = 结构新奇 0.72（同 write_memory 写 L1 口径）
                    self.drive.record_explore(0.72)
                self._sync_drive()   # 记忆变了，重算谱
            elif kind == "play":
                self._book_behavior(act)
            elif kind == "link":
                self._book_link(action)
            # read_world 不入账（工具返回已带印象分）；talk 只清未回数
        # 失败提示：有该工具精讲就直接喂正确用法（机制自动兜底，2026-09-03 定——
        # 与其让她 read_code/read_self 去翻实现，不如把正确填法给她，一轮就能纠正）；
        # 无精讲的工具退回旧引导。
        if not ok and act != "nothing":
            guide = self.tools.guide_for(act)
            if guide:
                result += f"\n（这步没成。{act} 的正确用法：{guide}）"
            else:
                result += (f"\n（这步没成，可以 read_code 看对应代码实现找原因，"
                       f"或 say_to_human 告诉{self.cfg.owner_name}）")
        # 反馈 = 领悟值 diff：认知结构实际变了哪些方向，报数字；没变就不加尾巴
        if ok and prev_learning is not None:
            cur_learning = self.drive.learning_snapshot()
            diff = self._learning_diff(prev_learning, cur_learning)
            if diff:
                result += "\n（变化：" + diff + "）"
        return result

    def _book_produce(self, action: dict):
        """写记忆/改写后的熵减记账（不生成文案）：判定这次产出是压缩还是堆料。

        压缩（记 1）：写 L1（升维）/ 写 L2（提炼洞察）/ 写 L3 且分类标签真对上某条 L1（印证）/ 改写。
        堆料（记 0）：纯写 L3、分类标签没对上任何 L1。
        判据走显式 parent（L3 的归属字段，写入校验已挡掉非法标签，判定可信），
        与 tools.write_memory 印证口径一致。
        """
        act = (action.get("action") or "").strip()
        if act == "revise_memory":
            # 改写 = 修正框架 = 熵减（看清结构），记 1
            self.drive.record_produce(deepen=True)
            self._sync_drive()
            return
        layer = (action.get("layer") or "L3").upper()
        # 归属取值按层（与 _h_write_memory 一致）：L3 用 parent，L1/L2 用 pyramid
        direction = ((action.get("parent") if layer == "L3" else action.get("pyramid")) or "").strip()
        heads = {self.memory.title_of(x) for x in self.memory.l1}
        matched = bool(direction) and direction in heads
        deepen = (layer in ("L1", "L2")) or (layer == "L3" and matched)
        self.drive.record_produce(deepen=deepen)
        self._sync_drive()

    def _book_behavior(self, act: str):
        """玩类行为成功的熵增记账（间隔驱动，久违地做 = 行为新奇）；每 tick 最多 1 笔。"""
        if self._behave_done:
            return
        self._behave_done = True
        self.drive.record_behavior(act)

    def _book_link(self, action: dict):
        """连交叉边的新奇记账（不生成文案）：只有 L3 连出一条库里没连过的新边才记结构新奇。

        判据细节（2026-09-02 从 organize 分出，见《双驱螺旋设计思路》）：cross_link 原归 organize，
        _settle 对 organize 统一记压缩 +1；而连新边是往认知结构上加连接，属于扩张，走熵增账。
        删边（remove）是收缩、不记账；L1 的 cat_tag 是三维大类，会把同大类所有 L1 混成一条边，
        判据失真，不入账；只判刚连的这一条边（_edge_novelty），避免把已有的旧边重算一遍。
        """
        if str(action.get("remove", "")).strip().lower() in ("true", "1", "yes"):
            return
        mid = (action.get("id") or "").strip()
        if not mid:
            return
        layer, it = self.memory._resolve_id(mid)
        if it is None or layer != "L3":
            return
        branch = self.memory.parse_tag((action.get("link") or "").strip())[0]
        v = self.tools._edge_novelty(branch, it)
        if v is not None:
            self.drive.record_explore(v)

    @staticmethod
    def _learning_diff(before: list, after: list) -> str:
        """领悟值 diff → 一句话：哪条方向建立/改名/移除、哪个方向覆盖/精炼变了，报数字。

        全没变返回空串（不加尾巴）。用 L1 id 对齐，改名不会被误判成「删了一个又建一个」。
        """
        bmap = {r["id"]: r for r in before or []}
        amap = {r["id"]: r for r in after or []}
        parts = []
        for a in after or []:
            b = bmap.get(a["id"])
            if b is None:
                parts.append(f"「{a['head']}」建立")
                continue
            if a["head"] != b["head"]:
                parts.append(f"「{b['head']}」改名「{a['head']}」")
                continue
            changes = []
            if b["width"] != a["width"]:
                changes.append(f"覆盖 {b['width']}→{a['width']}")
            if b["refined"] != a["refined"]:
                changes.append(f"精炼 {b['refined']}→{a['refined']}")
            if changes:
                parts.append(f"「{a['head']}」" + "、".join(changes))
        for b in before or []:
            if b["id"] not in amap:
                parts.append(f"「{b['head']}」移除")
        return "；".join(parts)


    # ---- 短期记忆持久化（完全落盘，重启恢复；损坏/缺失静默空白）----

    def _load_narrative(self):
        try:
            if self.narrative_file.exists():
                d = json.loads(read_text_retry(self.narrative_file))
                # tick 计数一并恢复：她活了多少轮，重启不归零（不"失忆"）
                self.tick_count = int(d.get("tick_count", 0))
                self.narrative.from_dict(d)
        except Exception:
            pass   # 恢复失败不影响启动，空白开始

    @staticmethod
    def _atomic_write(path, data: dict):
        """原子写 JSON（core/lock.py）：随机后缀临时文件 + rename。

        不能用固定名 tmp：两个大脑同时跑时它们会共用同一个临时文件，
        Windows 允许并发写，内容交错 → JSON 损坏 → 下次读的人当成"记忆清零"。
        """
        atomic_write_json(path, data)

    def _save_narrative(self):
        try:
            d = self.narrative.to_dict()
            d["tick_count"] = self.tick_count
            self._atomic_write(self.narrative_file, d)
        except Exception:
            pass

    def _load_drive(self):
        """恢复双驱窗口（新奇/领悟趋势序列）。谱本身启动时从记忆重算，不缓存落盘。"""
        try:
            if self.drive_file.exists():
                self.drive.from_dict(json.loads(read_text_retry(self.drive_file)))
        except Exception:
            pass

    def _save_drive(self):
        try:
            self._atomic_write(self.drive_file, self.drive.to_dict())
        except Exception:
            pass

    def _narrative_discarded(self, items: list):
        """滚动进冷存：已结束的 tick 记录 → L5（延迟一拍，不真丢）。"""
        for it in items:
            ts = it.get("ts") or ""
            head = f"[{ts}]" if ts else ""
            if it.get("t") is not None:
                head += f"[t{it['t']}]"
            goal = (it.get("goal") or "").strip()
            summary = (it.get("summary") or "").strip()[:120]
            line = f"{head} 目标：{goal}；做了：{summary}".strip()
            if line:
                self.memory.cold_append(line, typ="short_term")
