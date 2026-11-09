# -*- coding: utf-8 -*-
"""websearch.py —— 联网查资料。

给「自动作答」提供**可核验的外部依据**，避免模型只靠记忆硬猜。
多引擎设计，按需降级：

免密钥（默认，装了就能用）
  sogou       搜狗，中文题库命中率最稳——首选
  baidu       百度，质量最好但高频会弹验证码，排第二
  bing        www.bing.com/search，中文长句会退化成词典结果，仅兜底
  duckduckgo  html.duckduckgo.com，需代理
  searx       自建/公共 SearXNG 实例的 JSON API

需密钥（更稳，命中率高）
  tavily      https://tavily.com
  bocha       博查 https://bochaai.com（中文检索质量好）
  serper      https://serper.dev（Google 结果）

配置写在 config.json：
    "search": {"engine": "auto", "max_results": 5, "fetch_pages": 0,
               "tavily_api_key": "", "bocha_api_key": "", "serper_api_key": "",
               "searx_url": ""}

engine = "auto" 时按 付费引擎(若配) → sogou → baidu → bing → duckduckgo 依次尝试，
并记住上次成功的引擎，避免每次都先撞一次超时。
"""
import json
import re
import time
import urllib.parse
from html import unescape

import netkit

CACHE_TTL = 7 * 24 * 3600   # 检索结果缓存一周：同一套题反复跑不重复请求
PAGE_TTL = 3 * 24 * 3600


class SearchError(RuntimeError):
    """检索异常。kind 用来区分「引擎不可用」和「没有相关资料」——
    前者才该触发降级，后者是很正常的现象（题库不可能覆盖所有题）。
    kind: blocked（被反爬/冷却）/ irrelevant（无相关结果）/ error（其它）
    """

    def __init__(self, msg, kind="error"):
        super().__init__(msg)
        self.kind = kind


def conf(cfg):
    c = dict(cfg.get("search") or {})
    c.setdefault("engine", "auto")
    c.setdefault("max_results", 5)
    c.setdefault("fetch_pages", 0)
    return c


# --------------------------------------------------------------------------- #
# 查询词构造
# --------------------------------------------------------------------------- #
# 题干头部这些词不携带检索信息
_STOP_HEAD = re.compile(
    r"^(下列(关于|有关)?|以下(关于|有关)?|关于|根据|按照|对于|请|试述|简述|说明|"
    r"论述|指出|判断|(现在|目前|通常|一般)普遍?(认为|以为))")
_NOISE = re.compile(r"[【】\[\]（）()「」“”\"'`~!@#$%^&*_+=|\\/:;<>,.?！？。，、；：…—\-]+")

# 题干尾部的提问套话：「……的主要作用机理是」「……的方法包括」「……表现为」
# 这些词对检索毫无帮助，还会把检索结果带偏（搜「秋水仙素…机理是」会命中《庄子·秋水》）
_Q_TAIL = re.compile(
    r"(?:(?:的)?(?:主要|根本|基本|直接|具体|一般|通常|正确)?"
    r"(?:作用|原因|特点|特征|表现|目的|意义|依据|机理|机制|原理|方法|步骤|"
    r"条件|结果|影响|内容|方式|途径|说法|叙述|描述|过程|概念|定义)*"
    r"(?:是|为|有|包括|包含|表现为|指|在于|如下|的是))"
    r"[：:。？?！!\s]*$")


