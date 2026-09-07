"""冒烟：parse 失败连败计数 + 工具名提取（2026-09-05 加的熔断相关逻辑）。

背景：revise_memory 缺 content 连卡 283 次。放大器之一——parse 失败后 action 被替换成
nothing，而 _track_fail 排除 nothing → 连败计数每次清零，防循环提醒永不触发。
改法：inner loop 从报错里提取真实工具名喂 _track_fail；同一 tick 连续 PARSE_FAIL_LIMIT
步解析失败直接熔断收尾。这里验证可独立测的部分。

跑：.venv\\Scripts\\python.exe _smoke_parse_stall.py
"""
import os
import re
import shutil
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_parse_stall"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.agent import Agent, PARSE_FAIL_LIMIT
from core.action_schema import parse_action

ok = True
def check(name, cond, detail=""):
    global ok
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not cond:
        ok = False

cfg = Config()
a = Agent(cfg, None)

# 1. 熔断阈值存在且合理
check("PARSE_FAIL_LIMIT 是正整数", isinstance(PARSE_FAIL_LIMIT, int) and PARSE_FAIL_LIMIT > 0,
      str(PARSE_FAIL_LIMIT))

# 2. _track_fail 对真实工具名累计（parse 失败路径喂的是工具名而非 nothing）
a._track_fail({"action": "revise_memory"}, False)
a._track_fail({"action": "revise_memory"}, False)
a._track_fail({"action": "revise_memory"}, False)
check("连续 3 次同工具失败计数到 3", a._fail_count == 3, f"count={a._fail_count} tool={a._fail_tool}")
a._track_fail({"action": "write_memory"}, False)
check("换工具重置为 1", a._fail_tool == "write_memory" and a._fail_count == 1,
      f"tool={a._fail_tool} count={a._fail_count}")
a._track_fail({"action": "write_memory"}, True)
check("成功一步清零", a._fail_tool is None and a._fail_count == 0)

# 3. nothing 仍不计数（_track_fail 的原语义：nothing 不算工具失败）
a._track_fail({"action": "nothing"}, False)
check("nothing 失败不计数", a._fail_count == 0 and a._fail_tool is None)

# 4. 从真实报错里能提取工具名（inner loop 用这个喂 _track_fail）
SCHEMA = {
    "name": "air_action",
    "schema": {"type": "object",
               "properties": {"action": {"type": "string", "enum": ["revise_memory", "nothing"]},
                              "done": {"type": "boolean"},
                              "content": {"type": "string"}, "id": {"type": "string"}},
               "required": ["action", "done"], "additionalProperties": False},
}
REQ = {"revise_memory": ("revise_memory(id, content)：改写一条记忆。", (("id",), ("content",)))}
r = parse_action('{"action":"revise_memory","id":"W11-001","done":false}', SCHEMA,
                 {"revise_memory", "nothing"}, REQ)
check("缺参报错能提取工具名",
      bool(re.search(r"([a-z_]+) 的用法", r.error or ""))
      and re.search(r"([a-z_]+) 的用法", r.error or "").group(1) == "revise_memory",
      (r.error or "")[:80])

shutil.rmtree(TEST, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
