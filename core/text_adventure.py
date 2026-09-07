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
        "lighthouse": "我循着光找到了灯塔（勇气 {courage}，智慧 {wisdom}）。雾还在，但我知道自己没迷路。",
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