def build_query(stem, subject_hint="", max_len=26):
    """从题干提炼检索词。

    直译整句丢给搜索引擎效果很差，这里做四步：
    1. 去掉题型标记、括号内容、标点；
    2. 去掉「下列/关于/现在普遍认为」这类词头；
    3. 去掉「……的主要作用机理是」这类问句尾巴；
    4. 若结尾挂着一个抽象的「的××」，砍掉它让主题词更聚焦；
    5. 兜底截断到 max_len 字。
    """
    t = re.sub(r"^【[^】]*】", "", stem or "")
    t = re.sub(r"[（(][^）)]{0,30}[）)]", " ", t)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _STOP_HEAD.sub("", t).strip()

    before = t
    t = _Q_TAIL.sub("", t).strip()
    if len(t) < 6:            # 尾巴去太多说明题干本身就很短，退回原样
        t = before

    m = list(re.finditer("的", t))
    if m and m[-1].start() >= 8 and len(t) - m[-1].start() <= 8:
        t = t[:m[-1].start()]
    t = t.strip(" 的，,、")

    if subject_hint and len(t) < 8:
        hint = _NOISE.sub(" ", subject_hint).strip()
        if hint:
            t = f"{t} {hint}"
    return t[:max_len].strip()


# 提关键词时要剔掉的虚词 / 泛化名词
_TERM_DROP = re.compile(
    r"(?:主要|根本|基本|直接|具体|一般|通常|下列|关于|属于|称为|叫做|"
    r"作用|原因|特点|特征|表现|目的|意义|依据|机理|机制|原理|方法|步骤|"
    r"条件|结果|影响|内容|方式|途径|说法|叙述|描述|过程|概念|定义|"
    r"包括|包含|以及|及其|的是|是指|是|为|有|能|可|会|应|使|让|"
    r"的|和|与|及|或|在|对|把|被|了|着|过|者|所)")
_TERM_SPLIT = re.compile(r"[\s、，,；;。：:（）()]+")


def keyword_query(stem, subject_hint="", max_terms=4):
    """把题干压成「关键词版」：只留实词，最多 max_terms 段。

    必应对中文长句会退化成词典结果（搜「秋水仙素诱导染色体加倍」
    返回《庄子·秋水》），必须给它更短的组合；同时它也是整句查不到时的
    第二套查询词。
    """
    phrase = build_query(stem, "", max_len=40)
    cleaned = _TERM_DROP.sub(" ", phrase)
    terms = [t for t in _TERM_SPLIT.split(cleaned) if len(t) >= 2]
    if not terms:
        terms = [phrase]
    terms = terms[:max_terms]
    out = " ".join(terms)
    if subject_hint and len(out) < 6:
        out = f"{out} {subject_hint}".strip()
    return out[:40].strip()


def queries_for(stem, subject_hint=""):
    """给一道题准备 2 套检索词：整句版（中文引擎用）→ 关键词版（兜底/必应用）。

    去重后返回，保持顺序（调用方按顺序试，先命中先停）。
    """
    out = []
    for q in (build_query(stem, subject_hint), keyword_query(stem, subject_hint)):
        q = (q or "").strip()
        if q and q not in out:
            out.append(q)
    return out


# --------------------------------------------------------------------------- #
# 引擎实现
# --------------------------------------------------------------------------- #
# 反爬拦截页的特征词。命中就判定这个引擎本次不可用，
# 让 search() 立刻换下一个，而不是把「安全验证」当成「没搜到」。
_CAPTCHA_HINTS = ("安全验证", "验证码", "人机验证", "异常流量", "请输入验证码",
                  "captcha", "antispider", "unusual traffic", "are you a robot")


def _is_blocked(text):
    low = (text or "").lower()
    if len(low) < 4000 and any(h.lower() in low for h in _CAPTCHA_HINTS):
        return True
    return False


def _clean_url(href, real=None):
    """优先用真正的落地地址，拿不到再退回跳转链。"""
    href = unescape(href or "").strip()
    real = unescape(real or "").strip()
    if real.startswith("http"):
        return real
    return href


_RE_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fa5，。、；：？！“”‘’（）])\s+"
                         r"(?=[\u4e00-\u9fa5，。、；：？！“”‘’（）])")


def _cjk_join(text):
    """去掉汉字之间的多余空格。

    搜索引擎的高亮是 `<em>秋水仙素</em>诱导` 这种，转成纯文本后会变成
    「秋水仙素 诱导」，断词后既难读也影响后续按词组算相关度。
    """
    return _RE_CJK_GAP.sub("", text or "").strip()


