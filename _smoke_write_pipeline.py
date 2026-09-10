# -*- coding: utf-8 -*-
"""冒烟：写记忆/自省的分段约束（2026-09-05 晚，创建者定，原叫三段流水线）。

覆盖 agent._compose_write 的全部分支：
  1. 动作里 text 已填 → 直接用她的原文，普通生成一次都不调（省调用的设计目标）
  2. write_memory 缺 text → ② 普通生成起草，prompt 带 L3 层标准 + note + 素材
  3. revise_memory 缺 text → ② 的 prompt 带原记忆内容（L1 场景带 L1 标准）
  4. write_memory 缺 title/parent → ③ 约束解码补填（候选清单进 prompt）
  5. ② 空返回 → 返回失败 err，text 不被填上
  6. revise id 不对 → 直接放行（交给 handler 报 id 错），不空耗正文生成
  7. 非写记忆动作 → 原样通过

stub llm 记录每次调用；Tools/Memory 用真实现（内存目录），agent 用壳对象绑方法。
"""
import io
import os
import shutil
import sys
import types
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_pipeline"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.memory import Memory
from core.tools import Toolbox
from core.agent import Agent, TOOL_RESULT_PREFIX

PASS = []
def check(name, cond, detail=""):
    PASS.append(bool(cond))
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  — {detail}" if detail and not cond else ""))

class StubLLM:
    def __init__(self, chat_ret="", action_ret='{"title":"边界感","parent":"尊重他人"}'):
        self.chat_ret, self.action_ret = chat_ret, action_ret
        self.chat_calls, self.action_calls = [], []
    def chat(self, messages, **kw):
        self.chat_calls.append(messages)
        return self.chat_ret
    def chat_action(self, messages, schema, **kw):
        self.action_calls.append((messages, schema))
        return self.action_ret, ""

tmp = TEST
mem = Memory(Config())
tools = Toolbox(types.SimpleNamespace(memory=mem))
llm = StubLLM(chat_ret="条件→当朋友越过我的边界时；结论→我提醒而不是忍耐，边界靠表达守住")
agent = types.SimpleNamespace(llm=llm, tools=tools, memory=mem,
                              LAYER_STANDARDS=Agent.LAYER_STANDARDS)
for m in ("_compose_write", "_draft_text", "_draft_reflection", "_fill_tags"):
    setattr(agent, m, types.MethodType(getattr(Agent, m), agent))
agent._recent_material = Agent._recent_material   # staticmethod 经类访问就是底层函数

msgs = [{"role": "user", "content": TOOL_RESULT_PREFIX + "读完《三体》第三章，讲降维打击的不可逆"}]

