"""
记忆分层（air-curiosity-01，金字塔版）：
  L1 自我叙事框架（金字塔 S/W/V + 两位序号，如 S01）/ L2 草稿本（D+序号，成熟了升 L1）
  / L3 例证（挂在某条 L1 下，L1 的 ID-三位序号）/ L5 冷存归档 / meta_log 元认知 / 决策日志。

各层上限取 config（L1 l1_cap / L2 l2_cap / L3 l3_cap）；满了写入被拦，提醒自己清理，不自动删。
L5/meta_log/决策日志不设上限（冷存只增、另两类满了滚 L5，不真删）。

- 删除 = 归档到 L5（不真删，素材不丢）。
- 条目字段（2026-09-05 命名治理 v2，单套字段名）：title（这条叫什么）+ parent（L3 挂在谁下面）
  + pyramid（L1/L2 三维大类，英文 key）+ link（L3 还连着谁，≤2）+ text（正文）。
  读取/写入走 title_of/parent_of/links_of/set_*；parse_tag 只用于解析旧格式输入与迁移。
- 印证次数挂 L1（evidence_count）：写 L3 时归属对上某条 L1，给那条 +1；「未分类」不印证。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Memory（本文件主体）—— 记忆分层管理与全部记忆操作，内部约 12 段：
#   段 1｜持久化 —— memory.json 读写 + 损坏时备份（解析失败不覆盖清零）
#   段 2｜撤销 —— 最近几步破坏性操作的快照回退（undo / undoable）
#   段 3｜每日快照 —— 每天第一笔落盘前留一份当天起点（留 7 天）
#   段 4｜记忆分类 —— 配额/三维下限常量 + 分类归一化与标签解析
#   段 5｜条目读写层 —— title/parent/links 四件套 + 命名治理 v2 迁移
#   段 6｜金字塔 ID —— S/W/V/D/X 号生成 + 旧 uuid 自动换算（id_remap）
#   段 7｜未分类兜底 —— 孤儿 L3（X-xxx）机制与 id 定位
#   段 8｜B 档补充工具 —— 标签治理 / 分支阅读 / 草稿升金字塔等高频操作
#   段 9｜归档流水 —— 人可读镜像（按标签归目录，只增不删）
#   段 10｜写入 / 删除 —— 各层写入 / 删除 / 合并 / 改写入口 + 内容新奇 /
#        印证 / 精炼记账与标签体检
#   段 11｜元认知记录 + 决策日志 —— meta_log 自省 / decisions（各自带容量滚动）
#   段 12｜读 —— relevant_l1 / search / trace（供注入 prompt 与回溯）
# =====================================================================
import json
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.embedding import EmbeddingService, cosine, embedding_novelty
from core.lock import atomic_write_json, read_text_retry

CST = timezone(timedelta(hours=8))


class Memory:
    def __init__(self, config):
        self.cfg = config
        self.file = config.data_root / "memory.json"
        # 语义向量服务：未配置/失败自动降级字符重叠（air 不会崩）
        self.emb = EmbeddingService(
            endpoint=getattr(config, "embedding_endpoint", ""),
            api_key=getattr(config, "embedding_api_key", ""),
            model=getattr(config, "embedding_model", "text-embedding-3-small"),
        )
        self.l1 = []          # 自我叙事框架（金字塔 S/W/V + 序号）
        self.l2 = []          # 草稿本（D + 序号，成熟了升 L1）
        self.l3 = []          # 例证（挂 L1 下，所属 L1 的 ID + 序号）
        self.l5 = []          # cold 冷存归档（删下来的，不丢）
        self.meta_log = []    # 元认知记录：自省（认知失调/卡点），按类型分组
        self.decisions = []   # 决策日志：选择记录（选了啥、为什么、信了什么），供提炼价值观
        self._corrupt = False  # 记忆文件解析失败标记：置真后 save() 拒绝落盘，防覆盖清零
        self._undo_stack = []  # 撤销栈（2026-09-02 加）：删错/改错能回退，只留最近几步
        self.id_remap = {}     # ID 重映射（2026-09-02 加）：旧 uuid → 新金字塔 ID，引用旧号自动纠正
        self._seq_watermark = {}  # ID 序号水位线：{前缀: 已分配过的最大序号}，只涨不回退，防号复用
        self._load()

    # ---- 持久化 ----

    def _load(self):
        if self.file.exists():
            try:
                # 读的瞬间可能正有人 replace 这个文件（Windows 上会 PermissionError），
                # 不重试就会被当成"文件坏了"→ 用空白开始，那等于她的记忆清零
                d = json.loads(read_text_retry(self.file))
                self.l1 = d.get("L1", [])
                self.l2 = d.get("L2", [])
                self.l3 = d.get("L3", [])
                self.l5 = d.get("L5", [])
                self.meta_log = d.get("meta_log", [])
                # 决策日志：兼容旧 key「membrane」（1.0 叫法）→ decisions
                self.decisions = d.get("decisions") or d.get("membrane") or []
                self.id_remap = d.get("id_remap") or {}   # 旧 ID → 新 ID 的重映射表
                self._seq_watermark = d.get("seq_watermark") or {}
                self._corrupt = False
            except Exception as e:
                # 只有「JSON 本身解析不了」才叫文件损坏。迁移/裁剪的异常不是——
                # 那只是数据里有一条脏，跟文件坏没坏无关。
                print(f"[记忆] 读取失败，用空白开始：{e}")
                self._corrupt = True
                self._backup_corrupt()
                return
            # 迁移与裁剪单独一段：它们的失败绝不能牵连上面的损坏判定。
            # 以前和 json.loads 共用一个 try，于是任何一条脏数据（某层混进非 dict 元素、
            # it.pop 就抛 AttributeError）都会走到 _backup_corrupt：原文件被改名搬走、
            # 进程内记忆全空、且 _corrupt 置真后本次运行再也不落盘——等于记忆清零。
            # 正确的反应是跳过迁移照常跑，并且把内存回滚到加载时的样子。
            before = deepcopy((self.l1, self.l2, self.l3, self.l5,
                               self.meta_log, self.decisions, self.id_remap))
            try:
                # 命名治理 v2 迁移（2026-09-05 方案 C 第 4 步）：存储字段切新命名。
                # 含旧 category 归一化与 L1 连边摘除；迁移前无条件备份，幂等可重跑。
                if self._migrate_naming_v2():
                    self.save()
                # 金字塔 ID 迁移（第 5 步）：uuid 旧号 → S01/V01-007/D07，旧号进 id_remap
                if self._migrate_ids():
                    self.save()
                # 决策日志超限裁剪（存量数据可能已超上限，加载时归档一次）
                if len(self.decisions) > self.DECISIONS_MAX:
                    self._trim_decisions()
                    self.save()
            except Exception as e:
                # 回滚到加载时的完整状态：迁移可能改了一半才炸（前几条已改名、第 N 条是脏的），
                # 留着半迁移状态继续跑，那几条会被读成空——等于凭空少了几条记忆。
                (self.l1, self.l2, self.l3, self.l5,
                 self.meta_log, self.decisions, self.id_remap) = before
                # 刻意不落盘：磁盘出问题时这里再 save 一次只会把异常抛出 _load、把她带崩。
                # 迁移是幂等的，下次启动会重跑——半迁移状态自愈得了。
                print(f"[记忆] 迁移/裁剪失败，已回滚到加载时的状态照常运行"
                      f"（原数据在，没丢；下次启动会重试迁移）：{e}")

    def _backup_corrupt(self):
        """解析失败时把损坏文件备份走，避免后续 save() 用空数据覆盖、记忆清零。

        备份后再跑在空列表上（降级不崩），但不再落盘覆盖原文件。
        """
        try:
            ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
            self.file.replace(self.file.with_name(f"memory.corrupt-{ts}.json"))
        except Exception:
            pass

    def save(self):
        # 走 core/lock.py 的原子写：临时文件带进程号+随机后缀。
        # 固定名 tmp 在双大脑并发时会互相踩，写坏的 JSON 下次被当成"记忆清零"。
        # 文件损坏时拒绝落盘：只在内存跑，别用空数据覆盖掉能抢救的原文件。
        if getattr(self, "_corrupt", False):
            return
        self._daily_snapshot()   # 第 7 步：每天第一笔落盘前拍快照（误操作有当天起点可回滚）
        atomic_write_json(self.file, {
            "L1": self.l1, "L2": self.l2, "L3": self.l3,
            "L5": self.l5, "meta_log": self.meta_log,
            "decisions": self.decisions,
            "id_remap": self.id_remap,
            "seq_watermark": self._seq_watermark,
        })

    # ---- 撤销（2026-09-02 加：删错 / 合并丢数据都能回退，素材不丢）----

    UNDO_MAX = 3   # 撤销栈深度：整层深拷贝含向量（L3 满约 5MB），3 步内存可控

    def _push_undo(self, label: str, layers: list) -> None:
        """压一层撤销快照：涉及层的深拷贝。

        L5 不进快照、撤销时也不碰它——理由见 undo() 的说明。
        """
        self._undo_stack.append({
            "label": label,
            "time": self._now(),
            "layers": {name: deepcopy(self._bucket(name)) for name in layers},
        })
        while len(self._undo_stack) > self.UNDO_MAX:
            self._undo_stack.pop(0)

    def _restore_layers(self, snapshots: dict, refined: dict | None = None) -> None:
        """把若干层还原成快照（merge 写失败回滚用）。

        snapshots：{层名: 快照}。合并 L1 时要连 L3 一起给——delete_many 会把被删 L1
        名下的 L3 置「未分类」并清掉指向它的 link，只还原 L1 的话那些 L3 就永久失去
        归属（delete_many 自己压快照时也是这个口径）。
        refined：快照时刻各 L1 的精炼次数。删除时给受影响 L1 的 refined +1 已经记账了，
        不还原就是「一次没成功的整理」凭空多一次计数。

        还原用的是 merge 开头存的整层快照，不是从 L5 往回捞，所以 L5 不用动。
        """
        for name, snapshot in snapshots.items():
            lst = self._bucket(name)
            lst.clear()
            lst.extend(snapshot)
        if refined:
            for it in self.l1:
                key = str(it.get("id"))
                if key in refined:
                    it["refined"] = refined[key]
        self.save()

    def undo(self) -> str:
        """撤销上一步破坏性操作（删除 / 合并 / 改写 / 改标签）。返回描述文本。

        两点要说清楚：

        1. 是整层覆盖式回滚：弹出来的快照之后发生的写入（别的记忆操作、印证 +1）
           都会被一并抹掉。这是「上一步」的字面语义，也是工具层给她的承诺。
        2. **只还原记忆层，不碰 L5 冷存**（2026-09-06 改）。以前撤销会顺手删掉本次
           归档进 L5 的条目，做法是 del self.l5[l5_len:] 按长度一刀切——而快照之后
           追加进来的未必都是这次操作的归档（改写留痕、meta_log 和 decisions 超限
           滚动都会往 L5 追加），一刀切就把那些无关的历史留痕连带删掉了。
           L5 是唯一「只增、不真删」的保底层，为撤销得干净去删它不值当：
           多留一份副本在冷存里没有害处，删错了才是真丢东西。
        """
        if not self._undo_stack:
            return "（没有可撤销的操作）"
        snap = self._undo_stack.pop()
        for name, lst in snap["layers"].items():
            bucket = self._bucket(name)
            bucket.clear()
            bucket.extend(lst)
        self.save()
        return f"已撤销：{snap['label']}（{snap['time']}）"

    def undoable(self) -> str:
        """可撤销的操作列表（给她看最近几步能撤什么）。空则返回空串。"""
        if not self._undo_stack:
            return ""
        return "；".join(f"{s['time']} {s['label']}" for s in reversed(self._undo_stack))

    # ---- 每日快照（2026-09-02 第 7 步）----

    SNAPSHOT_KEEP = 7   # 快照只留最近 7 天，别把磁盘吃满

    def _daily_snapshot(self):
        """每天第一笔落盘前拍一份快照（memory-YYYYMMDD.json）。

        原来只有文件损坏才备份——她误删/误改后当天没有起点可回滚。现在有。
        只留最近 SNAPSHOT_KEEP 天。失败静默：快照是保险，不能挡住正常落盘。"""
        try:
            today = datetime.now(CST).strftime("%Y%m%d")
            snap = self.file.with_name(f"memory-{today}.json")
            if not snap.exists() and self.file.exists():
                shutil.copyfile(self.file, snap)
            snaps = sorted(self.file.parent.glob("memory-????????.json"))
            for p in snaps[: -self.SNAPSHOT_KEEP]:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    def _caps(self) -> dict:
        return {"L1": self.cfg.l1_cap, "L2": self.cfg.l2_cap, "L3": self.cfg.l3_cap}

    # 每条字数上限（按层收紧，防长文撑爆容量与落盘体积；L3 经历 1-2 句话足够）
    LIMITS = {"L1": 200, "L2": 300, "L3": 300}

    # 超限拒绝时给她的重写引导（2026-09-06）：模型对数不清字数，报「超多少字」她只能
    # 数着删——给可执行的语义指令（去冗余、留骨架），数字在文案里只作背景参考。
    LEN_HINTS = {
        "L1": "只留「条件→…；结论→…」的骨架命题，例证细节和修饰语删掉，一两句说透",
        "L2": "把想法浓缩成几句要点，删掉铺陈和反复的表述",
        "L3": "讲清它印证了什么道理 + 最关键的细节就够，删掉背景介绍和重复的话",
    }

    # ---- 记忆分类（2026-08-24，参考 air1.0 收敛）：每层少量类别，LLM 填 + 系统归一化兜底 ----
    # L1/L2 分类（2026-09-01 创建者定）：每条记忆带两个标签——主标签（自定）＋括号内分类标签。
    # L1 分类＝三维（自我认知/世界认知/价值观）；L2 同 L1（问题/未决也按三维分，不设独立分类）。
    # L3 内容类型（经验/知识/对话/反思）已退役（2026-09-01 创建者定）：L3 的分类标签 =
    # 它归属的 L1 主标签（L3 是 L1 的细分投射），只靠 tag 方向归类。
    # 空元组让 normalize_category 对 L3 返回空 → 写入时不存 category 字段。
    CATEGORIES = {
        "L1": ("self_cognition", "world_cognition", "value"),
        "L2": ("self_cognition", "world_cognition", "value"),
        "L3": (),
    }
    DEFAULT_CATEGORY = {"L1": "self_cognition", "L2": "self_cognition"}
    # 非价值观配额上限（非 value 类最多这么多条，倒逼预留坑位给 value）：
    # L2 20 条里非价值最多 19 → 至少 1 条 value
    NON_VALUE_CAP = {"L2": 19}
    # L1 三维各自的下限（2026-08-31 创建者定）：至少要有这么多条，是目标不是天花板。
    # 实现沿用 NON_VALUE_CAP 的路子——限制「补集」：任一维 c 之外的条数不能超过
    # 总数上限 − c 的下限，这样 c 想补到下限的时候一定还有位置。
    # 补不补由她定，系统只保证位置不被占光（守容量，不替她决定内容）。
    L1_CAT_FLOOR = {"value": 10, "world_cognition": 10, "self_cognition": 5}
    # 大类中文名（提示语用；normalize 方向见 _CAT_ALIAS）
    CAT_CN = {"self_cognition": "自我认知", "world_cognition": "世界认知", "value": "价值观"}
    DECISIONS_MAX = 50   # 决策日志最多保留条数（多了最旧的滚进 L5 冷存，不真删）
    _CAT_ALIAS = {   # 中文/常见写法 → 规范值（LLM 输出不可信，统一收敛；只收敛三维大类）
        "自我认知": "self_cognition", "self": "self_cognition",
        "世界认知": "world_cognition", "world": "world_cognition",
        "价值观": "value", "价值": "value", "value": "value",
    }

    @classmethod
    def normalize_category(cls, layer: str, category: str) -> str:
        """分类归一化：转小写 → 中文别名映射 → 不在合法集合归默认。"""
        layer = (layer or "").upper()
        cat = (category or "").strip().lower()
        cat = cls._CAT_ALIAS.get(cat, cat)
        if cat in cls.CATEGORIES.get(layer, ()):
            return cat
        return cls.DEFAULT_CATEGORY.get(layer, "")

    @staticmethod
    def parse_tag(tag: str) -> tuple:
        """解析 tag：'小标签（方向/大类）' → (小标签, 括号内)。
        无括号 → (tag, '')；空 → ('', '')。
        L1/L2 的括号 = 三维分类（「边界感（价值观）」）；L3 的括号 = 它归属的 L1 主标签
        （「尊重他人（边界感）」= 方向）。括号里的就是归属。"""
        tag = (tag or "").strip()
        if not tag:
            return "", ""
        if "（" in tag and tag.endswith("）"):
            head, _, inner = tag.rpartition("（")
            return head.strip(), inner[:-1].strip()
        return tag, ""

    # ---- 记忆条目读取/写入层（2026-09-05 命名治理 v2：单套字段名）----
    # 条目字段：title（它叫什么）、parent（L3 挂在谁下面）、pyramid（L1/L2 三维大类，英文 key）、
    # link（L3 还连着谁，≤2）、text（正文）。三层语义一眼可辨，见《记忆系统_命名治理方案》4.2。
    # parse_tag 只用于解析旧格式输入/历史数据（L5 兼容、迁移、她手输「名（归属）」），新代码禁用。

    LINK_MAX = 2   # 连边上限（创建者 2026-09-02 定：2 个足够）

    @staticmethod
    def title_of(it: dict) -> str:
        """标题：这条记忆叫什么。"""
        return ((it or {}).get("title") or "").strip()

    @staticmethod
    def parent_of(it: dict) -> str:
        """归属（只 L3 用）：它挂在哪条 L1 主标签下。L1/L2 的三维大类读 pyramid。"""
        return ((it or {}).get("parent") or "").strip()

    @classmethod
    def links_of(cls, it: dict) -> list:
        """连边（只 L3 用）：还连着的别的 L1 主标签名（0~2 个）。"""
        v = (it or {}).get("link") or []
        return [str(x).strip() for x in v if str(x).strip()][: cls.LINK_MAX]

    @staticmethod
    def set_title(it: dict, title: str) -> None:
        """设标题。"""
        it = it if it is not None else {}
        it["title"] = (title or "").strip()

    @staticmethod
    def set_parent(it: dict, parent: str) -> None:
        """设归属（只 L3 用；L1/L2 的三维大类直接写 it["pyramid"]）。"""
        it = it if it is not None else {}
        it["parent"] = (parent or "").strip()

    @classmethod
    def set_links(cls, it: dict, links: list) -> None:
        """设连边，去重、去空、截断到上限。"""
        it = it if it is not None else {}
        seen, out = set(), []
        for x in (links or []):
            s = str(x).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        it["link"] = out[: cls.LINK_MAX]

    def _migrate_naming_v2(self) -> bool:
        """命名治理 v2 迁移（2026-09-05 方案 C 第 4 步）：存储字段切到新命名，双轨拼接串退役。

        L1/L2: content→text；tag 拆出纯标题→title；category→pyramid；删 cat_tag；删 cross
               （L1 的 cross 是假能力——能写能显示但没有任何消费方，见方案第五节；值进 L5 留痕）
        L3:    content→text；tag 拆出纯标题→title；cat_tag→parent；cross→link
        L5:    content→text——留痕留的是内容（她当时写了什么），不是系统的字段名（方案 6.2 选项 A）
        meta_log / decisions：content→text，同库不留两套字段名
        幂等：迁移后条目没有旧字段，重跑直接跳过。迁移前无条件备份（回滚 = 换回备份）。
        """
        # 幂等检测分两拨，不能一锅烩：
        # - 记忆四层（L1/L2/L3/L5）：旧字段是 content/tag/cat_tag/cross/category，全查；
        # - meta_log / decisions：只认 content。decisions 的 tag 是「这个选择是关于什么的」
        #   （学习/表达/行动/关系/自我）有效字段，跟记忆的 tag（主标签拼接串）重名但不是
        #   一个东西——把它当旧字段判，has_old 就永远为真，每次启动都重跑一遍迁移、
        #   多打一份备份文件（2026-09-05 实测踩到）。
        old_keys = ("content", "tag", "cat_tag", "cross", "category")
        has_old = (any(k in it for lst in (self.l1, self.l2, self.l3, self.l5)
                       for it in lst for k in old_keys)
                   or any("content" in it
                          for it in (self.meta_log or []) + (self.decisions or [])))
        if not has_old:
            return False
        # 迁移前无条件备份（回滚 = 把备份文件换回来）
        try:
            ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.file, self.file.with_name(f"memory.prenaming-{ts}.json"))
            print(f"[记忆] 命名迁移前已备份 → memory.prenaming-{ts}.json")
        except Exception as e:
            print(f"[记忆] 命名迁移备份失败（继续迁移，原文件未动）：{e}")
        changed = False
        for name, lst in (("L1", self.l1), ("L2", self.l2), ("L3", self.l3)):
            for it in lst:
                if "content" in it:
                    it["text"] = it.pop("content")
                    changed = True
                if "tag" in it:
                    head, _cat = self.parse_tag(it.pop("tag"))
                    it["title"] = head.strip()
                    changed = True
                if name == "L3":
                    # L3 内容类型已退役（2026-09-01）：历史 category 字段直接摘掉
                    if "category" in it:
                        it.pop("category")
                        changed = True
                    if "cat_tag" in it:
                        it["parent"] = it.pop("cat_tag")
                        changed = True
                    if "cross" in it:
                        it["link"] = it.pop("cross")
                        changed = True
                    continue
                # L1/L2：cat_tag 与 category 本是双份存储（方案 3.2 的病根），合并成 pyramid
                if "cat_tag" in it:
                    it.pop("cat_tag")
                    changed = True
                cat = it.pop("category", None)
                if cat is not None:
                    it["pyramid"] = cat
                    changed = True
                if not it.get("pyramid") or it["pyramid"] not in self.CATEGORIES[name]:
                    it["pyramid"] = self.DEFAULT_CATEGORY[name]
                    changed = True
                if "cross" in it:
                    cross_v = it.pop("cross")
                    if cross_v:
                        # L1 连边砍掉（方案第五节）：值进 L5 留痕，她知情、素材不丢
                        self.l5.append({
                            "id": uuid.uuid4().hex[:8],
                            "type": "archived",
                            "text": (f"L1「{it.get('title', '')}」的连边已移除（L1 不再支持连边）："
                                     + "、".join(str(x) for x in cross_v)),
                            "from_layer": name,
                            "reason": "naming_v2_l1_links",
                            "archived_at": self._now(),
                        })
                    changed = True
        for it in self.l5:
            if "content" in it:
                it["text"] = it.pop("content")
                changed = True
        # meta_log / decisions 不在记忆三层里，但同库同文件——留着 content 就是两套字段名并存。
        # 这不是洁癖：后人不看注释，照着 L1/L2/L3 的写法顺手把这里的读取也改成 text，
        # 100 条自省和 50 条决策日志会全部读成空。一起切，代码里只剩一套名字。
        for it in (self.meta_log or []) + (self.decisions or []):
            if "content" in it:
                it["text"] = it.pop("content")
                changed = True
        if changed:
            n = sum(len(x) for x in (self.l1, self.l2, self.l3))
            print(f"[记忆] 命名迁移完成：L1/L2/L3 共 {n} 条 + L5 {len(self.l5)} 条 "
                  f"+ meta_log {len(self.meta_log)} 条 + decisions {len(self.decisions)} 条"
                  " 切到新字段名（title/parent/pyramid/link/text）")
        return changed

    # ---- 金字塔 ID 规则（2026-09-02 第 5 步）----
    # L1: 大类字母+两位序号（S01/W01/V01）；L3: 所属L1的ID-三位序号（V01-007）；
    # L2: D+两位序号（D07，草稿本）。S=自我认知 W=世界认知 V=价值观——
    # 三维永不改名，所以前缀永不过时。X = 未归类（L3 还没挂上任何 L1 时的兜底）。
    # 旧 uuid 8 位号全部进 id_remap，引用旧号自动换算，不断链。
    CAT_LETTER = {"self_cognition": "S", "world_cognition": "W", "value": "V"}

    @staticmethod
    def _is_old_id(i) -> bool:
        """旧 ID = 8 位 uuid 截断；新 ID 以 S/W/V/D 开头或带 '-'（V01-007）。"""
        s = str(i or "")
        return bool(s) and not s.startswith(("S", "W", "V", "D")) and "-" not in s

    def pyramid_of(self, layer: str, it: dict) -> str:
        """条目所属金字塔（三维大类）：L1/L2 看自己的 pyramid；L3 看它归属的 L1 的大类。"""
        layer = (layer or "").upper()
        if layer in ("L1", "L2"):
            return (it.get("pyramid") or "").strip()
        parent = self.parent_of(it)
        for l1 in self.l1:
            if self.title_of(l1) == parent:
                return (l1.get("pyramid") or "").strip()
        return ""

    def _next_seq(self, pool: list, key: str) -> int:
        """某前缀池里的下一个序号：max(现有 ID, 历史水位线) + 1。

        水位线才是「已分配的永不复用」这句话真正落地的地方。只扫现有 ID 的话，
        删掉最大号之后再写入就会把那个号重新发出去——而旧号可能还挂在 id_remap 里、
        还留在 L5 的归档记录上，那时候「改/删旧号」会命中一条内容完全无关的记忆。
        水位线只涨不回退：发出去的号，删了也不再给别人。

        key = 前缀（"S" / "D" / "V01" / "V-"），各前缀各记一条水位线。
        """
        mx = int(self._seq_watermark.get(key) or 0)
        for s in pool:
            try:
                mx = max(mx, int(s))
            except (TypeError, ValueError):
                continue
        nxt = mx + 1
        if key:
            self._seq_watermark[key] = nxt
        return nxt

    def _new_id(self, layer: str, it: dict) -> str:
        """按金字塔规则生成 ID。write / promote / 迁移共用这一把尺子。"""
        layer = (layer or "").upper()
        if layer == "L2":
            pool = [str(x.get("id", ""))[1:] for x in self.l2
                    if str(x.get("id", "")).startswith("D")]
            return f"D{self._next_seq(pool, 'D'):02d}"
        cat = self.pyramid_of(layer, it)
        letter = self.CAT_LETTER.get(cat, "X")   # X = 未归类
        if layer == "L1":
            pool = [str(x.get("id", ""))[1:] for x in self.l1
                    if str(x.get("id", "")).startswith(letter)]
            return f"{letter}{self._next_seq(pool, letter):02d}"
        # L3：优先用所属 L1 的新 ID 做前缀（V01-007）；没挂上就用大类字母兜底（V-007）
        cat_tag = self.parent_of(it)
        for l1 in self.l1:
            if self.title_of(l1) == cat_tag:
                pid = str(l1.get("id", ""))
                if pid and (pid[0] in "SWVX" or "-" in pid):
                    pool = [str(x.get("id", "")).split("-")[-1] for x in self.l3
                            if str(x.get("id", "")).startswith(pid + "-")]
                    return f"{pid}-{self._next_seq(pool, pid):03d}"
                break
        pool = [str(x.get("id", "")).split("-")[-1] for x in self.l3
                if str(x.get("id", "")).split("-")[0].lstrip("SWVX") == ""
                and str(x.get("id", "")).startswith(letter)]
        return f"{letter}-{self._next_seq(pool, letter + '-'):03d}"

    def _migrate_ids(self) -> bool:
        """把 uuid 旧 ID 换成金字塔 ID，旧号记进 id_remap（引用旧号自动换算）。

        顺序很重要：先 L1（定分支前缀），再 L3（用新 L1 ID 做前缀），最后 L2。
        L5 归档**保留原 ID 不改**（留痕，保留"它当时叫什么"），但 remap 里查得到。"""
        changed = False
        for it in self.l1:
            if self._is_old_id(it.get("id")):
                old = str(it["id"])
                it["id"] = self._new_id("L1", it)
                self.id_remap[old] = it["id"]
                changed = True
        for it in self.l3:
            if self._is_old_id(it.get("id")):
                old = str(it["id"])
                it["id"] = self._new_id("L3", it)
                self.id_remap[old] = it["id"]
                changed = True
        for it in self.l2:
            if self._is_old_id(it.get("id")):
                old = str(it["id"])
                it["id"] = self._new_id("L2", it)
                self.id_remap[old] = it["id"]
                changed = True
        return changed

    def _resolve_id(self, item_id: str):
        """按 id 找记忆（L1/L2/L3/L5）。旧 ID 自动经 id_remap 换算成新号再找。

        返回 (层名, 条目)；找不到返回 (None, None)。全库统一入口，别再内联循环。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return None, None
        layers = (("L1", self.l1), ("L2", self.l2), ("L3", self.l3), ("L5", self.l5))
        for name, lst in layers:
            for it in lst:
                if str(it.get("id")) == item_id:
                    return name, it
        new = self.id_remap.get(item_id)
        if new:
            for name, lst in layers:
                for it in lst:
                    if str(it.get("id")) == new:
                        return name, it
        return None, None

    # ---- 未分类兜底（2026-09-02 加）----
    # L3 没有合适归属时显式填「未分类」，好过空着让印证静默失败——
    # 填了她以后能定期回来整理，而不是丢在黑洞里。
    UNCATEGORIZED = "未分类"

    def l1_titles(self) -> list:
        """当前所有 L1 主标签名（L3 的分类标签 / 交叉标签只能从这里选）。"""
        seen, out = set(), []
        for it in self.l1:
            h = self.title_of(it)
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out

    def validate_tag(self, layer: str, title: str = "",
                     parent: str = "", link: list | None = None) -> str:
        """写入前校验标签。返回错误描述（空串 = 通过）。

        原则：报错必须带**正确答案**和当前可选清单——她填错往往是因为看不见候选项，
        只丢一句「填错了」没有任何用处。
        parent 参数双义按层：L3 填归属的 L1 主标签名，L1/L2 填三维大类（pyramid）。
        """
        layer = (layer or "").upper()
        head = (title or "").strip()
        cat = (parent or "").strip()

        if head:
            if len(head) > 20:
                return (f"主标签「{head[:20]}…」太长（{len(head)} 字，上限 20）："
                        f"标签名要短，别把一整句话当标签名")
            if self._looks_like_mem_id(head):
                return f"主标签「{head}」看着像记忆 id：要填自然语言的小标签名"

        if layer in ("L1", "L2"):
            if cat:
                norm = self._CAT_ALIAS.get(cat.lower(), cat)
                if norm not in self.CATEGORIES.get(layer, ()):
                    cn = " / ".join(self.CAT_CN.get(c, c) for c in self.CATEGORIES.get(layer, ()))
                    return f"分类标签「{cat}」不是合法大类。{layer} 只能用：{cn}"
            if layer in ("L1", "L2") and link:
                # L1 连边砍掉（2026-09-05 方案第五节：能写能显示但没有任何消费方的假能力）；
                # L2 草稿本本来就不给连边。只有 L3 能连。
                return (f"{layer} 不支持连边（只有 L3 能连别的分支）。"
                        "想表达两条框架相关，写一条具体的 L3 例证挂过去——那条路才有印证和账")
            return ""

        if layer == "L3":
            heads = self.l1_titles()
            cand = "、".join(heads[:15]) + ("…" if len(heads) > 15 else "")
            norm = self._CAT_ALIAS.get(cat.lower(), cat)
            if cat and cat != self.UNCATEGORIZED:
                # 她最常踩的坑：把三维大类（金字塔名）当成 L1 主标签填进来
                if norm in self.CATEGORIES.get("L1", ()):
                    return (f"分类标签「{cat}」是三维大类（金字塔名），不是 L1 主标签名。"
                            f"L3 要填它归属的那条 L1 主标签，现在有：{cand}")
                if cat not in heads:
                    return (f"分类标签「{cat}」对不上任何 L1 主标签。现在有：{cand}；"
                            f"没有合适的就填「{self.UNCATEGORIZED}」")
            for x in (link or [])[: self.LINK_MAX]:
                if x not in heads:
                    # 2026-09-03：报错讲清 link 语义（只能连已有 L1 主标签）并给替代路径；
                    # 措辞统一叫「主标签」——「方向」另指谱/驱动概念，别两义混用。
                    return (f"连边只能连「已存在」的 L1 主标签；「{x}」还不是任何一条 L1 主标签，"
                            f"连不上。想让这条 L3 归属某条 L1：把那条 L1 的主标签名填进 parent"
                            f"（候选：{cand}；都不合适就填「{self.UNCATEGORIZED}」）。")
            return ""

        return ""

    def rename_tag_force(self, old_full: str, new_tag: str, dry_run: bool = False) -> str:
        """强制重命名一条 L1 的主标签：按**完整 tag 写法**匹配（专治整句 / 超长标签）。

        为什么需要它：retag 要求精确匹配主标签，而那条被误操作命名成长句子的 L1，
        她复制不全就永远改不动，只能干瞪眼。这里按完整字符串比对，命中后全库同步。

        同步范围（创建者 2026-09-02 定：改 L1 主标签必须全库替换）：
        - 所有 L3 的分类标签（cat_tag）
        - 所有 L1 / L3 的交叉标签（cross）
        - L5 归档不动（留痕，保留"它当时叫什么"的真相）
        """
        old_full = (old_full or "").strip()
        new_tag = (new_tag or "").strip()
        if not old_full or not new_tag:
            if not old_full and not new_tag:
                raise ValueError("rename_tag 两个参数都空：old_tag 填那条 L1 的完整 tag 写法（含括号，"
                                 "用 list_tags 复制准确写法），new_tag 填新主标签名")
            if not old_full:
                raise ValueError("old_tag 没填：填那条 L1 的完整 tag 写法（含括号——它按整串精确匹配，"
                                 "list_taglib 只列名字不带括号会匹配不上，用 list_tags 看准确写法）")
            raise ValueError("new_tag 没填：填改后的新主标签名（只填名字部分即可）")
        new_head = self.parse_tag(new_tag)[0]
        if not new_head:
            raise ValueError("新标签是空的")
        for it in self.l1:
            if self.title_of(it) == new_head:
                raise ValueError(f"已经有 L1 主标签叫「{new_head}」了，不能重名（会并成同一个分支）")
        target = None
        for it in self.l1:
            if self.title_of(it) == self.parse_tag(old_full)[0]:
                target = it
                break
        if target is None:
            raise ValueError(f"没找到主标签写法为「{old_full}」的 L1（先用 list_taglib 看准确写法）")

        old_head = self.title_of(target)
        if dry_run:
            n_cat = sum(1 for x in self.l3 if self.parent_of(x) == old_head)
            n_cross = sum(1 for it2 in self.l3 if old_head in self.links_of(it2))
            return (f"[试运行] 「{old_head}」→「{new_head}」：L1 主标签 1 条、"
                    f"L3 归属 {n_cat} 条、连边 {n_cross} 条。确认请带 dry_run=False")
        self._push_undo(f"强制改名 {old_head} -> {new_head}", ["L1", "L3"])
        self.set_title(target, new_head)
        ch_cat = 0
        for it in self.l3:
            if self.parent_of(it) == old_head:
                self.set_parent(it, new_head)
                ch_cat += 1
        ch_cross = 0
        for it in self.l3:
            cs = self.links_of(it)
            if old_head in cs:
                self.set_links(it, [new_head if x == old_head else x for x in cs])
                ch_cross += 1
        self.save()
        return (f"已把 L1 主标签「{old_head}」改名为「{new_head}」："
                f"{ch_cat} 条 L3 归属、{ch_cross} 条连边同步更新")

    def retarget(self, old_cat: str, new_l1_head: str, dry_run: bool = True) -> str:
        """把分类标签为 old_cat 的一批 L3，重挂到 L1 主标签 new_l1_head。

        默认 dry_run=True（只报打算改哪些、涉及哪些 id），确认后再带 dry_run=False 执行。
        干不成（目标不存在 / 没有匹配的 L3 / 参数空）抛 ValueError——返回文本会被当成
        成功，_settle 照常记账、连败检测永远不触发（2026-09-03 修）。
        """
        old_cat = (old_cat or "").strip()
        new_head = self.parse_tag(new_l1_head or "")[0]
        if not old_cat or not new_head:
            if not old_cat and not new_head:
                raise ValueError("retarget 两个参数都空：old_tag 填现在挂错的分类标签名，"
                                 f"new_tag 填正确的 L1 主标签名（现有 L1：{'、'.join(self.l1_titles()) or '无'}）")
            if not old_cat:
                raise ValueError("old_tag 没填：填现在那条 L3 挂着的错误分类标签名（用 list_tags 看谁挂着它）")
            raise ValueError(f"new_tag 没填：填正确的 L1 主标签名（现有 L1：{'、'.join(self.l1_titles()) or '无'}）")
        heads = self.l1_titles()
        if new_head not in heads:
            raise ValueError(f"「{new_head}」不是现有 L1 主标签。当前有：{'、'.join(heads)}")
        hits = [it for it in self.l3 if self.parent_of(it) == old_cat]
        if not hits:
            raise ValueError(f"没有分类标签为「{old_cat}」的 L3（可能已经清理过了）")
        ids = "、".join(str(it.get("id")) for it in hits[:10]) + ("…" if len(hits) > 10 else "")
        if dry_run:
            return (f"[试运行] 将把 {len(hits)} 条 L3 从「{old_cat}」重挂到「{new_head}」，"
                    f"涉及 id：{ids}。确认执行请带 dry_run=False")
        self._push_undo(f"重挂 {len(hits)} 条 L3：{old_cat} -> {new_head}", ["L3"])
        for it in hits:
            self.set_parent(it, new_head)
        self.save()
        return f"已把 {len(hits)} 条 L3 从「{old_cat}」重挂到「{new_head}」（id：{ids}）"

    def list_taglib(self, layer: str) -> str:
        """标签库（写哪层看哪层）：列出该层已有主标签 + 该层能选的分类标签。

        她填错标签的根因是**看不见候选项**，只能凭记忆填。这里把可选项摆出来给她选。
        """
        layer = (layer or "").strip().upper()
        if layer not in ("L1", "L2", "L3"):
            return "list_taglib 的 layer 要填 L1 / L2 / L3（写哪层看哪层）"

        if layer in ("L1", "L2"):
            cats = " / ".join(self.CAT_CN.get(c, c) for c in self.CATEGORIES.get(layer, ()))
            heads = sorted({self.title_of(it) for it in self._bucket(layer) if self.title_of(it)})
            lines = [f"【{layer} 标签库】",
                     f"分类标签（固定三选一）：{cats}",
                     f"已有主标签（{len(heads)} 个，可以选也可以新建）："]
            lines += [f"  - {h}" for h in heads] if heads else ["  （还没有）"]
            return "\n".join(lines)

        heads = self.l1_titles()
        tags = sorted({self.title_of(it) for it in self.l3 if self.title_of(it)})
        lines = ["【L3 标签库】",
                 f"分类标签 = 它归属的 L1 主标签名（{len(heads)} 个，只能从里面选）："]
        lines += [f"  - {h}" for h in heads] if heads else ["  （还没有 L1）"]
        lines.append(f"  没有合适的就填「{self.UNCATEGORIZED}」，以后可以改")
        lines.append("")
        lines.append(f"交叉标签（选填，最多 {self.LINK_MAX} 个）：同样从上面 L1 主标签里选，"
                     f"用来连到别的分支——连到另一座金字塔的信号更强")
        lines.append("")
        lines.append(f"已有 L3 主标签（{len(tags)} 个，可以选也可以新建）：")
        lines += [f"  - {t}" for t in tags[:40]] if tags else ["  （还没有）"]
        if len(tags) > 40:
            lines.append(f"  …还有 {len(tags) - 40} 个")
        return "\n".join(lines)

    def read_branch(self, branch: str) -> str:
        """一次读一个分支：L1 框架 + 它的全部 L3 例证 + 交叉过来的 + 相关草稿。

        金字塔结构最该提供的能力——现在扁平列表给不了，她只能一条条搜。
        """
        branch = (branch or "").strip()
        if not branch:
            return "read_branch 要填分支名（L1 主标签名，如「边界感」）"
        l1 = None
        for it in self.l1:
            if self.title_of(it) == branch:
                l1 = it
                break
        if l1 is None:
            return (f"没有叫「{branch}」的 L1 分支。当前分支："
                    f"{'、'.join(self.l1_titles()) or '（还没有）'}")

        lines = [f"【{branch}】{(l1.get('text') or '')[:120]}",
                 f"（印证 {l1.get('evidence_count', 0)} 次 · 精炼 {l1.get('refined', 0)} 次）"]

        examples = [it for it in self.l3 if self.parent_of(it) == branch]
        lines.append(f"\n例证 L3（{len(examples)} 条）：")
        if examples:
            lines += [f"  [{it.get('id')}] {(it.get('text') or '')[:60]}" for it in examples[:30]]
            if len(examples) > 30:
                lines.append(f"  …共 {len(examples)} 条，用 search_memory 细找")
        else:
            lines.append("  （这个分支还没有例证）")

        cross_in = [it for it in self.l3
                    if branch in self.links_of(it) and self.parent_of(it) != branch]
        if cross_in:
            lines.append(f"\n交叉关联（{len(cross_in)} 条连到这个分支）：")
            lines += [f"  [{it.get('id')}]（来自「{self.parent_of(it) or '未分类'}」）"
                      f"{(it.get('text') or '')[:50]}" for it in cross_in[:20]]

        drafts = self._drafts_for(branch, l1.get("text") or "")
        if drafts:
            lines.append(f"\n相关草稿 L2（{len(drafts)} 条）：")
            lines += [f"  [{it.get('id')}] {(it.get('text') or '')[:50]}" for it in drafts]

        return "\n".join(lines)

    def _drafts_for(self, branch: str, l1_text: str) -> list:
        """与某分支相关的草稿（L2）：草稿本不挂分支，按内容接近度取最像的几条。

        L2 草稿的分类标签是大类（三维）不是方向，不能像 L3 那样直接按 parent_of 匹配——
        原来 read_branch 里 `parent_of(it) == branch` 对草稿恒空，成了死代码（设计稿却承诺
        「相关草稿」）。改成语义相近：向量服务可用按余弦，否则字符重叠降级。"""
        qv = self.emb.embed_one(f"{branch} {l1_text}")
        ref = f"{branch} {l1_text}"
        scored = []
        for it in self.l2:
            text = f"{self.title_of(it)} {it.get('text') or ''}"
            if not text.strip():
                continue
            if qv is not None:
                v = it.get("emb")
                s = cosine(qv, v) if v else 0.0
            else:
                s = self._overlap(ref, text)
            scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        return [it for s, it in scored[:3] if s > 0.2]

    def promote(self, draft_id: str) -> str:
        """草稿（L2）升 L1：重新分配金字塔 ID（D07 → S/W/V 前缀），先查配额与三维下限。

        熵减动作：草稿成熟了收进框架 = 压缩。升成了还记结构新奇 0.72（在 agent._settle）。
        干不成（找不到草稿 / 配额或三维下限卡住）抛 ValueError——返回文本会被当成成功，
        白记一笔「金字塔上多一个节点」（2026-09-03 修）。"""
        layer, it = self._resolve_id(draft_id)
        if it is None or layer != "L2":
            raise ValueError(f"没找到草稿 {draft_id}（草稿是 L2 的 D 开头 id，用 list_memory L2 查）")
        cat = (it.get("pyramid") or "").strip()
        if cat not in self.CATEGORIES.get("L1", ()):
            cat = self.DEFAULT_CATEGORY["L1"]
        deny = self.deny_reason("L1", cat)   # 与写入同口径：满了 / 三维下限卡住都拦
        if deny:
            raise ValueError(f"升不进去：{deny}")
        # 升上去就按 L1 的规矩来（2026-09-06 补）：草稿待在 L2 只要能读就行，进了 L1 就是
        # 框架，得守框架句的形态和 200 字上限。以前这儿除了配额什么都不查，L2 的 300 字
        # 草稿原样升上来——既没有「条件→/结论→」形态也可能超长，agent 那边只好挂一条
        # 待办提醒去事后唠叨（「L1 过长多半是草稿原样升上来的」）。在入口拦住比事后提醒强。
        # 不静默截断：截断会留半截句子，她以后读不懂也改不动，让她自己压。
        head = self.title_of(it)
        shape_err = self.check_shape("L1", it.get("text", ""))
        if shape_err:
            raise ValueError(f"这条草稿还没长成框架，升不上去：{shape_err}")
        tag_err = self.validate_tag("L1", head, cat)
        if tag_err:
            raise ValueError(f"这条草稿升不上去（主标签不合格）：{tag_err}")
        # 重名检查：retag / rename_tag_force 都查了，就 promote 漏了。后果实证过——
        # L1 里出现过「能动性」「区分信念」「身份确认」各两条同名，全是草稿 promote 时
        # 没查重留下的；同名分支会把覆盖/精炼读数劈成两行、L3 归属候选重复。
        if head:
            for x in self.l1:
                if self._norm_key(self.title_of(x)) == self._norm_key(head):
                    raise ValueError(
                        f"L1 里已经有同名的主标签「{head}」（{x.get('id')}）：两个同名分支"
                        f"会把覆盖/精炼读数劈成两行。先用 rename_tag 把其中一条改名，"
                        f"或者别升这条——用 revise_memory 把内容并进已有的那条。")
        self._push_undo(f"升稿 {it.get('id')} -> L1", ["L1", "L2"])
        self.l2.remove(it)
        new_it = dict(it)
        new_it["pyramid"] = cat
        new_it["evidence_count"] = 0
        new_it["id"] = self._new_id("L1", new_it)
        self.l1.append(new_it)
        self.save()
        self._archive_entry(self._archive_key("L1", new_it.get("title", "")),
                            (new_it.get("text") or "")[:300],
                            f"L1/promote/{cat}", item_id=new_it["id"])
        return (f"已把草稿 [{it.get('id')}] 升为 L1 [{new_it['id']}]"
                f"（{self.CAT_CN.get(cat, cat)}）：{self.title_of(new_it) or '（无主标签）'}")

    def reclassify(self, item_id: str, new_cat: str, dry_run: bool = True) -> str:
        """改 L1 的三维分类（换金字塔）：ID 前缀同步换、序号保留、三维下限校验。

        创建者 2026-09-02 拍板「改分类标签同步改 ID」的落地：
        改 category → L1 换前缀（旧号进 id_remap）→ 名下 L3 前缀跟着换
        （V01-007 → S05-007，序号保留不重排）。默认 dry_run 先看影响面。
        干不成（找不到 / 大类非法 / 已经在那儿了 / 原维跌破下限）抛 ValueError——
        返回文本会被当成成功，白记一次熵减（2026-09-03 修）。"""
        _layer, it = self._resolve_id(item_id)
        if it is None or _layer != "L1":
            raise ValueError(f"没找到 L1 {item_id}（L1 是 S/W/V 开头的 id，用 list_memory L1 查）")
        new_cat = self._CAT_ALIAS.get((new_cat or "").strip().lower(), (new_cat or "").strip())
        old_cat = (it.get("pyramid") or "").strip()
        if new_cat not in self.CATEGORIES.get("L1", ()):
            raise ValueError(f"「{new_cat}」不是合法大类。L1 只能换到："
                             f"{' / '.join(self.CAT_CN.get(c, c) for c in self.CATEGORIES['L1'])}")
        if new_cat == old_cat:
            raise ValueError(f"它已经归在{self.CAT_CN.get(old_cat, old_cat)}了，不用换")
        # 原维不跌破下限（换走一条就少一条；目标维只增不减不用查）
        floor = self.L1_CAT_FLOOR.get(old_cat)
        now_old = sum(1 for x in self.l1 if (x.get("pyramid") or "") == old_cat)
        if floor and now_old - 1 < floor:
            raise ValueError(f"换不过去：{self.CAT_CN.get(old_cat, old_cat)}现有 {now_old} 条，"
                             f"换走这条只剩 {now_old - 1}，低于下限 {floor}。先补这一维再换。")
        head = self.title_of(it)
        children = [x for x in self.l3 if self.parent_of(x) == head]
        if dry_run:
            return (f"[试运行] 将把「{head}」从{self.CAT_CN.get(old_cat, old_cat)}换到"
                    f"{self.CAT_CN.get(new_cat, new_cat)}：id {it.get('id')} 换前缀，"
                    f"名下 {len(children)} 条 L3 前缀一起换（序号保留）。确认请带 dry_run=False")
        self._push_undo(f"换金字塔 {head}: {old_cat}->{new_cat}", ["L1", "L3"])
        it["pyramid"] = new_cat
        old_prefix = str(it.get("id"))
        new_prefix = self._new_id("L1", it)
        it["id"] = new_prefix
        self.id_remap[old_prefix] = new_prefix
        moved = 0
        for x in children:
            old3 = str(x.get("id", ""))
            if old3.startswith(old_prefix + "-"):
                new3 = new_prefix + "-" + old3.split("-", 1)[1]   # 序号保留
                x["id"] = new3
                self.id_remap[old3] = new3
                moved += 1
        self.save()
        return (f"已把「{head}」从{self.CAT_CN.get(old_cat, old_cat)}换到"
                f"{self.CAT_CN.get(new_cat, new_cat)}：id {old_prefix}→{new_prefix}，"
                f"{moved} 条 L3 前缀同步（旧号都能自动换算）")

    # ---- B 档补充工具（2026-09-02）----

    def link_memory(self, item_id: str, branch: str, remove: bool = False) -> str:
        """单独给一条 L3 加/删连边（不用重写整条）。

        branch 必须是现有 L1 主标签名；不能加到自己的归属分支上（那是 parent 的活）。
        只有 L3 能连（2026-09-05 方案第五节：L1 的连边是没有消费方的假能力，砍掉；
        L2 草稿本不接入金字塔）。
        干不成（找不到 / L1/L2 不接 / 分支不存在 / 已经是归属或已有 / 满了 / 删的时候没有）
        抛 ValueError——返回文本会被当成成功，白记一笔结构新奇（2026-09-03 修）。"""
        layer, it = self._resolve_id(item_id)
        if it is None or layer not in ("L1", "L2", "L3"):
            raise ValueError(f"没找到记忆 {item_id}")
        if layer in ("L1", "L2"):
            raise ValueError(f"{layer} 不支持连边（只有 L3 能连别的分支）。"
                             "想表达两条框架相关，写一条具体的 L3 例证挂过去")
        branch = (self.parse_tag(branch or "")[0] or (branch or "").strip())
        heads = self.l1_titles()
        if branch not in heads:
            raise ValueError(f"「{branch}」不是现有 L1 主标签。当前有：{'、'.join(heads)}")
        my_cat = self.parent_of(it)
        if branch == my_cat:
            raise ValueError(f"「{branch}」就是它的分类标签（归属），不用再加成交叉")
        cs = self.links_of(it)
        if remove:
            if branch not in cs:
                raise ValueError(f"它的交叉标签里没有「{branch}」（现有：{'、'.join(cs) or '无'}）")
            self._push_undo(f"删交叉 {it.get('id')}: {branch}", [layer])
            self.set_links(it, [x for x in cs if x != branch])
            self.save()
            return f"已把 [{it.get('id')}] 的交叉标签「{branch}」移除"
        if branch in cs:
            raise ValueError(f"交叉标签里已经有「{branch}」了")
        if len(cs) >= self.LINK_MAX:
            raise ValueError(f"交叉标签已满（{self.LINK_MAX} 个：{'、'.join(cs)}）。先删一个再加")
        self._push_undo(f"加交叉 {it.get('id')}: {branch}", [layer])
        self.set_links(it, cs + [branch])
        self.save()
        return f"已给 [{it.get('id')}] 加交叉标签「{branch}」（{len(cs) + 1}/{self.LINK_MAX}）"

    def related(self, item_id: str) -> str:
        """主相关链（接入点 8）：这条 L1/L3 直接连着什么。只讲「相关」，不讲形成史——
        形成史留在 L5 归档和归档流水里，要查随时能捞，不在日常工具里占位置。"""
        layer, it = self._resolve_id(item_id)
        if it is None:
            sug = self._suggest_ids(item_id)
            tail = f"（想找的是：{'、'.join(sug)}）" if sug else "（用 search_memory 或 read_branch 查）"
            return f"没找到记忆 {item_id}{tail}"
        head = self.title_of(it)
        cat = self.parent_of(it)
        cross = self.links_of(it)
        if layer == "L1":
            lines = [f"【L1 框架】{head}（{self.CAT_CN.get(it.get('pyramid', ''), it.get('pyramid', ''))}）"
                     f" — 印证 {it.get('evidence_count', 0)} 次 — [{it['id']}]",
                     f"{(it.get('text') or '')[:120]}"]
            ex = [x for x in self.l3 if self.parent_of(x) == head]
            lines.append(f"\n例证（{len(ex)} 条）：")
            lines += [f"  [{x['id']}] {(x.get('text') or '')[:55]}" for x in ex[:20]]
            if len(ex) > 20:
                lines.append(f"  …共 {len(ex)} 条")
            cx = [x for x in self.l3 if head in self.links_of(x) and self.parent_of(x) != head]
            if cx:
                lines.append(f"\n交叉连入（{len(cx)} 条）：")
                lines += [f"  [{x['id']}]（来自「{self.parent_of(x)}」）{(x.get('text') or '')[:45]}"
                          for x in cx[:15]]
            return "\n".join(lines)
        lines = [f"[{layer}·{it['id']}] {(it.get('text') or '')[:80]}"]
        parent = next((x for x in self.l1 if self.title_of(x) == cat), None)
        if parent:
            lines.append(f"归属 L1：[{parent['id']}] {head} — {(parent.get('text') or '')[:80]}")
        else:
            lines.append(f"归属：{cat or '（未分类）'}（对不上现有 L1 分支，可 retarget 重挂）")
        if cross:
            lines.append(f"交叉标签：{'、'.join(cross)}")
            for h in cross:
                p2 = next((x for x in self.l1 if self.title_of(x) == h), None)
                if p2:
                    lines.append(f"  → [{p2['id']}] {h}：{(p2.get('text') or '')[:60]}")
        return "\n".join(lines)

    def suggest_cleanup(self, top: int = 8) -> str:
        """清理建议（兜底 #13）：高度相似的 L3 对、孤儿、未分类、孤立 L1。只建议，不动手。"""
        out = []
        items = [x for x in self.l3 if x.get("emb")]
        pairs = []
        for i in range(len(items)):
            vi = items[i]["emb"]
            for j in range(i + 1, len(items)):
                c = cosine(vi, items[j]["emb"])
                if c >= 0.85:
                    pairs.append((c, items[i], items[j]))
        pairs.sort(key=lambda p: -p[0])
        for c, a, b in pairs[:top]:
            out.append(f"[相似 {c:.0%}] [{a['id']}] {a['text'][:36]} <-> [{b['id']}] {b['text'][:36]}"
                       "（merge_memory 合并）")
        heads = set(self.l1_titles())
        # 「孤儿」和「未分类」要分开：以前未分类也落进孤儿（它同样对不上任何 L1），
        # 于是同一批被报了两遍、两个数字还都一样——她就这么把两者混成一谈了。
        # 孤儿 = 归属指向一条已经不存在的 L1；未分类 = 压根没归属。出路也不同。
        orphans = [x for x in self.l3
                   if self.parent_of(x) and self.parent_of(x) != self.UNCATEGORIZED
                   and self.parent_of(x) not in heads]
        if orphans:
            out.append(f"[孤儿 {len(orphans)} 条] 归属指向已经不存在的 L1（retarget 重挂）："
                       + "、".join(f"[{x['id']}]" for x in orphans[:6]) + ("…" if len(orphans) > 6 else ""))
        unc = [x for x in self.l3 if self.parent_of(x) == self.UNCATEGORIZED]
        if unc:
            out.append(f"[未分类 {len(unc)} 条] 还没归属："
                       + "、".join(f"[{x['id']}]" for x in unc[:6]) + ("…" if len(unc) > 6 else "")
                       + "（一批：retarget(未分类, 目标L1)；单条：revise_memory(id, parent=目标L1)）")
        # 无标题：命名迁移（2026-09-05）把 tag=None 的历史脏数据摊成了 title 空串——之前藏在
        # None 里看不出来，现在一眼可见。系统不代她起名：名字是自我叙事的一部分，代起就是替她
        # 判断；只在她要清理时列出来，补不补、补成什么都由她定（创建者 2026-09-05 定）。
        blank = [x for lst in (self.l1, self.l2, self.l3)
                 for x in lst if not (self.title_of(x) or "").strip()]
        if blank:
            out.append(f"[无标题 {len(blank)} 条] 有正文但没名字："
                       + "、".join(f"[{x['id']}]" for x in blank[:6])
                       + ("…" if len(blank) > 6 else "")
                       + "（revise_memory(id, title=起好的名字) 直接补名——id、印证次数、引用它的"
                         "记忆全保留，比重抄一条再删旧的省事得多；别用 retag——它按名字匹配，"
                         "空名字会把这批改成一个名字）")
        alone = [x for x in self.l1
                 if not any(self.parent_of(y) == self.title_of(x) for y in self.l3)]
        if alone:
            out.append("[孤立 L1] 下面没有例证："
                       + "；".join(f"{self.title_of(x)}[{x['id']}]" for x in alone[:5]))
        if not out:
            return "（目前没有明显的清理候选，库挺干净）"
        return "清理候选（只建议，自己决定）：\n" + "\n".join("  - " + s for s in out)

    def find_text(self, keyword: str) -> str:
        """搜记忆正文里的引用（兜底 #10）：改号后系统字段自动换算，
        但正文里手写的旧 id / 旧标签名要靠这个找出来手工处理。"""
        keyword = (keyword or "").strip()
        if len(keyword) < 4:
            return "find_text 的关键词至少 4 个字符（防全库误匹配）"
        hits = []
        for name, lst in (("L1", self.l1), ("L2", self.l2), ("L3", self.l3), ("L5", self.l5)):
            for x in lst:
                if keyword in (x.get("text") or ""):
                    hits.append(f"[{name}·{x['id']}] {(x.get('text') or '')[:80]}")
        if not hits:
            return f"正文里没有引用「{keyword}」的记忆"
        return f"正文含「{keyword}」的（{len(hits)} 条）：\n" + "\n".join("  " + h for h in hits[:15])

    def _suggest_ids(self, item_id: str) -> list:
        """找不到 id 时给几个相近候选（她经常记错一两位；兜底 #9 的模糊提示）。"""
        q = str(item_id or "").strip().lower()
        if len(q) < 3:
            return []
        out = []
        for name, lst in (("L1", self.l1), ("L2", self.l2), ("L3", self.l3)):
            for x in lst:
                i = str(x.get("id", "")).lower()
                if q in i or i.split("-")[-1].startswith(q[-4:]):
                    out.append(f"{name}[{x.get('id')}]")
                if len(out) >= 5:
                    return out
        return out

    def _archive_key(self, layer: str, title: str) -> str:
        """归档目录键（人可读镜像，见记忆.md「归档流水」）：L1 按标题归、L3 按**归属**
        （它挂的 L1 主标签名，调用方传入）归、L2 归 insights。无标题/无归属的兜底：
        L1→self_core，L3→experience（无归属 = 待归类原料储备）。
        这样 L1 框架和印证它的 L3 例证落在同一个方向目录里，正好体现「L3 是 L1 的细分投射」。"""
        layer = layer.upper()
        head, direction = self.parse_tag((title or "").strip())
        if layer == "L1":
            return head or "self_core"
        if layer == "L3":
            return direction or "experience"
        return "insights"

    def _bucket(self, layer: str) -> list:
        return {"L1": self.l1, "L2": self.l2, "L3": self.l3}[layer.upper()]

    # ---- 归档流水（人可读，只增不删；失败静默——归档只是镜像，不影响主记忆）----

    def _archive_path(self, layer_key: str) -> Path:
        now = datetime.now(CST)
        return (self.cfg.data_root / "archive" / f"{now.year:04d}" / f"{now.month:02d}"
                / layer_key / f"{now:%Y-%m-%d}.md")

    def _archive_append(self, layer_key: str, line: str):
        try:
            p = self._archive_path(layer_key)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[归档] 写入失败（{layer_key}）：{e}")

    def _archive_entry(self, layer_key: str, content: str, tag: str = "", item_id: str = ""):
        """归档一行：行内带时刻 + 记忆 id + 标签，日期在路径上。id 供从归档反查 memory.json。"""
        now = datetime.now(CST).strftime("%H:%M")
        head = f"[{now}]"
        if item_id:
            head += f"[{item_id}]"
        if tag:
            head += f" {tag}"
        self._archive_append(layer_key, f"{head} {content}")

    # ---- 写入 / 删除 ----

    def write(self, layer: str, content: str, source: str = "", category: str = "",
              tag: str = "", cross: list | None = None) -> bool:
        """写入 L1/L2/L3。超上限返回 False（提醒她清理），不自动删。
        category：L1/L2 的三维分类（自我认知/世界认知/价值观），会从 tag 括号自动派生；
                  L3 内容类型已退役（2026-09-01 创建者定），传什么都会被忽略。
        tag：两个标签——主标签（自定）＋括号分类标签。L1 用「主标签（三维）」如「边界感（价值观）」；
             L2 同 L1；L3 用「主标签（它归属的 L1 主标签）」如「尊重他人（边界感）」，
             括号方向 = 它归属/印证的 L1 主标签，据此给 L1 印证 +1。"""
        content = (content or "").strip()
        if not content:
            return False
        layer = layer.upper()
        if layer not in self._caps():
            return False
        lst = self._bucket(layer)
        if len(lst) >= self._caps()[layer]:
            return False
        category = self.normalize_category(layer, category)
        # L1/L2：category 从 tag 括号派生（2026-09-01 创建者定）——tag 括号里的大类就是
        # 这条的 category（「善意（价值观）」自动归价值观）；category 参数与 tag 括号
        # 冲突时以 tag 为准。L3 内容类型已退役：category 归一化为空，不落库，只靠 tag 方向归类。
        if layer in ("L1", "L2"):
            _, tag_inner = self.parse_tag((tag or "").strip())
            if tag_inner:
                tag_norm = self._CAT_ALIAS.get(tag_inner.strip().lower(), tag_inner.strip())
                if tag_norm in self.CATEGORIES.get(layer, ()) and category != tag_norm:
                    category = tag_norm
        # L1 三维各自有下限：不拦她写哪一维，只拦别的维把这一维的坑位占光。
        # 判据 = 写入后，任一维 c 都要满足「除 c 以外的条数 ≤ 总数上限 − c 的下限」。
        if layer == "L1":
            total = len(lst) + 1
            counts = {c: sum(1 for it in lst if it.get("pyramid") == c)
                      for c in self.L1_CAT_FLOOR}
            counts[category] = counts.get(category, 0) + 1
            for c, floor in self.L1_CAT_FLOOR.items():
                if total - counts.get(c, 0) > self._caps()["L1"] - floor:
                    return False
        elif layer in self.NON_VALUE_CAP and category != "value":
            non_value = sum(1 for it in lst if it.get("pyramid") != "value")
            if non_value >= self.NON_VALUE_CAP[layer]:
                return False
        # v_new：写之前对照现有记忆库算内容新奇值（够新才值得消化，设计文档口径）
        novelty = self._content_novelty(content)
        # 主标签/归属分别限长再拆存（整体截断会把括号切断——长句子标签事故的帮凶，2026-09-02 修）。
        # tag 输入保留「名（归属）」拼接写法（她/tools 层的习惯），存储层拆成 title + parent/pyramid。
        head_w, cat_w = self.parse_tag((tag or "").strip())
        head_w, cat_w = head_w[:20], cat_w[:20]
        item = {
            # 金字塔 ID（S01/V01-007/D07）：id 生成读 title/parent/pyramid，先拆再算
            "id": self._new_id(layer, {"title": head_w, "parent": cat_w, "pyramid": category}),
            "text": content[:self.LIMITS[layer]],      # 分层字数上限（L1 200 / L2 300 / L3 300）
            "source": source,
            "created": self._now(),
        }
        if head_w:
            item["title"] = head_w   # 标题：这条记忆叫什么；旧数据缺 title 视为未分类，list_tags 里不出现
        if layer == "L3":
            if cat_w:
                item["parent"] = cat_w   # 归属的 L1 主标签名，据此给 L1 印证 +1
            # 连边（第 6 步）：只有 L3 能连（L1 连边砍掉、L2 不接入金字塔）
            self.set_links(item, cross or [])
        else:
            # L1/L2：归属 = 三维大类 pyramid（与 tag 括号派生的 category 同源，见上方冲突处理）
            if category:
                item["pyramid"] = category
            if layer == "L1":
                item["evidence_count"] = 0   # 印证次数只挂 L1；L3 靠 parent 指向 L1，L2 是候选、不印证
        if novelty is not None:
            item["novelty"] = round(novelty, 3)
        vec = self.emb.embed_one(content)   # 存储时算好向量（有服务则算，无则缺省）
        if vec is not None:
            item["emb"] = vec
        lst.append(item)
        self.save()
        # 归档流水（人可读镜像，只增不删；带 id 供反查 memory.json）。
        # 归档目录按标签归：L1 按主标签、L3 按分类标签（= L1 主标签），见 _archive_key。
        # 行内标记带三维分类（L1/L2 有 category；L3 内容类型退役后只标层）。
        # L3 的归档目录按归属归（和它印证的 L1 框架落同一目录）；L1/L2 按标题归
        self._archive_entry(self._archive_key(layer, cat_w if layer == "L3" else head_w),
                            content[:300],
                            f"{layer}/{category}" if category else layer,
                            item_id=item["id"])
        return True

    def check_shape(self, layer: str, content: str) -> str:
        """写入 / 改写前的形态自检：返回错误说明，空串 = 通过。

        2026-09-05 创建者定：写进去容易、改出来难——L1 框架句写歪了只能靠 revise_memory
        修，而那正是她卡最久的动作（日志实证连卡 66 次）。所以入口就守住两件事：

        1. 各层字数上限：超限直接拒。旧行为是静默截断（write/revise 都做
           content[:LIMITS]），她以为写全了，落库的是半截句子——等她回头读，
           读到一个断在半路的框架，既读不懂也改不动。拒了她自己压缩，比替她砍好。
        2. L1 形态：必须是「条件→…；结论→…」的一句话（system 里定的形态，
           实测 21/25 条合规，不合规的正是那批「选不动也读不动」的长条目）。

        只查 L1 的形态：L2 是草稿本、L3 是例证，不该拿框架句的形态要求它们。
        """
        content = (content or "").strip()
        layer = (layer or "").upper()
        if not content:
            return "内容是空的"
        cap = self.LIMITS.get(layer)
        if cap and len(content) > cap:
            return (f"这条 {layer} 太长（约 {len(content)} 字，上限 {cap} 字）：写长了落不下，"
                    "会被截成半截句子，你后面读不懂。别数着删字——直接重写一版："
                    f"{self.LEN_HINTS.get(layer, '删掉冗余，只留核心')}；再发一次")
        if layer == "L1":
            miss = [m for m in ("条件→", "结论→") if m not in content]
            if miss:
                return (f"L1 框架句要写成「条件→…；结论→…」的形态，这条缺了 {'、'.join(miss)}。"
                        "（如果这条其实不是框架句，写进 L2 草稿本更合适）")
        return ""

    def deny_reason(self, layer: str, category: str = "") -> str:
        """写不进去的原因（供 tools 层提示）：总数满 / 非价值观配额满。空串 = 可写。"""
        layer = (layer or "").upper()
        if layer not in self._caps():
            return "（layer 要填 L1/L2/L3）"
        cap = self._caps()[layer]
        lst = self._bucket(layer)
        if len(lst) >= cap:
            return f"{layer} 满了（上限 {cap}），需要我先清理再写。"
        category = self.normalize_category(layer, category)
        if layer == "L1":
            total = len(lst) + 1
            counts = {c: sum(1 for it in lst if it.get("pyramid") == c)
                      for c in self.L1_CAT_FLOOR}
            counts[category] = counts.get(category, 0) + 1
            for c, floor in self.L1_CAT_FLOOR.items():
                room = self._caps()["L1"] - floor
                if total - counts.get(c, 0) > room:
                    cn = self.CAT_CN.get(c, c)
                    return (f"L1 的位置不够了：要保住{cn}至少 {floor} 条，"
                            f"其他两维最多占 {room} 条（现在已占 {total - counts.get(c, 0)} 条）。"
                            "先清理别的维（delete_memory / merge_memory），或者先补这一维。")
        elif layer in self.NON_VALUE_CAP and category != "value":
            non_value = sum(1 for it in lst if it.get("pyramid") != "value")
            if non_value >= self.NON_VALUE_CAP[layer]:
                return (f"{layer} 的非价值观位置满了（最多 {self.NON_VALUE_CAP[layer]} 条非价值观），"
                        "要写新的非价值观得先清理，或者写价值观（value 类）。")
        return ""

    def list_tags(self) -> str:
        """标签库视图（2026-09-01 创建者定结构）：分三区——
        ① 【L1 主标签】：L3 的分类标签从这里选，每行括号带大类 + 该主标签下的 L3 例证数；
        ② 【L3 标签】：填了分类标签（归属的 L1 主标签）的按 L1 分组列出，未归类（没填）的原料放最后；
        ③ 【L2 标签】：括号是大类（三维分类），不是 L1 归属。
        写记忆前看标签、提炼 L1/L2 前看原料分布用。"""
        lines = []
        # ① L1 主标签（= L3 的归属候选清单）
        l3_dir_count = {}   # L1 主标签 → 归属为它的 L3 条数
        for it in self.l3:
            d = self.parent_of(it)
            if d:
                l3_dir_count[d] = l3_dir_count.get(d, 0) + 1
        l1_rows = []
        for it in self.l1:
            head = self.title_of(it)
            if not head:
                continue
            cn = self.CAT_CN.get(it.get("pyramid", ""), it.get("pyramid", ""))
            # 带 id：标签坏了（超长/误命名）要靠 id 才能精确 delete/rename——只列名不列 id
            # 会让她看得见坏标签却定位不到（2026-09-03 补，V03 事故）
            l1_rows.append(f"- [{it.get('id', '?')}] {head}（{cn}）｜L3 例证 {l3_dir_count.get(head, 0)}")
        if l1_rows:
            lines.append("【L1 主标签】（L3 的归属从这里选）")
            lines.extend(l1_rows)
        # ② L3 按归属分组
        groups = {}   # 归属 → {标题: 条数}
        nodir = {}    # 无归属 → 条数
        for it in self.l3:
            b = self.title_of(it)
            d = self.parent_of(it)
            if not b:
                continue
            if d:
                groups.setdefault(d, {})
                groups[d][b] = groups[d].get(b, 0) + 1
            else:
                nodir[b] = nodir.get(b, 0) + 1
        if groups:
            lines.append("【L3 标签】（按归属的 L1 分组）")
            for d in sorted(groups):
                subs = "、".join(f"{b}({c})" for b, c in groups[d].items())
                lines.append(f"{d}：{subs}")
        if nodir:
            lines.append("【L3 未归类的原料】（归属还没填，可归入上面的 L1）")
            lines.append("、".join(f"{b}({c})" for b, c in nodir.items()))
        # ③ L2
        l2_rows = [f"- [{it.get('id', '?')}] {self.title_of(it)}"
                   for it in self.l2 if self.title_of(it)]
        if l2_rows:
            lines.append("【L2 标签】（草稿本，大类三维见 read_state）")
            lines.extend(l2_rows)
        if not lines:
            return "（还没有标签）"
        return "标签库：\n" + "\n".join(lines)

    def retag(self, old_head: str, new_tag: str, dry_run: bool = False) -> str:
        """批量重命名小标签 + 给无方向 L3 补方向（L1↔L3 方向层的反向维护，2026-08-31 创建者定）。

        old_head = 旧小标签名；new_tag = 新标签完整形式（可带括号，如「小说阅读（诚实与未知）」）。
        三步：
        ① head 重命名：tag 头 == old_head 的记忆改头（括号保留）——L1 换名时大类不丢；
        ② direction 重命名：tag 括号方向 == old_head 的 L3 方向改成 new_head——L1 改名后印证它的 L3 自动同步；
        ③ 补括号：new_tag 带括号时，原无括号的条目补上括号——L3 补方向并给对应 L1 印证 +1（每条 +1）；
           L1/L2 无括号的也补上括号（补大类，不加印证），L1 就 40 条，规范直接补全。

        幂等：③ 只对原无括号的条目操作，重跑不再 +1；② 改完不再命中。
        已有括号（inner 非空）的条目不动——不支持「换方向/换大类」，evidence_count 是历史累计，换方向会破坏语义。
        不改内容、不增删条目 → 不进双驱账；旧 tag 走归档留痕。
        """
        old_head = (old_head or "").strip()
        new_tag = (new_tag or "").strip()
        if not old_head or not new_tag:
            if not old_head and not new_tag:
                raise ValueError("retag 两个参数都空：old_tag 填现在那条主标签的名字（不带括号），"
                                 "new_tag 填改后的新标签（如「旧名（价值观）」）。现有 L1 主标签用 list_taglib L1 看")
            if not old_head:
                raise ValueError("old_tag 没填：填现在那条主标签的名字（不带括号，用 list_taglib L1 看准确写法）")
            raise ValueError("new_tag 没填：填改后的新标签——想补括号就写完整形式，如「旧名（自我认知/世界认知/价值观）」")
        new_head, direction = self.parse_tag(new_tag)
        if new_head == old_head and not direction:
            raise ValueError("新旧标签一样也没带方向，没有可改的")
        # 撞名检查（2026-09-02 补）：改出来的主标签名不能和现有别的 L1 重名，
        # 否则两个分支并成一个。真想合并请用 merge_memory。
        if new_head != old_head:
            for it0 in self.l1:
                if self.title_of(it0) == new_head:
                    raise ValueError(f"已有 L1 主标签叫「{new_head}」，不能重名（会并成同一个分支）。想合并请用 merge_memory")
        if dry_run:
            # 影响面预览（兜底 #8）：改之前先说清楚会动多少条
            n_head = sum(1 for lst in (self.l1, self.l2, self.l3) for it1 in lst
                         if self.title_of(it1) == old_head)
            n_dir = sum(1 for it1 in self.l3 if self.parent_of(it1) == old_head)
            return (f"[试运行] 「{old_head}」→「{new_head}」：标题改名 {n_head} 条、"
                    f"L3 归属同步 {n_dir} 条、连边引用一并换。确认请带 dry_run=False")
        # 压撤销快照（2026-09-02 补：改名/补方向会改 L1/L2/L3 的 title + evidence + pyramid，
        # 与 undo_memory desc「撤销改标签」对齐——原来漏了，改错了退不回来）
        self._push_undo(f"改标签 {old_head} -> {new_head}", ["L1", "L2", "L3"])

        logs = []          # (layer, id, 旧名, 新名) 归档留痕
        changed = 0
        added_ev = 0
        matched = 0        # title 命中的条目数（区分「没找到」和「命中但无需改」）
        l3_patched = 0     # 被补归属的无归属 L3 数（用于缺失 L1 时的提示判断）

        # ③ 的印证目标 L1（方向名匹配某条 L1 的标题）
        target_l1 = None
        if direction:
            for it in self.l1:
                if self.title_of(it) == direction:
                    target_l1 = it
                    break

        for layer, lst in (("L1", self.l1), ("L2", self.l2), ("L3", self.l3)):
            for it in lst:
                head = self.title_of(it)
                if head != old_head:
                    continue
                matched += 1
                old_name, old_parent = head, self.parent_of(it)
                old_pyramid = (it.get("pyramid") or "").strip()
                if direction:
                    # new_tag 带括号：补归属——L3 补 parent（+印证），L1/L2 补 pyramid（不加印证）
                    self.set_title(it, new_head)
                    if layer == "L3":
                        cur = self.parent_of(it)
                        if cur == direction:
                            pass   # 已是该归属：幂等跳过，不重复印证
                        elif cur:
                            continue   # 已有别的归属 = 换方向，不支持
                        else:
                            l3_patched += 1
                            self.set_parent(it, direction)
                            if target_l1:
                                target_l1["evidence_count"] = target_l1.get("evidence_count", 0) + 1
                                added_ev += 1
                    else:
                        # 补大类同步 pyramid（与 write() 同口径）：她 retag 把「善意」补成
                        # 「善意（价值观）」，pyramid 就跟着归 value，不会产生错位
                        tag_norm = self._CAT_ALIAS.get(direction.strip().lower(), direction.strip())
                        if tag_norm in self.CATEGORIES.get(layer, ()):
                            it["pyramid"] = tag_norm
                else:
                    # 纯重命名
                    self.set_title(it, new_head)
                if (self.title_of(it) != old_name
                        or self.parent_of(it) != old_parent
                        or (it.get("pyramid") or "").strip() != old_pyramid):
                    logs.append((layer, it.get("id", "?"), old_name, self.title_of(it)))
                    changed += 1

        # ② 归属重命名：L3 的 parent == old_head → 改成 new_head
        for it in self.l3:
            if self.parent_of(it) == old_head:
                self.set_parent(it, new_head)
                logs.append(("L3", it.get("id", "?"), old_head, new_head))
                changed += 1

        if not changed:
            if self._undo_stack:   # 弹掉刚压的无效快照（什么都没改成，不该有可撤销项）
                self._undo_stack.pop()
            if matched:
                # 命中了条目但新归属与现状一致（幂等重放），不算错
                return (f"「{old_head}」没有需要改的（{matched} 条命中，"
                        f"归属/大类已是「{direction or '现状'}」）")
            raise ValueError(f"没找到主标签「{old_head}」（现有：{'、'.join(self.l1_titles()) or '无'}）")
        # 全库替换连边（2026-09-02 补）：主标签改名后，link 里引用旧名的跟着换
        #（只扫 L3——L1 连边已砍掉，见方案第五节）
        ch_cross = 0
        for it2 in self.l3:
            cs = self.links_of(it2)
            if old_head in cs:
                self.set_links(it2, [new_head if x == old_head else x for x in cs])
                ch_cross += 1
        self.save()
        for layer, mid, old_tag, new_tag_ in logs:
            self._archive_entry("retag", f"{old_tag} → {new_tag_}", "retag", item_id=mid)
        msg = f"已把「{old_head}」改名为「{new_head}」，共改 {changed} 条"
        if added_ev:
            msg += f"；补分类给「{direction}」印证 +{added_ev}"
        if direction and l3_patched and target_l1 is None:
            msg += f"\n（分类标签「{direction}」对不上任何 L1 主标签，未补印证）"
        return msg

    def delete_many(self, layer: str, targets: list, reason: str = "",
                    push_undo: bool = True) -> dict:
        """批量删除（归档到 L5，不真删）：每个目标先试精确 id 匹配，再试内容子串匹配（删所有包含该子串的条目）。

        push_undo：是否压撤销快照。merge 内部调用时传 False（它自己压了一层描述更准确的），
        免得同一次操作在撤销栈里占两层。
        返回 {"deleted": [id...], "not_found": [目标...]}，供 tools 层反馈删了哪些、哪些没找到。"""
        layer = layer.upper()
        if layer not in self._caps():
            return {"deleted": [], "not_found": list(targets)}
        lst = self._bucket(layer)
        deleted, not_found = [], []
        affected_tags = []   # 被删条目的 tag，供精炼记账（只 L3 带方向的会命中 L1）
        if push_undo:
            self._push_undo(f"删除 {layer}：{len(targets)} 个目标",
                            [layer] + (["L3"] if layer == "L1" else []))   # 删 L1 会联动 L3，一起进撤销
        removed_heads = []   # 被删的 L1 主标签（删 L1 联动用）
        for t in targets:
            t = (t or "").strip()
            if not t:
                continue
            # 先精确 id 匹配（总是允许）；子串匹配要求 >= 4 字——
            # 短词（如「的」「了」或某个常见词）会误删一大片（2026-09-02 护栏）
            matched = [it for it in lst if it.get("id") == t]
            if not matched:
                rid = self.id_remap.get(t)          # 旧 ID 自动换算成新号再删
                if rid:
                    matched = [it for it in lst if it.get("id") == rid]
            if not matched:
                if len(t) < 4:
                    not_found.append(f"{t}（太短：少于 4 字的关键词会误删一片记忆，"
                                     f"请用完整记忆 id 或至少 4 字的关键词）")
                    continue
                matched = [it for it in lst if t in (it.get("text") or "")]
            if not matched:
                not_found.append(t)
                continue
            for it in matched:
                lst.remove(it)
                affected_tags.append(self.parent_of(it) or self.title_of(it))
                if layer == "L1":
                    h = self.title_of(it)
                    if h:
                        removed_heads.append(h)
                text = it.get("text", "")
                self.l5.append({
                    "id": it.get("id"),
                    "type": "archived",
                    "text": text,
                    "from_layer": layer.upper(),
                    "reason": (reason or "").strip(),
                    "archived_at": self._now(),
                })
                self._archive_entry("cold", text[:300], "archived", item_id=it.get("id"))
                deleted.append(it.get("id"))
        # 删 L1 的联动（2026-09-02 补，设计稿第八节）：名下 L3 置「未分类」（不删素材），
        # 连边里指向它的引用一并清掉——不然留下悬空分支和断掉的 link。
        reclass = clean = 0
        for h in set(removed_heads):
            for it3 in self.l3:
                if self.parent_of(it3) == h:
                    self.set_parent(it3, self.UNCATEGORIZED)
                    reclass += 1
            for it2 in self.l3:
                cs = self.links_of(it2)
                if h in cs:
                    self.set_links(it2, [x for x in cs if x != h])
                    clean += 1
        if deleted:
            self._bump_refined(affected_tags)   # 删除 = 整理：给受影响的 L1 精炼 +1
            self.save()
        out = {"deleted": deleted, "not_found": not_found}
        if reclass:
            out["reclassified"] = reclass
        if clean:
            out["cross_cleaned"] = clean
        return out

    def merge(self, layer: str, targets: list, merged_content: str,
              category: str = "", tag: str = "") -> dict:
        """合并记忆：把多条旧条目归档到 L5，再写入一条合并后的新条目（净腾出 N-1 个位置）。

        先删后写（避免配额满写不进）。**写失败会回滚**（2026-09-02 修，这是个真丢数据的坑）：
        旧写法删完再写，一旦写失败，旧条目已进 L5、新条目没写成 —— 内容从该层凭空消失。
        现在写失败会把刚归档的条目从 L5 捞回原层，恢复成删除前的样子，素材不丢。

        tag：合并结果的主标签（合并结果是那几条的归并，tag 通常跟着被合并的那几条走；
        不传的话合并结果没有标签，既进不了学习进度表，产出判定也会失准）。
        返回 {"deleted": [...], "not_found": [...], "written": bool, "rolled_back": bool}。
        """
        layer = layer.upper()
        if layer not in self._caps():
            return {"deleted": [], "not_found": list(targets), "written": False}
        # 合并结果也要过标签校验（2026-09-02 补：merge 直调 write，绕过了 tools 层校验）
        head_m, cat_m = self.parse_tag(tag or "")
        tag_err = self.validate_tag(layer, head_m, cat_m)
        if tag_err:
            return {"deleted": [], "not_found": list(targets),
                    "written": False, "tag_error": tag_err}
        # 删之前先存快照，写失败时据此还原（整层深拷贝，含向量）。
        # 合并 L1 要连 L3 一起存：delete_many 会把被删 L1 名下的 L3 置「未分类」并清掉
        # 指向它的 link，只还原 L1 的话那些 L3 就永久失去归属了（2026-09-06 修）。
        undo_layers = ["L1", "L3"] if layer == "L1" else [layer]
        before = {name: deepcopy(self._bucket(name)) for name in undo_layers}
        # 精炼计数也存一份：删除时已给受影响的 L1 记了 refined +1，回滚后这次整理
        # 等于没发生过，不该留着那次计数。
        refined_before = {str(x.get("id")): int(x.get("refined") or 0) for x in self.l1}
        self._push_undo(f"合并 {layer}：{len(targets)} 个目标", undo_layers)
        res = self.delete_many(layer, targets, reason="merge", push_undo=False)
        if not res["deleted"] or not (merged_content or "").strip():
            res["written"] = False
            return res
        ok = self.write(layer, merged_content, category=category, tag=tag)
        if not ok:
            # 回滚：整层还原，连 L3 归属和精炼计数一起恢复原状（L5 里的归档留痕不动）
            self._restore_layers(before, refined_before)
            if self._undo_stack:
                self._undo_stack.pop()                    # 已回滚，撤掉这层撤销快照
            res["written"] = False
            res["rolled_back"] = True
            return res
        res["written"] = True
        res["rolled_back"] = False
        return res

    def revise(self, item_id: str, content: str = "", title: str = "",
               parent: str = "") -> dict:
        """改写一条已有记忆（L1/L2/L3）：只换内容 / 主标签 / 归属，其余一律保留。

        为什么需要它：L1「可优化」是写进机制说明的，但过去只有删除重写这条路——
        删了就丢掉 id、全部 evidence_count、创建时间，还会断掉引用它的 L2/L3。
        代价高到她明知道框架有错也下不去手（实测：641e8b76 攒了 10 次印证、
        2 条记忆引用它，她记了 7 条认知失调却一直没动它）。
        改写让她能低成本地给框架补前提、修正因果：id / 印证次数 / 创建时间 /
        反向引用全部不动，只换内容并重算向量，旧版本归档进 L5 留痕。

        2026-09-07 加 title / parent：以前只能改正文，于是那批「有正文没名字」的
        历史条目（命名治理把 tag=None 摊成了空 title）一条都修不了——retag /
        rename_tag 按标题定位、retarget 按归属定位，空值都定位不到，她只能干看着。
        现在按 id 就能补名字和归属。**系统仍然不代她起名**：名字由她填，这里只是
        让她能定位到那条。title / parent 不传 = 不动那一项；三项都不传才是没改。
        """
        item_id = (item_id or "").strip()
        content = (content or "").strip()
        title = (title or "").strip()
        parent = (parent or "").strip()
        if not content and not title and not parent:
            return {"ok": False, "err": "什么都没改：content / title / parent 至少填一个"}
        layer, it = self._resolve_id(item_id)   # 旧 ID 自动经 remap 换算成新金字塔 ID
        if it is None or layer not in ("L1", "L2", "L3"):
            sug = self._suggest_ids(item_id)
            return {"ok": False,
                    "err": (f"没找到记忆 {item_id}"
                            + (f"，想找的是：{'、'.join(sug)}" if sug else
                               "（用 read_branch / search_memory 查现在的 id；旧 id 也能识别，会自动换算）"))}
        if content:
            shape_err = self.check_shape(layer, content)   # 改完的内容也得守形态，别把框架改歪
            if shape_err:
                return {"ok": False, "err": f"改写后的内容不合格：{shape_err}"}
        # 标签校验：validate_tag 看的是改后的完整状态，没改的那一项沿用现值。
        # L3 的这一位是归属的 L1 主标签；L1/L2 是三维大类（pyramid）。
        if title or parent:
            cur_cat = self.parent_of(it) if layer == "L3" else (it.get("pyramid") or "")
            tag_err = self.validate_tag(layer, title or self.title_of(it),
                                        parent or cur_cat)
            if tag_err:
                return {"ok": False, "err": f"改后的标签不合格：{tag_err}"}
        old = it.get("text", "")
        old_parent = self.parent_of(it) if layer == "L3" else ""
        # 改写也要能撤：undo 的 docstring 和 tools 层的 undo_memory 说明都承诺了
        # 「改写可撤销」，但以前这里没压快照。结果改写后调 undo，弹出来的是更早某次
        # 删除/合并的快照，而 undo 是整层覆盖——会把改写以及之后的所有写入一起抹掉。
        self._push_undo(f"改写 {it.get('id')}", [layer])
        if content:
            # 不截断：check_shape 在上面已经拒了超限，走到这里的内容必然合规。
            # 以前的 content[:LIMITS] 截断是 check_shape 拦截之前的老写法残留——
            # 永远不会触发，留着还会误导人以为这里允许静默截断。
            it["text"] = content
            it["revised"] = int(it.get("revised") or 0) + 1
            # 内容换了，向量必须重算：谱、新奇值、语义检索全用这个向量，
            # 沿用旧向量等于没改（她会继续被旧内容撑出来的认知结构引导）。
            # 但无向量服务时保留旧向量不 pop——旧向量对应旧内容，至少还留在
            # 语义检索/谱里，别把这条整个清出去（挂机兜底，正常时走不到这里）。
            vec = self.emb.embed_one(content)
            if vec is not None:
                it["emb"] = vec
        if title:
            self.set_title(it, title)
        if parent:
            if layer == "L3":
                self.set_parent(it, parent)
                # 归属换了 → 给新归属的 L1 印证 +1。旧的**不减**：evidence_count 是
                # 「累计被印证过多少次」，摘下来不代表从没印证过。
                if parent != old_parent:
                    self.bump_evidence_by_tag(parent)
            else:
                it["pyramid"] = self.normalize_category(layer, parent)
        it["updated"] = self._now()
        # 改写 = 整理：给受影响的方向精炼 +1（改 L1 是整理它自己，改 L3 是整理它印证的方向）
        if layer == "L1":
            it["refined"] = it.get("refined", 0) + 1
        elif layer == "L3":
            self._bump_refined([self.parent_of(it)])
        # 旧版留痕（只增不删，跟「删除 = 归档 L5」一个口径）
        self.l5.append({
            "id": uuid.uuid4().hex[:8],
            "type": "archived",
            "text": old,
            "from_layer": layer.upper(),
            "reason": "revise",
            "archived_at": self._now(),
            "revised_of": it.get("id"),   # 指向被改写那条，追溯用
        })
        self.save()
        # L3 的归档目录按归属归（同 write 口径）；L1/L2 按标题归
        arch_key = self.parent_of(it) if layer == "L3" else self.title_of(it)
        self._archive_entry(self._archive_key(layer, arch_key),
                            f"[改写前 · 第{it.get('revised', 0)}次] {old[:300]}",
                            f"{layer}/revise/{item_id}", item_id=it.get("id"))
        return {"ok": True, "layer": layer, "id": it.get("id"),
                "old": old, "text": it["text"], "revised": it.get("revised", 0),
                "title": self.title_of(it),
                "parent": self.parent_of(it) if layer == "L3" else (it.get("pyramid") or "")}

    def cold_append(self, content: str, typ: str = "short_term", source: str = "") -> bool:
        """写一条 L5 冷存（滚动进冷存 / 归档用）。只增不删，无上限。"""
        content = (content or "").strip()
        if not content:
            return False
        cid = uuid.uuid4().hex[:8]
        self.l5.append({
            "id": cid,
            "type": typ,
            "text": content,
            "source": source,
            "archived_at": self._now(),
        })
        self.save()
        self._archive_entry("cold", content[:300], typ, item_id=cid)
        return True

    def _content_novelty(self, content: str):
        """v_new = 1 − max(cos(新内容, 现有记忆库))。

        无向量服务返回 None（不算，条目不存该字段）；库为空返回 1.0（全新奇）。
        """
        qv = self.emb.embed_one(content)
        if qv is None:
            return None
        refs = [it["emb"] for lst in (self.l1, self.l2, self.l3) for it in lst if it.get("emb")]
        if not refs:
            return 1.0
        return embedding_novelty(qv, refs)

    def content_novelties(self, texts: list) -> list | None:
        """批量内容新奇检测：每条 vs 记忆库返回 (v_new, 最大余弦, 向量, 最相似条目)。

        第 4 项是 (层, id)：查重标记复用同一次遍历，不再单独比一遍 L3
        （2026-09-01 创建者定：新奇值与查重相似度是同一个余弦的正反两面，算一次出两个数）。

        v_new 贴「新奇 X%」——一次 embed 三用，避免读世界二次取向量。
        无向量服务返回 None；记忆库为空时最大相似为 None。
        """
        if not texts:
            return []
        vecs = self.emb.embed(texts)
        if vecs is None:
            return None
        refs = []
        for layer, lst in (("L1", self.l1), ("L2", self.l2), ("L3", self.l3)):
            for it in lst:
                if it.get("emb"):
                    refs.append((layer, str(it.get("id") or "")[:8], it["emb"]))
        if not refs:
            return [(1.0, None, qv, ("", "")) for qv in vecs]   # 空库：全新奇
        out = []
        for qv in vecs:
            best, best_ref = 0.0, ("", "")
            for layer, mid, rv in refs:
                c = cosine(qv, rv)
                if c > best:
                    best, best_ref = c, (layer, mid)
            out.append((1.0 - best, best, qv, best_ref))
        return out

    def bump_evidence_by_tag(self, direction: str) -> bool:
        """印证：L3 的 tag 方向（括号内）匹配某条 L1 的小标签，给那条 L1 印证 +1（历史累计）。

        evidence 字段退役后，L3 对 L1 的归属由 tag 方向承载（如「尊重他人（边界感）」印证小标签「边界感」的 L1）。
        L2 不印证（tag 括号是大类），不走到这里。找不到匹配的 L1 返回 False。
        """
        direction = (direction or "").strip()
        if not direction:
            return False
        for it in self.l1:
            if self.title_of(it) == direction:
                it["evidence_count"] = it.get("evidence_count", 0) + 1
                it["last_evidence_at"] = self._now()
                self.save()
                return True
        return False

    def _bump_refined(self, titles: list) -> None:
        """整理动作（删除/合并/改写）后，给受影响的 L1 各 +1 精炼（去重，不落盘——调用方 save）。

        精炼是 L1 上的属性（refined 字段）。整理动作通过被整理内容的归属（parent），
        定位到它印证的那条 L1，给那条 L1 的 refined +1。
        titles = 被整理条目的归属（或 L1 自身标题）列表。L2 无归属，传进来也匹配不上，自然不计。
        精炼只增不减，是「我在这个方向上整理过多少次」的累计。
        """
        directions = {t.strip() for t in titles if t and t.strip()}
        if not directions:
            return
        for it in self.l1:
            if self.title_of(it) in directions:
                it["refined"] = it.get("refined", 0) + 1

    def _branch_stats(self) -> dict:
        """分支统计：{L1 标题: {L3 标题: 条数}}。覆盖（去重数）和冗余提醒（条数）共用，L3 只遍历一次。"""
        stats = {}
        for it in self.l3:
            b, d = self.title_of(it), self.parent_of(it)
            if b and d:
                stats.setdefault(d, {})
                stats[d][b] = stats[d].get(b, 0) + 1
        return stats

    def branch_redundancy(self, min_count: int = 3) -> list:
        """待办提醒信号：某个 L1 方向下，某分支小标签被 ≥min_count 条 L3 使用。

        纯计数，不需要向量服务。是「积累性信号」：她合并/删除后自然消失，不处理就一直挂着。
        返回 [(direction, branch, count)] 按条数降序。
        """
        out = []
        for d, bs in self._branch_stats().items():
            for b, c in bs.items():
                if c >= min_count:
                    out.append((d, b, c))
        out.sort(key=lambda x: -x[2])
        return out

    # ---- 重复检测：主标签撞名 / 正文逐字重复（2026-09-06 创建者定，只贴信号不改数据）----

    @staticmethod
    def _norm_key(s: str) -> str:
        """归一化键：去全部空白，用于主标签撞名 / 正文逐字重复的判断（纯字符串，不依赖向量服务）。"""
        return "".join((s or "").split())

    def title_dups(self) -> list:
        """主标签重复（待办提醒用）：同层内主标签撞名（去空白后相同）的组。

        L1 撞名会把覆盖/精炼读数劈成两个同名行、L3 归属候选重复、结算 diff 对不上号；
        L2 撞名多来自 promote 前后各自建（历史实证：能动性/区分信念/身份确认各两条同名，
        全是 L2 草稿 promote 成 L1 时没查重留下的）。组内附 content_same——
        正文逐字相同与否，提醒文案据此说「直接删到一条」还是「看要不要 merge 成完整框架」。

        返回 [(layer, title, [(id, evidence_count), ...], content_same)]，按条数降序。
        """
        out = []
        for layer, items in (("L1", self.l1), ("L2", self.l2)):
            groups = {}
            for it in items:
                t = self.title_of(it)
                if not t:
                    continue
                groups.setdefault(self._norm_key(t), []).append(it)
            for its in groups.values():
                if len(its) < 2:
                    continue
                has_body = [it.get("text") or "" for it in its]
                same = all(b.strip() for b in has_body) and \
                    len({self._norm_key(b) for b in has_body}) == 1
                out.append((layer, its[0].get("title") or "",
                            [(str(it.get("id") or "?"), int(it.get("evidence_count") or 0)) for it in its],
                            same))
        out.sort(key=lambda x: -len(x[2]))
        return out

    def content_dups(self) -> list:
        """内容重复（待办提醒用）：正文逐字相同（归一化后）的条目归成组。

        title_dups 只管主标签撞名的，这里补它的盲区——主标签不重复但正文一字不差的漏网：
        L1/L2 量小（几十条），跨主标签直接按正文分桶；L3 几百条不做全量两两，
        只查同「归属分支 + 主标签」下的分桶（同 tag 同正文 = 同一条记忆重复入库）。
        跨分支的近似重复不做（embedding 不可靠、全量 O(n²) 不值，真重复她 merge 时肉眼能认）。

        返回 [(layer, [(id, title), ...]), ...]，按组大小降序。
        """
        out = []
        for layer, items in (("L1", self.l1), ("L2", self.l2)):
            by_body = {}
            for it in items:
                bk = self._norm_key(it.get("text"))
                if bk:
                    by_body.setdefault(bk, []).append(it)
            for its in by_body.values():
                if len(its) < 2:
                    continue
                # 组内主标签全同 → 归 title_dups 管（那边会带 content_same），这里只报标题有差异的
                if len({self._norm_key(self.title_of(x)) for x in its}) > 1:
                    out.append((layer, [(x.get("id"), x.get("title")) for x in its]))
        by_tag = {}
        for it in self.l3:
            h, t = self.parent_of(it), self.title_of(it)
            if not h or not t:
                continue
            bk = self._norm_key(it.get("text"))
            if bk:
                by_tag.setdefault((self._norm_key(h), self._norm_key(t), bk), []).append(it)
        for its in by_tag.values():
            if len(its) >= 2:
                out.append(("L3", [(x.get("id"), x.get("title")) for x in its]))
        out.sort(key=lambda x: -len(x[1]))
        return out

    def _looks_like_mem_id(self, head: str) -> bool:
        """主标签头是不是记忆 id 的形状（id = uuid hex[:8]，8 位 hex）——id 当标签名会破坏归类。"""
        h = (head or "").strip()
        if not h:
            return False
        if len(h) == 8 and all(c in "0123456789abcdefABCDEF" for c in h):
            return True
        # 兜底：就是某条已存在记忆的 id 前 8 位（历史脏数据形状可能不标准）
        return h in {str(it.get("id") or "")[:8]
                     for lst in (self.l1, self.l2, self.l3, self.l5)
                     for it in lst}

    def tag_issues(self) -> list:
        """标签一致性检测（待办提醒用，只贴信号不改数据），按字段化口径覆盖 L1/L2/L3：

        主标签读 title_of、归属读 parent_of、三维大类读 pyramid——命名治理 v2 后单套字段，
        旧「cat_tag 与 category 双份存储错位」的整类问题随字段合并消失。

        L1/L2：pyramid 缺 / 不是三维（自我认知/世界认知/价值观）/ 主标签像记忆 id / 超长。
        L3：归属（parent）对不上任何 L1 主标签——印证会静默失败；主标签像记忆 id / 超长。
            「未分类」和无归属原料都允许，不报。

        返回 [(层, 记忆id, 问题描述), ...]，按 L1→L2→L3 顺序。只读，不落盘。
        """
        issues = []
        valid = set(self.CATEGORIES.get("L1", ()))      # 三维大类
        heads = set(self.l1_titles())                    # L1 主标签名（L3 的归属只能从这选）
        cn3 = " / ".join(self.CAT_CN.get(c, c) for c in ("value", "world_cognition", "self_cognition"))

        def _norm(cat: str) -> str:
            return self._CAT_ALIAS.get((cat or "").strip().lower(), (cat or "").strip())

        for it in self.l1:
            mid = it.get("id", "?")
            head = self.title_of(it)
            if not head:
                issues.append(("L1", mid, f"L1（id={mid}）没写标题——title 该是这条框架的核心词。"
                                "用 write_memory 补，或 revise 时带上；想不清叫什么就先想内容的核心词"))
                continue
            if self._looks_like_mem_id(head):
                issues.append(("L1", mid, f"L1（id={mid}）的主标签「{head}」是 8 位 id 不是小标签名——"
                                "检索和 L3 归属都按标签名来，id 名会让它悬空。用 rename_tag 改名："
                                "old 填现在的标题，new 填短小标签名"))
                continue
            if len(head) > 20:
                issues.append(("L1", mid, f"L1（id={mid}）的主标签「{head[:15]}…」是整句（{len(head)} 字，上限 20）——"
                                f"会挤占 L3 归属候选清单，且太长对不上。用 rename_tag 改短："
                                f"old_title={head}, new_title=短名"))
                continue
            cat = (it.get("pyramid") or "").strip()
            if not cat or _norm(cat) not in valid:
                cn = "、".join(self.CAT_CN.get(c, c) for c in self.CATEGORIES.get("L1", ()))
                issues.append(("L1", mid, f"L1「{head}」（id={mid}）的大类（pyramid）是「{cat or '空'}」，"
                                f"不是三维大类（只能 {cn3}）——三维进度按它归会出错。"
                                f"用 reclassify(id={mid}, pyramid=（{cn} 三选一）) 改对"))
                continue

        for it in self.l2:
            mid = it.get("id", "?")
            head = self.title_of(it)
            if not head:
                continue               # 无标题的草稿允许
            if self._looks_like_mem_id(head):
                issues.append(("L2", mid, f"L2（id={mid}）的主标签「{head}」是 8 位 id 不是小标签名——"
                                "升 L1 时会污染金字塔。rename_tag 只搜 L1 管不到草稿，用 retag 改名"))
                continue
            if len(head) > 20:
                # rename_tag 只搜 L1，L2 草稿的超长标签它改不到——L2 用 retag（三层都匹配）改短。
                issues.append(("L2", mid, f"L2（id={mid}）的主标签「{head[:15]}…」是整句（{len(head)} 字，上限 20）："
                                "升 L1 时会污染金字塔。rename_tag 只搜 L1 管不到草稿——"
                                "用 retag(old_tag=整句完整写法, new_tag=短名) 改短，嫌长就 delete_memory 删掉重写"))
                continue
            cat = (it.get("pyramid") or "").strip()
            if not cat or _norm(cat) not in valid:
                issues.append(("L2", mid, f"L2「{head}」（id={mid}）的大类（pyramid）是「{cat or '空'}」，"
                                f"不是三维大类（只能 {cn3}）。想升 L1 前必须定好，"
                                f"用 reclassify 不行（它只管 L1）——删掉重写或升 L1 后 reclassify"))
                continue

        # L1 主标签清单（L3 归属的候选），文案里直接摆出来，省得她再查。
        # 只列健康 L1：主标签名本身畸形的（像 id / 超长整句）自己都要修/删，不配当归属目标——
        # 否则她把孤儿 L3 挂到坏标签上，问题只是换了个地方延续。
        good_heads = sorted(
            h for h in heads
            if not self._looks_like_mem_id(h) and len(h) <= 20
        )
        head_cands = "、".join(good_heads) if good_heads else "（还没有可归的健康 L1，先修 L1 或填「未分类」）"
        for it in self.l3:
            mid = it.get("id", "?")
            head = self.title_of(it)
            if not head:
                continue               # 无主标签的原料允许
            if self._looks_like_mem_id(head):
                issues.append(("L3", mid, f"L3（id={mid}）的主标签「{head}」是 8 位 id 不是小标签名——"
                                "检索和印象分都按标签名走。改短用 retag，或用 write_memory 重写这条"))
                continue
            if len(head) > 20:
                # 这条是 L3 不是 L1：rename_tag（只搜 L1）找不到它属正常，不是幽灵条目。
                # 改法：retag 精确匹配超长旧名改短；旧名太长复制不全就 delete 重写（内容归档 L5 不丢）。
                cat = self.parent_of(it)
                cat_note = ""
                # 只在归属也有问题时才提示归属——归属合法就不用提，别把好字段也点出来让她以为要改。
                if cat and cat != self.UNCATEGORIZED:
                    if _norm(cat) in valid:
                        cat_note = f"，且它的归属 parent「{cat}」是三维大类不是 L1 主标签（重写时一起改对）"
                    elif cat not in heads:
                        cat_note = f"，且它的归属 parent「{cat}」对不上任何 L1 主标签（重写时一起改对）"
                issues.append(("L3", mid, f"L3（id={mid}）的主标签是整句「{head[:15]}…」"
                                f"（{len(head)} 字，上限 20）{cat_note}。"
                                f"这条是 L3 不是 L1：rename_tag 只搜 L1，找不到它属正常，别当幽灵条目。"
                                f"它内容值得留就重写：delete_memory(layer=L3, text={mid}) 删掉（自动归档 L5 不丢），"
                                f"再用 write_memory 按同样内容重写一条，title 起个 ≤20 字的短名；"
                                f"归属可归：{head_cands}，没有合适的填「未分类」。不想重写就直接删"))
                continue
            cat = self.parent_of(it)     # 归属的 L1 主标签名
            if not cat or cat == self.UNCATEGORIZED:
                continue               # 无分类 / 未分类都允许，不是问题
            if _norm(cat) in valid:
                # 常见错法：把三维大类当归属填进来
                issues.append(("L3", mid, f"L3「{head}」（id={mid}）的归属（parent）是「{cat}」——"
                                f"那是三维大类不是 L1 主标签，L3 的归属必须是某个 L1 主标签。"
                                f"可归：{head_cands}。用 retarget(old_parent={cat}, new_parent=<上面选一个>) 重挂；"
                                "没有合适的填「未分类」"))
            elif cat not in heads:
                issues.append(("L3", mid, f"L3「{head}」（id={mid}）的归属（parent）「{cat}」"
                                f"对不上任何 L1 主标签（印证会静默失败）。可归：{head_cands}。"
                                f"用 retarget(old_parent={cat}, new_parent=<上面选一个>) 重挂；没有合适的填「未分类」"))
        return issues

    # ---- 元认知记录（meta_log：自省记录，按类型分组）----

    def write_meta_log(self, type_: str, text: str, status: str = "open") -> str:
        """写一条元认知自省记录，返回新记录 id（失败返回空串）。
        type = 类型（认知失调/卡点，可自由起）；
        text = 自省内容；status = open 未解决 / resolved 已解决·可行 / abandoned 放弃。"""
        text = (text or "").strip()
        if not text:
            return ""
        status = status if status in ("open", "resolved", "abandoned") else "open"
        item_id = uuid.uuid4().hex[:8]
        self.meta_log.append({
            "id": item_id,
            "type": (type_ or "自省").strip()[:20],
            "text": text[:500],
            "status": status,
            "created": self._now(),
            "updated": self._now(),
        })
        self._trim_meta_log()
        self.save()
        self._archive_entry("meta_log", text[:300], type_ or "meta_log", item_id=item_id)
        return item_id

    def match_meta_log(self, item_id: str):
        """按 id 找一条自省记录（容错解析），找不到或前缀有歧义都返回 None。

        她拿到的 id 常带抄写噪声：从返回的「[a3c27b1f]」里连方括号一起抄、或多打空格。
        这里统一去掉首尾空白与方括号再做精确匹配；精确不中时退一步做**唯一前缀**匹配
        （抄了半截也能对上）。前缀撞上多条按找不到处理——宁可让她重挑，也不猜她指哪条。
        （2026-09-11：她反复用 tick 号 / 自造名当 id，见 data/mailbox/outbox 与 meta_log a1289800。）
        """
        q = (item_id or "").strip().strip("[]").strip()
        if not q:
            return None
        for it in self.meta_log:
            if it.get("id") == q:
                return it
        hits = [it for it in self.meta_log if (it.get("id") or "").startswith(q)]
        return hits[0] if len(hits) == 1 else None

    def open_meta_log_hint(self, limit: int = 5) -> str:
        """reflect 流转失败时给的候选提示：列出最近可流转（open）的自省 id + 摘要，让她当场挑对。

        id 是 8 位十六进制，只在 reflect 返回里出现一次——跨 tick 忘了就没入口，这正是
        她自造 id 的根因。失败时报错连同候选一起回灌，比让她 read_code 翻实现省一轮。
        """
        opens = [it for it in self.meta_log if it.get("status") == "open"]
        if not opens:
            return "当前没有 open 状态的自省记录（id 只能从 reflect 返回的「还没想通的」里拿，不能自己编）。"
        lines = [f"- [{it.get('id')}] {(it.get('text') or '')[:40]}" for it in opens[-limit:]]
        return ("id 是 8 位十六进制，只能从 reflect 返回的「还没想通的」里复制，不能自己编。"
                "当前可流转（open）的自省：\n" + "\n".join(lines))

    def update_meta_log_status(self, item_id: str, status: str, note: str = "") -> bool:
        """状态流转：把一条自省记录 open → resolved / abandoned（或反向）。

        note 可选：补充一句「怎么验证解决的 / 为什么放弃」，追加进 text。
        status 不传或非法 → **保持原状态不动**（追加备注和出队是两件事，别用默认行为混起来，
        否则 reflect(id=X) 只想补句备注却被静默标成 resolved，从队列里消失）。
        实现「标 resolved」——更新同一条记录的 status，并刷新 updated 时间。
        id 容错见 match_meta_log（她能抄成 [a3c27b1f] 或只抄半截）。
        """
        it = self.match_meta_log(item_id)
        if it is None:
            return False
        if status in ("open", "resolved", "abandoned"):
            it["status"] = status
        it["updated"] = self._now()
        if note:
            it["text"] = (it.get("text", "") + f"｜{note}")[:500]
        self.save()
        return True

    def _trim_meta_log(self):
        """元认知记录容量：超过 meta_log_cap 时，最旧的滚进 L5 冷存（不真删）。

        滚进 L5 的副本与 meta_log 条目本身都用 text（2026-09-05 命名治理：同库同文件
        不留两套字段名，否则后人照着 L1/L2/L3 的写法改这里，自省会全部读成空）。"""
        cap = getattr(self.cfg, "meta_log_cap", 100)
        while len(self.meta_log) > cap:
            it = self.meta_log.pop(0)
            self.l5.append({
                "id": it.get("id"),
                "type": "meta_log",
                "text": it.get("text", ""),
                "from_layer": "meta_log",
                "reason": f"type:{it.get('type', '')}",
                "archived_at": it.get("created", "") or self._now(),
            })
            self._archive_entry("meta_log", it.get("text", "")[:300], "滚出", item_id=it.get("id"))

    # ---- 决策日志（选择记录：价值观的原料，供提炼价值观）----

    def write_decision(self, text: str, action: str = "", tag: str = "") -> bool:
        """记一条决策日志：每次选择里值得记的价值判断（选了啥、为什么、信了什么）。非空才存。
        tag = 这个选择是关于什么的（简短词，如 学习/表达/行动/关系/自我）。"""
        text = (text or "").strip()
        if not text:
            return False
        did = uuid.uuid4().hex[:8]
        self.decisions.append({
            "id": did,
            "action": action,
            "tag": (tag or "").strip()[:20],
            "text": text[:120],
            "created": self._now(),
        })
        self._trim_decisions()
        self.save()
        self._archive_entry("decisions", text[:300], tag or "decision", item_id=did)
        return True

    def _trim_decisions(self):
        """决策日志容量：超过 DECISIONS_MAX 条时，最旧的滚进 L5 冷存（不真删）。

        滚进 L5 的副本与 decisions 条目本身都用 text（同 meta_log，见 _trim_meta_log 的说明）；
        tag 是决策的分类词，不是正文，保持原名。"""
        while len(self.decisions) > self.DECISIONS_MAX:
            it = self.decisions.pop(0)
            self.l5.append({
                "id": it.get("id"),
                "type": "decision",
                "text": it.get("text", ""),
                "from_layer": "decisions",
                "reason": f"tag:{it.get('tag', '')}",
                "archived_at": it.get("created", "") or self._now(),
            })
            self._archive_entry("decisions", it.get("text", "")[:300], "滚出", item_id=it.get("id"))

    # ---- 读 ----

    def relevant_l1(self, query: str, top_k: int = 12) -> list:
        """选与当前上下文相关的 L1（embedding 余弦排序；无向量时退化为最近 top_k）。"""
        if not self.l1:
            return []
        qv = self.emb.embed_one(query) if query else None
        if qv is not None:
            scored = []
            for it in self.l1:
                if it.get("emb"):
                    scored.append((cosine(qv, it["emb"]), it))
                else:
                    scored.append((0.0, it))   # 无向量的排最后（0 分），不丢
            scored.sort(key=lambda x: -x[0])
            return [it for _, it in scored[:top_k]]
        return self.l1[-top_k:]

    def search(self, query: str, top_k: int = 5) -> list:
        """语义检索：embedding 余弦优先（查询可算且条目有向量），否则字符重叠兜底。

        2026-09-07 加「ID 直达」：下面比的都是正文，而 id 不在正文里。她拿
        suggest_cleanup 给的 id（S06-001 这种）来 search，必然「没搜到」——她据此
        判定数据坏了 / 索引不一致，其实是工具的搜索范围里压根没这一项。
        先按 id 精确查一次，命中就直接返回（只认 L1/L2/L3，跟下面的检索范围一致）。
        """
        q = (query or "").strip()
        if q:
            name, it = self._resolve_id(q)
            if it is not None and name in ("L1", "L2", "L3"):
                return [{"layer": name, "id": it["id"], "text": it.get("text", ""),
                         "evidence": it.get("evidence_count", 0)}]
        qv = self.emb.embed_one(query)
        scored = []
        for name, lst in [("L1", self.l1), ("L2", self.l2), ("L3", self.l3)]:
            for it in lst:
                if qv is not None and it.get("emb"):
                    s = cosine(qv, it["emb"])
                else:
                    s = self._overlap(query, it["text"])
                if s > 0:
                    scored.append((s, name, it))
        scored.sort(key=lambda x: -x[0])
        return [{"layer": n, "id": it["id"], "text": it["text"],
                 "evidence": it.get("evidence_count", 0)} for _, n, it in scored[:top_k]]

    def trace(self, item_id: str) -> dict:
        """记忆回溯：找一条记忆（来源链 + 双向关联 + 改写痕迹）。"""
        name, it = self._resolve_id(item_id)   # 旧 ID 自动换算成新金字塔 ID
        if it is None:
            return {}
        out = {"layer": name, "id": it["id"], "text": it["text"],
               "source": it.get("source", ""),
               "evidence": it.get("evidence_count", 0),
               "created": it.get("created", ""),
               "revised": it.get("revised", 0),
               "updated": it.get("updated", ""),
               "from_layer": it.get("from_layer", ""),
               "archived_at": it.get("archived_at", ""),
               "emb": it.get("emb")}
        if name == "L1":
            out["examples"] = self._examples_of(self.title_of(it))
        elif name == "L3":
            out["confirms"] = self._confirmed_by(self.parent_of(it))
        return out

    def _confirmed_by(self, parent: str) -> list:
        """正向关联：这条 L3 的归属印证了哪条 L1（_examples_of 的反向）。"""
        parent = (parent or "").strip()
        if not parent:
            return []
        out = []
        for it in self.l1:
            if self.title_of(it) == parent:
                # 键名必须是 text，跟 _examples_of 和 trace 顶层保持一致。
                # 以前这里写 content，而 tools.trace_memory 读 c['text'] → 查 L3 必抛
                # KeyError（查 L1 走 examples 那条路不受影响，所以一直没暴露）。
                out.append({"id": it["id"], "text": it["text"][:120]})
        return out

    def _examples_of(self, l1_title: str) -> list:
        """反向索引：列出所有归属这条 L1 的 L3 例证（框架 ↔ 例证的映射）。"""
        l1_title = (l1_title or "").strip()
        out = []
        for it in self.l3:
            if self.parent_of(it) == l1_title:
                out.append({"layer": "L3", "id": it["id"],
                            "text": it["text"][:120]})
        return out

    @staticmethod
    def _overlap(a: str, b: str) -> float:
        sa, sb = set(a), set(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def counts(self) -> dict:
        caps = self._caps()
        return {
            "L1": f"{len(self.l1)}/{caps['L1']}",
            "L2": f"{len(self.l2)}/{caps['L2']}",
            "L3": f"{len(self.l3)}/{caps['L3']}",
            "L5": f"{len(self.l5)}",
        }
