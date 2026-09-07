"""
LLM 客户端（单主通道，OpenAI 兼容 /v1；2026-09-03 起不稳就锁 tick，不做降级空转）。

2026-09-06 删掉了本地小模型那条通道（Ollama 原生 /api/chat）：本地小模型总返空，
创建者的显卡也喂不饱它，留着只是占地方。现在只剩一条——OpenAI 兼容层，主模型走远程 API。

注意别把 Ollama 整个删了：**语义服务（embedding，bge-m3）还挂在 Ollama 上**，
那是 core/embedding.py 的事，跟这里的 LLM 通道是两码事。

不稳就锁 tick（创建者 2026-09-03 定，与 embedding 侧同规格）：
- 主通道空返回/失败自动重试（最多 3 次尝试）。
- 仍失败 → 进熔断冷却（默认 300s，config.llm_cooldown）。冷却期内 available()=False，
  agent 据此原地锁 tick（tick 不推进、不涨计数、不再白打 API），每几分钟试一次。
- 冷却期一过自动恢复尝试主通道，成功了就回到正常。
- 不做降级空转：连不上就停，等服务恢复再继续，别在空转里白涨 tick。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class LLM —— 主模型客户端（单主通道，不稳就锁 tick）：
#   段 1｜对外入口 —— chat / chat_action：空返回自动重试 + 熔断冷却
#       （冷却期 available()=False，agent 据此锁 tick）
#   段 2｜通道分发 —— 单通道，没有分发逻辑了（本地小模型通道已删）
#   段 3｜OpenAI 兼容 /v1/chat/completions —— 唯一的通道
# =====================================================================
import time

from openai import OpenAI
import httpx


class LLM:
    def __init__(self, config):
        self.cfg = config
        self.client = None
        # key 没配要当场说，别等每轮请求 401 才熔断：那时候她已经白转了好几轮，
        # 仪表盘上还只是「活着」，看不出是没配 key。
        if not (config.llm_api_key or "").strip():
            print("[LLM] 没配 API key（AIR2_LLM_API_KEY 为空），她只会锁 tick。")
            self.ok = False
        else:
            try:
                self.client = OpenAI(
                    base_url=config.llm_endpoint,
                    api_key=config.llm_api_key,
                    timeout=httpx.Timeout(300.0, connect=10.0),
                    max_retries=0,
                )
                self.ok = True
            except Exception as e:
                print(f"[LLM] 初始化失败: {e}")
                self.ok = False
        self._cooldown_until = 0.0   # 主通道熔断冷却截止（unix ts）：冷却期内 available()=False，
                                     # agent 锁 tick，等服务恢复自动醒
        # 结构化输出能力位：unknown / json_schema / json_object。
        # 运行时首次探测（json_schema 400「unavailable」→ 降 json_object 并记住），不硬编码。
        self._cap_main = "unknown"

    def _cooldown_secs(self) -> float:
        """熔断冷却时长（默认 300s：创建者定的「每几分钟重试一次」锁 tick 节奏）。"""
        return float(getattr(self.cfg, "llm_cooldown", 300))

    # ---- 对外入口：重试 + 熔断 ----

    def chat(self, messages, max_tokens=None, think=None) -> str:
        """一次对话调用；主通道空返回/失败自动重试（最多 3 次尝试），仍失败返回空串。

        失败会进熔断冷却（冷却期内 available()=False，agent 锁 tick，不再白试）。
        think: None=用 config 默认；False/True 显式覆盖——
        压缩等低阶任务传 think=False，不走思考链，省 token 省延迟。
        """
        if not self.ok or self._in_cooldown():
            return ""
        content, _ = self._retry(self._call_openai, messages, max_tokens, think, "主通道")
        if content:
            self._cooldown_until = 0.0   # 主通了，清冷却
            return content
        # 主连续失败 → 无条件进熔断冷却（主模型抽风期每 tick 白打 API 只会拖垮节奏；
        # 冷却期内 agent 靠 available() 锁 tick，等服务恢复自动醒）
        self._cooldown_until = time.time() + self._cooldown_secs()
        return ""

    def chat_action(self, messages, schema, max_tokens=None, think=None):
        """结构化输出调用（tick 用）：返回 (content, reasoning_content)。

        格式交给 API：json_schema 严格档（真约束解码）；400 不支持才降 json_object
        （只保证合法 JSON，字段由 core/action_schema 兜底补齐）。失败进熔断冷却。
        """
        if not self.ok or self._in_cooldown():
            return "", ""
        content, reasoning = self._action_attempt(messages, max_tokens, think, schema)
        if content:
            self._cooldown_until = 0.0   # 主通了，清冷却
            return content, reasoning
        self._cooldown_until = time.time() + self._cooldown_secs()
        return "", ""

    def _action_attempt(self, messages, max_tokens, think, schema):
        """结构化调用 + 重试 + 能力位降级（json_schema → json_object）。返回 (content, reasoning)。"""
        for attempt in range(3):
            cap = self._cap_main
            # 选档：json_object 档只一个候选；否则先试 json_schema、400 不支持才降 json_object
            fmts = ([{"type": "json_object"}]
                    if cap == "json_object"
                    else [{"type": "json_schema", "json_schema": schema}, {"type": "json_object"}])
            for fmt in fmts:
                try:
                    content, reasoning = self._call_openai(
                        messages, max_tokens, think, self.client, response_format=fmt)
                    if content and content.strip():
                        # 只有真走了 json_schema 档并成功，才把能力位记成 json_schema。
                        # 因 500 / 网络故障临时回退到 json_object 档成功的，一律不记——
                        # 2026-09-03 修：主模型一次 500 后被记成 json_object，之后每轮都只走
                        # 无约束档、action 枚举约束再没生效（日志实证「未知动作：done」），
                        # 且进程不重启就恢复不了。
                        if fmt.get("json_schema"):
                            self._cap_main = "json_schema"
                        return content, reasoning
                except Exception as e:
                    if fmt.get("json_schema") and self._schema_unsupported(e):
                        self._cap_main = "json_object"   # 通道明确不支持：永久降档
                        print("[LLM] 主通道不支持 json_schema，已降为 json_object 档")
                        continue
                    # 500 / 网络 / 超时等服务端故障：本次回退 json_object 档兜底，
                    # 但不改能力位——下次仍先试 json_schema，免得一次抽风把约束解码踢下线
                    print(f"[LLM] 主通道结构化调用失败（服务端故障，不降能力位）: {e}")
        return "", ""

    @staticmethod
    def _schema_unsupported(e) -> bool:
        """判断是不是「API 不支持 json_schema 格式」的错误（400），而非网络/其他错误。"""
        s = str(e).lower()
        return ("response_format" in s and ("unavailable" in s or "not support" in s or "invalid" in s))

    def _in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def available(self) -> bool:
        """当前有没有可用的通道（agent 锁 tick 判断用，2026-09-03 加）。

        - 主通道 init ok 且不在熔断冷却 → 可用（这轮会试它）；
        - 冷却中 / 没配好 → False，agent 原地锁 tick（不推进、不涨计数），每几分钟试一次。
        """
        return self.ok and not self._in_cooldown()

    def _retry(self, fn, messages, max_tokens, think, label):
        """通用重试循环：最多 3 次尝试，仍失败返回 (空串, 空串)。"""
        for attempt in range(3):
            try:
                content, reasoning = fn(messages, max_tokens, think)
            except Exception as e:
                print(f"[LLM] {label} 第 {attempt + 1} 次调用失败: {e}")
                continue
            if content and content.strip():
                return content, reasoning
            print(f"[LLM] {label} 第 {attempt + 1} 次调用返回空，重试")
        return "", ""

    def _use_think(self, think) -> bool:
        return getattr(self.cfg, "llm_thinking", False) if think is None else think

    # ---- OpenAI 兼容 /v1（唯一通道）----

    def _call_openai(self, messages, max_tokens, think, client=None, model="", response_format=None):
        """唯一通道。client 不传就用 self.client——这样它能直接当 _retry 的 fn 用
        （签名 (messages, max_tokens, think) 对得上），不用再包一层适配器。"""
        client = client or self.client
        kwargs = {}
        if self._use_think(think):
            kwargs["extra_body"] = {"think": True}
        if response_format:
            kwargs["response_format"] = response_format
        resp = client.chat.completions.create(
            model=model or self.cfg.llm_model,
            messages=messages,
            # Ollama 0.32.15 兼容层 bug：qwen3 系 + max_tokens<512 → 稳定空输出，下限保护
            max_tokens=max(max_tokens or self.cfg.llm_max_tokens, 512),
            **kwargs,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        # reasoning_content：思考链（开 thinking 时独立返回）。openai 库可能没这个字段，
        # 落到 model_extra 里。多轮回填时 agent 用「reasoning_content」键，与这里一致。
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning is None and getattr(msg, "model_extra", None):
            reasoning = msg.model_extra.get("reasoning_content")
        return content, reasoning or ""
