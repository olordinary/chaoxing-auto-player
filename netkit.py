# -*- coding: utf-8 -*-
"""netkit.py —— 零依赖网络工具层。

为什么不用 requests：本项目 venv 只装了 playwright / pillow / numpy / fonttools，
为了一个 HTTP POST 引入新依赖不值得（而且国内装包很慢）。这里全部基于 urllib。

包含四件事：
1. 代理探测。chaoxing_player.py 在导入时会把 http_proxy 系列环境变量 pop 掉
   （否则本地 CDP 会被代理拦成 502），所以对外请求必须自己找回代理：
   config.net.proxy → CXP_HTTP_PROXY（pop 前的备份） → Windows 系统代理 → 本地常见端口探测。
2. HTTP 请求（JSON / 表单 / 原始），带重试与「代理失败后直连重试」。
3. HTML 转纯文本（检索结果与网页正文共用）。
4. 带 TTL 的磁盘缓存（检索结果、网页正文都缓存在 cache/web/ 下）。
"""
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html import unescape
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
CACHE_ROOT = TOOL_DIR / "cache" / "web"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")

# 本地常见代理端口（Clash / v2rayN / Shadowsocks 的默认值）
LOCAL_PROXY_PORTS = (7897, 7890, 10809, 10808, 1080, 8889, 8080)

# 进程级 Cookie 罐：检索站点会给首页下发 cookie（百度没有 BAIDUID 就只返回
# 安全验证页），所以必须跨请求保留。
_COOKIE_JAR = http.cookiejar.CookieJar()


class NetError(RuntimeError):
    """网络层统一异常，带人能看懂的中文提示。"""


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
def _norm_proxy(v):
    v = (v or "").strip()
    if not v:
        return None
    if "://" not in v:
        v = "http://" + v
    return v


