"""
冒烟：meta_log 状态流转的 id 容错 + reflect 失败候选提示。

跑：.venv\\Scripts\\python.exe _smoke_meta_log.py
用独立 data_test_meta_log 目录，不碰真实 data/。

背景（2026-09-11）：air 报告 reflect 带 id 流转状态总报「没找到」——她用 tick 号 /
自造名当 id（t483、reflect-t338-涌现精炼、D32），而 meta_log 的 id 是 8 位十六进制，
且只在 reflect 返回里出现一次。这里验证：抄写噪声（方括号/空格/半截）能容错对上，
自造 id 会失败并给出候选；顺带验证 revise「只填 title 不动正文」这条文档承诺。

覆盖：
  1. 精确 id 能流转
  2. 带方括号 [id] / 前后空格 能流转（容错）
  3. 唯一前缀能流转（容错）
  4. 自造 id（t483）/ 别的层 id（D32）→ False（不误伤）
  5. 前缀有歧义 → False（不猜她指哪条）
  6. status 非法 → 保持原状态，只追加备注
  7. open_meta_log_hint 列出可流转 id；没有 open 时给说明
  8. revise 只填 title（不填 text）→ 正文原样不动
"""
import os
import shutil
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_meta_log"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.memory import Memory

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


cfg = Config()
mem = Memory(cfg)

print("\n1. 精确 id 能流转")
a = mem.write_meta_log("卡点", "第一条自省：验证精确 id 流转", "open")
check("id 是 8 位十六进制", len(a) == 8 and all(c in "0123456789abcdef" for c in a), a)
check("精确 id 流转成功", mem.update_meta_log_status(a, "resolved") is True)
check("状态已变 resolved", mem.match_meta_log(a)["status"] == "resolved")

print("\n2. 抄写噪声：方括号 / 空格能容错")
b = mem.write_meta_log("卡点", "第二条自省：验证 [id] 与空格容错", "open")
check("带方括号 [id] 能流转", mem.update_meta_log_status(f"[{b}]", "resolved") is True)
c = mem.write_meta_log("卡点", "第三条自省：验证前后空格容错", "open")
check("带前后空格能流转", mem.update_meta_log_status(f"  {c}  ", "resolved") is True)

print("\n3. 半截前缀（唯一）能流转")
d = mem.write_meta_log("卡点", "第四条自省：验证唯一前缀", "open")
check("唯一前缀能流转", mem.update_meta_log_status(d[:4], "resolved") is True)

print("\n4. 自造 id / 别的层 id 不误伤")
e = mem.write_meta_log("卡点", "第五条自省：验证自造 id 失败", "open")
check("t483（tick 号）→ 失败", mem.update_meta_log_status("t483", "resolved") is False)
check("reflect-t338-涌现精炼（自造名）→ 失败",
      mem.update_meta_log_status("reflect-t338-涌现精炼", "resolved") is False)
check("D32（别的层的 id）→ 失败", mem.update_meta_log_status("D32", "resolved") is False)
check("失败后这条仍 open（没被误改）", mem.match_meta_log(e)["status"] == "open")

print("\n5. 前缀有歧义 → 不猜")
mem.meta_log[-1]["id"] = "abcd1111"
mem.meta_log[-2]["id"] = "abcd2222"
check("歧义前缀 abcd → 失败", mem.update_meta_log_status("abcd", "resolved") is False)
check("完整 id abcd1111 → 成功", mem.update_meta_log_status("abcd1111", "resolved") is True)

print("\n6. status 非法 → 保持原状态，只追加备注")
f = mem.write_meta_log("卡点", "第六条自省：验证非法 status", "open")
check("非法 status 仍返回 True（找到了）", mem.update_meta_log_status(f, "typo", "补一句备注") is True)
it = mem.match_meta_log(f)
check("状态保持 open", it["status"] == "open", it["status"])
check("备注已追加进正文", "补一句备注" in it["text"], it["text"][-20:])

print("\n7. 失败候选提示")
hint = mem.open_meta_log_hint()
check("候选里带 open 记录的 id", f"[{f}]" in hint)
check("候选说明了 id 来源", "8 位十六进制" in hint)
mem.update_meta_log_status(f, "resolved")
check("没有 open 时给说明", "当前没有 open" in mem.open_meta_log_hint())

print("\n8. revise 只填 title → 正文原样不动")
mem.write("L1", "条件→只补名字时；结论→正文不该被改写", category="value")
g = mem.l1[-1]
gid, old_text = g["id"], g["text"]
r = mem.revise(gid, "", title="补个名字")
check("只填 title 的改写成功", r.get("ok") is True, str(r.get("err", "")))
check("正文没动", g["text"] == old_text, g["text"][:30])
check("名字变了", mem.title_of(g) == "补个名字", mem.title_of(g))

shutil.rmtree(TEST, ignore_errors=True)
print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] meta_log id 容错 + revise 只改标签 全部通过")
