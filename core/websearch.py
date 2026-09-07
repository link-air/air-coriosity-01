"""
网页搜索（必应，无 key、纯标准库）。

设计：
- 默认 cn.bing.com（国内实测可达；DuckDuckGo 实测超时不可用），endpoint 可配
- 失败 / 无结果返回 []（优雅降级，不崩）
- 解析：贪婪匹配 <li class="b_algo"> 结果块，取 h2 标题 + href + b_caption 摘要
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜必应通道 —— search()（无 key、纯标准库抓 HTML）+ _parse()
#   段 2｜DeepSeek 原生通道 —— search_deepseek() / _parse_deepseek()
#        （可选后端：调服务端 web_search 工具拿结构化结果，无反爬问题）
# =====================================================================
import html as html_lib
import json
import re
import urllib.parse
import urllib.request

from core import fetch

ENDPOINT = "https://cn.bing.com/search"


def search(query: str, limit: int = 5, endpoint: str = ENDPOINT) -> list:
    """搜索网页，返回 [{title, snippet, link}]，最多 limit 条。失败返回 []。"""
    query = (query or "").strip()
    if not query:
        return []
    url = f"{endpoint}?q={urllib.parse.quote(query)}"
    r = fetch.get(url, timeout=20)
    if not r["ok"]:
        print(f"[websearch] 搜索失败（降级返回空）: {r['kind']} {r['msg']}")
        return []
    return _parse(r["text"], limit)


def _parse(page: str, limit: int) -> list:
    out = []
    for b in re.findall(r'<li class="b_algo".*?</li>', page, re.DOTALL):
        if len(out) >= limit:
            break
        a = re.search(r'<h2[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', b, re.DOTALL)
        if not a:
            continue
        link = html_lib.unescape(a.group(1))
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", a.group(2))).strip()
        cap = re.search(r'class="b_caption".*?<p[^>]*>(.*?)</p>', b, re.DOTALL)
        snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", cap.group(1))).strip() if cap else ""
        out.append({"title": title, "snippet": snippet, "link": link})
    return out


# ---- DeepSeek 原生 web_search（可选后端：Anthropic 兼容 Messages API + web_search_20250305 服务端工具）----
# 参考 DSH（DeepSeek Harness）web-search-deepseek 实现；调 API 拿结构化 JSON，无反爬问题。
DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
DEEPSEEK_SEARCH_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_VERSION = "2023-06-01"


def search_deepseek(query: str, api_key: str, limit: int = 5,
                    endpoint: str = DEEPSEEK_SEARCH_ENDPOINT,
                    model: str = DEEPSEEK_SEARCH_MODEL) -> list:
    """DeepSeek 原生 web_search：服务端工具返回结构化结果（url/title/snippet/发布时间）。

    与必应（抓 HTML 解析）不同——调 API 拿 JSON，不会有百度百科/知乎读不了那种反爬坑。
    无 key / 失败返回 []（tools 层降级，不崩）。"""
    query = (query or "").strip()
    if not query or not api_key:
        return []
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": f"Perform a web search for the query: {query}"}]}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
    }
    headers = {
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": DEEPSEEK_API_VERSION,
        "content-type": "application/json",
        "accept": "application/json",
    }
    try:
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"[websearch] DeepSeek 搜索失败（降级返回空）: {e}")
        return []
    return _parse_deepseek(payload, limit)


def _parse_deepseek(payload, limit: int) -> list:
    """从 DeepSeek Messages 响应提取搜索结果。

    content blocks：web_search_tool_result（含 web_search_result[]：url/title/page_age）
    + text（含 citations[]：url → cited_text，作为 snippet）。"""
    if not isinstance(payload, dict):
        return []
    blocks = payload.get("content") or []
    snippets = {}
    for b in blocks:
        if b.get("type") != "text":
            continue
        for cite in b.get("citations") or []:
            url = cite.get("url")
            if url and cite.get("cited_text") and url not in snippets:
                snippets[url] = cite["cited_text"]
    out = []
    seen = set()
    for b in blocks:
        if b.get("type") != "web_search_tool_result":
            continue
        for it in b.get("content") or []:
            if it.get("type") != "web_search_result":
                continue
            url = it.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"title": it.get("title", ""),
                        "snippet": snippets.get(url, ""),
                        "link": url})
            if len(out) >= limit:
                return out
    return out
