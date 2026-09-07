"""
订阅源：读世界（RSS 2.0 / Atom，纯标准库解析，不依赖 feedparser）。

每个源拉最新条目，返回标题 / 摘要 / 链接；失败源也返回（不静默吞掉）。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜XML 解析辅助 —— _local / _text_of / _link_of / _entry_dict / _parse_items
#       （纯标准库解析 RSS 2.0 / Atom，兼容两种 link 写法）
#   段 2｜class Rss —— fetch_all()：拉全部订阅源，成功源/失败源分开返回
# =====================================================================
import xml.etree.ElementTree as ET

from core import fetch


def _local(tag: str) -> str:
    """剥掉 XML namespace 前缀，取本地标签名。"""
    return tag.split("}")[-1] if "}" in tag else tag


def _text_of(el, name: str) -> str:
    """在 el 的后代里找第一个本地名为 name 的文本。"""
    for c in el.iter():
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def _link_of(el) -> str:
    """取 entry/item 的链接：兼容两种 Atom/RSS 写法——
    `<link>https://…</link>`（text 形式）和 `<link href="https://…"/>`（自闭合）。
    之前只认 text 形式，阮一峰/OneV's 这类 Atom feed 链接全丢（2026-09-01 修）。"""
    for c in el.iter():
        if _local(c.tag) != "link":
            continue
        href = (c.get("href") or "").strip()
        if href:
            return href
        if c.text and c.text.strip():
            return c.text.strip()
    return ""


def _entry_dict(el) -> dict:
    return {
        "title": _text_of(el, "title"),
        "summary": _text_of(el, "description") or _text_of(el, "summary"),
        "link": _link_of(el),
    }


def _parse_items(root, limit: int) -> list:
    """从 RSS/Atom XML 根解析条目，返回 [{title, summary, link}]，最多 limit 条。"""
    items = []
    seen = set()
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        d = _entry_dict(el)
        title = d.get("title", "")
        if title and title not in seen:
            seen.add(title)
            items.append(d)
        if len(items) >= limit:
            break
    return items


class Rss:
    def __init__(self, config):
        self.cfg = config

    def fetch_all(self, limit_per_source: int = 3) -> tuple:
        """拉所有源，返回 (成功源, 失败源)。成功源 = [(url, [{title, summary, link}])]，
        失败源 = [(url, kind)]。

        失败源不吞——read_rss 要报「哪几个源没抓到、什么原因」，让她自己判断换源还是等
        （2026-09-01 源健康度从「持久化聚合」简化为「本次实时报」：前者因 src 无法回填而空转）。"""
        ok, failed = [], []
        for url in self.cfg.rss_feeds:
            r = fetch.get(url, timeout=20)
            if not r["ok"]:
                failed.append((url, r["kind"]))
                continue
            try:
                root = ET.fromstring(r["text"])
            except Exception:
                failed.append((url, "parse"))
                continue
            items = _parse_items(root, limit_per_source)
            if items:
                ok.append((url, items))
        return ok, failed
