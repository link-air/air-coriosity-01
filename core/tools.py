"""
工具箱：air 的全部行为入口。

每个工具在 _build_tools() 注册表里声明一条（action 名 + SYSTEM_PROMPT 说明 + handler），
SYSTEM_PROMPT 的「工具」段落和 agent._execute 的分发都由注册表自动生成。
加一个工具 = 在 _build_tools() 里追加一条，不用再手改 agent.py。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜常量区 —— TOOL_GUIDES（失败时的机制精讲库）/ _FAIL_HINTS / 域名小工具
#   段 2｜class Toolbox（本文件主体）—— 装配：注入存档/画图配置 + 建工具注册表
#   段 3｜工具注册表（核心）—— _build_tools()：约 40 个工具，按类分
#        记忆 / 通信与自我 / 读世界 / 产出
#   段 4｜供 agent 生成 prompt / 分发 —— render_prompt / render_schema / kind / guide_for
#   段 5｜信息获取（读）—— read_state 身体状态 / 印象分 / read_self / read_code / 检索
#   段 6｜读世界（熵增入口）—— 本地读物 / RSS / 网页搜索 / 读 URL 正文
#   段 7｜纯工具（写/做）—— 写/删/合并/改写记忆（含查重、结构新奇、进账）
#   段 8｜元认知 / 回溯 —— reflect 自省 / 决策日志 / trace / list_memory
#   段 9｜产出类 —— 下棋 / 文字冒险 / 写代码
#   段 10｜画图 / 作品集
# =====================================================================
import re
from pathlib import Path
from urllib.parse import urlparse


class ToolError(Exception):
    """工具失败：handler 抛它表示「这一步没做成」（参数错 / 环境坏 / 容量限制）。

    对齐 DSH 的 isError 判别联合：失败是工具执行时显式声明的结构化信号，
    不是靠结果字符串事后猜。正常结果（含「没搜到」「是空的」这类空结果）用 return。
    """


# 机制自动兜底（2026-09-03 创建者定）：失败工具的"正确用法 + 照抄示例"精讲库。
# 她反复失败的根因常是"不知道参数怎么填 / 该填哪个字段"，与其让 air read_code/read_self
# 去翻实现，不如失败时直接把正确填法喂给她（agent._settle / fail_note 注入）。
# 只覆盖有 ToolError 失败路径的工具（写/改/标签/读结构类）；纯读世界/玩类不在此列。
# 每条 = 照抄即用的正确调用 + 易错点。字段名与 handler 实读一致。
# {OWNER} 是占位符（2026-09-08）：这里只写「对谁说话」，具体称呼由 guide_for 在
# 运行时用 config.owner_name 替换——称呼是可配的私人信息，不该写死在模块常量里。
TOOL_GUIDES = {
    "read_self": "text 直接填五个词之一：双驱螺旋 / 记忆 / 工具箱 / 短期记忆 / 元认知。"
                 "示例：read_self(工具箱)——别写『帮我读工具箱文档』这类句子。",
    "say_to_human": "text 填要对{OWNER}说的话。示例：say_to_human(卡住了：retag 一直报错)。",
    "read_code": "text 填「路径[:起始行或函数名]」，如 read_code(core/memory.py:retag)。",
    "list_memory": "layer 填 L1 或 L2（它不列 L3——找 L3 用 search_memory 搜）。示例：list_memory(L1)。",
    "search_memory": "text 填 2~6 字的核心关键词，不是一句话。示例：search_memory(人机关系)。"
                     "搜不到就换更短的关键词，别整句搜。",
    "list_taglib": "layer 填 L1/L2/L3（写哪层看哪层），看这层的主标签和分类候选。示例：list_taglib(L3)。",
    "write_memory": "write_memory(layer, text?, title, parent?, pyramid?, link?, note?)："
                    "正文 text 可以不填——约束解码写不了长正文，没填时这一步会放开约束，正文由我按这层标准"
                    "直接写（note 写清我的要点/素材出处；正文是我的表达，没有第二个作者）；"
                    "我填了就直接用我的。title=主标签名（≤20 字）。L3 填 parent=它归属的 L1 主标签名（没有填「未分类」）；"
                    "L1/L2 填 pyramid=三维大类（自我认知/世界认知/价值观）。标签分开填、不写括号，写前先 list_taglib(layer)。"
                    "link=选填，只能连「已存在」的 L1 主标签（≤2 个，从 list_taglib(L1) 里挑）；"
                    "想立的新主题先走 L2 草稿/promote 升 L1，别用它填 link。",
    "retag": "retag(old_title=现在的主标签名, new_title=新标签)：old_title 只填名字不带括号；"
             "new_title 想补归属就写完整形式，如 retag(old_title=善意, new_title=善意（价值观）)。"
             "默认只预览，确认影响面后带 dry_run=false 才真改。",
    "retarget": "retarget(old_parent=现在挂错的分类标签名, new_parent=正确的 L1 主标签名)：把一批 L3 重挂归属。"
                "示例：retarget(old_parent=价值观, new_parent=边界感)。默认只预览，带 dry_run=false 才真改。",
    "rename_tag": "rename_tag(old_title=那条 L1 的完整 tag 写法含括号, new_title=新主标签名)："
                  "old_title 必须含括号（用 list_tags 看准确写法），示例："
                  "rename_tag(old_title=边界感（自我认知）, new_title=边界感)。默认只预览，带 dry_run=false 才真改。",
    "revise_memory": "revise_memory(id, text?, title?, parent?, note?)：id 填要改的那条记忆 id"
                     "（list_memory/search_memory 查）。一次只改一类：改正文就自己把 text 写全；"
                     "或只填 note、不填 title/parent，让系统按这层标准 + 原内容起草正文。"
                     "只想补名字/补归属就填 title（主标签）/ parent（L3 归属的 L1 主标签名）——"
                     "**填了 title/parent 就不再代起草正文，正文原样不动**。示例："
                     "revise_memory(id=V01-003, note=把前提补进条件里，更精炼)。"
                     "L1/L2 换三维大类用 reclassify；旧版自动归档 L5。",
    "merge_memory": "merge_memory(layer, targets, text?, note?)：targets=要合并的记忆 id，逗号隔开，"
                    "如 merge_memory(L3, 1a2b3c,4d5e6f)。合并后的正文可以不填——没填时放开约束，由我按"
                    "这层标准 + 各目标原文直接写（note 写清想怎么归并）；我填了就直接用我的。",
    "delete_memory": "delete_memory(layer, text)：layer 填 L1/L2/L3；text 填记忆 id 或 4 字以上关键词，"
                     "多个用逗号/顿号隔开。示例：delete_memory(L3, X-036)。",
    "promote": "promote(id)：id 填 L2 草稿的 D 开头 id（list_memory(L2) 查）。示例：promote(D07)。",
    "reclassify": "reclassify(id, pyramid)：把 L1 换到另一座金字塔。id 填 L1 的 id，"
                  "pyramid 填三维大类（自我认知/世界认知/价值观）。示例：reclassify(S01, 价值观)。"
                  "默认只预览，带 dry_run=false 才真改。",
    "link_memory": "link_memory(id, link)：给一条 L3 记忆加/删交叉标签。id 填记忆 id，"
                   "link 填 L1 主标签名（不能是它自己的归属分支）。示例：link_memory(V01-007, 边界感)。",
    # 2026-09-04 补：她把「我选这篇是因为…」的思考整段填进 url 字段（一夜 15 次），
    # 底层的 unknown url type 只说「读不了」，她当成网站抓不动，在原地反复转。
    "read_url": "text 只填一条完整链接（http(s):// 开头），不填理由、标题或想法。"
                "示例：read_url(https://example.com/a.html)。"
                "链接从 read_rss / search_web 的返回里整条复制，别自己拼。",
}


# 抓取失败分类 → 给 air 的可自检文案（2026-09-01 读世界重构，见《读世界重构设计稿》6.2）
_FAIL_HINTS = {
    "403": "这个站点挡住了我（反爬），换一个源",
    "429": "这个站点在限流，过会儿再来",
    "404": "链接失效了",
    "ssl": "证书问题（环境），报给{OWNER}",
    "timeout": "超时，可以再试一次",
    "empty": "抓到了但没正文（可能需要 JS 渲染），换源",
    "net": "网络问题",
}

# 双字后缀（.com.cn 这类）：注册域要取三段而不是两段
_TWO_PART_TLD = ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn")


def _is_private_host(host: str) -> bool:
    """这个主机名是不是内网 / 本机 / 云元数据地址（read_url 的 SSRF 黑名单）。

    链接来自模型的输出，不设防的话她能构造 http://127.0.0.1:9878/ 去读仪表盘的
    接口，或者 169.254.169.254 抓云主机的元数据凭证——那些内容会被精读后写进
    上下文和记忆。她读世界，不该读到自家内网。

    两层：字面 IP 直接判；域名解析后逐个判（挡住「域名指向 127.0.0.1」的绕过）。
    局限：不防 DNS rebinding（解析和请求之间 IP 被换掉）。那是主动攻击场景，
    超出这个本地单用户工具的威胁模型，这里挡的是她无意构造出内网地址。
    """
    import ipaddress
    import socket
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return True
    if h in ("localhost", "localhost.localdomain", "::1", "0.0.0.0"):
        return True
    bad = ("is_private", "is_loopback", "is_link_local",
           "is_reserved", "is_unspecified", "is_multicast")
    try:
        ip = ipaddress.ip_address(h)
        return any(getattr(ip, a, False) for a in bad)
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(h, None):
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if any(getattr(ip, a, False) for a in bad):
                return True
    except Exception:
        return True      # 解析不出来就别去碰（也比放行安全）
    return False


def _site_of(url: str) -> str:
    """取站名（注册域近似）：dianda.cqvip.com / lib.cqvip.com → cqvip.com。

    同站限流按「站」算不按完整域名——搜索结果里同一家的链接常散在多个子域上
    （维普就横跨 dianda./lib.），按 netloc 匹配会漏。解析失败退回原文前 40 字，
    宁可误拦一个怪链接也不放行连撞。"""
    try:
        host = urlparse(url).netloc.split("@")[-1].split(":")[0].lower()
        parts = [p for p in host.split(".") if p]
        if len(parts) <= 2:
            return host
        two = ".".join(parts[-2:])
        if two in _TWO_PART_TLD:
            return ".".join(parts[-3:])
        return two
    except Exception:
        return (url or "")[:40]


class Toolbox:
    def __init__(self, agent):
        self.agent = agent
        # 文字冒险存档目录注入（air-curiosity-01 data_root/adventures）
        try:
            from core import text_adventure as T
            T.set_save_dir(str(agent.cfg.data_root / "adventures"))
        except Exception:
            pass
        # 画图配置注入（模型目录 + 出图目录）
        try:
            from core import image_gen as IG
            IG.set_config(str(agent.cfg.image_model_dir), str(agent.cfg.creations_dir))
        except Exception:
            pass
        # 工具注册表：加工具只需在 _build_tools 里追加一条
        self._tools = self._build_tools()

    # ---- 工具注册表（加工具只需在此追加一条）----

    @staticmethod
    def _text(action):
        """取 action 的 text 字段（strip 后）。LLM 可能把 text 输出成数字（如 read_local 的章号），
        先转 str 再 strip，否则 int.strip 直接报错。"""
        v = action.get("text")
        return (str(v) if v is not None else "").strip()

    @classmethod
    def _field(cls, action, *names):
        """按工具说明里的字段名取参：依次取第一个非空的（如 list_memory 的 layer /
        trace_memory 的 id / search_memory 的 query），全空则回退 text 字段。

        为什么需要它（2026-09-02 修）：工具说明写成「list_memory(layer)」「trace_memory(记忆id)」，
        她就会按说明把值填进 layer / id 字段，但旧 handler 只从 text 读——按说明填反而拿空
        （日志实证：她列 L1 却收到「list_memory 只列 L1/L2」，是填的 layer 没被读到）。
        这里让 handler 两种写法都收：说明里的字段名优先，text 兜底。"""
        for n in names:
            v = (action or {}).get(n)
            if v is not None and str(v).strip():
                return str(v).strip()
        return cls._text(action)

    @staticmethod
    def _resolve_tag_args(action, old_aliases=("old_title",),
                          new_aliases=("new_title",)):
        """标签类工具统一参数归一化（声明式 alias + 空校验）。

        2026-09-05 命名治理：别名制退役，每个语义只留一个字段名（old_title/new_title/
        old_parent/new_parent），与 schema 白名单一一对应——多别名兜底就是在教她填错
        （`new` 那次的教训）。旧别名（old/tag/new/裸 new）已随 schema 硬切换移除。

        等价给工具参数建 Pydantic schema：old / new 两个字段各有别名，
        各 handler 不再手写 `a.get("old") or a.get("tag")`——把散落的
        「两种写法都收」收敛成一处声明式逻辑。

        保守原则（不猜意图）：
        - 只按别名**回退**（alias 优先级），不跨语义猜意图；
        - old / new 都空才交给 memory 层抛 ValueError → ToolError，绝不写出脏数据。
        返回的 (old, new) 已 strip，调用方自行做空校验。
        """
        old = new = ""
        for n in old_aliases:
            v = (action or {}).get(n)
            if v is not None and str(v).strip():
                old = str(v).strip()
                break
        for n in new_aliases:
            v = (action or {}).get(n)
            if v is not None and str(v).strip():
                new = str(v).strip()
                break
        return old, new

    @staticmethod
    def _t(desc, handler, kind="tool", requires=None):
        """注册表条目：desc = SYSTEM_PROMPT 说明，handler = 执行函数，kind = 结算分派类别。

        kind 对齐 DSH 的 ToolDefinition（工具自描述其行为语义），让 _settle 按类别喂驱动信号。
        取值：
        - write     写记忆（产出事件：压缩 or 堆料）
        - revise    改写记忆（修正框架 = 压缩，统一结算）
        - organize  整理记忆（删除/合并，直接记 1 压缩）
        - read_world 读世界（RSS/URL/搜索/本地读物，只贴印象分，不入账）
        - play      玩类行为（画画/写代码/下棋/冒险，成功记行为新奇）
        - talk      和创建者说话
        - tool      其他工具（读自己/检索/冒险编排等，不喂驱动）

        requires：必填参数声明（2026-09-05 创建者定），元组套元组——**内层是「任一」、
        外层是「都要」**。即 (("id",), ("text",)) = id 和 text 都得有；
        (("query", "text"),) = query 或 text 有一个就算填了。

        为什么内层要列多个名：handler 普遍用 _field(a, "query") 取值，它落回 text——
        她把值填进 text 同样能跑。只认字段名会把一条能走通的路判成缺参。
        所以内层必须与 handler 的取值别名一致（retag 那组即 _resolve_tag_args 的别名）。

        没声明的工具不校验（无参工具，或空值本身有含义的：set_goal 空=清除方向、
        play_chess 空=开新局、start_adventure/adventure_choose 空=默认项）。
        """
        return (desc, handler, kind, requires)

    def _build_tools(self):
        """工具注册表：{action名: (说明, handler, kind, requires)}，条目结构见 _t。

        SYSTEM_PROMPT 的「工具」段落和 agent._execute 的分发都由它自动生成。
        加一个工具 = 在此追加一条 + 写一个 handler（简单工具用 lambda，
        需要多字段/转换/拆分的用 _h_* 方法）。handler 统一签名 handler(action dict) -> str。
        有必填参数的工具要在 _t 里声明 requires——缺参在解析层拦下并附用法（回灌重问
        一轮纠正），比工具跑起来才报错省事。声明规则见 _t 的 docstring。

        工具名（action）用中性名，不跟称呼走：say_to_human 对谁都一样，
        而「和{称呼}说话」这类文案在运行时取 config.owner_name——
        称呼是私人的、可配的，机械名不是（2026-09-08 定）。
        """
        c = self._text
        t = self._t
        owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
        return {
            # ---- 记忆 ----
            "write_memory": t("write_memory(layer, text?, title, parent?, pyramid?, link?, note?)：把一条理解写进记忆。正文 text 可不填——约束解码写不了长正文，没填时这一步会放开约束，正文由我按这层标准直接写（note 写清我的要点/素材出处）；我填了就直接用我的。title=主标签名（≤20 字）。L3 填 parent=归属的 L1 主标签名（没有填「未分类」）；L1/L2 填 pyramid=三维大类（自我认知/世界认知/价值观）；link=选填 ≤2 个别的 L1 主标签名。写前先 list_taglib(layer) 看候选清单。",
                              self._h_write_memory, "write", ()),
            "search_memory": t("search_memory(query)：跨层搜记忆（L1/L2/L3，语义优先），结果带记忆 id。",
                              lambda a: "搜索记忆结果：\n" + self.search_memory(self._field(a, "query")),
                              "tool", (("query", "text"),)),
            "list_memory": t("list_memory(layer)：列 L1/L2 记忆（带 id）。L3 用 search_memory 搜。layer 填 L1 或 L2。",
                            lambda a: self.list_memory(self._field(a, "layer")),
                            "tool", (("layer", "text"),)),
            "list_tags": t("list_tags()：全库标签视图（L1 主标签 / L3 按归属分组 / L2 草稿本）。看全局分布用；写记忆前按层看候选清单用 list_taglib(layer)。",
                          lambda a: self.agent.memory.list_tags()),
            "retag": t("retag(old_title, new_title, dry_run?)：批量重命名主标签 + 给没归属的 L3 补分类标签。old_title=现在的主标签名（不带括号）；new_title=改后的新标签（要补归属就写「名（归属）」）。默认只预览（它改的是全库），确认后带 dry_run=false。",
                       self._h_retag, "tool",
                       (("old_title",), ("new_title",))),
            "delete_memory": t("delete_memory(layer, text)：删记忆（归档 L5，不真删）。layer 填 L1/L2/L3；text 填记忆 id 或 ≥4 字关键词（多个用逗号/顿号隔开）。删 L1 时名下 L3 自动置「未分类」，不丢。",
                              lambda a: self.delete_memory((a.get("layer") or "").strip().upper(), c(a)), "organize",
                              (("layer",), ("text",))),
            "merge_memory": t("merge_memory(layer, targets, text?, title?, pyramid?, note?)：合并同层几条记忆为一条（旧归档 L5）。targets=要合并的记忆 id，逗号隔开。合并后的正文 text 可不填——没填时放开约束，由我按这层标准 + 各目标原文直接写（note 写清合并时想怎么归并）；我填了就直接用我的。title/pyramid 给合并结果的主标签/大类（L1 合并要传 pyramid）。",
                             lambda a: self.merge_memory((a.get("layer") or "").strip().upper(),
                                                         self._field(a, "targets"),
                                                         (a.get("title") or "").strip(),
                                                         (a.get("pyramid") or "").strip(), a), "organize",
                             (("layer",), ("targets",))),
            "revise_memory": t("revise_memory(id, text?, title?, parent?, note?)：按 id 改一条记忆——换正文 / 补主标签 / 改归属（id、印证次数、引用它的记忆全保留，旧版归档 L5）。一次只改一类：① 改正文——自己把 text 写全，或只填 note、不填 title/parent，由系统按这层记忆标准 + 原内容起草；② 改标签——L3 填 parent（归属的 L1 主标签名，会重挂并给新方向印证 +1），补/改名字填 title（L1/L2 换三维大类用 reclassify，不是 parent）。**填了 title/parent 就不会再代起草正文，正文原样不动**——想同时改正文又想改标签，正文必须自己写进 text。",
                               self._h_revise_memory, "revise",
                               (("id",), ("text", "title", "parent"))),
            "trace_memory": t("trace_memory(id)：记忆回溯——来源 / 改过几次 / 它印证的 L1 / 印证它的 L3。id 用 list_memory 或 search_memory 查。",
                             lambda a: self.trace_memory(self._field(a, "id")),
                             "tool", (("id", "text"),)),
            "list_taglib": t("list_taglib(layer)：看标签库（写哪层看哪层）。layer 填 L1/L2/L3——列出这层已有的主标签，和这层能选的分类标签，直接从里面挑，别凭记忆编。",
                            lambda a: self.agent.memory.list_taglib(self._field(a, "layer").strip().upper()),
                            "tool", (("layer",),)),
            "read_branch": t("read_branch(title)：一次读完一个分支——L1 框架 + 名下全部 L3 例证 + 交叉连入 + 相关草稿。title 填 L1 主标签名。",
                            lambda a: self.agent.memory.read_branch(self._field(a, "title")),
                            "tool", (("title", "text"),)),
            "undo_memory": t("undo_memory()：撤销上一步记忆操作（删除/合并/改写/改名/重挂/升 L1/换金字塔/加删交叉）。能撤什么用 list_undo 看。",
                            lambda a: self.agent.memory.undo(), "organize"),
            "list_undo": t("list_undo()：看最近几步能撤销什么。",
                          lambda a: (self.agent.memory.undoable() or "（现在没有可撤销的操作）")),
            "retarget": t("retarget(old_parent, new_parent, dry_run?)：把一批 L3 从挂错的分类标签重挂到正确的 L1 主标签。old_parent=现在的错误分类标签名；new_parent=正确的 L1 主标签名。默认只预览，带 dry_run=false 才真改。",
                         self._h_retarget, "organize",
                         (("old_parent",), ("new_parent",))),
            "rename_tag": t("rename_tag(old_title, new_title, dry_run?)：强制重命名一条 L1 主标签（按完整写法含括号匹配，专治被写成整句的标签），全库同步分类标签和交叉标签。old_title 填那条 L1 的完整 tag 写法（含括号，用 list_tags 看准确写法）；new_title 填新主标签名。默认只预览，带 dry_run=false 才真改。",
                           self._h_rename_tag, "organize",
                           (("old_title",), ("new_title",))),
            "promote": t("promote(id)：把草稿本（L2，D 开头 id）里成熟的想法升成 L1 框架——自动分配金字塔 ID（S/W/V 前缀），自动查配额和三维下限。id 用 list_memory L2 查。",
                        self._h_promote, "organize", (("id", "text"),)),
            "reclassify": t("reclassify(id, pyramid, dry_run?)：把一条 L1 换到另一座金字塔（ID 前缀自动换、名下 L3 一起换、旧号可换算）。id 填 L1 的 id；pyramid 填目标大类（自我认知/世界认知/价值观）。默认只预览，带 dry_run=false 才真改。",
                           self._h_reclassify, "organize",
                           (("id",), ("pyramid",))),
            "link_memory": t("link_memory(id, link, remove?)：单独给一条 L3 记忆加/删交叉标签。id 填记忆 id；link 填 L1 主标签名（不能是它自己的归属）；remove=true 就删。",
                           self._h_link_memory, "link",
                           (("id",), ("link", "text"))),
            "related": t("related(id)：看一条记忆的主相关链（它印证的 L1 / 印证它的 L3 / 交叉连的分支）。只讲相关不讲形成史。",
                        lambda a: self.agent.memory.related((a.get("id") or "").strip() or self._text(a)),
                        "tool", (("id", "text"),)),
            "suggest_cleanup": t("suggest_cleanup()：清理建议——高度相似的 L3 对、孤儿、未分类、无标题、孤立 L1。只列候选不动手，由我决定。",
                                lambda a: self.agent.memory.suggest_cleanup(), "organize"),
            "find_text": t("find_text(text)：搜记忆正文里手写的引用（≥4 字）——比如旧 id 或旧标签名被写进了正文。系统字段改号自动换算，正文手写的靠它找。",
                          lambda a: self.agent.memory.find_text(self._text(a)),
                          "tool", (("text",),)),
            "reflect": t("reflect(text?, type?, status?, id?)：元认知自省。text 可不填——没填时放开约束，由我按自省标准 + 最近的经历直接写（note 里写清想理清什么 / 触发点是什么）；我填了就直接用我的。不带 id=新写；带 id=给旧记录流转状态（resolved/abandoned/open，备注短，自己写）——id 是 8 位十六进制（如 a3c27b1f），只能从 reflect 返回的「还没想通的」里复制，别拿 tick 号或自造名字，对不上时报错会列出当前可流转的 id。type 自由起。",
                        self._h_reflect, "tool", ()),
            "read_decisions": t("read_decisions()：读决策日志（选择记录），按标签分组，供提炼价值观。",
                               lambda a: self.read_decisions()),
            # ---- 通信 / 自我 ----
            "set_goal": t("set_goal(内容)：设定/修改长期方向（覆盖式，空内容=清除）。别反复改，要改先 reflect。",
                          self._h_set_goal),
            "say_to_human": t(f"say_to_human(text)：和{owner}说话（回复、分享、求助；内容填在 text 字段）。",
                              self._h_say_to_human, "talk", (("text",),)),
            "read_self": t("read_self(主题)：读身体机制文档（双驱螺旋/记忆/工具箱/短期记忆/元认知）。",
                          lambda a: self.read_self(c(a)), "tool", (("text",),)),
            "read_code": t("read_code(路径[:起始行或函数名])：只读看 core/ 下源代码，想深入理解实现时用。工具失败时返回已给正确用法（先照做）；还卡住想看实现再 read_code（如 core/memory.py:retag）。",
                          lambda a: self.read_code(c(a))),
            # ---- 读世界 ----
            # read_local 空参 = 先列书目（2026-09-08 合并 list_local 进来）：
            # 之前她没有具体阅读目标时会反复漏参数（缺 text → 回灌重问，白耗一轮）——
            # 空值本身有含义（看有哪些可读），不再当缺参拦，直接给书目让她挑。
            "read_local": t("read_local(编号或标题关键词)：读本地读物（小说/短文）。不填参数=先列出有哪些可读（未读展开带编号，已读折叠成计数）；填纯数字=小说章号，填文字=标题关键词（未读优先）。读完挑感兴趣的点用自己的话写下来（原文不进上下文），自动标【已读】。",
                           lambda a: self.read_local(c(a)), "read_world"),
            "read_rss": t("read_rss()：读订阅源最新条目——标题 + 印象分（会归到哪个分支）+ 链接；已读的不再列出。感兴趣的就用 read_url 读全文。",
                         lambda a: self.read_rss(), "read_world"),
            "search_web": t("search_web(关键词)：网页搜索，返回标题+摘要+链接+印象分。",
                           lambda a: self.search_web(c(a)), "read_world", (("text",),)),
            "read_url": t("read_url(链接)：读一个链接的正文；读完挑感兴趣的点用自己的话写下来（原文不进上下文，精读提取不是原文搬运），自动记已读。",
                         lambda a: self.read_url(c(a)), "read_world", (("text",),)),
            # ---- 产出 ----
            # kind="play"：玩类行为，成功做一次 = 行为新奇（间隔驱动，久违地做熵增更高），见 _settle
            "play_chess": t("play_chess(走法)：和原始 AI 下棋。还没有进行中的棋局时，空走法=开新局；有棋局时走法填 UCI 走法（如 e2e4）走一步，空走法=让 AI 自己走一步。",
                           lambda a: self.play_chess(c(a)), "play"),
            "list_adventures": t("list_adventures()：列文字冒险剧本。",
                                lambda a: self.list_adventures()),
            "start_adventure": t("start_adventure(剧本id)：开一个文字冒险（空则开第一个）。id 用 list_adventures 查。",
                                lambda a: self.start_adventure(self._field(a, "id")), "play"),
            "adventure_choose": t("adventure_choose(选项)：推进文字冒险（空则自己随机选）。",
                                 lambda a: self.adventure_choose(c(a)), "play"),
            "write_code": t("write_code(想法)：写一段小程序草稿（会存进作品集）。",
                           lambda a: self.write_code(c(a)), "play", (("text",),)),
            "paint": t("paint(描述)：画一张图（SDXL 本地）。",
                      lambda a: self.paint(c(a)), "play", (("text",),)),
            "list_portfolio": t("list_portfolio()：列我的作品集（写过的代码等文本作品）。",
                               lambda a: self.list_portfolio()),
            "read_portfolio": t("read_portfolio(作品名)：读一个作品（名字用 list_portfolio 查）。",
                               lambda a: self.read_portfolio(c(a)), "tool", (("text",),)),
        }

    def _h_retag(self, a):
        """retag(old_title, new_title, dry_run?)：旧主标签名 → 新标签完整形式【dry_run 默认 true 只预览，与 retarget/rename_tag 同口径】（可带方向括号）。

        显式命名 old_title/new_title 是 2026-09-03 引入的（S04 事故：她连续 41 次把
        新标签填进 cat_tag，自己 read_code 看懂了也改不过来——LLM 输出层强习惯）。
        2026-09-05 命名治理硬切换：别名（old/tag/new）随 schema 移除，handler 只认
        old_title/new_title——留着兜底就是在教她填错（new 那次的教训）。
        """
        # 默认试运行（2026-09-03 统一口径）：retag 同样改全库（L1/L2/L3 + 交叉标签 + 印证计数），
        # 风险不低于 rename_tag/retarget，不该默认直接执行——她看完影响面再带 dry_run=false 执行。
        dry = str(a.get("dry_run", "true")).strip().lower() not in ("false", "0", "no", "")
        old_head, new_tag = self._resolve_tag_args(a, ("old_title",), ("new_title",))
        try:
            return self.agent.memory.retag(old_head, new_tag, dry)
        except ValueError as e:
            raise ToolError(str(e))

    def _h_retarget(self, a):
        """retarget(old_parent, new_parent, dry_run?)：old_parent=现在错的分类标签名，
        new_parent=正确的 L1 主标签名，dry_run 默认 True（只预览）。

        2026-09-05 命名治理硬切换：只认 old_parent/new_parent（别名已随 schema 移除）——
        这两个名字自带「挂错的重挂」语义，比通用词更不容易填错。"""
        old, new = self._resolve_tag_args(a, ("old_parent",), ("new_parent",))
        dry = str(a.get("dry_run", "true")).strip().lower() not in ("false", "0", "no", "")
        try:
            return self.agent.memory.retarget(old, new, dry)
        except ValueError as e:
            raise ToolError(str(e))

    def _h_rename_tag(self, a):
        """rename_tag(old_title, new_title, dry_run?)：old_title=那条 L1 现在的完整标签写法
        （含括号，list_tags 看准），new_title=新主标签名（只填主标签部分）；
        dry_run=true 只预览影响面。（字段名是 old_title/new_title，不是 old_tag/new_tag。）"""
        old, new = self._resolve_tag_args(a, ("old_title",), ("new_title",))
        dry = str(a.get("dry_run", "true")).strip().lower() not in ("false", "0", "no", "")
        try:
            return self.agent.memory.rename_tag_force(old, new, dry)
        except ValueError as e:
            raise ToolError(str(e))

    def _h_reclassify(self, a):
        """reclassify：id=L1 的 id，pyramid=目标大类（自我认知/世界认知/价值观），dry_run 默认 true。"""
        mid = (a.get("id") or "").strip()
        new = (a.get("pyramid") or "").strip()
        dry = str(a.get("dry_run", "true")).strip().lower() not in ("false", "0", "no", "")
        if not mid:
            raise ToolError("reclassify 需要 L1 的 id（list_memory L1 查）")
        try:
            return self.agent.memory.reclassify(mid, new, dry)
        except ValueError as e:
            raise ToolError(str(e))

    def _h_promote(self, a):
        """promote：草稿（L2）升 L1。

        ValueError → ToolError 这一步不能省：memory 层抛的是 ValueError，而 _execute
        只把 ToolError 判为失败。不转的话「升不进去」会被当成成功，白记一笔结构新奇
        0.72 + 熵减 1，而且连败检测永远不触发（2026-09-03 修）。
        """
        try:
            return self.agent.memory.promote(self._field(a, "id"))
        except ValueError as e:
            raise ToolError(str(e))

    def _h_link_memory(self, a):
        """link_memory：给一条 L3 记忆加/删交叉标签。同上，ValueError 必须转成 ToolError。

        schema 里 link 是数组（write_memory 一次能连好几个分支），但这个工具按说明
        只连一个主标签名。传了多个就取第一个，并明说其余的没连——静默丢弃更糟。
        """
        raw = a.get("link")
        if isinstance(raw, list):
            extra = [str(x) for x in raw[1:]]
            raw = (raw[0] if raw else "")
        else:
            extra = []
        try:
            out = self.agent.memory.link_memory(
                (a.get("id") or "").strip(),
                (raw or self._text(a) or "").strip(),
                str(a.get("remove", "")).strip().lower() in ("true", "1", "yes"))
        except ValueError as e:
            raise ToolError(str(e))
        if extra:
            out += f"（这次只连了第一个；{'、'.join(extra)} 没连——一次连多个用 write_memory）"
        return out

    def _h_write_memory(self, a):
        layer = (a.get("layer") or "L3").strip().upper()
        # link 到这里一定是数组：validate_action 的 _coerce_str_list 对字符串也按
        # 分隔符切（以前这里还有一层 isinstance(str) 容错，是它被 schema 管起来之前的
        # 遗留，两处做同一件事，留一处就够）。
        raw_link = a.get("link") or []
        title = (a.get("title") or "").strip()
        # 归属按层取字段：L3 填 parent（挂哪条 L1 下），L1/L2 填 pyramid（三维大类）——
        # 两个字段各自语义单一，取代旧 cat_tag 的双义（2026-09-05 命名治理，见 4.1）
        cat = ((a.get("parent") if layer == "L3" else a.get("pyramid")) or "").strip()
        # 兼容旧习惯：主标签里带了括号（她一时改不过来），拆开就算分开填了
        if "（" in title and not cat:
            title, cat = self.agent.memory.parse_tag(title)
        # 把解析后的权威字段写回 action，供 _settle/_book_produce 判定熵减（印证）用——
        # 与 tools.write_memory 的印证口径保持一致：分开填的 title/parent 在 schema 里
        # 可能没带括号，不能靠解析 action 的括号拿到方向。
        a["title"] = title.strip()
        if layer == "L3":
            a["parent"] = cat.strip()
        else:
            a["pyramid"] = cat.strip()
        # 这里必须按关键字传。以前按位置传 6 个实参，而形参是
        # (layer, text, title, parent, pyramid, link, source)——第 5、6 位串了：
        # link 列表落进 pyramid、source 落进 link。后果是写 L1/L2 时三维大类被静默吞掉
        #（pyramid 拿到的是个列表，取值时空 → 恒归默认 self_cognition），
        # 写 L3 时交叉标签变成 source 字符串、source 本身丢失。
        # 改成关键字传参后，形参顺序再怎么调整都不会错位。
        cat_s = cat.strip()
        return self.write_memory(
            layer, self._text(a),
            title=title.strip(),
            parent=cat_s if layer == "L3" else "",
            pyramid="" if layer == "L3" else cat_s,
            link=[str(x).strip() for x in raw_link],
            source=(a.get("source") or "").strip(),
        )

    def _h_say_to_human(self, a):
        text = self._text(a)
        if not text:
            owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
            raise ToolError(f"想和{owner}说话，但没说内容")
        return self.say_to_human(text)

    def _h_set_goal(self, a):
        """设定/修改/清除长期方向：覆盖式写 narrative.direction（空内容 = 清除）。"""
        text = self._text(a)
        old = self.agent.narrative.direction
        self.agent.narrative.direction = text
        self.agent._save_narrative()
        if not text:
            return "已清除长期方向"
        if old and old != text:
            return f"已把长期方向从「{old}」改为「{text}」"
        return f"已设定长期方向：「{text}」"

    # ---- 供 agent 生成 prompt / 分发 ----

    def render_prompt(self) -> str:
        """生成 SYSTEM_PROMPT 的「工具」段落（由注册表自动拼）。"""
        lines = ["工具："]
        for name, entry in self._tools.items():
            lines.append(f"- {entry[0]}")
        return "\n".join(lines)

    def render_schema(self) -> dict:
        """生成 JSON Schema（供 llm.chat_action 的 response_format 用）。

        action enum 从注册表自动生成（加新工具天然同步）；字段只做结构约束，不做语义教学。
        字段清单 = 元字段（done/goal/decision/decision_tag）+ 工具复用字段
        （layer/text/note/targets/source/type/status/id）+ 工具参数白名单
        （title/parent/pyramid/link/old_title/new_title/old_parent/new_parent/query/remove/dry_run）
        ——写哪一层用哪几个，多余字段由各 handler 自己忽略。result 已退役，不纳入。

        字段命名卫生（2026-09-05 起，三刀）：
        1. `content` → `text`：主模型约束解码实测在 `content` 名下长文本（>100 字）几乎必丢
           （revise_memory 改写 200 字 L1 连卡 283 次 + 重启后仍卡；对照实验 content 1/5 vs
           改名 text 4/5 成功落进）。`write_memory` 短内容偶尔能成、长内容必丢——问题在字段名
           不在模型。全字段（schema / 工具说明 / handler / requires）改名 `text`。
           注意：消息结构的 `content`（assistant 消息正文）与 `reasoning_content` 是另一层，
           不受影响；memory.json 存储字段也不动。
        2. `new` 从白名单移除：它语义像「新内容」、从没在工具说明里教过，模型却稳定把改写内容
           填进去（283 次样本实证）。移除后约束解码下 `new` 不存在，「新内容」只能进 text。
        3. 标签字段硬切换（命名治理方案 4.1）：main_tag/cat_tag/cross/branch/category/tag/old_tag/
           new_tag/old/new 全部退役，换成语义单一的 title/parent/pyramid/link/old_title/
           new_title/old_parent/new_parent——`cat_tag` 的双义（L1/L2=三维大类、L3=归属）拆成
           pyramid 和 parent 两个字段，`category` 与 `pyramid` 的双份存储随之消除。
           硬切换不留别名兜底：留着兜底就是在教她填错（`new` 的教训）。填旧名 → 约束解码产不出 /
           json_object 档被剔除 → 缺必填参数 → 报错附用法 → 回灌重问一轮纠正。
        结论：白名单里每个字段名都是给模型的提示词，字段语义模糊或与教学不符就是在教它填错。

        2026-09-05 晚（分段约束，创建者定，原叫三段流水线）：上面第 1 条的结论被推翻——改名 text
        后真实运行仍 10/10 丢正文（t335/t336），且她思考链里正文完整、JSON 里就是消失。真根因：
        约束解码在「动作 JSON 里的长文本字段」上系统性丢失，与字段名无关、与上下文长度正相关。
        治理：write_memory/revise_memory 的 text 不再必填（正文给得出就直接用，给不出走 agent 的
        _compose_write：普通生成起草正文 + 约束解码补标签，见 core/agent.py）——正文从此
        不赌约束解码。note 字段同批新增（她这步的写作要点）。
        """
        return {
            "name": "air_action",
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(self._tools.keys()) + ["nothing"]},
                    "done": {"type": "boolean"},
                    "goal": {"type": "string"},
                    "decision": {"type": "string"},
                    "decision_tag": {"type": "string"},
                    "layer": {"type": "string"},
                    "text": {"type": "string"},
                    "note": {"type": "string"},      # 写记忆的写作要点（正文缺时放开约束按它补写）
                    "source": {"type": "string"},
                    "type": {"type": "string"},
                    "status": {"type": "string"},
                    "id": {"type": "string"},
                    "targets": {"type": "string"},   # merge_memory 要合并的记忆 id（逗号隔开）
                    # 记忆标签四字段（2026-09-05 命名治理：title/parent/pyramid/link 语义单一，
                    # 取代 main_tag/cat_tag/cross/category/tag 的多别名双义体系）
                    "title": {"type": "string"},     # 主标签名（这条记忆叫什么；read_branch 也用它）
                    "parent": {"type": "string"},    # L3 归属的 L1 主标签名
                    "pyramid": {"type": "string"},   # 三维大类（自我认知/世界认知/价值观）
                    # 交叉标签是**数组**（写记忆时一次能连好几个分支）。
                    # 以前声明成 string，模型传 ["边界感","诚实"] 时被 _coerce_str 走
                    # json.dumps 转成 '["边界感", "诚实"]'，handler 再按逗号一切，
                    # 落进记忆库的是 ['["边界感"', '"诚实"]'] 这种脏标签（2026-09-06 修）。
                    "link": {"type": "array"},       # 连向的 L1 主标签名（可多个）
                    "query": {"type": "string"},     # search_memory 的关键词
                    "old_title": {"type": "string"}, # retag / rename_tag：旧主标签名
                    "new_title": {"type": "string"}, # retag / rename_tag：新主标签名
                    "old_parent": {"type": "string"},# retarget：现在挂错的分类标签名
                    "new_parent": {"type": "string"},# retarget：正确的 L1 主标签名
                    "remove": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["action", "done"],
                "additionalProperties": False,
            },
        }

    def kind(self, action: str) -> str:
        """查某工具的结算类别（write/organize/link/read_world/talk/tool），未知返回 tool。

        link 是 2026-09-02 从 organize 里分出来的：连交叉边是往认知结构上加连接
        （扩张），不是整理（压缩），两种账不能混在 organize 里统一记熵减。"""
        entry = self._tools.get(action)
        return entry[2] if entry else "tool"

    def requires_map(self) -> dict:
        """{action: (工具说明, 必填参数组)}——给 core/action_schema 做缺参校验用。

        只收录声明了 requires 的工具（无参工具、空值本身有含义的工具不校验）。
        说明一起带过去：缺参报错里直接附用法，回灌重问一轮就能纠正，
        不用她再 read_self 翻一遍。
        """
        return {name: (entry[0], entry[3])
                for name, entry in self._tools.items() if entry[3]}

    def guide_for(self, action: str) -> str:
        """失败时喂给 air 的机制精讲（正确用法 + 照抄示例，2026-09-03 机制自动兜底）。

        有精讲 → agent 失败提示直接给她用法，省去 read_code/read_self 翻实现；
        无精讲（纯读/玩类不常失败）→ 返回空串，agent 退回旧 read_code 引导。

        文案里的 {OWNER} 在这里换成实际称呼（见 TOOL_GUIDES 的说明）。"""
        g = TOOL_GUIDES.get(action or "", "")
        if not g or "{OWNER}" not in g:
            return g
        owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
        return g.replace("{OWNER}", owner)

    # ---- 信息获取（读）----

    def read_state(self) -> str:
        """读自己：身体状态（双驱螺旋读数 + 记忆容量 + 三维进度），每 tick 注入 prompt 用。"""
        feel = self.agent.drive.feel()
        counts = self.agent.memory.counts()
        lines = [
            f"熵增趋势 {feel['熵增趋势']}",
            f"熵减趋势 {feel['熵减趋势']}",
            f"领悟值 {feel['领悟值']}",
        ]
        lines.append(f"记忆容量 L1 {counts['L1']}，L2 {counts['L2']}，L3 {counts['L3']}")
        # 三维进度：下限是目标不是上限，只报事实（哪一维还差几条）
        fl = getattr(self.agent.memory, "L1_CAT_FLOOR", None)
        if fl:
            parts = []
            for c, floor in fl.items():
                n = sum(1 for it in self.agent.memory.l1 if it.get("pyramid") == c)
                cn = self.agent.memory.CAT_CN.get(c, c)
                parts.append(f"{cn} {n}/{floor}" + (f"（还差 {floor - n}）" if n < floor else ""))
            lines.append("三维：" + " · ".join(parts))
        return "；".join(lines)

    def _novelty_tag(self, nov) -> str:
        """把 content_novelties 的 (v_new, max_sim, vec, ref) 渲染成「新奇 X%」标注。无服务/空库不标。"""
        if not nov:
            return ""
        v = nov[0]
        return f"（新奇 {int(v * 100)}%）"

    def _novelty_fail_note(self) -> str:
        """新奇值没算出来时的一句准话：是这次没测出来，不是内容不够新。

        2026-09-04 加：语义服务在 tick 中途抽一次（一夜 4 次 HTTP 400）时，_text_novelty
        返回 None、_novelty_tag 返回空串，她只看到「已写入 L3」后面什么都没有——
        和「一切正常」长得一样，她接着在没有新奇值的情况下判断「够不够新」。
        探活（agent._embed_halt）保证走到这儿时服务是配好的，所以 nov 为 None
        只可能是这次调用失败，不是没配。
        账本不受影响：nov 为 None 时 L3 只记结构新奇、L2 不记账（失真数据进不去），
        这里补的只是让她看见事实。
        """
        emb = getattr(self.agent.memory, "emb", None)
        if emb is None or not getattr(emb, "available", False):
            return ""      # 压根没配服务：tick 开头探活就挂机了，走不到这里
        return "\n（语义服务这次没算出来，这条没记新奇值——不是内容不够新，是这次没测出来）"

    def _impression(self, texts: list):
        """对一批文本打印象分：返回 [(预期新奇值, 方向名)]，无向量/无谱降级 (None, None)。

        预期新奇值 = 投影残差（预估这条写进去会有多新，0~1）。方向名 = 归入的已有方向
        （新方向为 None）。仅用于给读到的内容贴单条标注（决策用），不喂趋势窗口——
        读世界不进账（2026-08-30 创建者定），熵增账由写记忆时的 v_new（真正的新奇值）喂。

        2026-09-01 创建者定：删掉「预期领悟值」——它 = 1 − 残差，与预期新奇恒等互补、
        零信息量，且方向可能反（把「重复已知」当「高领悟」）。领悟是事后量，事前预测不成立。
        """
        out = []
        for text in texts:
            vec = self.agent.memory.emb.embed_one(text)
            if vec is None:
                out.append((None, None))
                continue
            idx, residual = self.agent.drive.spectrum.classify(vec)
            if residual is None:
                out.append((None, None))
                continue
            if idx is not None:
                cache = self.agent.drive.spectrum._cache
                reps = cache["reps"] if cache else []
                rep = reps[idx] if idx < len(reps) else ""
                out.append((residual, rep))
            else:
                out.append((residual, None))
        return out

    def _impression_tag(self, score) -> str:
        """把印象分 (预期新奇值, 方向名) 渲染成标注。无数据返回空串。

        健康区概念已移除（2026-08-30 创建者定）：只报预期新奇值，不预设「高 = 该停」。
        预期领悟值已删（2026-09-01 创建者定，见 _impression）。
        """
        nov, rep = score
        if nov is None:
            return ""
        pct = int(nov * 100)
        if rep:
            return f"（预期新奇 {pct}%，会归到「{rep}」）"
        return f"（预期新奇 {pct}%，新方向）"

    def _digest(self, text: str) -> str:
        """精读提取（创建者 2026-09-05 定，极简）：读完一遍，挑感兴趣的点用自己的话写下。

        一次无约束 llm.chat。prompt 刻意极简、不加方向/标准注入——让她自己判断带什么走，
        而不是替她引导。原文不进上下文，只留这份自己写下的笔记。空返回 = LLM 不可用或
        没产出（调用方降级提示，不再给原文兜底）。"""
        llm = getattr(self.agent, "llm", None)
        if not (llm and getattr(llm, "ok", False)):
            return ""
        try:
            s = llm.chat([{"role": "user", "content": "文章读完了，看看有没有感兴趣的部分，想一想，"
                                                       "挑几点，用自己的话写下来。\n\n" + (text or "")[:8000]}],
                         max_tokens=400, think=False)
            return (s or "").strip()
        except Exception:
            return ""

    def read_self(self, topic: str) -> str:
        """读自己的身体机制文档（双驱螺旋 / 记忆 / 工具箱 / 短期记忆 / 元认知）。纯机制，无解释。"""
        aliases = {
            "双驱螺旋": "双驱螺旋", "drive": "双驱螺旋", "驱动": "双驱螺旋",
            "记忆": "记忆", "memory": "记忆",
            "工具箱": "工具箱", "tools": "工具箱", "工具": "工具箱",
            "短期记忆": "短期记忆", "short": "短期记忆", "short_term_memory": "短期记忆",
            "元认知": "元认知", "metacognition": "元认知", "自省": "元认知", "reflect": "元认知", "meta_log": "元认知",
        }
        name = aliases.get((topic or "").strip().lower(), "")
        if not name:
            raise ToolError("read_self 主题要填：双驱螺旋 / 记忆 / 工具箱 / 短期记忆 / 元认知")
        p = Path(self.agent.cfg.self_docs_dir) / f"{name}.md"
        if not p.exists():
            raise ToolError(f"没找到机制文档 {name}")
        text = p.read_text(encoding="utf-8")
        # 文档里的称呼写的是 {OWNER} 占位符（文档是静态的、称呼是可配的），
        # 读出来时换成她实际叫的——文档说的「{OWNER}来信」和她嘴里的「创建者来信」
        # 才能对上，不会让她的自我认知和日常称呼脱节。
        owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
        return text.replace("{OWNER}", owner)

    def read_code(self, spec: str) -> str:
        """只读查看自己的源代码（核心代码，不能改）。
        spec = 路径[:起始行] 或 路径:函数名（如 core/tools.py:delete_memory，自动定位到函数定义处）。
        限制在项目核心代码（core/ 与 config.py / main.py / dashboard.py），
        防路径穿越、防读敏感数据文件；无写入口，天然不能改代码。"""
        spec = (spec or "").strip()
        if not spec:
            raise ToolError("read_code 要填文件路径，如 core/tools.py 或 config.py")
        path_s = spec
        start = 1
        keyword = ""   # 按函数/方法名定位（如 core/tools.py:delete_memory）
        if ":" in spec:
            path_s, _, line_s = spec.rpartition(":")
            if line_s.isdigit():
                start = max(1, int(line_s))
            elif line_s:
                keyword = line_s
        root = Path(__file__).resolve().parent.parent          # <项目根>（core/ 的上一级）
        p = (root / path_s).resolve()
        # 防路径穿越：解析后必须在项目根内。用 relative_to 判，不用字符串前缀——
        # startswith 会把同盘的兄弟目录（<项目根>2\...）也算成「在根内」，那是假防线，
        # 留着只会让人误以为它挡住了什么（2026-09-06 删）。
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise ToolError("read_code 只能看自己项目里的代码")
        allowed = (rel.parts and rel.parts[0] in ("core",)) or rel.name in ("config.py", "main.py", "dashboard.py")
        if not allowed or p.suffix != ".py":
            raise ToolError("read_code 只能看 core/ 下的 .py 文件，或 config.py / main.py / dashboard.py")
        if not p.exists():
            raise ToolError(f"没找到 {path_s}，可用路径：core/tools.py、core/agent.py、core/memory.py、core/llm.py、core/drive.py、config.py、main.py")
        lines = p.read_text(encoding="utf-8").splitlines()
        if keyword:
            # 定位到 def <keyword> 所在行（方法名也行，正则匹配 def 行）
            hit = None
            for i, ln in enumerate(lines):
                if ln.lstrip().startswith("def ") and keyword in ln:
                    hit = i + 1
                    break
            if hit is None:
                raise ToolError(f"在 {path_s} 里没找到函数/方法 {keyword}，试试先 read_code({path_s}) 看有哪些 def")
            start = hit
        if start > len(lines):
            raise ToolError(f"{path_s} 总共 {len(lines)} 行，第 {start} 行超出了范围")
        end = min(len(lines), start + 79)   # 每次最多 80 行，防撑爆上下文
        body = "\n".join(f"{i+1:>4} {lines[i]}" for i in range(start - 1, end))
        tail = ""
        if end < len(lines):
            tail = f"\n…（共 {len(lines)} 行，用 {path_s}:{end + 1} 看下一段）"
        loc = f"第 {start}-{end} 行" + (f"，定位到 {keyword}" if keyword else "")
        return f"{rel}（只读，{loc}）：\n{body}{tail}"

    def search_memory(self, query: str) -> str:
        """读记忆：语义余弦优先、字符重叠兜底。返回带记忆 id，方便直接拿去 delete_memory。"""
        hits = self.agent.memory.search(query)
        if not hits:
            return "（没搜到相关记忆）"
        return "\n".join(f"[{h['layer']}][{h['id']}] {h['text']}" for h in hits)

    # ---- 读世界（熵增入口）----

    def list_local(self) -> str:
        """列本地读物（小说章节 + 短文）：只列未读，已读折叠成计数（防 137 篇撑爆上下文）。"""
        items = self.agent.reading.list_all()
        if not items:
            return "（本地读物里还没有内容）"
        novels = [x for x in items if x[0] == "小说"]
        essays = [x for x in items if x[0] == "短文"]
        unread_n = [x for x in novels if not self.agent.reading.is_read(x[3])]
        unread_e = [x for x in essays if not self.agent.reading.is_read(x[3])]
        lines = []
        if unread_n:
            lines.append("小说（未读）：")
            for _k, num, title, _p in unread_n[:40]:
                lines.append(f"- [{num}] {title}")
        if unread_e:
            lines.append("短文（未读）：")
            for _k, num, title, _p in unread_e[:20]:
                lines.append(f"- [{num}] {title}")
        read_n = len(novels) - len(unread_n)
        read_e = len(essays) - len(unread_e)
        note = []
        if read_n:
            note.append(f"小说已读 {read_n} 篇")
        if read_e:
            note.append(f"短文已读 {read_e} 篇")
        if not unread_n and not unread_e:
            tail = "、".join(note) if note else "全部读完"
            return f"本地读物都读过了（{tail}）。read_local 用标题关键词可以重读。"
        header = ("本地读物（未读，已读折叠成计数；read_local 用编号或标题关键词读，"
                  "读完自动标【已读】）：")
        tail = f"\n（{'、'.join(note)}）" if note else ""
        return header + "\n" + "\n".join(lines) + tail

    def read_local(self, query: str) -> str:
        """读一个本地读物（小说章节/短文）。匹配：纯数字 → 小说章号，其次短文编号；
        否则标题关键词（未读优先）。读完给文件加【已读】后缀。"""
        query = (query or "").strip()
        if not query:
            # 空参 = 看有哪些可读（原 list_local 的职责）：
            # 她还没想读什么时，给书目比打回重问更有用。
            return self.list_local()
        items = self.agent.reading.list_all()
        if not items:
            raise ToolError("本地读物里还没有内容")
        if query.isdigit():
            n = int(query)
            for kind, num, title, path in items:
                if kind == "小说" and num == n:
                    return self._read_one(path, title)
            for kind, num, title, path in items:
                if kind == "短文" and num == n:
                    return self._read_one(path, title)
            raise ToolError(f"没找到编号 {n} 的读物，不填参数可先列出有哪些")
        # 关键词：未读优先，其次按顺序
        for kind, num, title, path in items:
            if query in title and not self.agent.reading.is_read(path):
                return self._read_one(path, title)
        for kind, num, title, path in items:
            if query in title:
                return self._read_one(path, title)
        raise ToolError(f"没找到标题含「{query}」的读物，不填参数可先列出有哪些")

    def _read_one(self, path, title: str) -> str:
        """读一个文件：印象分 + 已读标记 + 挂正在读 + 精读提取（原文不留在上下文）。
        rename 失败降级提示不崩；精读失败降级提示（本地文件还在，可再读）。"""
        text = self.agent.reading.read(path)
        if not text.strip():
            raise ToolError(f"《{title}》读出来是空的")
        score = self._impression([text[:8000]])[0]
        tag = self._impression_tag(score)
        marked = self.agent.reading.mark_read(path)
        mark_note = "" if marked else "\n（读完了，但没标上【已读】：文件可能被占用或只读）"
        # 只挂「正在读哪章」的指针，不存全文——精读提取后原文不留在任何地方（创建者 2026-09-05 定）
        self.agent.current_reading = {"title": title}
        notes = self._digest(text)
        if notes:
            return f"《{title}》{tag}{mark_note}\n\n{notes}"
        return f"《{title}》{tag}{mark_note}（读完了，但当前没法精读——本地文件还在，需要时再读）"

    def read_rss(self) -> str:
        """读订阅源最新条目 + 印象分（预期新奇 + 归入方向）。已读的（done/dead）不再列出；
        partial/失败过的列出但标注状态；失败在退避期内的跳过。抓不到的源在末尾报出来。"""
        from urllib.parse import urlparse
        pairs, failed = self.agent.rss.fetch_all(limit_per_source=2)
        if not pairs and not failed:
            raise ToolError("订阅源都没抓到，可能是网络问题")
        rl = self.agent.readlog
        tick = getattr(self.agent.cfg, "tick_interval", 60)
        entries = []   # (title, summary, link, state, e)
        for _src, items in pairs:
            for it in items:
                title = it.get("title", "")
                link = it.get("link", "")
                if not title:
                    continue
                e = rl.get(link)
                state = e.get("state", "new") if e else "new"
                if state in ("done", "dead"):
                    continue
                if e and not rl.due(link, tick):
                    continue
                entries.append((title, it.get("summary", ""), link, state, e))
        # 抓不到的源：报事实，不摘除（换不换源由她定）
        failed_note = ""
        if failed:
            hosts = "、".join(f"{urlparse(u).netloc or u}（{k}）" for u, k in failed)
            failed_note = f"（这几个源没抓到：{hosts}）"
        if not entries:
            msg = "订阅源没有新条目（都读过，或都还在失败退避期）"
            return msg + (f"\n{failed_note}" if failed_note else "")
        # 印象分：title + summary 一起算（只看标题时短文本 embedding 区分度差）
        scores = self._impression([f"{t} {s}" for t, s, _, _, _ in entries])
        lines = []
        for (title, summary, link, state, e), score in zip(entries, scores):
            tag = self._impression_tag(score)
            line = f"- 【{title}】{tag}"
            if state == "partial" and e:
                line += f"（上次只读到 {e.get('chars', 0)} 字）"
            elif state == "new" and e and e.get("fail_count", 0) > 0:
                line += f"（上次失败：{e.get('last_kind', '')}）"
            if link:
                line += f"\n  {link}"
            lines.append(line)
        out = "订阅源（新条目 + 印象分，已读的不再列出）：\n" + "\n".join(lines)
        return out + (f"\n\n{failed_note}" if failed_note else "")

    def search_web(self, query: str, limit: int = 5) -> str:
        """网页搜索（有 DeepSeek key 优先原生 web_search，失败/无 key 退回必应）。返回标题/摘要/链接，每条附印象分。"""
        from core import websearch
        results = None
        key = getattr(self.agent.cfg, "deepseek_api_key", "")
        if key:
            # 透传 endpoint/model：之前没透传，导致 config 里的 AIR2_DEEPSEEK_SEARCH_* 完全不生效
            results = websearch.search_deepseek(
                query, key, limit,
                endpoint=getattr(self.agent.cfg, "deepseek_search_endpoint", ""),
                model=getattr(self.agent.cfg, "deepseek_search_model", ""),
            )
        if not results:
            results = websearch.search(query, limit, self.agent.cfg.websearch_endpoint)
        if not results:
            return "（没搜到，换个关键词试试）"   # 空结果是正常结果，不算失败（工具箱.md 约定）
        rl = self.agent.readlog
        texts = [f"{r['title']} {r['snippet']}" for r in results]
        scores = self._impression(texts)
        lines = []
        for i, r in enumerate(results):
            tag = self._impression_tag(scores[i] if scores else None)
            e = rl.get(r["link"])
            seen = "（已读）" if e and e.get("state") in ("done", "partial") else ""
            lines.append(f"- 【{r['title']}】{r['snippet'][:80]}{tag}{seen}\n  {r['link']}")
        return "\n".join(lines)

    @staticmethod
    def _normalize_url(raw: str) -> tuple:
        """整理 read_url 的参数 → 能打开的链接。返回 (url, 报错)；url 为 None = 这条没法用。

        2026-09-04 加：她把自己「我选这篇是因为…」的思考整段填进 url 字段，urlopen
        只回一句 unknown url type，她当成网站抓不动，在同一个方向上连试三次。
        问题不在她不努力，在反馈指错了方向——这里把话说准：哪个字段、该怎么填。
        只做零歧义的机械补全（缺 scheme 的域名补 https://，没有第二种解释）；
        猜不出来就报错，不替她选源。
        """
        s = (raw or "").strip()
        if s.startswith(("http://", "https://")):
            return s, ""
        # 明显的自然语言：含空格/换行/中文，或长到不可能是一条链接
        if " " in s or "\n" in s or len(s) > 200 or re.search(r"[\u4e00-\u9fff]", s):
            return None, (f"read_url 要填链接（http(s):// 开头），填的是一段文字：{s[:60]}…\n"
                          "链接从 read_rss / search_web 的返回里整条复制，不要填选它的理由。")
        # 像域名但缺 scheme（www.x.com / x.com/path）：补 https://
        if re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/|$)", s):
            return "https://" + s, ""
        return None, f"read_url 要填链接（http(s):// 开头），看不懂这个：{s[:60]}"

    def read_url(self, url: str) -> str:
        """读一个链接的正文并精读提取（挑感兴趣的点写下来），原文不进上下文。
        读完记进已读账本（partial/done/失败分类），失败回传让她自己判断换源/重试/放弃。"""
        from core import webreader
        url = (url or "").strip()
        if not url:
            raise ToolError("read_url 需要链接，如 https://...")
        norm, err = self._normalize_url(url)
        if norm is None:
            raise ToolError(err)
        url = norm
        # SSRF 防护（2026-09-06 加）：链接可能来自模型输出，挡住内网/本机/云元数据。
        # 见 _is_private_host 的说明。
        host = urlparse(url).hostname or ""
        if _is_private_host(host):
            raise ToolError(f"这个地址是内网或本机（{host}），读不了：读世界不该读到自家内网。"
                            "要读就填一个公网站点。")
        # 同站每轮一篇（2026-09-04 创建者定）：她搜到几篇同站论文就连着抓，请求间隔才 1 秒，
        # 学术库按 IP 限流（429）当场翻脸。发起就算占用——失败重试正是撞墙来源之一，
        # 拦在请求发出之前最省。同站下一篇下轮再读（tick 间隔 5 分钟，正好当冷却）。
        dom = _site_of(url)
        if dom in self.agent._read_domains:
            raise ToolError(f"（{dom}）这轮已经读过一篇了，同站连读会触发对方限流。"
                            "同站的其他链接下轮再读；这轮可以换别的站，或先消化已读到的。")
        res = webreader.extract(url,
                                max_len=int(getattr(self.agent.cfg, "read_maxlen", 40000) or 40000))
        self.agent._read_domains.add(dom)
        if not res["ok"]:
            state = self.agent.readlog.record_fail(url, res["kind"])
            note = _FAIL_HINTS.get(res["kind"], "抓取失败")
            if state == "dead":
                note += "（已放弃这条：失败太多次）"
            raise ToolError(f"读不了 {url}：{note}")
        text = res["text"]
        truncated = res["truncated"]
        if truncated:
            self.agent.readlog.record_partial(url, res["chars"])
        else:
            self.agent.readlog.record_done(url, res["chars"])
        score = self._impression([text[:8000]])[0]
        tag = self._impression_tag(score)
        # 精读提取（2026-09-05 创建者定）：原文不进上下文，只留她自己写下的笔记。
        # 不做原文截断兜底；LLM 不可用 → 降级提示（读完了但没法精读，原文在已读账本）。
        notes = self._digest(text)
        if notes:
            return f"《{url}》{tag}\n\n{notes}"
        owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
        return f"《{url}》{tag}（读完了，但当前没法精读——原文没留存，想再看重读这条或告诉{owner}）"

    # ---- 纯工具（写/做）----

    def write_memory(self, layer: str, text: str, title: str = "",
                     parent: str = "", pyramid: str = "",
                     link: list | None = None, source: str = "") -> str:
        """写记忆：自己决定写什么、放哪层、哪类、哪个主标签。超上限返回提醒。source = 信源。

        写 L3 时查重拦截（和已有记忆最大余弦 ≥ 90% → 拒写入，提示改写/合并）。
        印证由归属 parent 承载：写 L3 且归属对上某条 L1 主标签 → 给那条 L1 印证 +1。
        L2 是草稿本、不印证；写 L1 不进内容新奇账、但记结构新奇（新建分支 = 0.72）。
        熵增账：写 L2/L3 成功后用内容新奇或结构新奇取大者喂（读世界不进账）。

        2026-09-05 命名治理：参数 title/parent/pyramid/link 取代 main_tag/cat_tag/cross；
        内部拼「title（归属）」串传给 memory.write（暂存层接口，存储层会拆回
        title + parent/pyramid 字段落库——拼接串只是输入格式，不再是存储格式）。
        """
        layer = layer.upper()
        if layer not in ("L1", "L2", "L3"):
            raise ToolError("layer 要填 L1/L2/L3")   # 非法层先拦下，别让「满了」背锅
        if not text:
            raise ToolError("写记忆没有正文：系统起草没成功，text 也没填上——再发一次这个动作，"
                            "note 里写清要点（正文会重新起草），或把 text 自己填上")
        # 形态/字数校验（2026-09-05 创建者定）：写歪的 L1 只能靠 revise_memory 修，
        # 而那个动作最难走通——入口守住比事后返工便宜。see memory.check_shape。
        shape_err = self.agent.memory.check_shape(layer, text)
        if shape_err:
            raise ToolError(shape_err)
        # 归属取值按层：L3 用 parent，L1/L2 用 pyramid（两个字段语义单一，见 4.1）
        head_s = (title or "").strip()
        cat_s = ((parent if layer == "L3" else pyramid) or "").strip()
        # 标签校验（2026-09-02 加）：主标签/分类标签分开校验，报错必须带候选清单——
        # 光说「填错了」，她还是不知道该填什么。
        tag_err = self.agent.memory.validate_tag(layer, head_s, cat_s, link)
        if tag_err:
            raise ToolError(tag_err)
        # 写前状态算新奇（写后会把自身算进去）；写 L1 不贴新奇也不进账，跳过这次 embed
        nov = self.agent.memory.content_novelties([text]) if layer != "L1" else None
        nov = nov[0] if nov else None
        # 重复拦截（2026-09-02 补，兜底 #4）：相似 ≥90% 拒绝写入——
        # 原来 86%~100% 相似照样进库，L3 越堆越胖、新奇值越刷越低。
        if layer == "L3" and nov and nov[1] is not None and nov[1] >= 0.9 and nov[3]:
            ref = nov[3]
            raise ToolError(
                f"这条和已有的 {ref[0]}[{ref[1]}] 相似 {nov[1]:.0%}，基本是重复的，写进来只会堆冗余。"
                "三选一：① revise_memory 改写那条；② merge_memory 合并过去；③ 换个真正新的角度再写。")
        dup_note = self._dup_note(nov) if layer == "L3" else ""
        # 「主标签（分类标签）」由系统拼——她永远不碰括号（设计稿第三节的承诺）
        full = f"{head_s}（{cat_s}）" if (head_s and cat_s) else head_s
        ok = self.agent.memory.write(layer, text, source, "", full, link)
        if not ok:
            # deny_reason 内部会归一化分类别名，直接传 cat_s（用户填的分类标签）
            raise ToolError(self.agent.memory.deny_reason(layer, cat_s))
        # 新奇值算不出来时补一句准话（2026-09-04）：空串不等于正常，得让她知道是这次没测出来
        tag_note = "" if layer == "L1" else (self._novelty_tag(nov) or self._novelty_fail_note())
        if layer == "L3":
            # 熵增账（第 6 步合并）：内容新奇 vs 结构新奇取大者记一笔（一次写入只进一笔账）。
            # 结构新奇 = 交叉标签**每条各自的第一次**连到某分支；跨金字塔信号强于同金字塔，
            # 返回前已打 0.8 折——权重低于内容新奇（创建者拍板的口径）。
            new_item = self.agent.memory._bucket(layer)[-1] if self.agent.memory._bucket(layer) else None
            struct = self._struct_novelty(new_item)
            base = nov[0] if nov is not None else None
            final = base
            if struct is not None and (base is None or struct > base):
                final = struct
            if final is not None:
                self.agent.drive.record_explore(final)
            # 印证走显式 cat_tag（写入时已校验，到这里基本不会落空）
            parent_name = self.agent.memory.parent_of(new_item) if new_item else ""
            if parent_name and parent_name != self.agent.memory.UNCATEGORIZED:
                if not self.agent.memory.bump_evidence_by_tag(parent_name):
                    tag_note += f"\n（印证的 L1 没找到：归属「{parent_name}」对不上任何一条 L1 主标签）"
            else:
                tag_note += "\n（这条没填分类标签，没印证任何 L1——用 list_taglib L3 看能挂到哪个分支）"
        elif layer == "L2":
            # 写 L2 = 提炼洞察（L1 的候补，不印证），记 v_new
            if nov is not None:
                self.agent.drive.record_explore(nov[0])
        else:
            # 写 L1（第 6 步）：新建分支 = 强结构信号（0.9 × 0.8 折 = 0.72）——
            # 金字塔上多一个节点是认知结构的大事，原来完全不进账，漏了这条最重要的熵增
            self.agent.drive.record_explore(0.72)
        return f"已写入 {layer}{tag_note}{dup_note}"

    def _struct_novelty(self, item) -> float | None:
        """结构新奇（2026-09-02 第 6 步）：**分支之间的边**第一次被建立才有。

        口径（创建者拍板"每条各自的第一次"）：这条记忆把「我的归属分支」和某个交叉
        分支连起来，若库里此前没有别的记忆连过这条边（任一方向），就是一条新边。
        同金字塔新边 0.5、跨金字塔新边 0.7，返回前打 0.8 折（→ 0.4 / 0.56），
        权重低于内容新奇。无交叉标签 / 未归类 → None。"""
        if item is None:
            return None
        mem = self.agent.memory
        cross = mem.links_of(item)
        if not cross:
            return None
        my_cat = mem.parent_of(item)
        if not my_cat or my_cat == mem.UNCATEGORIZED:
            return None
        cat_of = {mem.title_of(x): (x.get("pyramid") or "").strip() for x in mem.l1}
        my_pyramid = cat_of.get(my_cat, "")
        best = None
        for h in cross:
            if h == my_cat or h == mem.UNCATEGORIZED:
                continue
            # 这条边（my_cat ↔ h）之前出现过吗：任何别的 L3 归属其一、连向另一个
            seen = any(
                (mem.parent_of(x) == my_cat and h in mem.links_of(x))
                or (mem.parent_of(x) == h and my_cat in mem.links_of(x))
                for x in mem.l3 if x is not item
            )
            if seen:
                continue
            v = 0.5 if cat_of.get(h, "") == my_pyramid else 0.7
            best = v if best is None else max(best, v)
        if best is None:
            return None
        return round(best * 0.8, 3)

    def _edge_novelty(self, branch: str, item=None) -> float | None:
        """单条边（归属分支 ↔ branch）的结构新奇：库里没别的记忆连过这条边才有值。

        和 _struct_novelty 的区别（2026-09-02 修）：那个遍历整条记忆的**全部**交叉标签，
        语义是「新建条目，这些边都是刚连的」；cross_link 是**增量连一条**，只看这一条。
        沿用 _struct_novelty 会把该记忆已有的旧边重算一遍——她先连 A 再连 B，
        连 B 时 A 又被记一次，同一条边重复进账。

        口径与 _struct_novelty 一致：跨金字塔 0.7、同金字塔 0.5，返回前打 0.8 折。
        """
        if item is None or not branch:
            return None
        mem = self.agent.memory
        my_cat = mem.parent_of(item)
        if not my_cat or my_cat == mem.UNCATEGORIZED or branch == my_cat:
            return None
        cat_of = {mem.title_of(x): (x.get("pyramid") or "").strip() for x in mem.l1}
        my_pyramid = cat_of.get(my_cat, "")
        # 这条边（my_cat ↔ branch）之前别的记忆连过吗（任一方向）
        seen = any(
            (mem.parent_of(x) == my_cat and branch in mem.links_of(x))
            or (mem.parent_of(x) == branch and my_cat in mem.links_of(x))
            for x in mem.l3 if x is not item
        )
        if seen:
            return None
        v = 0.5 if cat_of.get(branch, "") == my_pyramid else 0.7
        return round(v * 0.8, 3)

    def _dup_note(self, nov) -> str:
        """写 L3 的查重标记：复用写记忆时算过的新奇值（nov），不再单独比一遍 L3。

        nov = content_novelties 单条结果 (v_new, 最大余弦, 向量, (层, id))。
        新奇值与查重相似度是同一个余弦的正反两面（v_new = 1 − sim），
        所以这里只补「和谁的 id」，不再报一个与新奇互补的百分比（2026-09-01 创建者定：
        算一次出两个数）。

        拦的是「内容重复」（余弦），不是「标签复用」（tag 相同 ≠ 内容重复）。
        """
        if not nov:
            return ""
        sim, ref = nov[1], nov[3]
        thr = getattr(self.agent.cfg, "l3_dup_threshold", 0.85)
        if sim is not None and sim >= thr and ref and ref[1]:
            return f"\n（查重：和 {ref[0]}[{ref[1]}] 相似 {sim:.0%}，可能重复——合并/改写/保留/放弃，自己决定）"
        return ""

    @staticmethod
    def _split_targets(text: str) -> list:
        """把目标串拆成列表：兼容中文标点（，、；）和英文标点（, ;）及空格分隔。"""
        return [t for t in (text or "").replace("，", " ").replace("、", " ").replace(",", " ")
                .replace("；", " ").replace(";", " ").split() if t]

    def delete_memory(self, layer: str, text: str) -> str:
        """批量删记忆：layer 必填（L1/L2/L3）；text 可给多个目标（逗号/顿号/空格分隔），
        每个先试精确 id、再试内容子串匹配。"""
        layer = (layer or "").strip().upper()
        if layer not in ("L1", "L2", "L3"):
            raise ToolError("delete_memory 需要 layer（L1/L2/L3）+ text（记忆 id 或关键词）。"
                            "layer 漏填会报「没找到」——那是指定层的问题，不是记忆不存在")
        targets = self._split_targets(text)
        if not targets:
            raise ToolError("要删的目标是空的：给记忆 id，或用内容关键词匹配；多个用逗号隔开")
        res = self.agent.memory.delete_many(layer, targets)
        if res["deleted"]:
            msg = f"已删除 {len(res['deleted'])} 条（归档到 L5）：" + "、".join(res["deleted"])
            if res["not_found"]:
                msg += "；没找到：" + "、".join(res["not_found"])
            return msg
        raise ToolError("没找到匹配这条的内容：" + "、".join(res["not_found"]))

    def merge_memory(self, layer: str, targets_s: str, tag: str = "",
                     category: str = "", action: dict | None = None) -> str:
        """合并记忆：targets = 要合并的记忆 id/关键词（逗号/顿号/空格隔开），text = 合并后的完整表述。

        归档 N 条旧条目 → 写入 1 条合并结果（净腾出 N-1 个位置）。返回合并结果 + 没找到的。
        tag = 合并结果的主标签（不传的话合并结果没有标签，归不进任何方向）。
        category = 合并结果的大类（不传则归默认；L1 合并尤其要传，否则总归 self_cognition）。

        2026-09-05 晚（分段约束）：targets 拆成独立短字段（合并谁她必须亲自指定）；
        text 只装合并后正文、不再必填（缺了走 agent._compose_write 按段B 起草）。
        兼容旧整体格式：text 带「目标们：合并后内容」时前半仍当 targets。
        """
        layer = (layer or "").strip().upper()
        if layer not in ("L1", "L2", "L3"):
            raise ToolError("layer 要填 L1/L2/L3（带空格/大小写都会被容忍，但必须是这三层之一）")
        text = self._text(action or {})
        targets = self._split_targets(targets_s or "")
        merged = text
        if "：" in (targets_s or ""):
            # 旧整体格式兜底：targets 位实际装的是「目标们：合并后内容」整串
            #（旧写法或 _field 回退把 text 塞进来），前半当 targets（收下不拦）
            old_targets, _, merged = targets_s.partition("：")
            targets = self._split_targets(old_targets)
        if not targets:
            raise ToolError("要合并的目标是空的：targets 填要合并的记忆 id，多个用逗号隔开")
        if not merged.strip():
            raise ToolError("合并后的内容不能为空：系统起草没成功，text 也没填上——"
                            "再发一次这个动作（note 写清想怎么归并，正文会重新起草），或把 text 自己填上")
        res = self.agent.memory.merge(layer, targets, merged.strip(),
                                      category=(category or "").strip(), tag=(tag or "").strip())
        if not res["deleted"]:
            if res.get("tag_error"):
                raise ToolError(res["tag_error"])   # 合并结果的标签没过校验（2026-09-02 补）
            # 一个目标都没合并到 = 没做成（和 delete_memory 对称），抛失败而非返回空提示
            raise ToolError("没有可合并的条目" if not res["not_found"]
                            else "没找到匹配这条的内容：" + "、".join(res["not_found"]))
        msg = [f"已合并 {len(res['deleted'])} 条（归档到 L5）：" + "、".join(res["deleted"])]
        if res["not_found"]:
            msg.append("没找到：" + "、".join(res["not_found"]))
        if res["written"]:
            msg.append("合并结果已写入")
        else:
            msg.append("（合并结果没写进去，但旧条目已归档 L5——内容可能丢了）")
        return "；".join(msg)

    def _h_revise_memory(self, a):
        """改写一条已有记忆：换内容 / 主标签 / 归属，id 和印证次数全保留。

        2026-09-07：正文不再是必填——只补 title 或 parent 也是一次合法改写。
        （那批「有正文没名字」的条目就是靠这条路修的：retag / rename_tag 按标题定位、
        retarget 按归属定位，空值都定位不到，只有按 id 的 revise 能碰到它们。）
        """
        mid = (a.get("id") or "").strip()
        text = self._text(a)
        title = (a.get("title") or "").strip()
        parent = (a.get("parent") or "").strip()
        if not mid:
            raise ToolError("revise_memory 要带 id（改哪一条；用 list_memory 查 id）")
        if not text and not title and not parent:
            raise ToolError("这次改写什么都没填：text（新正文）/ title（主标签）/"
                            "parent（L3 填归属的 L1 主标签名，L1/L2 填三维大类）至少填一个。"
                            "只想补个名字就只填 title，正文不用重写。")
        res = self.agent.memory.revise(mid, text, title=title, parent=parent)
        if not res.get("ok"):
            raise ToolError(res.get("err") or "改写失败")
        # 注意：这里不记账、不重算谱——_settle 按 kind="revise" 统一结算
        # （改写 = 修正框架 = 熵减，记 1；谱由 _settle 重算）。别在这里写记账，否则会和
        # _settle 的结算重复（2026-09-01：旧「桶末尾」假设已随记账重构移除）。
        changed = []
        if text:
            changed.append("正文")
        if title:
            changed.append(f"主标签→「{res.get('title', '')}」")
        if parent:
            changed.append(f"归属→「{res.get('parent', '')}」")
        head = (f"已改写 {res['id']}（{res['layer']}，改了{'、'.join(changed)}；"
                f"id、印证次数、引用它的记忆都没动，旧版已归档 L5）")
        # 只改标签时正文没动，别再打一遍「新内容」——那会让她以为正文也被改了
        return head if not text else head + f"\n新内容：{res['text']}"

    # ---- 元认知 / 回溯 ----

    def reflect(self, text: str, type_: str = "", status: str = "", item_id: str = "") -> str:
        """元认知自省：① 新写一条（不带 id）；② 流转状态（带 id，把旧记录标 resolved/abandoned）。

        type = 类型，status = open/resolved/abandoned，item_id = 要流转状态的旧记录 id。
        写新自省后返回里附同类未解决自省（含 id），供流转状态 / 回看，替代原来的 read_meta_log。
        """
        item_id = (item_id or "").strip()
        if item_id:
            valid = status in ("open", "resolved", "abandoned")
            ok = self.agent.memory.update_meta_log_status(item_id, status, (text or "").strip())
            if not ok:
                raise ToolError(f"没找到自省记录 {item_id}。"
                                + self.agent.memory.open_meta_log_hint())
            if not valid:
                return "（没改状态：status 要填 open/resolved/abandoned，只追加了备注）"
            return f"已把自省 {item_id} 标为 {status}"
        text = (text or "").strip()
        if not text:
            raise ToolError("reflect 需要自省内容：想想根因是什么、涉及哪条框架、前提还成立吗、下次怎么做")
        new_id = self.agent.memory.write_meta_log(type_, text, status)
        if not new_id:
            return "（自省内容为空）"
        # 2026-09-03：新写自省 → 暂存摘要，收尾进短期记忆，下个 tick 显示一次（不会忘了自己想了啥）
        if hasattr(self.agent, "_note_reflection"):
            self.agent._note_reflection(f"（{type_ or '自省'}）{text}")
        hist = self._meta_history(type_, exclude_id=new_id)
        msg = f"已记下自省 {new_id}"
        if hist:
            msg += "\n" + hist
        return msg

    def _meta_history(self, type_: str = "", exclude_id: str = "") -> str:
        """reflect 写自省后附的参考：分两段返回。

        第一段「还没想通的」= 同类 open（可流转状态，带 id）；
        第二段「之前怎么处理的」= 同类已出队（resolved/abandoned）最近 2 条，
        让她记得上次同类是怎么想通的。type 空时对齐写入默认值「自省」（M9）。
        exclude_id 排除刚写的那条，别把自己当成历史反复流转（M8）。
        """
        type_ = (type_ or "自省").strip()
        open_items = [it for it in self.agent.memory.meta_log
                      if it.get("status") == "open"
                      and it.get("type") == type_
                      and it.get("id") != exclude_id]
        done_items = [it for it in self.agent.memory.meta_log
                      if it.get("status") in ("resolved", "abandoned")
                      and it.get("type") == type_]
        if not open_items and not done_items:
            return ""
        out = []
        if open_items:
            out.append("还没想通的（带 id 可直接流转）：")
            for it in open_items[-3:]:
                out.append(f"- [{it['id']}] {it.get('text', '')[:60]}")
        if done_items:
            out.append("这类之前我是这么处理的：")
            for it in done_items[-2:]:
                st = "已想通" if it.get("status") == "resolved" else "已放弃"
                out.append(f"- [{it['id']}]（{st}）{it.get('text', '')[:60]}")
        return "\n".join(out)

    def _h_reflect(self, a):
        return self.reflect(self._text(a), (a.get("type") or "").strip(),
                            (a.get("status") or "").strip(),
                            (a.get("id") or "").strip())

    def read_decisions(self) -> str:
        """读决策日志：按标签分组显示最近 10 条选择记录，供提炼价值观。"""
        dec = self.agent.memory.decisions
        if not dec:
            return "（决策日志还是空的，还没有值得记的价值判断）"
        groups = {}
        for m in dec[-10:]:
            groups.setdefault(m.get("tag") or "未分类", []).append(m.get("text", ""))
        lines = []
        for tag, items in groups.items():
            lines.append(f"【{tag}】")
            for it in items:
                lines.append(f"- {it}")
        return "\n".join(lines)

    def trace_memory(self, item_id: str) -> str:
        """记忆回溯：看一条记忆的形成过程 + 关联（它印证的 L1 / 印证它的 L3 例证）。"""
        t = self.agent.memory.trace(item_id)
        if not t:
            raise ToolError(f"没找到记忆 {item_id}")
        lines = [f"[{t['layer']}] {t['text']}"]
        if t.get("source"):
            lines.append(f"来源：{t['source']}")
        meta = f"印证 {t.get('evidence', 0)} 次 · 建于 {t.get('created', '')}"
        if t.get("revised"):
            meta += f" · 改写过 {t['revised']} 次"
            if t.get("updated"):
                meta += f"（最后 {t['updated']}）"
        lines.append(meta)
        if t.get("from_layer"):
            lines.append(f"（从 {t['from_layer']} 归档 · {t.get('archived_at', '')}）")
        if t.get("confirms"):
            lines.append("它印证的 L1：")
            for c in t["confirms"]:
                lines.append(f"  - [{c['id']}] {c['text']}")
        if t.get("examples"):
            lines.append("印证它的 L3：")
            for ex in t["examples"]:
                lines.append(f"  - [{ex['layer']}] {ex['text']}")
        # 谱位置：这条记忆在认知结构里的位置和影响
        emb = t.get("emb")
        if emb:
            loc = self.agent.drive.spectrum.locate(emb)
            if loc:
                if loc["branch_idx"] is not None:
                    reps = self.agent.drive.spectrum._cache["reps"]
                    rep = reps[loc["branch_idx"]] if loc["branch_idx"] < len(reps) else ""
                    pillar = "，是该方向的支柱" if rep and t["text"].startswith(rep) else ""
                    lines.append(f"谱位置：方向{loc['branch_idx'] + 1}（{rep}）{pillar}")
                else:
                    lines.append(f"谱位置：游离于已有方向之外（残差 {loc['residual']:.2f}）")
        return "\n".join(lines)

    def list_memory(self, layer: str) -> str:
        """列 L1/L2 的记忆（L3/L5 不列——L3 量大了列不完，找 L3 用 search_memory 搜）。"""
        layer = (layer or "").upper()
        bucket = {"L1": self.agent.memory.l1, "L2": self.agent.memory.l2}.get(layer)
        if bucket is None:
            raise ToolError("list_memory 只列 L1/L2；L3 用 search_memory 搜")
        if not bucket:
            return f"（{layer} 是空的）"
        lines = []
        for it in bucket[-15:]:
            ev = it.get("evidence_count", 0)
            cat = it.get("pyramid", "")
            head = f"[{it['id']}]"
            if cat:
                head += f"<{cat}>"
            lines.append(f"{head} {it['text'][:60]}" + (f"（印证{ev}）" if ev else ""))
        return f"{layer}（最近 {len(lines)} 条）：\n" + "\n".join(lines)

    def say_to_human(self, text: str) -> str:
        """和使用者说话（发信 + 追加进对话线程，她记得自己说过什么）。

        工具 action 名是中性 say_to_human，但这里的确认文案说「已发送给{称呼}」——
        称呼来自 config.owner_name，可配（默认 human）。
        """
        self.agent.mailbox.send(text)
        if hasattr(self.agent, "chat"):
            self.agent.chat.append("我", text)
        owner = getattr(getattr(self.agent, "cfg", None), "owner_name", "") or "human"
        return f"已发送给{owner}"

    # ---- 产出类：下棋 / 文字冒险 / 写代码 ----

    @staticmethod
    def _chess_board_view(board) -> str:
        """把棋盘局面 + 合法走法渲染成文本，让她不再盲下。"""
        legal = " ".join(sorted(board.uci(m) for m in board.legal_moves)[:20])
        return f"\n局面（FEN）：{board.fen()}\n合法走法：{legal or '（无）'}"

    def play_chess(self, move: str) -> str:
        """和原始 AI 下棋（negamax 引擎）。move 是 UCI 走法，如 e2e4；空则只开新局。"""
        try:
            import chess
            from core.chess_engine import choose_move
        except ImportError:
            raise ToolError("没装 python-chess，下不了棋")
        board = getattr(self.agent, "_chess_board", None)
        if board is None or board.is_game_over():
            board = chess.Board()
            self.agent._chess_board = board
            return "摆好棋盘，开始一盘新棋。用 play_chess(走法) 走，如 play_chess(e2e4)。" + self._chess_board_view(board)
        if move:
            try:
                board.push(chess.Move.from_uci(move.strip()))
            except Exception as e:
                raise ToolError(f"走法 {move} 不合法：{e}")
        mv = choose_move(board, depth=2)
        if mv is None:
            self.agent._chess_board = None
            return "棋局结束了。"
        san = board.san(mv)
        board.push(mv)
        self.agent._chess_board = board
        return f"我走了 {move or '（未走）'}，原始 AI 回了 {san}。" + self._chess_board_view(board)

    def list_adventures(self) -> str:
        """列文字冒险剧本。"""
        from core import text_adventure as T
        scs = T.list_scenarios()
        if not scs:
            return "（还没有剧本）"
        return "剧本：" + "；".join(f"{s['id']}({s['title']})" for s in scs)

    def start_adventure(self, sid: str) -> str:
        """开一个冒险（sid 用 list_adventures 查；空则开第一个）。有存档先读回续上，无则新开。"""
        from core import text_adventure as T
        scs = T.list_scenarios()
        if not scs:
            raise ToolError("还没有剧本")
        sid = (sid or "").strip() or scs[0]["id"]
        sc = T.get_scenario(sid)
        if sc is None:
            raise ToolError(f"没有剧本 {sid}")
        eng = T.load_latest()   # 先读回最新存档：跨重启继续冒险，不丢进度
        if eng is not None and eng.scenario.id == sid:
            self.agent._adventure = eng
            return f"续上存档《{eng.scenario.title}》：\n{eng.describe()}\n可选项：{eng.choices_text()}"
        eng = T.AdventureEngine(sc)
        self.agent._adventure = eng
        return f"翻开剧本《{sc.title}》：\n{eng.describe()}\n可选项：{eng.choices_text()}"

    def adventure_choose(self, label: str) -> str:
        """推进冒险：label 是选项文字或序号（如「1」）；空则自己随机选一个。"""
        eng = getattr(self.agent, "_adventure", None)
        if eng is None or eng.is_ended():
            raise ToolError("没有进行中的冒险，先 start_adventure")
        if not label:
            choice = eng.auto_choose()
            if choice is None:
                self.agent._adventure = None
                return "这段冒险走不下去了。"
        else:
            label = label.strip()
            # 支持序号选择：传「1」等价于选第一个可选项（之前只做 label 精确匹配，传序号会无效）
            choices = eng.available_choices()
            if label.isdigit() and 1 <= int(label) <= len(choices):
                choice = choices[int(label) - 1]
            else:
                choice = {"label": label}
        res = eng.choose(choice.get("label"))
        if not res.get("ok"):
            raise ToolError(res.get("reason", "无效选择"))
        eng.save()
        text = res.get("text", "")[:200]
        tail = "\n" + res.get("reflection", "") if res.get("ended") and res.get("reflection") else ""
        opts = "" if res.get("ended") else f"\n可选项：{eng.choices_text()}"
        return (text + opts + tail) if text else "在冒险里推进…"

    def write_code(self, idea: str) -> str:
        """写一段小程序草稿（LLM 生成，只输出代码）；产出落盘到作品集/写代码/。"""
        if not idea or not idea.strip():
            raise ToolError("写代码需要 idea，描述要写什么")
        llm = getattr(self.agent, "llm", None)
        if not llm or not getattr(llm, "ok", False):
            raise ToolError("LLM 不可用，写不了代码")
        out = llm.chat([
            {"role": "system", "content": "写一段能跑的小程序草稿，只输出代码，不要解释。"},
            {"role": "user", "content": f"写一个小程序：{idea.strip()}"},
        ], max_tokens=1500)
        if not out:
            raise ToolError("没写出来")
        # 落盘作品集（她的产出积累，供以后读作品集回看）
        try:
            from datetime import datetime
            d = Path(self.agent.cfg.portfolio_dir) / "写代码"
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            (d / f"{ts}.md").write_text(f"想法：{idea.strip()}\n\n代码：\n{out}", encoding="utf-8")
        except Exception:
            pass   # 落盘失败不影响返回
        return out or "（没写出来）"

    # ---- 产出：画图 / 作品集 ----

    def paint(self, description: str) -> str:
        """画一张图（SDXL 本地）。图读不了，只能读画的文字描述——返回路径 + 描述。"""
        description = (description or "").strip()
        if not description:
            raise ToolError("paint 需要描述，我想画什么")
        from core import image_gen as IG
        path = IG.generate(description)
        if path is None:
            raise ToolError(f"画不了：{IG.check_setup()}")
        return f"画好了：{path}（内容：{description[:80]}）——图我看不了，记下描述就行"

    def list_portfolio(self) -> str:
        """列作品集（文本作品，按类）。"""
        root = Path(self.agent.cfg.portfolio_dir)
        if not root.exists():
            return "（作品集还是空的，还没有作品）"
        lines = []
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            files = sorted(sub.glob("*.md"))
            if files:
                lines.append(f"{sub.name}：")
                for f in files[-10:]:
                    lines.append(f"  - {f.stem}")
        if not lines:
            return "（作品集还是空的，还没有作品）"
        return "作品集：\n" + "\n".join(lines)

    def read_portfolio(self, name: str) -> str:
        """读一个文本作品（名字用 list_portfolio 查；模糊匹配文件名）。"""
        name = (name or "").strip()
        if not name:
            raise ToolError("read_portfolio 需要作品名，用 list_portfolio 查")
        root = Path(self.agent.cfg.portfolio_dir)
        if not root.exists():
            raise ToolError("作品集还是空的")
        for p in root.rglob("*.md"):
            if name in p.stem:
                return p.read_text(encoding="utf-8")[:2000]
        raise ToolError(f"没找到作品 {name}，用 list_portfolio 看看有哪些")