# ---- 360 搜索 -------------------------------------------------------------- #
_RE_SO_LI = re.compile(r'<li class="res-list"[^>]*>', re.S)
_RE_SO_TITLE = re.compile(r'<h3 class="res-title[^"]*"[^>]*>\s*<a([^>]*)>(.*?)</a>', re.S)
_RE_SO_DESC = re.compile(r'<p class="res-desc"[^>]*>(.*?)</p>', re.S)
_RE_ATTR_MDURL = re.compile(r'data-mdurl="([^"]+)"')
_RE_ATTR_HREF = re.compile(r'href="([^"]+)"')

# 360 首条常是「AI 智能解题」卡片：题干 + 答案 + 选项，等于半个答案本
_RE_360_SUBJECT = re.compile(r'<div class="mh-subject"[^>]*>(.*?)</div>', re.S)
_RE_360_ANSWER = re.compile(r'<[pd][^>]*class="mh-answer"[^>]*>(.*?)</[pd]>', re.S)
_RE_360_ITEM = re.compile(
    r'<li[^>]*class="[^"]*mh-choice-item[^"]*"[^>]*>(.*?)</li>', re.S)
_RE_360_SITE = re.compile(r'class="mh-site"[^>]*>(.*?)</a>', re.S)
_RE_360_TYPE = re.compile(r'<div class="mh-type[^"]*"[^>]*>(.*?)</div>', re.S)
_RE_SPAN_TITLE = re.compile(r'<span[^>]*title="([^"]+)"', re.S)


def _parse_360_ai_card(html, query):
    """把 360 的「AI 智能解题」卡片抽成一条证据。

    结构（实测 2026-10）：
        <div class="mh-subject"><a>题干</a></div>
        <p class="mh-answer">答案：A.抑制细胞分裂时形成纺锤体</p>
        <ul class="mh-choice"><li class="mh-choice-item"><span title="A.…">
    这种卡片在题库类题目上命中率很高，但答案是别家模型/题库生成的，
    **不能当权威**——所以标题里标明来源，提示词里归到「检索资料」一档。
    """
    ans = _RE_360_ANSWER.search(html)
    if not ans:
        return None
    answer = _cjk_join(netkit.html_to_text(ans.group(1)))
    if not answer:
        return None
    subj = _RE_360_SUBJECT.search(html)
    stem = _cjk_join(netkit.html_to_text(subj.group(1))) if subj else query

    items = []
    for raw in _RE_360_ITEM.findall(html):
        t = _RE_SPAN_TITLE.search(raw)
        txt = unescape(t.group(1)) if t else netkit.html_to_text(raw)
        txt = _cjk_join(txt)
        if txt:
            items.append(txt[:60])
    site = _RE_360_SITE.search(html)
    src = _cjk_join(netkit.html_to_text(site.group(1))) if site else ""
    qtype = _RE_360_TYPE.search(html)
    qtype = _cjk_join(netkit.html_to_text(qtype.group(1))) if qtype else ""

    snippet = f"题干：{stem[:120]}｜{answer[:80]}"
    if items:
        snippet += "｜选项：" + " ".join(items[:6])

    return {"title": (f"360「AI 智能解题」对同类题的现成答案"
                      + (f"（来源 {src}）" if src else "")),
            "url": "https://www.so.com/s?" + urllib.parse.urlencode({"q": query}),
            "snippet": snippet[:600], "engine": "360", "kind": "ai_card",
            "qtype": qtype, "site": src}