print("1. text 已填 → 直用，不调生成")
a = {"action": "write_memory", "done": False, "layer": "L3", "text": "我自己的原文",
     "title": "边界感", "parent": "尊重他人"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("text 保持原文", a["text"] == "我自己的原文")
check("没调 chat", len(llm.chat_calls) == 0)
check("没调 chat_action", len(llm.action_calls) == 0)

print("\n2. write_memory 缺 text → ② 起草（prompt 带 L3 标准 + note + 素材）")
a = {"action": "write_memory", "done": False, "layer": "L3",
     "note": "记三体第三章的例子", "title": "边界感", "parent": "尊重他人"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("text 被起草", "条件→" in a["text"])
p = llm.chat_calls[-1][0]["content"]
check("prompt 带 L3 层标准", "L3 是例证层" in p)
check("prompt 带 note", "三体第三章" in p)
check("prompt 带素材", "降维打击" in p)
check("prompt 禁止 JSON/标签", "不要 JSON" in p)

print("\n3. revise_memory 缺 text → ② 带原文 + L1 标准")
mem.write("L1", "条件→当我面对不确定时；结论→我先承认不知道再行动", category="value", tag="诚实（价值观）")
l1_id = mem.l1[-1]["id"]
a = {"action": "revise_memory", "done": False, "id": l1_id, "note": "补一条前提"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("text 被起草", bool(a["text"]))
p = llm.chat_calls[-1][0]["content"]
check("prompt 带原文", "先承认不知道" in p)
check("prompt 带 L1 标准", "L1 是金字塔的框架层" in p)

print("\n4. write_memory 缺 title/parent → ③ 约束解码补填")
llm.action_ret = '{"title":"边界感","parent":"尊重他人"}'
a = {"action": "write_memory", "done": False, "layer": "L3", "text": "一条具体的例证"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("title 补上", a["title"] == "边界感")
check("parent 补上", a["parent"] == "尊重他人")
p, sch = llm.action_calls[-1]
check("prompt 带候选清单", "候选清单" in p[0]["content"])
check("schema 只含缺的字段", set(sch["schema"]["required"]) == {"title", "parent"})

print("\n5. ② 空返回 → 失败 err，text 不填")
llm.chat_ret = ""
a = {"action": "write_memory", "done": False, "layer": "L3", "note": "x"}
err = agent._compose_write(a, msgs)
check("err 非空", bool(err))
check("text 没被填", "text" not in a)

print("\n6. revise id 不对 → 放行给 handler 报错")
a = {"action": "revise_memory", "done": False, "id": "不存在", "note": "x"}
err = agent._compose_write(a, msgs)
check("err 为空（放行）", err == "", err)
check("text 没被填", "text" not in a)

print("\n7. 非写记忆动作 → 原样通过")
a = {"action": "read_state", "done": False}
check("err 为空", agent._compose_write(a, msgs) == "")

print("\n8. 缺标签补填失败 → 放行给 validate_tag 报错")
llm.chat_ret = "一条正文"
llm.action_ret = "不是JSON"
a = {"action": "write_memory", "done": False, "layer": "L3", "text": ""}
err = agent._compose_write(a, msgs)
check("err 为空（放行）", err == "", err)
check("title 没被填", "title" not in a)

print("\n9. merge_memory 缺 text → ② 带各目标原文起草")
mem.write("L3", "例证一：朋友越界时我提醒了他", tag="边界感（尊重他人）")
mem.write("L3", "例证二：室友翻我东西我明确说不", tag="边界感（尊重他人）")
ids = [x["id"] for x in mem.l3[-2:]]
llm.chat_ret = "条件→当两条边界例证讲同一件事时；结论→合并表述"
a = {"action": "merge_memory", "done": False, "layer": "L3", "targets": ",".join(ids), "note": "两条并一条"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("text 被起草", bool(a["text"]))
p = llm.chat_calls[-1][0]["content"]
check("prompt 带目标一原文", "例证一" in p)
check("prompt 带目标二原文", "例证二" in p)
check("prompt 标明合并语境", "归并成一条" in p)

print("\n10. merge_memory 缺 title/pyramid → ③ 补填 pyramid")
llm.action_ret = '{"title":"边界感","pyramid":"价值观"}'
a = {"action": "merge_memory", "done": False, "layer": "L1", "targets": "V01", "text": "合并正文"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("pyramid 补上", a["pyramid"] == "价值观")
check("title 补上", a["title"] == "边界感")

print("\n11. 旧整体格式兼容：targets 位带「目标们：正文」→ handler 拆对")
n_l5_before = len(mem.l5)
res = tools.merge_memory("L3", f"{ids[0]},{ids[1]}：条件→合并后的例证；结论→边界靠表达",
                         action={"text": ""})
check("合并成功", "已合并 2 条" in res, res)

print("\n12. reflect 新写缺 text → ② 起草（prompt 带自省写法 + type + note + 素材）")
llm.chat_ret = "反复 revise 正文消失：根因可能是约束解码吞长文本，前提是内容超 200 字，下次正文留空走起草"
a = {"action": "reflect", "done": False, "type": "卡点", "note": "为什么 revise 时正文老是丢"}
err = agent._compose_write(a, msgs)
check("err 为空", err == "", err)
check("text 被起草", bool(a["text"]))
p = llm.chat_calls[-1][0]["content"]
check("prompt 带自省写法", "自省 = 回头看" in p)
check("prompt 带 type", "卡点" in p)
check("prompt 带 note", "正文老是丢" in p)
check("prompt 禁止 JSON", "不要 JSON" in p)

print("\n13. reflect 流转状态（带 id）→ 不起草，备注短自己写")
before_chat = len(llm.chat_calls)
a = {"action": "reflect", "done": False, "id": "abc123", "status": "resolved"}
err = agent._compose_write(a, msgs)
check("err 为空（放行给 handler）", err == "", err)
check("没调 chat 起草", len(llm.chat_calls) == before_chat)
check("text 没被填", "text" not in a)

shutil.rmtree(TEST, ignore_errors=True)
print(f"\n{'[冒烟通过]' if all(PASS) else '[有失败]'} 分段约束 {sum(PASS)}/{len(PASS)} 通过")
sys.exit(0 if all(PASS) else 1)