def _wininet_proxy():
    """读 Windows 系统代理设置（Edge/Chrome 默认用的就是这个）。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not int(enable):
                return None
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
    except Exception:
        return None
    if not server:
        return None
    server = str(server)
    parts = {}
    for seg in server.split(";"):
        if "=" in seg:
            kk, vv = seg.split("=", 1)
            parts[kk.strip().lower()] = vv.strip()
        elif seg.strip():
            parts["http"] = parts["https"] = seg.strip()
    return _norm_proxy(parts.get("https") or parts.get("http"))


def _port_open(port, host="127.0.0.1", timeout=0.15):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_local_proxy():
    for port in LOCAL_PROXY_PORTS:
        if _port_open(port):
            return f"http://127.0.0.1:{port}"
    return None


_PROXY_CACHE = {"value": None, "done": False}


def detect_proxy(explicit=None):
    """解析本次请求该走哪个代理。

    explicit 取值：
      - None / "auto"  → 自动探测
      - "none" / "off" / "direct" → 强制直连
      - "http://host:port" → 指定代理
    """
    if isinstance(explicit, str):
        low = explicit.strip().lower()
        if low in ("none", "off", "direct", "no"):
            return None
        if low and low != "auto":
            return _norm_proxy(explicit)

    if _PROXY_CACHE["done"]:
        return _PROXY_CACHE["value"]

    p = (_norm_proxy(os.environ.get("CXP_HTTP_PROXY"))
         or _norm_proxy(os.environ.get("HTTPS_PROXY"))
         or _norm_proxy(os.environ.get("https_proxy"))
         or _norm_proxy(os.environ.get("ALL_PROXY"))
         or _norm_proxy(os.environ.get("all_proxy")))
    if not p:
        p = _wininet_proxy()
    if not p:
        p = _probe_local_proxy()
    _PROXY_CACHE["value"] = p
    _PROXY_CACHE["done"] = True
    return p


def reset_proxy_cache():
    _PROXY_CACHE["value"] = None
    _PROXY_CACHE["done"] = False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Resp:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers or {}

    @property
    def text(self):
        enc = None
        ctype = self.headers.get("content-type") or self.headers.get("Content-Type") or ""
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            enc = m.group(1)
        if not enc:
            m = re.search(rb'charset=["\']?([\w-]+)', self.body[:4096], re.I)
            if m:
                enc = m.group(1).decode("ascii", "ignore")
        for cand in (enc, "utf-8", "gb18030", "latin-1"):
            if not cand:
                continue
            try:
                return self.body.decode(cand)
            except Exception:
                continue
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


def _opener(proxy):
    handlers = [urllib.request.HTTPCookieProcessor(_COOKIE_JAR)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 明确不走环境代理
    if os.environ.get("NETKIT_INSECURE") == "1":
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def warmup(url, proxy="auto", timeout=15):
    """先访问一次首页把 Cookie 拿到手。

    百度这类站点对小流量直接返回「安全验证」页，带上首页下发的 BAIDUID
    cookie 后才正常出结果。失败不影响主流程（调用方自行忽略异常）。
    """
    try:
        http_request(url, timeout=timeout, proxy=proxy, retries=0,
                     headers={"Accept": "text/html,application/xhtml+xml"})
        return True
    except Exception:
        return False


def _do(url, method, body, headers, timeout, proxy):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with _opener(proxy).open(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return Resp(getattr(r, "status", 200) or 200, raw, dict(r.headers))


def http_request(url, method="GET", data=None, form=None, headers=None, timeout=40,
                 proxy="auto", retries=2, allow_direct_fallback=True):
    """发一个请求，返回 Resp。

    data=dict → JSON body；form=dict → 表单 body。
    4xx 不重试（除非 429），5xx / 网络错误按指数退避重试。
    代理连不上时自动改直连再试一次（国内代理端口误判很常见）。
    """
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "identity",
    }
    if headers:
        hdrs.update(headers)

    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

    px = detect_proxy(proxy)
    tried_direct = px is None
    last_err = None
    for attempt in range(retries + 1):
        try:
            return _do(url, method, body, hdrs, timeout, px)
        except urllib.error.HTTPError as e:
            payload = b""
            try:
                payload = e.read()[:800]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(e.headers.get("Retry-After") or (2 ** attempt))
                time.sleep(min(wait, 20))
                last_err = NetError(f"HTTP {e.code} {payload[:200]!r}")
                continue
            raise NetError(f"HTTP {e.code} {url} :: {payload[:300].decode('utf-8', 'replace')}")
        except Exception as e:
            last_err = e
            # 代理挂了 → 换直连再试一次
            if px and not tried_direct and allow_direct_fallback:
                tried_direct = True
                px = None
                continue
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise NetError(f"请求失败 {url} :: {type(e).__name__}: {e}")
    raise NetError(f"请求失败 {url} :: {last_err}")


# --------------------------------------------------------------------------- #
# HTML → 文本
# --------------------------------------------------------------------------- #
_RE_SCRIPT = re.compile(r"<(script|style|noscript|svg|iframe)\b[^>]*>.*?</\1>", re.S | re.I)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|table|ul|ol)\b[^>]*>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")


def html_to_text(html, limit=None):
    if not html:
        return ""
    t = _RE_COMMENT.sub(" ", html)
    t = _RE_SCRIPT.sub(" ", t)
    t = _RE_BLOCK.sub("\n", t)
    t = _RE_TAG.sub(" ", t)
    t = unescape(t)
    t = t.replace("\u00a0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in t.split("\n")]
    lines = [ln for ln in lines if ln]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if limit:
        t = t[:limit]
    return t.strip()


# --------------------------------------------------------------------------- #
# 磁盘缓存
# --------------------------------------------------------------------------- #
def _cache_file(namespace, key):
    h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:20]
    d = CACHE_ROOT / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def cache_get(namespace, key, ttl=None):
    f = _cache_file(namespace, key)
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if ttl is not None and (time.time() - float(d.get("ts", 0))) > ttl:
        return None
    return d.get("payload")


def cache_put(namespace, key, payload, meta=None):
    f = _cache_file(namespace, key)
    try:
        f.write_text(json.dumps({"ts": time.time(), "key": str(key),
                                 "meta": meta or {}, "payload": payload},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return f


def cache_clear(namespace=None):
    import shutil
    target = CACHE_ROOT / namespace if namespace else CACHE_ROOT
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    return target