def _search_360(query, n, proxy):
    """360 搜索（so.com）。中文题目命中率最高，且不弹验证码。

    结果块：<li class="res-list"> → <h3 class="res-title"> → <a data-mdurl="真实地址">
    摘要块：<p class="res-desc">…<span data-result-summary>…</span>
    首条若为「AI 智能解题」卡片，会额外收一条（见 _parse_360_ai_card）。
    """
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    r = netkit.http_request(url, timeout=25, proxy=proxy,
                            headers={"Accept": "text/html,application/xhtml+xml",
                                     "Referer": "https://www.so.com/"})
    html = r.text
    if _is_blocked(html):
        raise SearchError("返回了验证页")

    out = []
    card = _parse_360_ai_card(html, query)
    if card:
        out.append(card)

    marks = list(_RE_SO_LI.finditer(html))
    for i, m in enumerate(marks):
        if len(out) >= n:
            break
        end = marks[i + 1].start() if i + 1 < len(marks) else min(
            len(html), m.start() + 30000)
        block = html[m.start():end]
        t = _RE_SO_TITLE.search(block)
        if not t:
            continue
        title = _cjk_join(netkit.html_to_text(t.group(2)))
        if not title:
            continue
        attrs = t.group(1)
        href = _RE_ATTR_HREF.search(attrs)
        mdurl = _RE_ATTR_MDURL.search(attrs)
        # 摘要：先取 res-desc，退而求其次把整块文本的前 300 字当摘要
        d = _RE_SO_DESC.search(block)
        snippet = _cjk_join(netkit.html_to_text(d.group(1))) if d else ""
        snippet = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日\s*[-–—]?\s*", "", snippet).strip()
        if not snippet:
            snippet = _cjk_join(netkit.html_to_text(block[t.end():], limit=500))
        out.append({"title": title,
                    "url": _clean_url(href.group(1) if href else "",
                                      mdurl.group(1) if mdurl else ""),
                    "snippet": snippet[:400], "engine": "360"})
    return out[:n]



# ---- 搜狗 ------------------------------------------------------------------ #
_RE_SOGOU_H3 = re.compile(
    r'<h3 class="vr-title[^"]*"[^>]*>\s*<a([^>]*)>(.*?)</a>', re.S)


def _search_sogou(query, n, proxy):
    """搜狗。中文题库站（作业帮、知了好学等）经常直接命中同款题。

    注意 class 后面常带尾随空格（`class="vr-title  "`），
    所以匹配必须用 `vr-title[^"]*` 而不是精确串——这里踩过坑，全套结果会变 0。
    """
    r = netkit.http_request(
        "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query}),
        proxy=proxy, timeout=25,
        headers={"Accept": "text/html,application/xhtml+xml",
                 "Referer": "https://www.sogou.com/"})
    html = r.text
    if _is_blocked(html):
        raise SearchError("返回了验证页")

    out = []
    for m in _RE_SOGOU_H3.finditer(html):
        title = _cjk_join(netkit.html_to_text(m.group(2)))
        if not title:
            continue
        attrs = m.group(1)
        href = _RE_ATTR_HREF.search(attrs)
        tail = html[m.end():m.end() + 4000]
        mu = re.search(r'data-url="(https?://[^"]+)"', tail)
        sn = re.search(r'id="cacheresult_summary_[^"]*"[^>]*>(.*?)</div>', tail, re.S)
        if not sn:
            # 新版布局把摘要放在 <p class="star-wiki ..."> 或普通 <p> 里
            sn = re.search(r'<p[^>]*class="[^"]*(?:star-wiki|space-txt|str-text)[^"]*"[^>]*>'
                           r'(.*?)</p>', tail, re.S)
        if not sn:
            sn = re.search(r"<p[^>]*>(.*?)</p>", tail, re.S)
        # 摘要里带 <em><!--red_beg-->…<!--red_end--></em> 高亮标记，html_to_text 会清掉
        snippet = _cjk_join(netkit.html_to_text(sn.group(1))) if sn else ""
        real = mu.group(1) if mu else ""
        href_v = href.group(1) if href else ""
        if not real and href_v.startswith("/"):
            real = "https://www.sogou.com" + unescape(href_v)
        out.append({"title": title, "url": _clean_url(href_v, real),
                    "snippet": snippet[:400], "engine": "sogou"})
        if len(out) >= n:
            break
    return out


