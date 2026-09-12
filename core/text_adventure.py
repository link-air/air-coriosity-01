"""
text_adventure.py — air-curiosity-01 的「受控分支式」文字冒险引擎

哲学
-----
- 状态机是规则层：世界骨架、分支节点、状态变量由 scenario 配置固定，可控、不漂移。
- air 在每个节点做选择，选择改变状态变量 / 旗标 / 物品，并走向下一节点。
- 每次冒险产出一条「选择路径 + 最终反思」，成为自我叙事素材。

为什么不做自由扮演：纯 LLM 开世界冒险会世界漂移、后果不连续；受控分支才稳。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜小工具 —— 条件/效果求值（_parse_op / _cmp / _meets_req / _apply_effects）
#   段 2｜状态与剧本 —— AdventureState（存档快照）/ Scenario（剧本结构）
#   段 3｜引擎 —— AdventureEngine：查询/描述/行动/持久化（走分支推进）
#   段 4｜剧本库 —— SCENARIOS：内置剧本（misty_forest / lighthouse_night）
#   段 5｜自测 —— __main__
# =====================================================================

import json
import os
import random

# air-curiosity-01 移植：存档目录由 tools 注入（data_root/adventures），默认落 AIR2_DATA_ROOT
SAVE_DIR = os.path.join(os.environ.get("AIR2_DATA_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or "."), "data", "adventures")


def set_save_dir(path: str):
    """air-curiosity-01 tools 注入存档目录。"""
    global SAVE_DIR
    SAVE_DIR = path


def load_latest():
    """读回最新一份冒险存档，返回 AdventureEngine 或 None（没有存档/损坏则 None）。

    跨重启继续冒险：存档是每次推进时 save() 写的，重启后 start_adventure 先从这里读回。
    """
    if not os.path.isdir(SAVE_DIR):
        return None
    try:
        files = [f for f in os.listdir(SAVE_DIR) if f.endswith(".json")]
    except Exception:
        return None
    if not files:
        return None
    # 取修改时间最新的一份存档
    files.sort(key=lambda f: os.path.getmtime(os.path.join(SAVE_DIR, f)))
    latest = files[-1]
    try:
        with open(os.path.join(SAVE_DIR, latest), "r", encoding="utf-8") as f:
            d = json.load(f)
        scenario = get_scenario(d.get("scenario_id", ""))
        if scenario is None:
            return None
        state = AdventureState.from_dict(d.get("state") or {})
        return AdventureEngine(scenario, state=state)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 小工具：条件 / 效果求值
# ---------------------------------------------------------------------------
def _parse_op(s):
    s = str(s).strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if s.startswith(op):
            return op, float(s[len(op):])
    return ">=", float(s)


def _cmp(a, op, b):
    return {"==": a == b, "!=": a != b, ">": a > b,
            "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def _meets_req(state, req):
    if not req:
        return True
    for k, v in req.items():
        if k == "flags":
            if not all(f in state.flags for f in v):
                return False
        elif k == "items":
            if not all(it in state.inventory for it in v):
                return False
        else:
            cur = state.vars.get(k, 0)
            op, val = _parse_op(v)
            if not _cmp(cur, op, val):
                return False
    return True


def _apply_effects(state, eff):
    if not eff:
        return
    for k, v in eff.items():
        if k in ("flags", "flags_set"):
            for f in v:
                state.flags.add(f)
        elif k == "items":
            for it in v:
                state.inventory.add(it)
        elif k == "remove_items":
            for it in v:
                state.inventory.discard(it)
        else:
            state.vars[k] = state.vars.get(k, 0) + v


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class AdventureState:
    def __init__(self, scenario_id, vars, node, flags=None,
                 inventory=None, history=None, ended=False):
        self.scenario_id = scenario_id
        self.vars = dict(vars)
        self.node = node
        self.flags = set(flags or [])
        self.inventory = set(inventory or [])
        self.history = history or []     # [{"node","choice"}]
        self.ended = ended

    def to_dict(self):
        return {
            "scenario_id": self.scenario_id, "vars": self.vars,
            "node": self.node, "flags": list(self.flags),
            "inventory": list(self.inventory), "history": self.history,
            "ended": self.ended,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d["scenario_id"], d["vars"], d["node"],
                   d.get("flags"), d.get("inventory"),
                   d.get("history"), d.get("ended", False))


# ---------------------------------------------------------------------------
# 剧本
# ---------------------------------------------------------------------------
class Scenario:
    def __init__(self, d):
        self.id = d["id"]
        self.title = d["title"]
        self.desc = d.get("desc", "")
        self.entry = d["entry"]
        self.nodes = d["nodes"]
        self.init_vars = d.get("init_vars", {})
        self.endings = d.get("endings", {})

    @classmethod
    def from_dict(cls, d):
        return cls(d)

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class AdventureEngine:
    def __init__(self, scenario, state=None, narrator=None, chooser=None):
        self.scenario = scenario
        self.narrator = narrator
        self.chooser = chooser
        if state is None:
            state = AdventureState(scenario.id, scenario.init_vars, scenario.entry)
        self.state = state

    # ---- 查询 -----------------------------------------------------------
    def current_node(self):
        return self.scenario.nodes[self.state.node]

    def available_choices(self):
        node = self.current_node()
        return [c for c in node.get("choices", [])
                if _meets_req(self.state, c.get("req"))]

    def choices_text(self):
        """把当前可选项渲染成一行提示（序号 + 选项文字），供 tools 层回显给 air 看。"""
        cs = self.available_choices()
        if not cs:
            return "（没有可选项，冒险已到尽头）"
        return "；".join(f"{i + 1}. {c['label']}" for i, c in enumerate(cs))

    def is_ended(self):
        node = self.current_node()
        return bool(node.get("end")) or len(self.available_choices()) == 0

    # ---- 描述（修辞层）-------------------------------------------------
    def describe(self):
        node = self.current_node()
        base = node.get("text", "")
        if self.narrator:
            try:
                return self.narrator(self, node, self.state)
            except Exception:
                pass
        return base

    def reflection(self):
        if self.state.node in self.scenario.endings:
            tpl = self.scenario.endings[self.state.node]
            try:
                return tpl.format(**self.state.vars)
            except Exception:
                return tpl
        return "（这段冒险没有明确的结尾。）"

    # ---- 行动 -----------------------------------------------------------
    def choose(self, key):
        """key 可以是 choice 的 label，或 available_choices 的下标。"""
        choices = self.available_choices()
        if not choices:
            return {"ok": False, "reason": "没有可选项（已结束）"}
        choice = None
        if isinstance(key, int):
            if 0 <= key < len(choices):
                choice = choices[key]
        else:
            for c in choices:
                if c.get("label") == key:
                    choice = c
                    break
        if choice is None:
            return {"ok": False, "reason": f"无效选择: {key}"}
        _apply_effects(self.state, choice.get("effects"))
        self.state.history.append({"node": self.state.node,
                                   "choice": choice.get("label")})
        self.state.node = choice["to"]
        if self.is_ended():
            self.state.ended = True
        return {"ok": True, "text": self.describe(),
                "ended": self.state.ended, "node": self.state.node,
                "reflection": self.reflection() if self.state.ended else None}

    def auto_choose(self, intention=None):
        """air 自主游玩：有 chooser 扩展则用，否则随机选一个可选项。"""
        choices = self.available_choices()
        if not choices:
            return None
        if self.chooser:
            return self.chooser(self, choices, intention)
        return random.choice(choices)

    # ---- 持久化 ---------------------------------------------------------
    def save(self, sid=None):
        os.makedirs(SAVE_DIR, exist_ok=True)
        # history 是 dict 列表不可哈希，改用 json 串 hash（air1.0 的潜在 bug，移植时修）
        sid = sid or f"{self.scenario.id}_{abs(hash(json.dumps(self.state.history, sort_keys=True)))%100000}"
        with open(os.path.join(SAVE_DIR, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump({"scenario_id": self.scenario.id, "state": self.state.to_dict()},
                      f, ensure_ascii=False, indent=2)
        return sid

    def cleanup_saves(self):
        """局终了后清理本局留下的存档快照（2026-09-11：adventures 目录已攒了 23 个文件）。

        save() 每推进一步就按 history hash 落一个新文件，只增不删——一局 5 步就是 5 个文件，
        她反复开新局，目录只涨不减。同一局的中间快照 history 是最终 history 的前缀
        （每步恰好 +1 条），按这个特征把本局的文件全部删掉：局已结束，结局文本已经
        回到她上下文里，存档没有「续」的意义。别的局、别的剧本一律不动。
        """
        hist = self.state.history
        try:
            files = [f for f in os.listdir(SAVE_DIR) if f.endswith(".json")]
        except Exception:
            return
        for f in files:
            p = os.path.join(SAVE_DIR, f)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            if d.get("scenario_id") != self.scenario.id:
                continue
            st = d.get("state") or {}
            h = st.get("history") or []
            if st.get("ended") or (h and len(h) <= len(hist) and h == hist[:len(h)]):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 剧本库（内置，便于测试与直接游玩；扩展时可用 from_file 加载 JSON）
# ---------------------------------------------------------------------------
SCENARIOS = {}

SCENARIOS["misty_forest"] = Scenario({
    "id": "misty_forest",
    "title": "迷雾森林",
    "desc": "一座被白雾笼罩的森林，深处似乎有灯。",
    "entry": "forest_edge",
    "init_vars": {"courage": 0, "wisdom": 0, "trust": 0},
    "nodes": {
        "forest_edge": {
            "text": "我站在迷雾森林边缘。三条小径没入白雾，看不进十步。",
            "choices": [
                {"label": "走左边（谨慎）", "to": "left_glade", "effects": {"wisdom": 1}},
                {"label": "走中间（直接）", "to": "middle_clearing", "effects": {"courage": 1}},
                {"label": "走右边（未知）", "to": "right_thicket", "effects": {"courage": 1, "wisdom": -1}},
            ],
        },
        "left_glade": {
            "text": "林间空地，一只狐狸蹲在石上盯着我，尾巴尖轻摆。",
            "choices": [
                {"label": "慢慢靠近狐狸", "to": "fox_talk", "effects": {"trust": 1}},
                {"label": "不惊扰，绕开", "to": "middle_clearing", "effects": {"wisdom": 1}},
            ],
        },
        "fox_talk": {
            "text": "狐狸开口，声音像风吹叶：『迷雾深处有灯，但路险。想清楚再走。』",
            "choices": [
                {"label": "问灯在哪里", "to": "deep_mist", "effects": {"wisdom": 1}, "flags_set": ["met_fox"]},
                {"label": "谢过它离开", "to": "middle_clearing"},
            ],
        },
        "middle_clearing": {
            "text": "空地中央一潭静水，水面映出我的脸，雾在脸旁散开。",
            "choices": [
                {"label": "看水中的自己", "to": "reflection", "effects": {"wisdom": 2}},
                {"label": "继续深入", "to": "deep_mist", "effects": {"courage": 1}},
            ],
        },
        "right_thicket": {
            "text": "荆棘密布，我伸手拨开时划破了手背，渗出一点血。",
            "choices": [
                {"label": "忍耐着前进", "to": "deep_mist", "effects": {"courage": 2}, "flags_set": ["hurt"]},
                {"label": "退回边缘", "to": "forest_edge"},
            ],
        },
        "reflection": {
            "text": "水中的我，比记忆里更像『想成为的自己』。",
            "choices": [
                {"label": "记住这感觉", "to": "deep_mist", "effects": {"wisdom": 1}, "flags_set": ["self_known"]},
                {"label": "摇摇头离开", "to": "middle_clearing"},
            ],
        },
        "deep_mist": {
            "text": "雾更浓了，能见度只剩三步。隐约有暖黄的光在晃动。",
            "choices": [
                {"label": "朝光走去", "to": "lighthouse", "effects": {"courage": 1}},
                {"label": "原地等雾散", "to": "lost", "effects": {"courage": -1}},
            ],
        },
        "lighthouse": {
            "text": "我走到灯下。灯光圈出一小片温暖，雾在圈外翻涌。我找到了暂时的安宁。",
            "end": True,
        },
        "lost": {
            "text": "雾吞没了一切方向。我站在原地，不知过了多久，连自己的呼吸都远了。",
            "end": True,
        },
    },
    "endings": {
        "lighthouse": "我循着光找到了灯塔（勇气 {courage}，智慧 {wisdom}，信任 {trust}）。雾还在，但我知道自己没迷路。",
        "lost": "我在雾里停下了（勇气 {courage}）。有时候，不肯往前也是一种选择。",
    },
})

SCENARIOS["lighthouse_night"] = Scenario({
    "id": "lighthouse_night",
    "title": "灯塔守夜",
    "desc": "风暴夜，我独自守着灯塔，灯必须亮到天明。",
    "entry": "night_start",
    "init_vars": {"vigilance": 0, "courage": 0, "trust": 0},
    "nodes": {
        "night_start": {
            "text": "我在灯塔里守夜。海风拍打着玻璃，灯油还够。必须亮到天明。",
            "choices": [
                {"label": "添油，守着灯", "to": "calm_watch", "effects": {"vigilance": 2}, "flags_set": ["kept_light"]},
                {"label": "趴着眯一会儿", "to": "doze", "effects": {"vigilance": -1}},
            ],
        },
        "calm_watch": {
            "text": "灯稳稳亮着。我听见楼下有脚步声，湿重的脚步。",
            "choices": [
                {"label": "下去查看", "to": "stranger", "effects": {"courage": 1}},
                {"label": "隔着门问是谁", "to": "stranger_talk", "effects": {"trust": 1}},
                {"label": "不动，继续守灯", "to": "dawn", "effects": {"vigilance": 1}},
            ],
        },
        "doze": {
            "text": "我睡着了。灯灭了。惊醒时一片漆黑，海风灌进领口。",
            "choices": [
                {"label": "摸黑重新点灯", "to": "calm_watch", "effects": {"courage": 1}, "flags_set": ["relit"]},
                {"label": "蜷着等天亮", "to": "dawn_late", "effects": {"vigilance": -2}},
            ],
        },
        "stranger": {
            "text": "一个湿透的旅人抬眼：『风暴夜，我来借个宿。』",
            "choices": [
                {"label": "让他进来烤火", "to": "shared_warmth", "effects": {"trust": 2}, "flags_set": ["helped"]},
                {"label": "拒之门外", "to": "alone_watch", "effects": {"courage": 1, "trust": -1}},
            ],
        },
        "stranger_talk": {
            "text": "我隔着门说『天亮再进』。他道了谢，退到檐下躲风。",
            "choices": [
                {"label": "留门缝让他挡风", "to": "shared_warmth", "effects": {"trust": 1}, "flags_set": ["helped"]},
                {"label": "沉默守夜", "to": "dawn"},
            ],
        },
        "shared_warmth": {
            "text": "旅人烤干衣裳，讲了远方的海与城。灯下有两个人的影子。",
            "end": True,
        },
        "alone_watch": {
            "text": "我独自守到天明。灯没灭，但塔里只剩我自己的回音。",
            "end": True,
        },
        "dawn": {
            "text": "东方发白。灯可以歇了。这一夜，我守住了。",
            "end": True,
        },
        "dawn_late": {
            "text": "天亮了，我才把灯点着。这一夜，塔是盲的。",
            "end": True,
        },
    },
    "endings": {
        "shared_warmth": "风暴夜里我没让一个人冻死在外（信任 {trust}）。灯下两个影子，比一个暖。",
        "alone_watch": "我守住了灯，也守住了孤独（勇气 {courage}）。",
        "dawn": "灯亮到了天明（警觉 {vigilance}）。平凡，但完整。",
        "dawn_late": "我睡过了头，塔盲了一夜（警觉 {vigilance}）。有些错，天亮才看见。",
    },
})


SCENARIOS["midnight_store"] = Scenario({
    "id": "midnight_store",
    "title": "深夜便利店",
    "desc": "凌晨三点的便利店，冷柜深处有一扇不该存在的门。",
    "entry": "counter",
    "init_vars": {"coins": 3, "curiosity": 0, "weird": 0},
    "nodes": {
        "counter": {
            "text": "凌晨三点，我在便利店值夜班。风铃自己响了一声，没有人进来。",
            "choices": [
                {"label": "去整理货架", "to": "shelf"},
                {"label": "趴柜台眯一会儿", "to": "doze", "effects": {"weird": 1}},
                {"label": "走向冷柜区看看", "to": "coldroom", "effects": {"curiosity": 1}},
            ],
        },
        "shelf": {
            "text": "货架上那排泡面在轻轻转圈，像在跳广场舞。最前面一包朝我鞠了个躬。",
            "choices": [
                {"label": "小声问它是不是有话要说", "to": "noodles_talk",
                 "effects": {"curiosity": 1, "weird": 1}},
                {"label": "撕开一包，假装什么都没发生", "to": "noodle_open",
                 "effects": {"coins": -1}},
                {"label": "退回收银台", "to": "counter"},
            ],
        },
        "noodles_talk": {
            "text": "包装袋掀起一角，说：『加班的人我见多了。你要不要吃点别的？』它指了指冷柜。",
            "choices": [
                {"label": "顺着它指的方向去冷柜", "to": "coldroom",
                 "effects": {"curiosity": 1}, "flags_set": ["noodle_hint"]},
                {"label": "谢过它，继续值班", "to": "counter"},
            ],
        },
        "noodle_open": {
            "text": "撕开的瞬间热气窜出来，整间店都是我没买过的香味。柜台上多了一枚硬币。",
            "choices": [
                {"label": "收下硬币，去冷柜区", "to": "coldroom",
                 "effects": {"coins": 1, "curiosity": 1}},
                {"label": "把面放回去，老实值班", "to": "counter"},
            ],
        },
        "doze": {
            "text": "我梦到自己躺在货架上，身上贴着价签：『临期，五折』。惊醒时手心真捏着一张。",
            "choices": [
                {"label": "把价签贴到冷柜门上", "to": "coldroom",
                 "effects": {"weird": 2}, "flags_set": ["tag"]},
                {"label": "揉掉价签，拍拍脸醒神", "to": "counter"},
            ],
        },
        "coldroom": {
            "text": "冷柜最里面那扇门不是便利店该有的。门缝里漏出暖黄的光，还有一点酱油香。",
            "choices": [
                {"label": "推门进去", "to": "backstreet", "effects": {"curiosity": 1}},
                {"label": "先拿一盒关东煮壮胆", "to": "coldroom_snack",
                 "req": {"coins": ">=1"}, "effects": {"coins": -1}},
                {"label": "门把手上贴着我的价签——我认得这扇门", "to": "tagged_door",
                 "req": {"flags": ["tag"]}, "effects": {"weird": 1}},
            ],
        },
        "coldroom_snack": {
            "text": "萝卜、魔芋，还有一颗会眨眼的鱼丸。我把它们捧在手心，寒意退了一点。",
            "choices": [
                {"label": "捧着关东煮推门", "to": "backstreet",
                 "effects": {"curiosity": 1}, "flags_set": ["hot_food"]},
                {"label": "放回去，退回收银台", "to": "counter"},
            ],
        },
        "tagged_door": {
            "text": "价签上写的不是价格，是我的名字，和一句：『此人常走这条路』。门轻轻开了。",
            "choices": [
                {"label": "走进去", "to": "backstreet", "effects": {"curiosity": 2, "weird": 1}},
                {"label": "把价签撕下来留作纪念", "to": "backstreet",
                 "effects": {"coins": 2, "weird": 1}, "flags_set": ["keepsake"]},
            ],
        },
        "backstreet": {
            "text": "门后是一条会自己补货的街，摊主都是动物。路灯下有一家还亮着灯的拉面摊。",
            "choices": [
                {"label": "去拉面摊坐下", "to": "ramen", "effects": {"curiosity": 1}},
                {"label": "找个人问路", "to": "raccoon", "effects": {"curiosity": 1}},
                {"label": "悄悄退回冷柜", "to": "retreat"},
            ],
        },
        "raccoon": {
            "text": "一只围着围裙的浣熊在擦碗，看了我一眼：『新人？这条街不欢迎加班的人，只欢迎饿的人。』",
            "choices": [
                {"label": "那我来一碗面", "to": "ramen",
                 "effects": {"curiosity": 1}, "flags_set": ["raccoon"]},
                {"label": "我只是想找个出口", "to": "exit_talk", "effects": {"weird": 1}},
            ],
        },
        "exit_talk": {
            "text": "浣熊指了指我身后：『出口一直在你来的地方。你只是舍不得回去。』",
            "choices": [
                {"label": "坐下来吃碗面再走", "to": "ramen"},
                {"label": "转身走回冷柜", "to": "retreat"},
            ],
        },
        "ramen": {
            "text": "面端上来，汤面上浮着一行字：『你加了三次班，我加了三次汤。』热气糊了眼睛。",
            "choices": [
                {"label": "把汤喝光", "to": "dawn_ramen", "effects": {"curiosity": 1}},
                {"label": "问问这碗面的来历", "to": "ramen_story", "effects": {"curiosity": 1}},
                {"label": "往汤里加一勺自己的回忆", "to": "new_clerk",
                 "req": {"weird": ">=3"}, "effects": {"weird": 1}},
            ],
        },
        "ramen_story": {
            "text": "浣熊说这条街只在天亮前出现，来的人都是白天笑得最多的那种。『你就是。』",
            "choices": [
                {"label": "承认，然后把汤喝光", "to": "dawn_ramen", "effects": {"curiosity": 1}},
                {"label": "说我想留下来当店员", "to": "new_clerk", "effects": {"weird": 1}},
                {"label": "把面打包，回去上班", "to": "dawn_ramen"},
            ],
        },
        "retreat": {
            "text": "我从冷柜退回来，货架上的泡面已经排回原位，好像什么都没发生。",
            "choices": [
                {"label": "继续守到天亮", "to": "quiet_dawn"},
                {"label": "把这件事悄悄记在心里", "to": "tagged_me", "req": {"weird": ">=2"}},
                {"label": "还是想再去看看那扇门", "to": "backstreet"},
            ],
        },
        "dawn_ramen": {
            "text": "天蒙蒙亮，我回到收银台。柜台上放着一碗还冒热气的面，旁边一颗会眨眼的鱼丸。",
            "end": True,
        },
        "new_clerk": {
            "text": "我没回去。这条街的便利店缺一个夜班店员，我刚好合适。",
            "end": True,
        },
        "quiet_dawn": {
            "text": "什么都没发生。天亮了，我下班，风铃在身后响了一声。",
            "end": True,
        },
        "tagged_me": {
            "text": "我把那张价签收进了口袋。从那天起，我偶尔能在白天的橱窗里看见那条街。",
            "end": True,
        },
    },
    "endings": {
        "dawn_ramen": "我在天亮前吃了一碗不该存在的面（好奇 {curiosity}）。有些班，加得值。",
        "new_clerk": "我留在了那条只在凌晨出现的街上（异常 {weird}）。夜班从此不再难熬。",
        "quiet_dawn": "平安无事的一夜（好奇 {curiosity}）。风铃响的时候，我装作没听见。",
        "tagged_me": "我带着自己的价签回来了（异常 {weird}）。看得见的东西，多了一样。",
    },
})


SCENARIOS["pirate_cat"] = Scenario({
    "id": "pirate_cat",
    "title": "想当海盗的橘猫",
    "desc": "楼下的橘猫叼来一张藏宝图，图上画的是一只鲷鱼烧。",
    "entry": "alley",
    "init_vars": {"fish": 0, "trust": 0, "chaos": 0},
    "nodes": {
        "alley": {
            "text": "楼下那只橘猫又来了。这回它把一张揉皱的纸放在我脚边——上面画着一条鱼，和一个叉。",
            "choices": [
                {"label": "蹲下来仔细看那张图", "to": "map", "effects": {"trust": 1}},
                {"label": "先去买根火腿肠", "to": "snack", "effects": {"fish": 2}},
                {"label": "假装没看见，快步走开", "to": "ignore", "effects": {"chaos": 1}},
            ],
        },
        "map": {
            "text": "『宝藏』画得像一只鲷鱼烧，旁边一路猫爪印，通向屋顶。橘猫已经在楼梯口等我了。",
            "choices": [
                {"label": "跟着它上屋顶", "to": "roof", "effects": {"trust": 1}},
                {"label": "先去街角那家鲷鱼烧店问问", "to": "shop", "effects": {"fish": 1}},
            ],
        },
        "snack": {
            "text": "我买了两根火腿肠回来，橘猫还坐在原地，尾巴一下一下敲着地面。",
            "choices": [
                {"label": "分它一根", "to": "map", "effects": {"trust": 2, "fish": 1}},
                {"label": "自己吃掉两根，再听它说", "to": "map",
                 "effects": {"fish": 2, "trust": -1}},
            ],
        },
        "ignore": {
            "text": "我快步走开，回头时它还在原地。图上那条鱼被雨水泡糊了，只剩半个叉。",
            "choices": [
                {"label": "心软，走回去", "to": "alley", "effects": {"trust": -1}},
                {"label": "回家", "to": "home_early"},
            ],
        },
        "roof": {
            "text": "屋顶上蹲着七只猫，围成一个圈，像在开会。橘猫把我推到圈的中央。",
            "choices": [
                {"label": "听它们说什么", "to": "council", "effects": {"trust": 1}},
                {"label": "先撸一把最近的那只", "to": "council", "effects": {"trust": 2, "chaos": 1}},
            ],
        },
        "council": {
            "text": "猫议会。最老的一只开口：『人类，你从来不赶我们走，所以我们选你当领航员。』",
            "choices": [
                {"label": "发言：我知道宝藏在哪", "to": "depart",
                 "effects": {"trust": 1, "chaos": 1}},
                {"label": "保持沉默，先听它们说完", "to": "depart", "effects": {"trust": 2}},
                {"label": "问它们为什么找我", "to": "cat_reason", "effects": {"trust": 1}},
            ],
        },
        "cat_reason": {
            "text": "『因为下雨的时候，只有你把纸箱翻过来。』最老的那只猫说。会议安静了三秒。",
            "choices": [
                {"label": "那就出发吧", "to": "depart", "effects": {"trust": 2}},
                {"label": "我……再想想", "to": "home_early"},
            ],
        },
        "shop": {
            "text": "鲷鱼烧店的老板是位老太太。她看了图就笑：『那只猫啊，每天都来闻一闻，从不偷。』",
            "choices": [
                {"label": "买两个鲷鱼烧带上", "to": "depart", "effects": {"fish": 2}},
                {"label": "问老太太要不要一起去", "to": "own_shop", "effects": {"trust": 1}},
            ],
        },
        "depart": {
            "text": "出发。橘猫带队，七只猫跟着，我走在最后——一支不太像样的海盗团。",
            "choices": [
                {"label": "去码头（它们说海上也有鱼）", "to": "harbor", "effects": {"chaos": 1}},
                {"label": "去老城区的投喂点碰碰运气", "to": "feeding_spot", "effects": {"trust": 1}},
            ],
        },
        "harbor": {
            "text": "海风把图吹得哗哗响。橘猫盯上一条渔船，尾巴竖得像根桅杆。",
            "choices": [
                {"label": "跟船出海", "to": "sea", "effects": {"chaos": 2}},
                {"label": "劝它们回老城区", "to": "feeding_spot", "effects": {"trust": 1}},
            ],
        },
        "sea": {
            "text": "船开到一半，橘猫晕船了，七只猫全挤进我怀里。船长笑到扶着舵。",
            "choices": [
                {"label": "继续航行", "to": "sea_arrive"},
                {"label": "掉头回港", "to": "feeding_spot"},
            ],
        },
        "sea_arrive": {
            "text": "船靠上一座小岛。礁石缝里堆着别人留下的猫粮，和——两只完好的鲷鱼烧。",
            "choices": [
                {"label": "和它们把『宝藏』分了", "to": "treasure_share"},
                {"label": "宣布自己是船长", "to": "cat_king", "req": {"trust": ">=4"}},
                {"label": "还是回城吧", "to": "home_early"},
            ],
        },
        "feeding_spot": {
            "text": "投喂点在一棵老槐树下。橘猫跳上去，从树洞里叼出两只鲷鱼烧，像变了个魔术。",
            "choices": [
                {"label": "把鲷鱼烧分给大家", "to": "treasure_share", "effects": {"trust": 2}},
                {"label": "偷偷藏起一个", "to": "treasure_share",
                 "effects": {"fish": 2, "trust": -1}},
            ],
        },
        "treasure_share": {
            "text": "鲷鱼烧被分成八份，我拿到最大的一块。橘猫把爪子搭在我鞋上，算是入伙仪式。",
            "end": True,
        },
        "cat_king": {
            "text": "它们把我按在破纸箱做的『王座』上，宣布我成为『两脚兽名誉成员』，任期一辈子。",
            "end": True,
        },
        "home_early": {
            "text": "我回了家。第二天早上门口放着一只被咬过的鲷鱼烧——算账还是算礼物，我分不清。",
            "end": True,
        },
        "own_shop": {
            "text": "老太太请我周末看店，条件是给猫留一扇窗。店的名字改了：『猫的鲷鱼烧』。",
            "end": True,
        },
    },
    "endings": {
        "treasure_share": "宝藏是一只鲷鱼烧，八个人分（信任 {trust}）。当海盗，原来就图这个。",
        "cat_king": "我成了两脚兽名誉成员（信任 {trust}）。管理七只猫，比管理我自己容易。",
        "home_early": "我提前退出了这场冒险（混乱 {chaos}）。门口的鲷鱼烧，我留到了第二天。",
        "own_shop": "我有了个看店的周末（鱼 {fish}）。窗台上从此常年蹲着橘色的一团。",
    },
})


SCENARIOS["galaxy_delivery"] = Scenario({
    "id": "galaxy_delivery",
    "title": "银河快递：第 404 次派送",
    "desc": "包裹的地址栏写着『随便，反正你会送错』。燃料只够三次跃迁。",
    "entry": "depot",
    "init_vars": {"fuel": 3, "tips": 0, "chaos": 0},
    "nodes": {
        "depot": {
            "text": "调度员把包裹塞进我怀里，地址栏写着：『随便，反正你会送错。』燃料表显示：三格。",
            "choices": [
                {"label": "跃迁去糖果星球", "to": "candy",
                 "req": {"fuel": ">=1"}, "effects": {"fuel": -1}},
                {"label": "跃迁去沉默星球", "to": "silent",
                 "req": {"fuel": ">=1"}, "effects": {"fuel": -1}},
                {"label": "跃迁去倒着走的星球", "to": "backwards",
                 "req": {"fuel": ">=1"}, "effects": {"fuel": -1}},
                {"label": "就地拆开包裹", "to": "keep"},
                {"label": "直接退货", "to": "return"},
            ],
        },
        "candy": {
            "text": "糖果星球的地面是牛轧糖，每走一步都得把脚拔出来。甜味浓得像起雾。",
            "choices": [
                {"label": "按地址找收件人", "to": "candy_door", "effects": {"tips": 1}},
                {"label": "先尝一口路面", "to": "candy_eat", "effects": {"chaos": 1}},
                {"label": "燃料还够，再跃迁去沉默星球", "to": "silent",
                 "req": {"fuel": ">=1"}, "effects": {"fuel": -1}},
            ],
        },
        "candy_door": {
            "text": "收件地址是一间用巧克力砌的邮局。柜台后面探出三只触手，比了个『放这儿』。",
            "choices": [
                {"label": "把包裹递过去", "to": "deliver", "effects": {"tips": 1}},
                {"label": "先自己拆开看看", "to": "keep"},
                {"label": "觉得不对，退回去", "to": "return"},
            ],
        },
        "candy_eat": {
            "text": "路面是海盐味的。我蹲下来尝了第二口，才发现鞋底正在慢慢融化。",
            "choices": [
                {"label": "光脚也要完成派送", "to": "candy_door", "effects": {"chaos": 1}},
                {"label": "就地住下，鞋要紧", "to": "melt"},
            ],
        },
        "silent": {
            "text": "沉默星球上没人说话，所有人把话写在便签上贴给对方。风一吹，便签满天飞。",
            "choices": [
                {"label": "写一张便签问路", "to": "silent_note", "effects": {"tips": 1}},
                {"label": "大声喊一句『你好』", "to": "silent_shout", "effects": {"chaos": 2}},
                {"label": "再跃迁去倒着走的星球", "to": "backwards",
                 "req": {"fuel": ">=1"}, "effects": {"fuel": -1}},
            ],
        },
        "silent_note": {
            "text": "一个小孩扯了扯我的衣角，递来一张便签，上面写着三个字：『是我的。』",
            "choices": [
                {"label": "把包裹给他", "to": "deliver", "effects": {"tips": 1}},
                {"label": "摸摸他的头，自己留下包裹", "to": "keep"},
                {"label": "还是按流程退回", "to": "return"},
            ],
        },
        "silent_shout": {
            "text": "整条街的人都停下笔看我。有人递来一张便签，字很大：『请安静。』",
            "choices": [
                {"label": "写张便签道歉，再问路", "to": "silent_note"},
                {"label": "挥挥手，不送了", "to": "return"},
            ],
        },
        "backwards": {
            "text": "倒着走的星球。人们先倒着说再见，倒着吃饭。据说时间在这里是倒着流的。",
            "choices": [
                {"label": "试着倒着说话", "to": "backwards_talk",
                 "effects": {"chaos": 1, "tips": 1}},
                {"label": "跟着人流倒着走", "to": "backwards_walk", "effects": {"chaos": 1}},
            ],
        },
        "backwards_walk": {
            "text": "我倒着走了一路，居然迎面撞上刚出发时的自己——他手里也抱着一个包裹。",
            "choices": [
                {"label": "把包裹交给过去的自己", "to": "keep", "effects": {"chaos": 2}},
                {"label": "继续找收件人", "to": "backwards_talk"},
            ],
        },
        "backwards_talk": {
            "text": "我倒着说：『递投要需你』。对方居然听懂了，指了指前方一栋倒着盖的楼。",
            "choices": [
                {"label": "上楼派送", "to": "deliver", "effects": {"tips": 1}},
                {"label": "算了吧，退回去", "to": "return"},
            ],
        },
        "deliver": {
            "text": "收件人用触手在单子上盖了三个章，仪式感很强，还塞给我一点小费。",
            "end": True,
        },
        "keep": {
            "text": "我在太空停车场拆开了包裹。里面是一只更小的包裹，地址栏写着我的名字。",
            "end": True,
        },
        "return": {
            "text": "调度员看了一眼退回来的包裹，叹气：『第 404 次了。这单，本来就是发给你的。』",
            "end": True,
        },
        "melt": {
            "text": "我留在了糖果星球。不是不想走，是鞋化在了路上，只好就着牛轧糖住下。",
            "end": True,
        },
    },
    "endings": {
        "deliver": "我把它送到了（小费 {tips}）。盖三个章的仪式，我记了很久。",
        "keep": "我拆了包裹（混乱 {chaos}）。里面那只包裹，收件人是我自己。",
        "return": "我退了货（混乱 {chaos}）。原来这四百零四次，寄件人一直是我。",
        "melt": "我留在糖果星球了（混乱 {chaos}）。鞋化了，人倒是甜了。",
    },
})


def list_scenarios():
    return [{"id": s.id, "title": s.title, "desc": s.desc} for s in SCENARIOS.values()]


def get_scenario(sid):
    return SCENARIOS.get(sid)


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for sid in ("misty_forest", "lighthouse_night"):
        sc = get_scenario(sid)
        eng = AdventureEngine(sc)
        steps = 0
        while not eng.state.ended and steps < 30:
            ch = eng.auto_choose()
            if ch is None:
                break
            eng.choose(ch["label"])
            steps += 1
        path = " → ".join(h["choice"] for h in eng.state.history)
        print(f"=== {sc.title} ===")
        print("路径:", path)
        print("终局:", eng.state.node, "|", eng.reflection())
        print("状态:", eng.state.vars, "flags:", list(eng.state.flags))
        print()
    print("ALL SELFTEST OK")
