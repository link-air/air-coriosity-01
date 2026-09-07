"""冒烟验证：mailbox 时间戳 + 已读持久化（read_ids 去重，重启不重读）。"""
import sys
import shutil
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.mailbox import Mailbox

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_mail"
shutil.rmtree(cfg.data_root, ignore_errors=True)   # 清残留（防上次崩溃留下的脏数据）
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

box = Mailbox(cfg)

# 去信（她发话）带时间戳
box.send("human，我收到信了")
d = json.loads(box.outbox.read_text(encoding="utf-8").strip())
check("send 带时间戳", "ts" in d and len(d["ts"]) == 16)
check("send 文本正确", d["text"] == "human，我收到信了")

# 来信（使用者）带时间戳 → poll 返回时间前缀
with open(box.inbox, "a", encoding="utf-8") as f:
    f.write(json.dumps({"text": "早上好", "ts": "2026-08-23 08:00"}, ensure_ascii=False) + "\n")
msgs = box.poll()
check("poll 返回带时间前缀", msgs and msgs[0] == "[2026-08-23 08:00] 早上好")

# 旧数据兼容（无 ts）
with open(box.inbox, "a", encoding="utf-8") as f:
    f.write(json.dumps({"text": "旧消息"}, ensure_ascii=False) + "\n")
msgs2 = box.poll()
check("旧数据无 ts 原样返回", msgs2 and "旧消息" in msgs2)

# 非 JSON 行兜底
with open(box.inbox, "a", encoding="utf-8") as f:
    f.write("纯文本行\n")
msgs3 = box.poll()
check("非 JSON 行原样返回", msgs3 and "纯文本行" in msgs3)

# 已读持久化：mark_all_read 后 poll 为空；重启（新实例）不重读旧消息
box.mark_all_read()
check("mark_all_read 后 poll 为空", box.poll() == [])
box2 = Mailbox(cfg)
check("重启后不重读旧消息（read_ids 持久化）", box2.poll() == [])

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