# ---- 百度 ------------------------------------------------------------------ #
_RE_BAIDU_START = re.compile(r'<div[^>]*class="[^"]*c-container[^"]*"[^>]*>', re.S)
_RE_H3_A = re.compile(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)

_BAIDU_WARMED = {"done": False, "block_until": 0.0}
_BAIDU_COOLDOWN = 900   # 被反爬一次就冷却 15 分钟，期间直接跳过，不浪费每次 2s


def _search_baidu(query, n, proxy):
    """百度。中文命中率最好，但小流量会弹「安全验证」，必须先拿首页 cookie。

    预热一次即可（进程级缓存）；仍被拦就抛错让 auto 换下一个引擎，
    不要静默返回空结果——否则会被误判成「这题没资料」。
    连续被拦会进入冷却，避免每道题都白撞一次。
    """
    now = time.time()
    if now < _BAIDU_WARMED["block_until"]:
        left = int((_BAIDU_WARMED["block_until"] - now) / 60) + 1
        raise SearchError(f"反爬冷却中（约 {left} 分钟后重试）")

    if not _BAIDU_WARMED["done"]:
        netkit.warmup("https://www.baidu.com/", proxy=proxy)
        _BAIDU_WARMED["done"] = True

    url = "https://www.baidu.com/s?" + urllib.parse.urlencode(
        {"wd": query, "rn": max(n, 10), "ie": "utf-8"})
    r = netkit.http_request(url, proxy=proxy, timeout=25,
                            headers={"Accept": "text/html,application/xhtml+xml",
                                     "Referer": "https://www.baidu.com/"})
    html = r.text
    if _is_blocked(html):
        _BAIDU_WARMED["done"] = False   # 下次重新预热
        _BAIDU_WARMED["block_until"] = now + _BAIDU_COOLDOWN
        raise SearchError("返回了安全验证页（触发反爬，已冷却 15 分钟）")

    marks = list(_RE_BAIDU_START.finditer(html))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else min(
            len(html), m.start() + 20000)
        block = html[m.start():end]
        h = _RE_H3_A.search(block)
        if not h:
            continue
        title = _cjk_join(netkit.html_to_text(h.group(2)))
        if not title:
            continue
        href = unescape(h.group(1))
        mu = re.search(r'\bmu="(https?://[^"]+)"', block)
        real = mu.group(1) if (mu and "baidu.com" not in mu.group(1)) else ""
        rest = block[h.end():]
        snippet = _cjk_join(netkit.html_to_text(rest, limit=500))
        snippet = re.sub(r"^(网页快照|百度快照|\d{4}年\d{1,2}月\d{1,2}日)", "",
                         snippet).strip()
        out.append({"title": title, "url": _clean_url(href, real),
                    "snippet": snippet[:320], "engine": "baidu"})
        if len(out) >= n:
            break
    return out


# ---- 必应 ------------------------------------------------------------------ #
_RE_BING_LI = re.compile(r'<li class="b_algo".*?</li>', re.S)
_RE_A_HREF_TITLE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_RE_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _search_bing(query, n, proxy):
    """必应。中文长句会被切碎退化成词典结果，所以只当兜底；
    调用方会给它传「关键词版」查询（见 queries_for）。"""
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "mkt": "zh-CN", "setlang": "zh-hans", "count": max(n, 5)})
    r = netkit.http_request(url, proxy=proxy, timeout=25,
                            headers={"Accept": "text/html,application/xhtml+xml"})
    out = []
    for block in _RE_BING_LI.findall(r.text):
        m = _RE_A_HREF_TITLE.search(block)
        if not m:
            continue
        href = unescape(m.group(1))
        if href.startswith("/"):
            href = "https://www.bing.com" + href
        if not href.startswith("http"):
            continue
        pm = _RE_P.search(block)
        out.append({"title": _cjk_join(netkit.html_to_text(m.group(2))), "url": href,
                    "snippet": _cjk_join(netkit.html_to_text(pm.group(1))) if pm else "",
                    "engine": "bing"})
        if len(out) >= n:
            break
    return out


