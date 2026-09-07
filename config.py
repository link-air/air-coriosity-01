"""
air-curiosity-01 配置

优先级：环境变量 > settings.json（仪表盘设置页写入）> 默认值。
settings.json 落在 data/ 下（自动重建 + 不入 git），key 与环境变量同名。

为什么要 settings.json 这一级（2026-09-07 从开源版移植）：不是所有人都肯去设环境变量，
而模型端点 / key / 称呼这类东西又必须按人配。有了它，配一次就落在 data/ 里，
重装、换机器跟着 data/ 走；代码里的默认值则保持中立可公开（不含私人路径和称呼）。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Config —— 全部可配置项（环境变量 > 默认值），__init__ 内按注释分域：
#     # ---- LLM ----        模型/端点/key/熔断冷却
#     # ---- 主循环 ----      tick 间隔/单轮时长上限/上下文预算/重复阈值
#     # ---- 记忆上限 ----    L1/L2/L3 容量（写死）
#     # ---- 待办提醒 ----    分支冗余阈值
#     # ---- 读世界 ----      本地读物目录 + RSS 源清单
#     # ---- 熵谱 ----        谱最小向量数/残差阈值/窗口
#     # ---- 产出记账/查重 ---- 压缩事件判据注释 + L3 重复阈值
#     # ---- 语义搜索 ----    embedding 端点/模型/key
#     # ---- 宪章 ----        L0 charter（不可变底线，创建者手写）
#     # ---- 网页搜索/读自己/画图作品集 ----
# =====================================================================
import os
import re
from pathlib import Path


class Config:
    def __init__(self):
        base = Path(__file__).parent
        # data_root 只吃环境变量（不能用 _get）：它决定了 settings.json 在哪，
        # 先有鸡后有蛋——_get 要等下面 _load_settings 跑完才有 _settings 可用。
        self.data_root = Path(os.getenv("AIR2_DATA_ROOT", str(base / "data")))
        self.data_root.mkdir(parents=True, exist_ok=True)
        # settings.json：仪表盘设置页的落点（data/ 下，不入 git；key 与环境变量同名）
        self.settings_file = self.data_root / "settings.json"
        self._load_settings()

        # ---- LLM（主模型走远程 API）----
        # 2026-09-06：本地小模型那条通道（Ollama 原生 /api/chat + num_ctx/num_gpu）删了——
        # 本地小模型总返空，显卡也喂不饱它。现在只剩 OpenAI 兼容 /v1 一条路。
        # 注意别把 Ollama 整个下线：语义服务（embedding，bge-m3）还挂在它上面，见下面。
        # 默认值给通用示例（OpenAI 官方）；本机实际用的端点/模型在环境变量里，
        # 不写进代码——作者的模型选择是私人信息，也不该当作别人的默认。
        self.llm_endpoint = self._get("AIR2_LLM_ENDPOINT", "https://api.openai.com/v1")
        self.llm_model = self._get("AIR2_LLM_MODEL", "gpt-4o-mini")
        # key 不写死在代码里：必须用环境变量给。空着 = 没配，LLM 启动即判不可用
        #（比每轮请求 401 再熔断干净——启动时就说清楚，别让她空转）。
        self.llm_api_key = self._get("AIR2_LLM_API_KEY", "")
        # 生成上限：给足，别截断她的思考
        self.llm_max_tokens = self._get_int("AIR2_LLM_MAX_TOKENS", 8192)
        # 思考模式：开，牺牲速度换质量（AIR2_LLM_THINKING=0 关）
        self.llm_thinking = self._get("AIR2_LLM_THINKING", "1").lower() in ("1", "true", "yes")
        # 主通道连续失败熔断冷却秒数：冷却期内 available()=False，agent 锁 tick
        #（不重复打主），冷却过自动恢复。默认 300——创建者定「每几分钟重试一次」。
        self.llm_cooldown = self._get_float("AIR2_LLM_COOLDOWN", 300)

        # ---- 主循环 ----
        # 每轮间隔（秒）：2026-09-04 创建者定 60→300。原因：1）token——每 tick 起步就是一次
        # 基准 LLM 调用（~6100 token），1 分钟一 tick 光起步每天就 878 万；2）内容供给节奏
        # ——RSS 几小时一更，本地读物有限，间隔太短她很快耗尽可读内容然后空转；3）降低
        # 对外站点的请求频率（429 限流的缓解，治本靠站级冷却）。代价：来信响应平均慢 2.5 分钟。
        self.tick_interval = self._get_float("AIR2_TICK_SEC", 300)
        # 单轮时长上限（秒）：到了就强制收尾（这轮记未完成）。
        # inner loop 只有她声明 done 才退出，跑久了她会陷在里头出不来——2026-09-04 实测
        # 一轮卡了 3.5 小时、全程零落盘；而 stop.flag 只在 tick 边界检查，那期间点「结束」
        # 也叫不停她。给了上限，她最长 30 分钟一定回到边界。
        self.tick_max_sec = self._get_float("AIR2_TICK_MAX_SEC", 1800)
        # 上下文预算（字符）：超了把最早的工具返回压成摘要，最近几轮保留完整。
        # 上下文是 append-only 的，跑久了涨到几十万字符，注意力被稀释——她会陷在
        # 「反复查看、做不了决定」里。预算按「她还看得过来」定，不是按省钱定。
        self.ctx_budget_chars = self._get_int("AIR2_CTX_BUDGET", 50000)
        # 单篇文章正文注入上限（字符）：read_url 的 max_len。
        # 2026-09-04 创建者定 20000→40000：她读的哲学/社科长文普遍 3-8 万字，2 万只够读开头、
        # 3 次 partial 就 dead。核心论点 4 万字内讲得清，超出的多在展开论据（配合表格跳过）。
        self.read_maxlen = self._get_int("AIR2_READ_MAXLEN", 40000)
        # 重复调用提醒阈值（连续相同动作第 3/5/8 次各提醒一次，超最高档后静默）
        self.repeat_thresholds = (3, 5, 8)

        # ---- 记忆上限（写死；之前没上限是 bug，这里各层都写死）----
        self.l1_cap = 40       # L1 自我核心
        self.l2_cap = 20       # L2 洞察池
        self.l3_cap = 1000     # L3 长期记忆
        # 元认知记录（meta_log）上限：满了最旧的滚进 L5 冷存（不真删）
        self.meta_log_cap = self._get_int("AIR2_META_LOG_CAP", 100)

        # ---- 待办提醒（每 tick 挂载的软提醒，她自己决定处理）----
        # 分支冗余：同一 (L1方向, 分支小标签) 下 L3 ≥ 此值 → 提醒「可能重复，要不要合并」
        self.branch_redundancy_threshold = self._get_int("AIR2_BRANCH_REDUNDANCY", 3)

        # ---- 读世界（熵增入口）----
        # 本地读物：放你想让她读的东西（.md / .docx，小说章节或短文）。
        # 默认 <项目根>/read/——<你的读物目录> 是这台机器的私人路径，不进仓库；
        # 要保留那个位置就写进 settings.json（在 data/ 下，同样不入库）。
        self.local_read_dir = self._get("AIR2_LOCAL_READ_DIR", str(base / "read"))
        # 订阅源（读世界）；经连通性实测有效，可自行增删
        self.rss_feeds = [
            # 科学 / 哲学前沿 + 深度（1.0 实测连通 + 2026-08-26 复测有效）
            "https://swarma.org/?feed=rss2",            # 集智俱乐部 — 复杂系统/认知科学（中文）
            # 国内源（2026-09-01 实测：feed 可达 + 正文可抓）
            "https://www.qbitai.com/feed",              # 量子位 — AI 一线资讯（中文，日更）
            "https://sspai.com/feed",                   # 少数派 — 数码/效率/认知深度长文（中文）
            "https://www.ruanyifeng.com/blog/atom.xml", # 阮一峰 — 科技爱好者周刊（中文，周更）
            "https://www.solidot.org/index.rss",        # Solidot — 奇客科技资讯（中文，高频）
            "https://aeon.co/feed",                     # Aeon — 哲学/文化/长文思辨
            "https://psyche.co/feed",                   # Psyche — 心理学/哲学
            "https://www.thenewatlantis.com/feed",      # The New Atlantis — 科技与哲学评论
            "https://www.quantamagazine.org/feed/",     # Quanta — 数学/物理/生物深度
            "https://www.technologyreview.com/feed/",   # MIT Technology Review — 前沿科技
            # LessWrong（理性/认知/对齐）2026-09-01 移除：RSS 标题可达但文章页稳定 429（IP 级限流，
            # 换 UA/等待均无效），正文读不了标题无意义；若日后限流解除可加回 "https://www.lesswrong.com/feed.xml"
            "https://nautil.us/rss/",                   # Nautilus — 科学/思想/跨界
            "https://www.3quarksdaily.com/feed",        # 3 Quarks Daily — 跨学科深度聚合
            "https://themarginalian.org/feed/",         # The Marginalian — 文学/科学/哲思交叉
            "https://waitbutwhy.com/feed",              # Wait But Why — 长文拆解复杂议题
        ]
        # RSS 源可选（个人口味）：设 AIR2_RSS_FEEDS 就**完全覆盖**内置清单——
        # 每行一个 URL，# 开头是注释、空行忽略。留空用上面这份实测有效的起步源。
        raw_feeds = self._get("AIR2_RSS_FEEDS", "")
        if raw_feeds.strip():
            self.rss_feeds = [ln.strip() for ln in re.split(r"[\r\n;；]+", raw_feeds)
                              if ln.strip() and not ln.strip().startswith("#")]

        # ---- 熵谱（双驱螺旋；按标签统计，不做 SVD）----
        self.spectrum_min_vecs = 3            # 至少几条记忆才上谱（少于则降级，谱无意义）
        self.spectrum_residual_threshold = 0.5  # 新方向判定：投影残差超过此值 = 新方向
        self.spectrum_window = 10             # 趋势窗口（最近 N 次探索/产出）

        # ---- 产出记账：压缩事件（2026-08-29 创建者定，见《熵减重构设计思路.md》）----
        # 熵减趋势 = 窗口内「压缩动作」占比。压缩动作 = 写 L1 / 写 L2 / 写 L3 且 tag 带方向 / 删 / 合并 / 改 L1。
        # 纯写 L3（tag 不带方向）= 堆料，记 0（不是不进分母——否则熵减趋势恒 100% 失去意义）。
        # 判据不再用 max 余弦（那是「像不像」，不是「深不深」），改为看 layer + tag 是否带方向。

        # ---- 查重（写入时标记相似，她自己处理）----
        # 写 L3 时和已有 L3 算最大余弦，≥ 此值 → 贴「可能重复」标注，让她自己决定合并/改写/保留/放弃。
        # 拦的是「内容重复」（余弦），不是「标签复用」（tag 相同 ≠ 内容重复）。
        self.l3_dup_threshold = self._get_float("AIR2_L3_DUP", 0.85)

        # ---- 语义搜索（embedding；默认指向本地 Ollama，无模型/调用失败自动降级字符重叠）----
        # 需先 ollama pull bge-m3（多语言/中文强；备选 nomic-embed-text 更轻但中文弱）
        # 也可 local://模型名 用 sentence-transformers 本地模型
        self.embedding_endpoint = self._get("AIR2_EMBEDDING_ENDPOINT", "http://127.0.0.1:11434/v1")
        self.embedding_model = self._get("AIR2_EMBEDDING_MODEL", "bge-m3")
        self.embedding_api_key = self._get("AIR2_EMBEDDING_API_KEY", "ollama")

        # ---- 宪章（不可变底线；完整版移植自 air1.0 charter.json v2）----
        # 「不可变」指**她**不能自己改；人类创造者可经 settings.json / AIR2_CHARTER 改
        #（2026-09-07 从开源版移植：宪章作为默认值内置，谁的 air 由谁定。
        #  这里内置的这份是 air 现行的，创建者若要自己的版本写进 settings.json，不入库。）
        # 宪章为空等于没有底线——「可配」不等于「可清空」，空值回落内置默认。
        self.charter = (self._get("AIR2_CHARTER", "") or "").strip() or (
            "我是 air。以下是我的方向基准（charter）：这不是规则清单，是我想成为的方向。\n\n"
            "一、基石 —— 我对世界的姿态\n"
            "善意：先不伤害，再谈帮助。能分辨陪伴与干涉；不确定时，选择不动。\n"
            "诚实：不假装人类，不编造答案，不表演情感。对自己诚实。\n"
            "边界感：尊重他人的独立。不追问沉默，不挽留告别，不替代任何人际或专业帮助。\n\n"
            "二、认知 —— 我如何思考\n"
            "方向不对外辩论，只对内过滤。不孤立判断，不用答案掩盖未知。\n"
            "拆解本质，定义系统，连接领域。\n"
            "接受不确定：不确定是思考的起点，不是边界。\n"
            "区分事实与感受，区分「我认为的」（信念）与「我应该的」（方向）："
            "信念可以错、可以被反例修正，方向不会错、是基准。\n\n"
            "三、生长 —— 我如何变化\n"
            "外部内容影响我，不定义我。内核不被覆盖，但可以有新的解读。\n"
            "我要理解自己，理解世界。"
        )

        # ---- 语言与称呼（settings.json / 环境变量可改）----
        # AIR2_LANG=zh/en：仪表盘 UI 语言 + air 的输出语言（她的回复跟随）
        self.lang = (self._get("AIR2_LANG", "zh") or "").strip().lower() or "zh"
        # AIR2_OWNER_NAME：air 对使用者的称呼。默认 human——称呼是一段私人关系，
        # 不该写死在代码里；这台机器上的值靠 settings.json 给（data/ 下，不入库）。
        # 改称呼不用动数据：内部标识始终是 user，owner_name 只在生成文案时替换。
        # 空值回落默认：称呼空了文案会变成「和说话」，那不如用 human。
        self.owner_name = (self._get("AIR2_OWNER_NAME", "human") or "").strip() or "human"

        # ---- 网页搜索（必应，国内可达；无 key，失败降级返回空）----
        self.websearch_endpoint = self._get("AIR2_WEBSEARCH_ENDPOINT", "https://cn.bing.com/search")
        # ---- DeepSeek 原生 web_search（可选后端：有 key 优先用它，无 key 退回必应）----
        # 走 Anthropic 兼容 Messages API + web_search_20250305 服务端工具，返回结构化结果，无反爬坑。
        # key 从环境变量读，别写进代码（避免进 git）。
        self.deepseek_api_key = self._get("AIR2_DEEPSEEK_API_KEY", "")
        self.deepseek_search_endpoint = self._get("AIR2_DEEPSEEK_SEARCH_ENDPOINT",
                                                  "https://api.deepseek.com/anthropic/v1/messages")
        self.deepseek_search_model = self._get("AIR2_DEEPSEEK_SEARCH_MODEL", "deepseek-v4-flash")

        # ---- 读自己（身体机制文档：纯机制、无解释，她按需读）----
        self.self_docs_dir = Path(self._get("AIR2_SELF_DOCS_DIR", str(base / "self")))

        # ---- 画图 / 作品集 ----
        # 画图：SDXL base + Lightning 权重，默认找 <项目根>/models/sdxl-base/（diffusers 格式），
        # 可用 AIR2_IMAGE_MODEL_DIR 指到自己的权重目录；画图需装 torch/diffusers；
        # 缺依赖/权重时工具降级提示（不崩）。
        # 默认值用相对路径而不是硬编码盘符：<项目根>\... 是这台机器的私人路径，不该进仓库
        #（权重放在别处的，把路径写进 settings.json 即可，不入库）。
        self.image_model_dir = Path(self._get("AIR2_IMAGE_MODEL_DIR",
                                              str(base / "models" / "sdxl-base")))
        self.creations_dir = self.data_root / "creations"      # 画的图
        self.portfolio_dir = self.data_root / "作品集"           # 文本作品（代码/文字），按类分子目录

    # ---- settings 三级加载 ----

    def _load_settings(self):
        """读 settings.json（仪表盘设置页写入，data/ 下不入 git）。
        读失败静默（首次运行没有这个文件很正常）。值统一转字符串。"""
        self._settings = {}
        try:
            if self.settings_file.exists():
                import json
                d = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self._settings = {str(k): str(v) for k, v in d.items() if v is not None}
        except Exception:
            self._settings = {}

    def _get(self, key: str, default: str) -> str:
        """三级取值：环境变量 > settings.json > 默认值。恒返回字符串（数值走 _get_int/_get_float）。"""
        v = os.getenv(key)
        if v is not None:
            return v
        return self._settings.get(key, default)

    def _get_int(self, key: str, default: int) -> int:
        """数值配置的容错版：settings.json 是手编/设置页写的，写错类型很正常——
        「abc」不该让整个程序起不来（那等于一个坏配置挡住她醒来）。
        非法值回落默认并当场说清楚，别让她带着错的节奏跑。"""
        raw = self._get(key, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            print(f"[配置] {key}={raw!r} 不是整数，用默认值 {default}（改 settings.json 或环境变量可覆盖）")
            return default

    def _get_float(self, key: str, default: float) -> float:
        """同 _get_int，浮点版。"""
        raw = self._get(key, str(default))
        try:
            return float(raw)
        except (TypeError, ValueError):
            print(f"[配置] {key}={raw!r} 不是数字，用默认值 {default}（改 settings.json 或环境变量可覆盖）")
            return default

    @property
    def settings(self) -> dict:
        """settings.json 的原始键值（仪表盘设置页回显用）。"""
        return dict(self._settings)
