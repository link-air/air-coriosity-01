"""
冒烟：动作 JSON 的确定性解析 + 校验 + 修复（core/action_schema）。

背景（对齐《结构化输出重构设计思路》）：格式正确性交给 API 之后，运行时还需要一层兜底——
API 档位会降（json_schema 严格档不支持就退 json_object），而 json_object 只保证
「是合法 JSON」，不保证符合动作结构。这层兜的就是结构对不对。

重点验证旧实现的坑 + 2026-09-05 补的缺参校验：
1. 一条贪婪正则 `\\{.*\\}` 在原文出现第二处花括号时会抠错整段 → 静默降级 nothing
   （她以为这轮做过了）；
2. 解析出 dict 后不校验直接用 → action 名 / 字段类型错只能等到下一轮才被发现；
3. （2026-09-05 起）缺必填参数判错并附用法、不再补空值放行——revise_memory 连卡
   66 次（日志实证）的根因就是补空值让「该填没填」一路走到 handler 才炸，而回灌
   重问只认解析失败、不认字段为空。

跑法：python _smoke_action_schema.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows GBK 控制台打不出 ✓/✗
except Exception:
    pass

from core.action_schema import parse_action, extract_json

# 与 tools.render_schema() 同构的最小 schema（够验证校验逻辑，不牵连真实工具表）。
# 字段名必须跟真实 schema 同步（2026-09-06 校对过一遍）：以前这里还留着 content / cross /
# main_tag / cat_tag / old_tag / new_tag 这批已退役的旧名，而真实 schema 里它们早就不在
# 白名单里了（content→text、cross→link、标签四字段换 title/parent/pyramid/link，
# 见 tools.render_schema 的字段命名卫生说明）。后果是失真：retag 那几条测出来的
# 「缺参报错」，真实原因是字段被 additionalProperties:false 剔除了，压根没走到
# 「组内任一 / 组间都要」的判定逻辑上。
SCHEMA = {
    "name": "air_action",
    "schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["write_memory", "search_memory", "retag", "delete_memory", "nothing"]},
            "done": {"type": "boolean"},
            "text": {"type": "string"},
            "layer": {"type": "string"},
            "query": {"type": "string"},
            "old_title": {"type": "string"},
            "new_title": {"type": "string"},
            "link": {"type": "array", "items": {"type": "string"}},
            "dry_run": {"type": "boolean"},
        },
        "required": ["action", "done"],
        "additionalProperties": False,
    },
}
ACTS = {"write_memory", "search_memory", "retag", "delete_memory", "nothing"}

FAILED = []


def check(name, cond, extra=""):
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def t_extract():
    print("\n[提取]")
    o, _ = extract_json('{"action":"nothing","done":true}')
    check("纯 JSON", o == {"action": "nothing", "done": True}, str(o))

    o, _ = extract_json('好的：\n```json\n{"action":"nothing","done":true}\n```\n就这样。')
    check("代码块包裹", o == {"action": "nothing", "done": True}, str(o))

    o, _ = extract_json('{"action":"nothing","done":true,}')
    check("尾随逗号", o == {"action": "nothing", "done": True}, str(o))

    o, _ = extract_json('{"action":"nothing","done":true,"text":"例：{a,b} 这样"}')
    check("字符串内的花括号不干扰", (o or {}).get("text") == "例：{a,b} 这样", str(o))

    # 旧贪婪正则会抠成 '{"old": 1} 不对...{"action":...}' 整段 → loads 失败 → 静默 nothing
    o, how = extract_json('我想了想 {"old": 1} 不对，重新来：{"action":"nothing","done":true}')
    check("夹了思路碎片仍取到真动作", o == {"action": "nothing", "done": True}, f"how={how} o={o}")

    o, _ = extract_json("今天什么都没做。")
    check("无 JSON → None", o is None, str(o))

    o, _ = extract_json("[1,2,3]")
    check("数组不算动作对象", o is None, str(o))


def t_validate():
    print("\n[校验 + 修复]")
    r = parse_action('{"action":"retag","done":false,"old_title":"x","new_title":"y"}', SCHEMA, ACTS)
    check("正常动作原样通过（已登记字段不动、无修复）",
          r.error == "" and r.action["action"] == "retag" and not r.fixes, str(r._asdict()))

    r = parse_action('{"action":"Write_Memory","done":true}', SCHEMA, ACTS)
    check("action 大小写/下划线被修正", r.error == "" and r.action["action"] == "write_memory", str(r._asdict()))

    r = parse_action('{"action":"retag（整理）","done":true}', SCHEMA, ACTS)
    check("action 带括号后缀被修正", r.error == "" and r.action["action"] == "retag", str(r._asdict()))

    r = parse_action('{"action":"writeMemory2","done":true}', SCHEMA, ACTS)
    check("未知动作要报错（不猜）", r.error.startswith("未知动作"), str(r.error))

    r = parse_action('{"action":"nothing","done":"true"}', SCHEMA, ACTS)
    check("done 字符串 → bool", r.error == "" and r.action["done"] is True, str(r.action))

    r = parse_action('{"action":"nothing"}', SCHEMA, ACTS)
    check("done 缺失补 false（不误判完成）", r.error == "" and r.action["done"] is False, str(r.action))

    r = parse_action('{"action":"search_memory","done":false,"text":42}', SCHEMA, ACTS)
    check("text 数字 → 字符串", r.error == "" and r.action["text"] == "42", str(r.action))

    r = parse_action('{"action":"write_memory","done":false,"link":"边界感、善意"}', SCHEMA, ACTS)
    check("link 字符串 → 数组", r.error == "" and r.action["link"] == ["边界感", "善意"], str(r.action))

    r = parse_action('{"action":"retag","done":false,"dry_run":"false"}', SCHEMA, ACTS)
    check("dry_run 字符串 → bool", r.error == "" and r.action["dry_run"] is False, str(r.action))

    # 未登记字段剔除：字段比对已确认没有 handler 在读它（见设计稿第七节遗留项）
    r = parse_action('{"action":"nothing","done":true,"evidence":"会被丢"}', SCHEMA, ACTS)
    check("未登记字段剔除且记录",
          r.error == "" and "evidence" not in r.action and any("剔除" in f for f in r.fixes),
          str(r._asdict()))

    print("\n[判错]")
    r = parse_action('{"done":true}', SCHEMA, ACTS)
    check("缺 action 报错", "缺 action" in r.error, str(r.error))

    r = parse_action("[1,2]", SCHEMA, ACTS)
    check("非对象报错", r.error != "", str(r.error))

    r = parse_action("我今天不想动。", SCHEMA, ACTS)
    check("纯文本报错", r.error != "", str(r.error))

    r = parse_action("", SCHEMA, ACTS)
    check("空输出报错", r.error != "", str(r.error))


def t_requires():
    print("\n[必填参数：缺参判错，不再补空值放行（2026-09-05 起）]")
    # 与 tools.requires_map() 同构的最小声明：内层「任一」外层「都要」。
    # 字段名跟 tools 的真实 requires 对齐（retag 是 old_title/new_title，
    # delete_memory 是 layer/targets）——用旧名的话测的就不是这套判定了。
    REQ = {
        "retag": ("retag(old_title, new_title, dry_run?)：批量重命名主标签。",
                  (("old_title",), ("new_title",))),
        "write_memory": ("write_memory(layer, text, title, parent, pyramid, link)：把一条理解写进记忆。",
                         (("text",),)),
        "search_memory": ("search_memory(query)：跨层搜记忆。", (("query", "text"),)),
        "delete_memory": ("delete_memory(layer, targets)：删记忆。", (("layer",), ("targets",))),
    }
    r = parse_action('{"action":"write_memory","done":false}', SCHEMA, ACTS, REQ)
    check("缺 text 报错", "缺必填参数" in r.error and "text" in r.error, str(r.error))

    r = parse_action('{"action":"retag","done":false,"new_title":"新标签"}', SCHEMA, ACTS, REQ)
    check("retag 缺旧标签报错（组间『都要』）", "old_title" in r.error, str(r.error))

    r = parse_action('{"action":"retag","done":false,"old_title":"旧标签"}', SCHEMA, ACTS, REQ)
    check("retag 缺新标签报错", "new_title" in r.error, str(r.error))

    r = parse_action('{"action":"delete_memory","done":false,"targets":"X-001"}', SCHEMA, ACTS, REQ)
    check("delete_memory 缺 layer 报错", "layer" in r.error, str(r.error))

    r = parse_action('{"action":"search_memory","done":false,"text":"边界感"}', SCHEMA, ACTS, REQ)
    check("text 兜底算填了 query（组内『任一』）",
          r.error == "" and r.action["text"] == "边界感", str(r.error))

    r = parse_action('{"action":"write_memory","done":false,"text":"   "}', SCHEMA, ACTS, REQ)
    check("全空白 text 也算没填", "text" in r.error, str(r.error))

    r = parse_action('{"action":"write_memory","done":false,"text":"善意"}', SCHEMA, ACTS, REQ)
    check("写全了才放行", r.error == "", str(r.error))

    r = parse_action('{"action":"nothing","done":false}', SCHEMA, ACTS, REQ)
    check("未声明的动作不受影响", r.error == "", str(r.error))

    r = parse_action('{"action":"write_memory","done":false}', SCHEMA, ACTS)
    check("没传 requires 时维持旧行为（向后兼容）", r.error == "", str(r.error))

    r = parse_action('{"action":"write_memory","done":false}', SCHEMA, ACTS, REQ)
    check("报错附工具用法（重问能直接照抄）",
          "write_memory" in r.error and "text" in r.error, str(r.error))


def main():
    t_extract()
    t_validate()
    t_requires()
    print()
    if FAILED:
        print(f"✗ {len(FAILED)} 项没过：" + "、".join(FAILED))
        return 1
    print("✓ 动作 JSON 解析校验层全通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