# ----  DuckDuckGo / SearXNG / 付费 API -------------------------------------- #
def _decode_ddg(href):
    if "duckduckgo.com/l/" not in href:
        return href
    q = urllib.parse.urlparse(href).query
    real = urllib.parse.parse_qs(q).get("uddg")
    if real:
        return urllib.parse.unquote(real[0])
    return href


_RE_DDG_A = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_RE_DDG_S = re.compile(
    r'class="(?:result__snippet|result-snippet)"[^>]*>(.*?)</(?:a|div|span)>', re.S)


def _search_ddg(query, n, proxy):
    # 走代理经常超时，只试一次，别让兜底引擎把整轮检索拖到 100s
    r = netkit.http_request("https://html.duckduckgo.com/html/", "POST",
                            form={"q": query, "kl": "cn-zh"}, proxy=proxy,
                            timeout=15, retries=0,
                            headers={"Referer": "https://duckduckgo.com/"})
    html = r.text
    titles = [( _decode_ddg(unescape(h)), netkit.html_to_text(t)) for h, t in _RE_DDG_A.findall(html)]
    snips = [netkit.html_to_text(s) for s in _RE_DDG_S.findall(html)]
    out = []
    for i, (url, title) in enumerate(titles[:n]):
        if not url.startswith("http"):
            continue
        out.append({"title": title or url, "url": url,
                    "snippet": snips[i] if i < len(snips) else "", "engine": "duckduckgo"})
    return out


def _search_searx(query, n, proxy, base):
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "language": "zh-CN"})
    r = netkit.http_request(url, proxy=proxy, timeout=25)
    data = r.json()
    out = []
    for it in (data.get("results") or [])[:n]:
        out.append({"title": it.get("title") or "", "url": it.get("url") or "",
                    "snippet": it.get("content") or "", "engine": "searx"})
    return out


def _search_tavily(query, n, proxy, key):
    r = netkit.http_request("https://api.tavily.com/search", "POST",
                            data={"api_key": key, "query": query,
                                  "max_results": n, "search_depth": "basic",
                                  "include_answer": False},
                            proxy=proxy, timeout=30)
    data = r.json()
    return [{"title": it.get("title") or "", "url": it.get("url") or "",
             "snippet": (it.get("content") or "")[:600], "engine": "tavily"}
            for it in (data.get("results") or [])[:n]]


def _search_bocha(query, n, proxy, key):
    r = netkit.http_request("https://api.bochaai.com/v1/web-search", "POST",
                            data={"query": query, "count": n, "summary": True},
                            headers={"Authorization": f"Bearer {key}"},
                            proxy=proxy, timeout=30)
    data = r.json()
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value") or [])
    out = []
    for it in pages[:n]:
        out.append({"title": it.get("name") or "", "url": it.get("url") or "",
                    "snippet": (it.get("summary") or it.get("snippet") or "")[:600],
                    "engine": "bocha"})
    return out


def _search_serper(query, n, proxy, key):
    r = netkit.http_request("https://google.serper.dev/search", "POST",
                            data={"q": query, "gl": "cn", "hl": "zh-cn", "num": n},
                            headers={"X-API-KEY": key}, proxy=proxy, timeout=30)
    data = r.json()
    return [{"title": it.get("title") or "", "url": it.get("link") or "",
             "snippet": it.get("snippet") or "", "engine": "serper"}
            for it in (data.get("organic") or [])[:n]]


# 实测（2026-10 复测，同一套「染色体工程」测验，18 题）：
#   360    → 中文题目命中率最高，不弹验证码；还常带「AI 智能解题」卡片直接给答案 → 首选
#   sogou  → 题库站（作业帮等）命中好；但 class 名带尾随空格，解析稍有变动就全 0 → 次选
#   baidu  → 质量最好，但小流量必弹「安全验证」，需先拿首页 cookie，且仍可能被拦 → 第三
#   bing   → 中文长句会退化成词典结果（搜「秋水仙素…」返回《庄子·秋水》），只吃关键词版查询 → 兜底
#   ddg    → 走代理常 502，最后兜底
ENGINE_ORDER = ["searx", "bocha", "tavily", "serper", "360", "sogou", "baidu", "bing", "duckduckgo"]

