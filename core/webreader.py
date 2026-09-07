"""
网页正文读取：trafilatura 优先，正则兜底。

read_url 工具用它把 URL 变成正文。抓取交给 core/fetch.py（统一 HTTP 层），
这里只做「HTML → 正文」这一件事。

2026-09-01 换底座（见《读世界重构设计稿_2026-09-01.md》5.2）：
- trafilatura 在官方评测里 F1 0.924（990 文档、Python 3.13），比 readability-lxml 高约 10 个点
- 输出 Markdown（保留标题层级，LLM 更好读）
- 装不上/提取失败退回正则 _extract_regex（降级原则：外部依赖不可用时不崩）

之前这里的 fetch() 用 urllib 直连 + 只认 <article>/<main> 的正则，现在拆成两层：
抓取（fetch.py）+ 提取（本文件），失败原因能分类回传给调用方。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜extract() 公共入口 —— URL → 正文，永不抛异常（失败带分类 kind）
#   段 2｜_extract_main —— HTML → 正文：trafilatura 优先，失败/未装退正则
#   段 3｜_extract_regex —— 正则兜底：正文区块 / 去脚本样式 / 压空白
# =====================================================================
import html as html_lib
import re

from core import fetch

try:
    import trafilatura
except Exception:
    trafilatura = None


def extract(url: str, max_len: int = 40000, timeout: float = 20.0) -> dict:
    """URL → 正文。返回结构化结果，永不抛异常。

    成功：{"ok": True, "text": str, "truncated": bool, "chars": int, "engine": "trafilatura"|"regex"}
    失败：{"ok": False, "kind": "403"|"429"|"404"|"ssl"|"timeout"|"empty"|"net", "msg": str}

    truncated = 提取到的全文超过 max_len 被截断，调用方应标「没读完」。
    chars = 全文长度（截断前的），供调用方显示「全文 N 字，给了 M 字」。

    2026-09-04：max_len 20000→40000（创建者定），表格式数据不再进正文（include_tables=False）
    ——她读的哲学/社科长文 3-8 万字，2 万只够读开头，核心论点 4 万内讲得清，
    表格是正文里最大的数据块，去掉它省出的空间让论述读得更全。
    """
    r = fetch.get(url, timeout)
    if not r["ok"]:
        return {"ok": False, "kind": r["kind"], "msg": r["msg"],
                "text": "", "truncated": False, "chars": 0, "engine": ""}
    text, engine = _extract_main(r["text"])
    if not text:
        return {"ok": False, "kind": "empty",
                "msg": "抓到了但没提取出正文（可能需要 JS 渲染）",
                "text": "", "truncated": False, "chars": 0, "engine": engine}
    chars = len(text)
    truncated = chars > max_len
    if truncated:
        text = text[:max_len]
    return {"ok": True, "text": text, "truncated": truncated,
            "chars": chars, "engine": engine, "kind": "", "msg": ""}


def _extract_main(html: str) -> tuple:
    """HTML → 正文。trafilatura 优先，失败/未装退回正则。返回 (正文, 引擎名)。"""
    if trafilatura is not None:
        try:
            raw = trafilatura.extract(html, output_format="markdown",
                                      include_comments=False, include_tables=False)
            if raw and raw.strip():
                return raw.strip(), "trafilatura"
        except Exception:
            pass
    return _extract_regex(html), "regex"


def _extract_regex(page: str) -> str:
    """正则兜底：优先 <article>/<main>（正文区块足够长才用），退化全页；
    去 script/style/noscript → 去标签 → 解实体 → 压空白。"""
    body = page
    for tag in ("article", "main"):
        m = re.search(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", page)
        if m and len(re.sub(r"<[^>]+>", "", m.group(1))) > 200:
            body = m.group(1)
            break
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    text = html_lib.unescape(body)
    return re.sub(r"\s+", " ", text).strip()
