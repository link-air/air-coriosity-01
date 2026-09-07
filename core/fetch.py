"""
统一 HTTP 层：抓取 / 限速 / 重试退避 / 失败分类。

三个读世界模块（rss / webreader / websearch）共享这一层，之前 urlopen 散在四处，
只有 webreader 传了 SSL context，rss 和 websearch 都没传——同一台机器上抓正文不报证书错、
抓 RSS 和搜索会报。收在一处后，这个坑就只剩一个。

设计（见《读世界重构设计稿_2026-09-01.md》第三节）：
- SSL：统一 _create_unverified_context（正文抓取是只读低危操作，放宽证书校验换可用性）
- 限速：全局限速（模块级时间戳），防一个 tick 内打爆同一个站
- 重试：只对 429 退避；403/404/SSL/超时不重试（重试无意义）
- 失败分类：返回结构化结果，让 tools 层能给 air 具体的、可自检的文案

永不抛异常：永远返回 dict。给「不会停下来的主体」用的，抛异常会让整个 tick 挂掉。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜公共入口 get() —— 抓一个 URL，解码成文本，返回结构化结果
#        rss / webreader / websearch 三个读世界模块都走它。
#   段 2｜内部辅助 —— 限速 _throttle / 解码 _decode / 原样抓取 _fetch_raw
#        被 get() 用；同时带失败分类（403/429/404/ssl/timeout/net）。
# =====================================================================
import gzip
import os
import ssl
import time
import urllib.error
import urllib.request

# 抓取 UA / 语言偏好可配：部分站点对非浏览器 UA 直接拒绝，若默认 UA 被 429/403，
# 或想让抓回来的内容更贴合自己的语言，设 AIR2_FETCH_UA / AIR2_FETCH_LANG 即可。
HEADERS = {
    "User-Agent": os.getenv(
        "AIR2_FETCH_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": os.getenv("AIR2_FETCH_LANG", "zh-CN,zh;q=0.9,en;q=0.8"),
}

# 国内不少站点证书链不完整/自签名（如 qstheory.cn），严格校验会直接拒绝。
# 正文抓取是只读的低危操作，放宽证书校验换取可用性（webreader 2026-09-01 已验证的做法）。
_SSL_CTX = ssl._create_unverified_context()

# 全局限速：跨模块共享（rss/webreader/websearch 都走 get()）。
_last_request = 0.0
_MIN_INTERVAL = 1.0  # 秒


# ---- 段 2：内部辅助（限速 / 解码 / 抓取 + 失败分类）----

def _throttle():
    """全局限速：保证两次请求至少隔 _MIN_INTERVAL 秒。"""
    global _last_request
    wait = _MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("gb18030", errors="replace")


def _fetch_raw(url: str, timeout: float) -> bytes:
    # Referer 用目标 URL 自身：伪装成站内点击，部分站点会因此放行
    req = urllib.request.Request(url, headers={**HEADERS, "Referer": url})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        data = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
        return data


def _ok(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "kind": "empty", "msg": "抓到了但内容是空的（可能需要 JS 渲染）"}
    return {"ok": True, "text": text, "chars": len(text)}


# ---- 段 1：公共入口 get()（永不抛异常，返回结构化结果）----

def get(url: str, timeout: float = 20.0, retries_429: int = 2) -> dict:
    """抓一个 URL 的原始字节，解码成文本，返回结构化结果。

    成功：{"ok": True, "text": str, "chars": int}
    失败：{"ok": False, "kind": "403"|"429"|"404"|"ssl"|"timeout"|"empty"|"net", "msg": str}

    永不抛异常。429 退避重试（2s/4s/8s），其余失败直接返回对应 kind。
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "kind": "empty", "msg": "链接是空的"}
    _throttle()
    try:
        data = _fetch_raw(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            for i in range(retries_429):
                time.sleep(2 * (i + 1))
                _throttle()
                try:
                    return _ok(_decode(_fetch_raw(url, timeout)))
                except Exception:
                    continue
            return {"ok": False, "kind": "429", "msg": "限流，重试后仍失败"}
        kind = "403" if e.code == 403 else ("404" if e.code == 404 else "net")
        return {"ok": False, "kind": kind, "msg": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        reason = str(e.reason or "")
        rl = reason.lower()
        if "certificate" in rl or "ssl" in rl:
            return {"ok": False, "kind": "ssl", "msg": reason}
        if "timed out" in rl or "timeout" in rl:
            return {"ok": False, "kind": "timeout", "msg": reason}
        return {"ok": False, "kind": "net", "msg": reason}
    except Exception as e:
        return {"ok": False, "kind": "net", "msg": str(e)}
    return _ok(_decode(data))