ENGINE_LABEL = {
    "360": "360 搜索", "sogou": "搜狗", "baidu": "百度", "bing": "必应",
    "duckduckgo": "DuckDuckGo", "searx": "SearXNG",
    "bocha": "博查", "tavily": "Tavily", "serper": "Serper",
}


def available_engines(cfg):
    c = conf(cfg)
    out = []
    for e in ENGINE_ORDER:
        if e == "searx" and not c.get("searx_url"):
            continue
        if e in ("bocha", "tavily", "serper") and not c.get(f"{e}_api_key"):
            continue
        out.append(e)
    return out


def search(cfg, query, n=None, engine=None, proxy="auto", use_cache=True,
           ttl=CACHE_TTL, judge=True):
    """检索一次。返回 {'query','engine','results','cached','raw_count','errors'}。

    judge=True（默认）时会做相关性判定：某引擎返回的命中与查询词毫无交集，
    就当这个引擎没成功，继续试下一个。这一步很关键——bing 对中文长句经常
    返回同名词条的词典页，光看"有结果"会一直被它挡住。
    """
    c = conf(cfg)
    n = n or int(c["max_results"])
    engine = engine or c["engine"]
    query = (query or "").strip()
    if not query:
        return {"query": "", "engine": engine, "results": [], "cached": False}

    key = f"{engine}|{n}|{query}"
    if use_cache:
        hit = netkit.cache_get("search", key, ttl=ttl)
        if hit is not None:
            hit["cached"] = True
            return hit

    todo = available_engines(cfg) if engine == "auto" else [engine]
    if not todo:
        raise SearchError("没有可用的检索引擎：免密钥引擎需要能访问 360/搜狗/百度，"
                          "或者配置 search.bocha_api_key / search.tavily_api_key。")

    errors, results, used = [], [], ""
    for e in todo:
        try:
            if e in ("360", "so"):
                got = _search_360(query, n, proxy)
            elif e == "sogou":
                got = _search_sogou(query, n, proxy)
            elif e == "baidu":
                got = _search_baidu(query, n, proxy)
            elif e == "searx":
                got = _search_searx(query, n, proxy, c["searx_url"])
            elif e == "bocha":
                got = _search_bocha(query, n, proxy, c["bocha_api_key"])
            elif e == "tavily":
                got = _search_tavily(query, n, proxy, c["tavily_api_key"])
            elif e == "serper":
                got = _search_serper(query, n, proxy, c["serper_api_key"])
            elif e == "duckduckgo":
                got = _search_ddg(query, n, proxy)
            elif e == "bing":
                got = _search_bing(query, n, proxy)
            else:
                errors.append(f"{e}: 未知引擎")
                continue
        except Exception as ex:
            errors.append(f"{e}: {type(ex).__name__}: {str(ex)[:120]}")
            continue
        if not got:
            errors.append(f"{e}: 无结果")
            continue
        if judge:
            kept = filter_relevant(got, query)
            if not kept:
                errors.append(f"{e}: {len(got)} 条均与查询不相关")
                continue
            got = kept
        results, used = got, e
        break

    if not results:
        joined = "；".join(errors)
        msg = "检索失败：" + (joined[:400] if errors else "所有引擎都返回空结果")
        if errors and all(("无结果" in e or "不相关" in e) for e in errors):
            kind = "irrelevant"      # 引擎本身正常，只是没这道题的资料
        elif any(k in joined for k in ("冷却", "安全验证", "反爬", "验证码")):
            kind = "blocked"         # 被反爬拦了，换个引擎或等冷却
        else:
            kind = "error"
        raise SearchError(msg, kind=kind)

    payload = {"query": query, "engine": used, "results": results, "cached": False,
               "raw_count": len(results), "errors": errors,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if use_cache:
        netkit.cache_put("search", key, payload)
    return payload


def fetch_page(url, proxy="auto", limit=5000, use_cache=True, ttl=PAGE_TTL):
    """抓网页正文（去掉脚本样式后的纯文本），用于补充检索摘要不足的情况。"""
    if use_cache:
        hit = netkit.cache_get("page", url, ttl=ttl)
        if hit is not None:
            return hit
    r = netkit.http_request(url, proxy=proxy, timeout=30, retries=1,
                            headers={"Accept": "text/html,application/xhtml+xml"})
    ctype = (r.headers.get("content-type") or r.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype or "image" in ctype:
        return ""
    text = netkit.html_to_text(r.text, limit=limit)
    if use_cache and text:
        netkit.cache_put("page", url, text)
    return text


def _bigrams(s):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def filter_relevant(results, query, min_overlap=None):
    """丢掉与查询词几乎没有交集的命中。

    搜索引擎对中文长句经常"只认第一个词"：搜「秋水仙素诱导染色体加倍的作用机理」，
    bing 会返回《庄子·秋水》百科。这种噪声喂给模型只会带偏，直接判为不相关。
    """
    qb = _bigrams(query)
    if len(qb) < 3:
        return results
    need = min_overlap if min_overlap is not None else (2 if len(qb) >= 5 else 1)
    kept = []
    for r in results:
        hb = _bigrams((r.get("title") or "") + (r.get("snippet") or ""))
        if len(qb & hb) >= need:
            kept.append(r)
    return kept


def dedupe(results, per_domain=2):
    seen_url, seen_dom, out = set(), {}, []
    for r in results:
        u = (r.get("url") or "").split("#")[0]
        if not u or u in seen_url:
            continue
        dom = urllib.parse.urlparse(u).netloc
        if dom and seen_dom.get(dom, 0) >= per_domain:
            continue
        seen_url.add(u)
        seen_dom[dom] = seen_dom.get(dom, 0) + 1
        out.append(r)
    return out


def format_evidence(results, max_chars=2400):
    """把检索结果压成给模型看的紧凑文本。

    标了 kind="ai_card" 的（360 的 AI 解题卡）会额外打个警示：
    它是别家模型生成的现成答案，只能当线索，不能当权威依据。
    """
    lines, total = [], 0
    for i, r in enumerate(results, 1):
        snip = re.sub(r"\s+", " ", (r.get("snippet") or "")).strip()
        tag = "〔他站 AI 作答，仅作线索，须自行核对〕" if r.get("kind") == "ai_card" else ""
        block = f"[{i}]{tag} {(r.get('title') or '').strip()}\n    {r.get('url', '')}\n"
        if snip:
            block += f"    {snip[:400]}\n"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)
    return "".join(lines).strip()


def probe_engines(cfg, query="染色体 秋水仙素 加倍", n=3, timeout_note=""):
    """逐个引擎试一次，返回诊断列表（给 doctor 命令用）。

    每项：{engine, ok, count, relevant, ms, error}
    不走缓存，确保测的是当下真实可用性。
    """
    out = []
    for e in available_engines(cfg):
        t0 = time.time()
        item = {"engine": e, "label": ENGINE_LABEL.get(e, e), "ok": False,
                "count": 0, "relevant": 0, "ms": 0, "error": ""}
        try:
            got = search(cfg, query, n=n, engine=e, use_cache=False, judge=False)
            hits = got.get("results") or []
            item["ok"] = bool(hits)
            item["count"] = len(hits)
            item["relevant"] = len(filter_relevant(hits, query))
            if not hits:
                item["error"] = "无结果"
        except Exception as ex:
            item["error"] = str(ex)[:120]
        item["ms"] = int((time.time() - t0) * 1000)
        out.append(item)
    return out

