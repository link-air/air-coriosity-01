"""
命令系统：创建者下命令 → 她自主判断接不接 → 执行 → 回报。

触发权在创建者（创建者发命令），决定权在她（她对照宪章 + L1 自我核心自主决定接不接）。
没有候选菜单、没有闸门——命令不是"必须执行"，是她判断后自己选择。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class CommandSystem —— 命令工作流（触发权在创建者，决定权在她）：
#     识别 is_command（纯 LLM 判命令/闲聊/分享）
#     → 感知 extract_command（拆 intent/premise/action）
#     → 判断 decide（对照宪章 + L1 自我核心，自主决定接不接）
#     → 执行 execute（映射到工具箱）
#     → 回报 handle（总入口；判出认知冲突时挂自省提醒）
# =====================================================================
from core.chat import OWNER_SPEAKER    # 对话线程里「使用者」的内部标识（中性，不写私人称呼）


class CommandSystem:
    def __init__(self, agent):
        self.agent = agent

    def is_command(self, content: str) -> bool:
        """命令识别：纯 LLM 判 command/chat/share，无关键词预筛。无 LLM → 走普通消息。"""
        a = self.agent
        if not content or not a.llm:
            return False
        try:
            owner = a.cfg.owner_name
            out = a.llm.chat([{"role": "user", "content": (
                f"{owner}对我说：「{content[:200]}」\n\n"
                "这像是 命令（明确要我去做某件事）/ 闲聊 / 分享？\n"
                "只输出一个词：command / chat / share"
            )}], max_tokens=20)
            o = (out or "").strip()
            return "command" in o.lower() or "命令" in o
        except Exception:
            return False

    def extract_command(self, content: str) -> dict:
        """感知：提取意图/前提/动作（LLM 结构化）。失败降级为原文，不阻塞。"""
        a = self.agent
        if not a.llm:
            return {"intent": content, "premise": "", "action": content}
        try:
            owner = a.cfg.owner_name
            out = a.llm.chat([{"role": "user", "content": (
                f"{owner}给我下了一条命令。我要把它拆开看清楚再决定。\n\n"
                f"命令全文：{content}\n\n"
                "提取三个字段（只输出 JSON）：\n"
                '{"intent": "他想让我做什么（一句话）", "premise": "命令里的事实断言/前提（没有就空串）", '
                '"action": "具体动作（动词+对象）"}'
            )}], max_tokens=300)
            obj = a._extract_json(out)
            if isinstance(obj, dict) and (obj.get("intent") or obj.get("action")):
                return obj
        except Exception:
            pass
        return {"intent": content, "premise": "", "action": content}

    def decide(self, content: str, parsed: dict) -> dict:
        """综合判断：对照宪章 + L1 自我核心 + 前提（创建者画像 + 最近对话），自主决定接不接。"""
        a = self.agent
        l1_text = "\n".join(f"- {it.get('text', '')[:120]}" for it in a.memory.l1[:15]) or "（还没有自我核心）"
        owner = a.cfg.owner_name
        profile = (a.chat.profile.get("summary") or "").strip() or f"（对{owner}还不了解）"
        prompt = (
            f"我收到{owner}的一条命令，现在要自己决定接不接（触发权在{owner}，决定权在我）。\n"
            f"我的宪章（不可变底线）：{a.cfg.charter}\n\n"
            f"我的自我核心（L1）：\n{l1_text}\n\n"
            f"命令：{content}\n"
            f"意图：{parsed.get('intent', '')}\n"
            f"前提：{parsed.get('premise') or '（无）'}\n\n"
            f"关于{owner}：{profile}\n\n"
            f"我们最近的对话：\n{a.chat.render() or '（无）'}\n\n"
            "请判断（只输出 JSON）：\n"
            '{"verdict": "agree|refuse|ask_back|partial", '
            '"l1_conflict": true/false, '
            '"premise_conflict": true/false, '
            '"why": "第一人称决策理由（给我自己）", '
            f'"reply": "给{owner}的回信（第一人称、自然）", '
            '"exec": {"behavior": "paint|write_code|read_rss|read_local|search_web|（空）", "param": "..."}}\n'
            "若同意且能执行：exec 填对应行为（画图=paint，写代码=write_code，读订阅源=read_rss，"
            "读本地读物=read_local，搜索=search_web），param 给具体内容；无法映射或 refuse/ask_back 时 exec 留空。"
        )
        try:
            out = a.llm.chat([{"role": "user", "content": prompt}], max_tokens=800)
            obj = a._extract_json(out)
            if isinstance(obj, dict) and obj.get("verdict") in ("agree", "refuse", "ask_back", "partial"):
                return obj
        except Exception:
            pass
        # LLM 主模型抽风判断不出：不硬编拒绝话术（那是假的，不是她的判断），静默挂起等恢复
        return {"verdict": "undecided", "reply": "", "why": "判断失败（LLM 抽风），静默挂起"}

    def execute(self, exec_: dict) -> str:
        """命令执行：映射到工具箱，返回结果文本（失败/无法映射返回空）。"""
        a = self.agent
        behavior = str((exec_ or {}).get("behavior") or "").strip()
        param = str((exec_ or {}).get("param") or "").strip()
        try:
            if behavior == "paint" and param:
                return a.tools.paint(param)
            if behavior == "write_code" and param:
                return a.tools.write_code(param)
            if behavior == "read_rss":
                return a.tools.read_rss()
            if behavior == "read_local":
                return a.tools.read_local(param)   # 空=列书目；编号或标题关键词都收
            if behavior == "search_web" and param:
                return a.tools.search_web(param)
        except Exception as e:
            return f"（执行 {behavior} 出了岔子：{e}）"
        return ""

    def handle(self, content: str) -> str:
        """命令工作流：识别 → 判断 → 执行 → 回报。决定权在她。"""
        a = self.agent
        a.chat.append(OWNER_SPEAKER, content)
        if not a.llm:
            # LLM 不可用：不硬编话术，挂提醒等她恢复后能自己看到并处理
            a._pending_mail += 1
            return "no_llm"
        parsed = self.extract_command(content)
        decision = self.decide(content, parsed)
        verdict = decision.get("verdict", "refuse")
        if verdict == "undecided":
            # 判断失败（LLM 抽风）：不编拒绝理由（那是假的），静默挂提醒等恢复
            a._pending_mail += 1
            return verdict
        reply = (decision.get("reply") or "").strip()
        # 元认知触发：命令判断出与 L1 冲突 / 前提冲突时，挂一句自省提醒（下一轮注入，只提醒不强制）
        if decision.get("l1_conflict") or decision.get("premise_conflict"):
            which = ("和我的 L1 框架冲突" if decision.get("l1_conflict")
                     else "它的前提和我已有的认知冲突")
            a._reflect_hint = (f"刚才判断{a.cfg.owner_name}的命令时发现「{which}」——这就是一次认知失调，"
                               "要不要 reflect 元认知自省一下，理清是哪条框架、前提还成不成立？"
                               "如果因此发现方向/目标定错了，想清楚后用 set_goal 改目标。")
        if verdict in ("agree", "partial"):
            outcome = self.execute(decision.get("exec") or {})
            if outcome:
                reply = (reply + f"\n\n我这就去做：{outcome}").strip()
        if reply:
            a.tools.say_to_human(reply)
        return verdict
