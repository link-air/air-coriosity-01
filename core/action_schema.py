"""
动作 JSON 的确定性解析 + 校验 + 修复（2026-09-03 创建者定）。

为什么要有这一层（2026-09-03 实测校准过，见《结构化输出重构设计思路》第六节）：

- 客户端约束解码（outlines / lm-format-enforcer 那套「解码时屏蔽非法 token」）确实
  对她不适用——主通道是远程 API，拿不到 logits。
- 但**主通道的 json_schema 档是服务端真约束解码**：实测诱导它「别输出 JSON、
  用不存在的动作名」，它仍只能输出合法结构——格式这一层本来就不用靠模型自觉。
- 约束会掉线：通道不支持时降 json_object 档、服务端 500 / 网络故障也会回退该档
  （2026-09-03 修过一个 bug：一次 500 会把能力位永久降级、不重启恢复不了），
  而 json_object 只保证「是合法 JSON」，实测**会缺必填字段**（如 done）。

所以这一层兜的是「约束没生效的那些时候」——instructor 的 reask 范式的轻量版：

    解析 → 校验 → 能确定性修的就地修 → 修不了的回灌重问（限一次）→
    仍失败要明确报错，不再静默降级成 nothing

三块：
1. 提取：代码块 → 括号配对扫描 → 尾随逗号修复 → 贪婪兜底。
   旧版只有一条贪婪正则 `\\{.*\\}`，原文里出现第二处花括号（说明文字、示例）就会把
   整段抠进去、json.loads 失败 → 静默降级 nothing（她「以为这轮做过了」）。
2. 校验：按 tools.render_schema 生成的结构自动校验（加工具/加字段不用改这里）。
   action 必须命中枚举；类型按 schema 的 type 强制；additionalProperties:False 的
   多余字段剔除。
3. 修复（保守，对齐 tools._resolve_tag_args 的「不猜意图」原则）：只做确定性变换——
   大小写/分隔符/括号后缀归一化、布尔与字符串类型强制、字符串切分成数组。
   归一化后命中多个候选就不改、直接判错（免得把 delete_memory 猜成别的动作）。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜提取 —— extract_json：从 LLM 原文里抠出合法 JSON
#   段 2｜类型强制 + 动作名解析 —— 按 schema 转类型；归一化动作名（唯一命中才改）
#   段 3｜校验 + 修复 —— validate_action：剔多余字段 / 拦缺参；parse_action 总入口
# =====================================================================
from __future__ import annotations

import json
import re
from typing import NamedTuple

# ---- 提取 ----

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_fence(raw: str):
    """有 ```json ... ``` 包裹就取块内内容，否则返回原对象（调用方靠 is 判断是否剥过）。"""
    m = _FENCE_RE.search(raw or "")
    if m and (m.group(1) or "").strip():
        return m.group(1)
    return raw


def _scan_objects(text: str):
    """括号配对扫描：产出所有「看起来完整」的 JSON 对象子串（跳过字符串内的花括号）。"""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        break
            j += 1
        i += 1


def _loads(s: str):
    """loads，失败先修尾随逗号再试（模型输出最常见的手抖），仍失败返回 None。"""
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", s))
    except Exception:
        return None


def extract_json(raw: str, prefer=("action",)):
    """从 LLM 原文里抠出 JSON 对象。返回 (对象|None, 提取方式)。

    顺序：代码块内 → 全文配对扫描 → 贪婪兜底。

    扫描时优先返回含 prefer 任一键的对象（动作 JSON 都带 action）：原文里夹了思路片段
    （「我想了想 {"old":1} 不对，重新来：{"action":...}」）时，先扫到的碎片不会抢走
    真正的动作；都没有 prefer 键就退回第一个能解析的对象。

    同一段里有多个带 prefer 键的对象时取**最后一个**（2026-09-06 修）：模型常在思考
    链里先举个 {"action": ...} 的例子再给真正的动作，取第一个会执行错工具。
    段间仍是 code_fence 优先——包在代码块里的才是它明确声明的输出。
    """
    if not (raw or "").strip():
        return None, ""
    order = []
    fenced = _strip_fence(raw)
    if fenced is not raw:
        order.append((fenced, "code_fence"))
    order.append((raw, "raw"))
    first = None
    first_how = ""
    for text, tag in order:
        hit = None
        hit_how = ""
        for idx, piece in enumerate(_scan_objects(text)):
            v = _loads(piece)
            if not isinstance(v, dict):
                continue
            how = f"{tag}/scan#{idx}"
            if prefer and any(k in v for k in prefer):
                hit, hit_how = v, how      # 段内往后覆盖，最终留下最后一个
                continue
            if first is None:
                first, first_how = v, how
        if hit is not None:
            return hit, hit_how
    if first is not None:
        return first, first_how
    # 兜底（括号不配对时 _scan_objects 出不来东西）：从首 { 或末 { 抠到最后一个 }。
    # 旧版只试一次 `\{.*\}`（首 { 到末 }），原文里出现第二处花括号就必然失败，
    # 兜底形同虚设。补上「最后一个 {」这一路：模型常见写法是先铺一段思路再给动作
    # JSON，那种情况下最后一个 { 才是动作本身。两次尝试，不是 O(n²)。
    for start in (raw.find("{"), raw.rfind("{")):
        end = raw.rfind("}")
        if start < 0 or end <= start:
            continue
        v = _loads(raw[start:end + 1])
        if isinstance(v, dict):
            return v, "greedy"
    return None, ""


# ---- 类型强制 ----

_TRUE_SET = {"true", "1", "yes", "y", "t", "是"}
_FALSE_SET = {"false", "0", "no", "n", "f", "否", ""}
_SPLIT_RE = re.compile(r"[,，、;；/|]")
# 布尔强转失败时**不能**静默置 False 的字段（2026-09-06 加）：这些字段解析不出来
# 会让语义整个反过来——remove 变成「加标签」而不是「删标签」、dry_run 变成真刀真枪
# 跑一遍，方向错了还一点声音都没有。这类宁可判缺参让她重填。
# done 不在此列：缺 done 按 false（继续做）是安全方向，不会把「做完了」说成没做完。
_STRICT_BOOL_FIELDS = {"remove", "dry_run"}


def _coerce_bool(v):
    """→ (bool|None, 是否成功)"""
    if isinstance(v, bool):
        return v, True
    if v is None:
        return None, False
    if isinstance(v, (int, float)):
        return bool(v), True
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE_SET:
            return True, True
        if s in _FALSE_SET:
            return False, True
    return None, False


def _coerce_str(v):
    """→ (str|None, 是否成功)"""
    if v is None:
        return None, False
    if isinstance(v, str):
        return v, True
    if isinstance(v, bool):
        return ("true" if v else "false"), True
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False), True
    return str(v), True


def _coerce_str_list(v):
    """→ (list[str], 是否成功)：字符串按分隔符切，单个值包成一项。"""
    if v is None:
        return None, False
    if isinstance(v, list):
        return [s for s in (_coerce_str(i)[0] for i in v) if s], True
    if isinstance(v, str):
        return [p.strip() for p in _SPLIT_RE.split(v) if p.strip()], True
    s, ok = _coerce_str(v)
    return ([s] if ok and s else []), True


# ---- action 名解析 ----

def _norm_name(s: str) -> str:
    """归一化：小写、去括号内容、去分隔符。用于「确定性修正」而非模糊猜测。"""
    s = (s or "").strip().lower()
    s = re.sub(r"[（(].*?[)）]", "", s)
    return re.sub(r"[\s_\-]+", "", s)


def _resolve_action(name, valid):
    """→ (合法动作名|None, 是否被修正过)。只在唯一命中时修正，不猜意图。"""
    raw = name if isinstance(name, str) else (_coerce_str(name)[0] or "")
    raw = raw.strip()
    if raw in valid:
        return raw, False
    n = _norm_name(raw)
    if not n:
        return None, False
    hits = [a for a in valid if _norm_name(a) == n]
    if len(hits) == 1:
        return hits[0], True
    return None, False


# ---- 校验 + 修复 ----

def _inner_schema(schema: dict) -> dict:
    """render_schema 返回 {"name":..,"schema":{...}}，取内层；容错直接传内层的情况。"""
    if not isinstance(schema, dict):
        return {}
    if "properties" in schema:
        return schema
    inner = schema.get("schema")
    return inner if isinstance(inner, dict) else {}


def validate_action(obj, schema: dict, valid_actions, requires: dict | None = None):
    """按 schema 校验并修复一个动作 dict。返回 (action|None, fixes:list[str], error:str)。

    error 非空 = 修不了（调用方该回灌重问或明确报错）；
    fixes = 做了哪些自动修复（给人看 / 记日志）。

    requires = {action: (说明, 必填参数组)}（tools.requires_map()），缺参判 error：
    2026-09-05 创建者定——旧行为是给缺失字段补空值放行，于是「该填没填」一路走到
    工具里才炸（ToolError），而回灌重问只认解析失败、不认字段为空，她就在原地转
    （日志实证：revise_memory 连卡 66 次）。缺参在解析层就拦下，报错里附用法。
    """
    if not isinstance(obj, dict):
        return None, [], "输出不是一个 JSON 对象"
    inner = _inner_schema(schema)
    props = inner.get("properties") or {}
    required = inner.get("required") or []
    addl = inner.get("additionalProperties", True)
    valid = set(valid_actions or [])
    fixes: list[str] = []
    out: dict = {}

    if "action" not in obj:
        return None, fixes, "缺 action 字段（不知道要做哪个动作）"
    name, changed = _resolve_action(obj.get("action"), valid)
    if name is None:
        return None, fixes, f"未知动作：{_coerce_str(obj.get('action'))[0] or '（空）'}"
    if changed:
        fixes.append(f"action：{obj.get('action')} → {name}")
    out["action"] = name

    for k, v in obj.items():
        if k == "action":
            continue
        spec = props.get(k)
        if spec is None:
            # schema 登记外的字段：剔除（只剔除 additionalProperties:false 的情况）。
            # 《结构化输出重构设计思路》第七节遗留的「handler 实读字段 vs schema 字段」
            # 全量比对已于 2026-09-03 做掉：实读字段全部登记在案（evidence 之类已无
            # handler 在读），所以剔除不会误杀。这样 json_object 降级档与 json_schema
            # 严格档行为一致——两档都不留脏字段，也就不必按档切换。
            if addl is False:
                fixes.append(f"剔除了未登记字段 {k}")
                continue
            out[k] = v
            continue
        t = (spec or {}).get("type")
        if t == "boolean":
            b, okb = _coerce_bool(v)
            if okb:
                if not isinstance(v, bool):
                    fixes.append(f"{k}：{v!r} → {b}")
                out[k] = b
            elif k in _STRICT_BOOL_FIELDS:
                # 见 _STRICT_BOOL_FIELDS 的说明：这两个字段静默降级会让动作反向
                return None, fixes, f"{k} 要填 true/false，填的是 {v!r}"
            else:
                fixes.append(f"{k} 不是布尔值，按 false 处理")
                out[k] = False
        elif t == "string":
            if v is None:
                fixes.append(f"{k} 是空值，已去掉")
                continue
            s, oks = _coerce_str(v)
            if oks:
                if not isinstance(v, str):
                    fixes.append(f"{k}：{v!r} → {s}")
                out[k] = s
        elif t == "array":
            lst, okl = _coerce_str_list(v)
            if okl:
                if not isinstance(v, list):
                    fixes.append(f"{k}：{v!r} → {lst}")
                out[k] = lst
        else:
            out[k] = v

    for k in required:
        if k in out or k == "action":
            continue
        if k == "done":
            # 缺 done 一律按 false：继续做比误判「做完了」安全
            out["done"] = False
            fixes.append("done 缺失，按 false 处理（继续做，不误判完成）")
        else:
            t = (props.get(k) or {}).get("type")
            out[k] = False if t == "boolean" else ([] if t == "array" else "")
            fixes.append(f"{k} 缺失，补空值")
    # 必填参数校验：组内任一字段非空即算填了（handler 的 _field 有 content 兜底），
    # 组与组之间都要有。缺失不补空值——补了工具照样报错，而重问不会被触发。
    req_spec = (requires or {}).get(name)
    if req_spec:
        desc, groups = req_spec
        missing = [g[0] for g in groups
                   if not any(str(out.get(f, "") or "").strip() for f in g)]
        if missing:
            return None, fixes, f"缺必填参数：{'、'.join(missing)}（{name} 的用法：{desc}）"
    return out, fixes, ""


class ParseResult(NamedTuple):
    action: dict | None      # 校验通过（可能已自动修复）的动作；失败时 None
    fixes: list              # 自动修复说明（给人看 / 记日志）
    error: str               # 致命错误原因；空串 = 通过
    how: str                 # JSON 提取方式（排障用）


def parse_action(raw: str, schema: dict, valid_actions, requires: dict | None = None) -> ParseResult:
    """一步到位：从 LLM 原文解析出合法动作 dict。"""
    obj, how = extract_json(raw)
    if obj is None:
        return ParseResult(None, [], "没从输出里读出 JSON 对象", how)
    action, fixes, error = validate_action(obj, schema, valid_actions, requires)
    return ParseResult(action, fixes, error, how)
