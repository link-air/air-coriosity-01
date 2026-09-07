"""
冒烟：结构化输出的能力位 + 主通道熔断挂机（回归保护）。

一、能力位（2026-09-03 修的 bug）：主模型一次 500 会把能力位误记成 json_object，
之后每轮都走无 schema 约束的档、action 枚举约束失效，且不重启恢复不了。
正确行为：400「不支持」→ 永久降档；500 / 网络 → 临时回退兜底但**不改能力位**。

二、挂机（2026-09-03 加）：主通道整段抽风（两档全 500）→ 无条件进熔断冷却 →
available()=False → agent 原地挂机（不每 tick 白打 API）→ 冷却过了自动醒。

跑法：python _smoke_capability.py
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows GBK 控制台打不出 ✓/✗
except Exception:
    pass

from core.llm import LLM

FAILED = []


def check(name, cond, extra=""):
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Cfg:
    """最小配置：只为构造 LLM（OpenAI 客户端构造不联网），不发真实请求。"""
    llm_endpoint = "http://127.0.0.1:1/v1"
    llm_api_key = "test"
    llm_model = "test-model"
    llm_max_tokens = 512
    llm_thinking = False
    llm_cooldown = 300.0


SCHEMA = {"name": "air_action", "schema": {"type": "object",
          "properties": {"action": {"type": "string", "enum": ["nothing"]},
                         "done": {"type": "boolean"}},
          "required": ["action", "done"]}}

# _action_attempt 签名（2026-09-05 重构后）：(messages, max_tokens, think, schema)
# ——旧的 fallback 形参已随能力降档重构移除（json_schema 400 才降，500/网络临时回退）。
def attempt(llm):
    return llm._action_attempt([{"role": "user", "content": "x"}], None, None, SCHEMA)


def make_llm(raise_kind):
    """raise_kind:
    '500'          主 json_schema 档 500（json_object 兜底可用）
    'unsupported'  主明确不支持 json_schema（400）
    'down'         主整段抽风（两档都 500）
    """
    llm = LLM(_Cfg())
    llm.calls = []

    def fake_call(messages, max_tokens, think, client, model="", response_format=None):
        fmt = response_format or {}
        llm.calls.append("json_schema" if fmt.get("json_schema") else "json_object")
        if raise_kind == "down":
            raise Exception("Error code: 500 - Internal Server Error")
        if fmt.get("json_schema"):
            if raise_kind == "500":
                raise Exception("Error code: 500 - {'error': {'code': '500', "
                                "'message': 'Internal Server Error'}}")
            raise Exception("Error code: 400 - response_format type is unavailable now")
        return '{"action":"nothing","done":true}', ""

    llm._call_openai = fake_call
    return llm


def t_capability():
    print("[500 服务端故障：不该降级]")
    llm = make_llm("500")
    content, _ = attempt(llm)
    check("故障后仍拿到输出（回退 json_object 兜底）", bool(content), repr(content))
    check("能力位没被误降级", llm._cap_main != "json_object", f"_cap_main={llm._cap_main}")
    before = len(llm.calls)
    attempt(llm)
    check("下次仍先试 json_schema 严格档", "json_schema" in llm.calls[before:], str(llm.calls[before:]))

    print("\n[400 明确不支持：应该永久降档]")
    llm2 = make_llm("unsupported")
    content2, _ = attempt(llm2)
    check("不支持时仍能出结果", bool(content2), repr(content2))
    check("不支持时永久降档", llm2._cap_main == "json_object", f"_cap_main={llm2._cap_main}")
    before2 = len(llm2.calls)
    attempt(llm2)
    check("降档后不再白试严格档", "json_schema" not in llm2.calls[before2:], str(llm2.calls[before2:]))


def t_halt():
    print("\n[挂机：整段抽风 → 熔断冷却 → available False → 冷却过自动醒]")
    llm = make_llm("down")
    llm.cfg.llm_cooldown = 0.2   # 缩短冷却便于测试
    content, _ = llm.chat_action([{"role": "user", "content": "x"}], SCHEMA)
    check("整段抽风拿不到输出", content == "", repr(content))
    check("主进入熔断冷却", llm._cooldown_until > time.time(),
          f"now={time.time():.2f} until={llm._cooldown_until:.2f}")
    check("available()=False → agent 该挂机", not llm.available())
    time.sleep(0.3)
    check("冷却过了 available()=True → 自动醒", llm.available())
    # 冷却过了再抽风一次 → 又冷却（恢复后抽风仍会正确挂回）
    content2, _ = llm.chat_action([{"role": "user", "content": "x"}], SCHEMA)
    check("恢复后又抽风：再次进入冷却", content2 == "" and not llm.available())


def main():
    t_capability()
    t_halt()
    print()
    if FAILED:
        print(f"✗ {len(FAILED)} 项没过：" + "、".join(FAILED))
        return 1
    print("✓ 能力位 + 挂机逻辑全通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
