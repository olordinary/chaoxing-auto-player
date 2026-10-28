#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
chaoxing_player.py —— 超星 / 学习通 原生播放器控制器

用真实浏览器（隔离配置目录 + CDP）驱动学习通章节页里的原生播放器，
支持：列出章节、按章节播放、多章节顺序连播、边播边抽帧、播放状态查询。

依赖：playwright（已装在 .workbuddy 的 python venv 中）、Pillow（仅 analyze 需要）

常用：
    python chaoxing_player.py browser                 # 启动/确认浏览器（detached，不随命令退出）
    python chaoxing_player.py login                   # 打开登录页并等待扫码
    python chaoxing_player.py list                    # 列出目录（含 knowledgeid）
    python chaoxing_player.py status                  # 当前播放状态
    python chaoxing_player.py play --chapters 2.1      # 播一个章节
    python chaoxing_player.py play --from 2 --to 5     # 按目录顺序连播 2~5 章
    python chaoxing_player.py play --all --mute        # 全部连播、静音、后台跑
    python chaoxing_player.py analyze <run_dir>        # 把抽到的帧切成"一页一图"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 本地 CDP 不能被系统 http_proxy 拦掉
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "config.json"
VERSION = "1.0.0"

# 注意：course 留空，由 `init` 命令从你自己的课程链接里解析后写入 config.json
DEFAULT_CONFIG = {
    "cdp_port": 9225,
    "edge": "",
    "profile_dir": str(TOOL_DIR / "profile"),
    "output_dir": str(TOOL_DIR / "runs"),
    "course": {},
    "quiz_policy": {
        "require_submitted": False,
        "watermark": "参考答案 · 仅供预习与复习，考试以线下为准",
    },
}

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def find_edge(cfg):
    """按 config -> 环境变量 -> 常见安装路径 的顺序找 Edge。"""
    for cand in (cfg.get("edge"), os.environ.get("EDGE_PATH")):
        if cand and Path(cand).exists():
            return cand
    for cand in EDGE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def now_str():
    return datetime.now().strftime("%H:%M:%S")


def log(*a):
    print(f"[{now_str()}]", *a, flush=True)


def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def course_ready(cfg):
    c = cfg.get("course") or {}
    return all(c.get(k) for k in ("courseId", "clazzid", "cpi", "enc"))


def course_url(cfg, kid=None):
    """拼出章节页地址；未初始化课程时返回 None。"""
    c = cfg.get("course") or {}
    if not course_ready(cfg):
        return None
    kid = kid or c.get("entry_chapter") or ""
    return ("https://mooc1.chaoxing.com/mycourse/studentstudy?"
            f"chapterId={kid}&courseId={c['courseId']}&clazzid={c['clazzid']}"
            f"&cpi={c['cpi']}&enc={c['enc']}&mooc2=1&hidetype=0&fanyaVersion=0")


def parse_course_url(u):
    """从 studentsstudy 链接里取出课程参数。"""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(u).query)

    def g(k):
        return (q.get(k) or [""])[0]

    return {"courseId": g("courseId"), "clazzid": g("clazzid"), "cpi": g("cpi"),
            "enc": g("enc"), "entry_chapter": g("chapterId") or g("knowledgeid")}


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #
def cdp_alive(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_browser(cfg, open_url=None, wait=60):
    """确保带调试端口的 Edge 在跑；没跑就 detached 启动（命令退出后浏览器仍在）。"""
    port = cfg["cdp_port"]
    if cdp_alive(port):
        return True
    edge = find_edge(cfg)
    if not edge:
        log("找不到 Microsoft Edge。请安装 Edge，或在 config.json 里把 edge 指向 msedge.exe。")
        return False
    profile = cfg["profile_dir"]
    Path(profile).mkdir(parents=True, exist_ok=True)
    url = open_url or "about:blank"
    args = [edge, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--start-maximized", url]
    log("启动浏览器 (port %d) ..." % port)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)
    for _ in range(wait):
        if cdp_alive(port):
            return True
        time.sleep(1)
    return False


def connect(playwright, cfg):
    return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg['cdp_port']}", timeout=30000)


def is_login_page(page):
    u = (page.url or "").lower()
    return "passport" in u or "/login" in u


def get_course_page(ctx, cfg):
    for pg in ctx.pages:
        if "mycourse/studentstudy" in (pg.url or ""):
            return pg
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    url = course_url(cfg) or "https://i.mooc.chaoxing.com/"
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)
    return page


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #
JS_CATALOG = r"""
() => {
  return [...document.querySelectorAll('.posCatalog_select')].map(n => {
    const a = n.querySelector('.posCatalog_name');
    return {
      dom_id: n.id,
      kid: (n.id || '').replace(/^cur/, ''),
      level: (n.className || '').toString().includes('firstLayer') ? 1 : 2,
      name: (a ? a.innerText : n.innerText || '').trim().replace(/\s+/g, ' '),
      active: (n.className || '').toString().includes('active')
    };
  });
}
"""


def read_catalog(page):
    rows = page.evaluate(JS_CATALOG) or []
    out = []
    for r in rows:
        m = re.match(r"^\s*(\d+(?:\.\d+)*)\s*", r["name"])
        r["num"] = m.group(1) if m else ""
        r["title"] = r["name"][m.end():].strip() if m else r["name"]
        out.append(r)
    return out


def cache_path(cfg):
    return Path(cfg["output_dir"]) / "_catalog_cache.json"


def get_catalog(page, cfg, refresh=False):
    cp = cache_path(cfg)
    if cp.exists() and not refresh:
        try:
            cached = json.loads(cp.read_text(encoding="utf-8"))
            live = read_catalog(page)
            if len(cached) == len(live) and cached and cached[0].get("kid") == live[0].get("kid"):
                for c, l in zip(cached, live):
                    c["active"] = l.get("active")
                return cached
        except Exception:
            pass
    cat = read_catalog(page)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    return cat


def resolve(cat, tokens):
    """把 '2.1' / '2' / '培养' / '#12' 解析成目录项列表（保持目录顺序）。"""
    picked, unknown = [], []
    for t in tokens:
        t = str(t).strip()
        if not t:
            continue
        if t.startswith("#") and t[1:].isdigit():
            i = int(t[1:]) - 1
            if 0 <= i < len(cat):
                picked.append(cat[i])
                continue
            unknown.append(t)
            continue
        hit = [c for c in cat if c["num"] == t]
        if not hit:
            hit = [c for c in cat if c["num"] and c["num"].startswith(t + ".")]
        if not hit:
            hit = [c for c in cat if t in c["name"]]
        if not hit:
            unknown.append(t)
        else:
            picked.extend(hit)
    seen, out = set(), []
    for c in picked:
        if c["kid"] not in seen:
            seen.add(c["kid"])
            out.append(c)
    return out, unknown


def select_range(cat, a, b):
    ia = next((i for i, c in enumerate(cat) if c["num"] == a), None)
    ib = next((i for i, c in enumerate(cat) if c["num"] == b), None)
    if ia is None or ib is None:
        return []
    if ia > ib:
        ia, ib = ib, ia
    return cat[ia:ib + 1]


# --------------------------------------------------------------------------- #
# 播放
# --------------------------------------------------------------------------- #
JS_VIDEO_STATE = r"""
() => {
  const v = document.querySelector('video');
  if (!v) return {none: true};
  return {
    t: +v.currentTime.toFixed(2),
    dur: +(v.duration || 0).toFixed(2),
    paused: v.paused,
    ended: v.ended,
    rs: v.readyState,
    rate: v.playbackRate,
    muted: v.muted,
    vol: v.volume
  };
}
"""

JS_CLICK_CATALOG = r"""
(kid) => {
  const el = document.getElementById('cur' + kid) || document.getElementById(kid);
  if (!el) return 'notfound';
  const a = el.querySelector('.posCatalog_name') || el;
  a.click();
  return 'clicked';
}
"""

SAFE_BUTTON_TEXTS = ("继续播放", "继续学习", "继续观看", "我知道了", "知道了", "确定", "确认", "关闭")


def find_video_frame(page, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            if "modules/video" in (f.url or ""):
                return f
        try:
            page.wait_for_timeout(600)
        except Exception:
            return None
    return None


def open_chapter(page, cfg, kid):
    r = None
    try:
        r = page.evaluate(JS_CLICK_CATALOG, kid)
    except Exception as e:
        log("  目录点击异常：", repr(e)[:80])
    if r != "clicked":
        log("  目录节点未找到，改用 URL 跳转")
        page.goto(course_url(cfg, kid), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3500)
    return find_video_frame(page, timeout=25)


def dismiss_modals(page):
    clicked = []
    for fr in page.frames:
        for txt in SAFE_BUTTON_TEXTS:
            try:
                loc = fr.get_by_text(txt, exact=True)
                n = loc.count()
                for i in range(min(n, 2)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(timeout=1200)
                        clicked.append(txt)
                        page.wait_for_timeout(400)
            except Exception:
                continue
    return clicked


def start_play(page, vf, attempts=5):
    """在原生播放器里点开始播放（必须真实鼠标坐标点击）。"""
    try:
        page.bring_to_front()
    except Exception:
        pass
    for i in range(attempts):
        try:
            st = vf.evaluate(JS_VIDEO_STATE)
            if st.get("none"):
                page.wait_for_timeout(800)
                continue
            if not st.get("paused") and not st.get("ended"):
                return True
            btn = vf.locator(".vjs-play-control").first
            try:
                box = btn.bounding_box()
            except Exception:
                box = None
            if box and box["width"] > 4:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                vb = vf.locator("video").first.bounding_box()
                if vb:
                    page.mouse.click(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
            page.wait_for_timeout(1300)
            st = vf.evaluate(JS_VIDEO_STATE)
            if st and not st.get("none") and not st.get("paused"):
                return True
            if i >= 2:
                dismiss_modals(page)
        except Exception as e:
            log("  播放点击异常：", repr(e)[:90])
            page.wait_for_timeout(800)
    return False


JS_VIDEO_RECT = r"""
() => {
  const v = document.querySelector('video');
  if (!v) return null;
  const r = v.getBoundingClientRect();
  const cs = getComputedStyle(v);
  // 画面实际绘制区域（object-fit: contain 时可能比元素窄）
  let pw = r.width, ph = r.height, px = r.x, py = r.y;
  const ar = (v.videoWidth && v.videoHeight) ? v.videoWidth / v.videoHeight : 0;
  if (ar && (cs.objectFit === 'contain' || cs.objectFit === '')) {
    const boxAr = r.width / r.height;
    if (ar > boxAr) { pw = r.width; ph = r.width / ar; }
    else { ph = r.height; pw = r.height * ar; }
    px = r.x + (r.width - pw) / 2;
    py = r.y + (r.height - ph) / 2;
  }
  // 被祖先容器裁剪后的可见区域
  let cx1 = px, cy1 = py, cx2 = px + pw, cy2 = py + ph;
  let p = v.parentElement;
  while (p && p !== document.documentElement) {
    const c = getComputedStyle(p);
    if (c.overflow !== 'visible' || c.overflowX !== 'visible' || c.overflowY !== 'visible') {
      const pr = p.getBoundingClientRect();
      cx1 = Math.max(cx1, pr.x); cy1 = Math.max(cy1, pr.y);
      cx2 = Math.min(cx2, pr.x + pr.width); cy2 = Math.min(cy2, pr.y + pr.height);
    }
    p = p.parentElement;
  }
  cx1 = Math.max(cx1, 0); cy1 = Math.max(cy1, 0);
  cx2 = Math.min(cx2, document.documentElement.clientWidth);
  cy2 = Math.min(cy2, document.documentElement.clientHeight);
  if (cx2 - cx1 < 40 || cy2 - cy1 < 40) { cx1 = px; cy1 = py; cx2 = px + pw; cy2 = py + ph; }
  return {x: cx1, y: cy1, w: cx2 - cx1, h: cy2 - cy1,
          fullW: pw, fullH: ph, cut: (cx2 - cx1) < pw - 2};
}
"""


def video_page_rect(page, vf):
    """返回视频可见画面在【整页坐标】中的矩形（含 iframe 偏移与容器裁剪）。"""
    v = vf.evaluate(JS_VIDEO_RECT)
    if not v or v["w"] < 40:
        return None
    ox = oy = 0.0
    try:
        fe = vf.frame_element()
        fb = fe.bounding_box()
        if fb:
            ox, oy = fb["x"], fb["y"]
    except Exception:
        pass
    return ox + v["x"], oy + v["y"], v["w"], v["h"]


def save_frame(page, vf, out_dir, idx):
    """整页截图（内存中）→ 按几何位置裁出视频画面。全程不落临时文件。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"f{idx:04d}.jpg"
    rect = video_page_rect(page, vf)
    if not rect:
        return None
    try:
        import io
        from PIL import Image
        vp = page.evaluate("() => ({w: document.documentElement.clientWidth,"
                           " h: document.documentElement.clientHeight})")
        buf = page.screenshot()
        im = Image.open(io.BytesIO(buf))
        scale = im.width / float(vp["w"] or im.width)
        x, y, w, h = rect
        box = (max(0, int(x * scale)), max(0, int(y * scale)),
               min(im.width, int((x + w) * scale)), min(im.height, int((y + h) * scale)))
        if box[2] - box[0] < 40 or box[3] - box[1] < 40:
            return None
        im.crop(box).convert("RGB").save(path, quality=82)
        return path
    except Exception:
        return None


def read_control(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return (d.get("action") or "").lower()
    except Exception:
        return ""


def write_state(path, **kw):
    kw["ts"] = datetime.now().isoformat(timespec="seconds")
    try:
        Path(path).write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def play_chapter(page, cfg, chap, run_dir, opts, state_path):
    """播放单个章节的视频，返回记录 dict。"""
    rec = {"kid": chap["kid"], "num": chap["num"], "title": chap["title"],
           "status": "unknown", "played": 0.0, "duration": 0.0, "frames": 0}
    log(f"→ 打开 {chap['num']} {chap['title']}")
    vf = open_chapter(page, cfg, chap["kid"])
    if is_login_page(page):
        rec["status"] = "need_login"
        log("  ! 登录已失效，需要重新扫码（python chaoxing_player.py login）")
        return rec
    if vf is None:
        log("  该节点没有视频任务点，跳过")
        rec["status"] = "no_video"
        return rec

    # 任务点数量
    try:
        tabs = page.evaluate("() => [...document.querySelectorAll('#prev_tab li, .prev_tab li')].length")
        rec["task_points"] = tabs
    except Exception:
        rec["task_points"] = None

    if opts.mute:
        try:
            vf.evaluate("() => { const v=document.querySelector('video'); if(v){v.muted=true;} }")
        except Exception:
            pass
    if opts.rate and opts.rate != 1.0:
        try:
            vf.evaluate("(r) => { const v=document.querySelector('video'); if(v){v.playbackRate=r;} }", opts.rate)
        except Exception:
            pass

    # 等待元数据
    for _ in range(20):
        st = vf.evaluate(JS_VIDEO_STATE)
        if st and not st.get("none") and st.get("dur", 0) > 0:
            break
        page.wait_for_timeout(700)

    if not start_play(page, vf):
        rec["status"] = "cannot_play"
        log("  ! 无法开始播放")
        return rec

    for _ in range(15):
        st = vf.evaluate(JS_VIDEO_STATE)
        if st and not st.get("none") and st.get("dur", 0) > 0:
            break
        page.wait_for_timeout(600)
    rec["duration"] = (st or {}).get("dur", 0) or 0
    log(f"  时长 {rec['duration']:.0f}s，开始播放")

    cap_dir = run_dir / "frames" / chap["kid"]
    base = len(list(cap_dir.glob("f*.jpg"))) if cap_dir.exists() else 0
    if base:
        log(f"  已存在 {base} 帧，继续往后编号")
    rec["frames"] = base
    next_cap = 0.0
    play_started = time.time()
    last_report = 0.0
    stall = 0
    cap_miss = 0
    last_t = 0.0
    while True:
        act = read_control(run_dir / "control.json")
        if act == "stop":
            rec["status"] = "stopped_by_user"
            log("  收到 stop，结束")
            break
        if act == "pause":
            try:
                vf.evaluate("() => document.querySelector('video').pause()")
            except Exception:
                pass
            log("  收到 pause，暂停等待 resume/stop ...")
            while True:
                time.sleep(2)
                a2 = read_control(run_dir / "control.json")
                if a2 == "resume":
                    start_play(page, vf)
                    break
                if a2 == "stop":
                    rec["status"] = "stopped_by_user"
                    return rec

        st = vf.evaluate(JS_VIDEO_STATE)
        if not st or st.get("none"):
            log("  ! 播放器消失（可能被切走）")
            rec["status"] = "player_lost"
            break

        rec["played"] = st["t"]
        rec["duration"] = st.get("dur", rec["duration"]) or rec["duration"]

        if opts.capture and st["t"] >= next_cap:
            p = save_frame(page, vf, cap_dir, rec["frames"] + 1)
            if p:
                rec["frames"] += 1
                cap_miss = 0
            else:
                cap_miss += 1
                rec["capture_misses"] = rec.get("capture_misses", 0) + 1
                if cap_miss >= 2:
                    # 页面被滚走了就拉回视野，否则截不到画面
                    try:
                        vf.evaluate("() => { const v=document.querySelector('video');"
                                    " if(v) v.scrollIntoView({block:'center'}); }")
                    except Exception:
                        pass
                    cap_miss = 0
            next_cap = st["t"] + opts.capture_interval

        if st.get("ended"):
            rec["status"] = "finished"
            log(f"  播放完成 {st['t']:.0f}/{rec['duration']:.0f}s，抽帧 {rec['frames']} 张")
            break

        if st.get("paused"):
            stall += 1
            if stall >= 4:
                log("  检测到暂停，尝试恢复 ...")
                dismiss_modals(page)
                start_play(page, vf, attempts=3)
                stall = 0
        else:
            stall = 0
            if abs(st["t"] - last_t) < 0.05:
                pass
            last_t = st["t"]

        if opts.max_seconds and (time.time() - play_started) > opts.max_seconds:
            rec["status"] = "truncated"
            log(f"  达到 --max-seconds，当前 {st['t']:.0f}s 处停止")
            break

        write_state(state_path, chapter=f"{chap['num']} {chap['title']}", kid=chap["kid"],
                    t=round(st["t"], 1), dur=round(rec["duration"], 1),
                    status=rec["status"], frames=rec["frames"])
        if time.time() - last_report > 30:
            last_report = time.time()
            pct = (st["t"] / rec["duration"] * 100) if rec["duration"] else 0
            log(f"  … {st['t']:.0f}s / {rec['duration']:.0f}s ({pct:.0f}%)  帧 {rec['frames']}")
        page.wait_for_timeout(1000)

    rec["completion"] = round(rec["played"] / rec["duration"] * 100, 1) if rec["duration"] else 0
    if rec.get("capture_misses"):
        log(f"  提示：有 {rec['capture_misses']} 次抽帧失败（页面滚出视野/窗口变化），已自动拉回")
    return rec


# --------------------------------------------------------------------------- #
# 抽帧后处理：切成"一页一图"
# --------------------------------------------------------------------------- #
def analyze_frames(src_dir, out_dir, threshold=None):
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        log("缺少 Pillow：pip install pillow")
        return None
    import statistics
    files = sorted(Path(src_dir).glob("f*.jpg"))
    if len(files) < 3:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)

    def sig(p):
        im = Image.open(p).convert("L")
        w, h = im.size
        return im.crop((int(w * .04), int(h * .05), int(w * .96), int(h * .88))).resize((160, 110))

    sigs = [sig(f) for f in files]
    diffs = [0.0]
    for i in range(1, len(sigs)):
        diffs.append(ImageStat.Stat(ImageChops.difference(sigs[i - 1], sigs[i])).mean[0])
    mu = statistics.mean(diffs[1:]) if len(diffs) > 1 else 0
    sd = statistics.pstdev(diffs[1:]) if len(diffs) > 2 else 0
    thr = threshold or max(4.0, mu + 1.4 * sd)

    segs, start = [], 0
    for i in range(1, len(files)):
        if diffs[i] > thr:
            segs.append((start, i - 1))
            start = i
    segs.append((start, len(files) - 1))

    index = []
    for a, b in segs:
        pick = files[b]
        dst = out_dir / f"p{len(index) + 1:02d}_{pick.stem}.png"
        Image.open(pick).save(dst)
        index.append({"page": len(index) + 1, "source": pick.name,
                      "frames": [files[a].name, files[b].name]})
    (out_dir / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(index), thr


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_browser(cfg, args):
    ok = ensure_browser(cfg, open_url=(course_url(cfg) if args.open else None))
    print("CDP:", "OK" if ok else "FAIL", cdp_alive(cfg["cdp_port"]))
    return 0


def cmd_init(cfg, args):
    """把课程参数写进 config.json：--url 直接解析，或 --detect 从已打开的标签页里找。"""
    if args.url:
        info = parse_course_url(args.url)
        if not info.get("courseId"):
            print("这个链接里没有 courseId，请复制「章节详情」页面地址栏的完整链接。")
            return 1
        cfg["course"] = info
        save_config(cfg)
        print("已保存课程配置：", json.dumps(info, ensure_ascii=False))
        print("config.json 已被 .gitignore 忽略，不会误提交。")
        return 0

    from playwright.sync_api import sync_playwright
    ensure_browser(cfg, open_url="https://i.mooc.chaoxing.com/")
    with sync_playwright() as p:
        ctx = connect(p, cfg).contexts[0]
        page = get_course_page(ctx, cfg)
        page.bring_to_front()
        print("请在浏览器里登录，并打开你的课程任意一节的「章节详情」页面。")
        print("（最多等 5 分钟；也可以直接在地址栏复制链接后用 --url 参数）")
        for i in range(100):
            for pg in ctx.pages:
                u = pg.url or ""
                if "studentstudy" in u and "courseId=" in u:
                    info = parse_course_url(u)
                    if info.get("courseId"):
                        cfg["course"] = info
                        save_config(cfg)
                        print("识别到课程：", json.dumps(info, ensure_ascii=False))
                        print("已写入 config.json（已被 .gitignore 忽略）。")
                        return 0
            if i % 10 == 0:
                print(f"  等待中 {i * 3}s")
            time.sleep(3)
    print("没识别到课程页。请打开章节后重跑，或用 --url 直接指定。")
    return 1


def cmd_login(cfg, args):
    from playwright.sync_api import sync_playwright
    ensure_browser(cfg, open_url=course_url(cfg) or "https://i.mooc.chaoxing.com/")
    with sync_playwright() as p:
        ctx = connect(p, cfg).contexts[0]
        page = get_course_page(ctx, cfg)
        page.bring_to_front()
        print("请在浏览器窗口用学习通 App 扫码登录（最多等 15 分钟）...")
        for i in range(300):
            if not is_login_page(page):
                print("登录成功：", page.url[:100])
                return 0
            if i % 10 == 0:
                print(f"  等待中 {i * 3}s")
            time.sleep(3)
    print("超时，未检测到登录")
    return 1


def cmd_list(cfg, args):
    from playwright.sync_api import sync_playwright
    if not course_ready(cfg):
        print("还没有配置课程。先运行：python chaoxing_player.py init")
        return 4
    ensure_browser(cfg)
    with sync_playwright() as p:
        ctx = connect(p, cfg).contexts[0]
        page = get_course_page(ctx, cfg)
        if is_login_page(page):
            print("需要登录：python chaoxing_player.py login")
            return 2
        page.bring_to_front()
        page.wait_for_timeout(1200)
        cat = get_catalog(page, cfg, refresh=args.refresh)
        print(f"{'#':<4}{'编号':<8}{'knowledgeid':<14}标题")
        print("-" * 78)
        for i, c in enumerate(cat, 1):
            indent = "  " if c["level"] > 1 else ""
            print(f"{i:<4}{c['num']:<8}{c['kid']:<14}{indent}{c['title']}")
        print("-" * 78)
        print(f"共 {len(cat)} 个节点。用 --chapters 2.1 / --from 2 --to 5 / --index 12 选择。")
    return 0


def cmd_status(cfg, args):
    from playwright.sync_api import sync_playwright
    if not cdp_alive(cfg["cdp_port"]):
        print("浏览器未运行")
        return 1
    with sync_playwright() as p:
        ctx = connect(p, cfg).contexts[0]
        page = next((pg for pg in ctx.pages if "mycourse/studentstudy" in (pg.url or "")), None)
        if not page:
            print("没有打开课程页面")
            return 1
        vf = next((f for f in page.frames if "modules/video" in (f.url or "")), None)
        print("页面：", page.url[:110])
        if not vf:
            print("当前章节没有视频播放器")
            return 0
        st = vf.evaluate(JS_VIDEO_STATE)
        if st.get("none"):
            print("视频元素未就绪")
            return 0
        pct = (st["t"] / st["dur"] * 100) if st.get("dur") else 0
        print(f"播放：{st['t']:.0f}s / {st['dur']:.0f}s  ({pct:.1f}%)   "
              f"{'已暂停' if st['paused'] else '播放中'}   倍速 {st['rate']}   静音 {st['muted']}")
    return 0


def expand_units(cat, items):
    """把"整章"（level 1）展开成它的各小节（level 2）；小节原样保留。"""
    idx = {c["kid"]: i for i, c in enumerate(cat)}
    out = []
    for it in items:
        i = idx.get(it["kid"])
        if i is None:
            out.append(it)
            continue
        if it["level"] == 1:
            kids = []
            j = i + 1
            while j < len(cat) and cat[j]["level"] == 2:
                kids.append(cat[j])
                j += 1
            out.extend(kids if kids else [it])
        else:
            out.append(it)
    seen, res = set(), []
    for c in out:
        if c["kid"] not in seen:
            seen.add(c["kid"])
            res.append(c)
    return res


def build_plan(cat, args):
    if args.all:
        return expand_units(cat, cat), []
    if args.from_ or args.to:
        if not (args.from_ and args.to):
            print("--from 与 --to 需要同时给出")
            return [], []
        return expand_units(cat, select_range(cat, args.from_, args.to)), []
    toks = []
    if args.chapters:
        toks = [x.strip() for x in re.split(r"[,，\s]+", args.chapters) if x.strip()]
    if args.index:
        toks += [f"#{i}" for i in args.index]
    if not toks:
        print("请用 --chapters / --from --to / --all / --index 指定章节")
        return [], []
    items, unknown = resolve(cat, toks)
    return expand_units(cat, items), unknown


def cmd_play(cfg, args):
    from playwright.sync_api import sync_playwright
    if not course_ready(cfg):
        print("还没有配置课程。先运行：python chaoxing_player.py init")
        return 4
    ensure_browser(cfg)
    run_id = args.run or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(cfg["output_dir"]) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 长时间无人值守：把自己 detach 出去，父进程退出也不影响播放
    if args.detach and not os.environ.get("CXP_DETACHED"):
        logf = run_dir / "player.log"
        argv = [sys.executable, str(Path(__file__).resolve())]
        argv += [a for a in sys.argv[1:] if a != "--detach"]
        if "--run" not in argv:
            argv += ["--run", run_id]
        env = dict(os.environ)
        env["CXP_DETACHED"] = "1"
        fh = open(logf, "a", encoding="utf-8")
        subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env=env,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        print("已在后台启动（不受当前终端退出影响）")
        print("  run 目录：", run_dir)
        print("  日志：    ", logf)
        print("  状态：    ", run_dir / "state.json")
        print("  停止：    python chaoxing_player.py control", run_id, "stop")
        return 0

    (run_dir / "control.json").write_text(json.dumps({"action": ""}), encoding="utf-8")
    state_path = run_dir / "state.json"
    global_state = TOOL_DIR / "state.json"

    with sync_playwright() as p:
        ctx = connect(p, cfg).contexts[0]
        page = get_course_page(ctx, cfg)
        if is_login_page(page):
            print("需要登录：python chaoxing_player.py login")
            return 2
        page.bring_to_front()
        page.wait_for_timeout(1200)
        cat = get_catalog(page, cfg)
        plan, unknown = build_plan(cat, args)
        if unknown:
            print("未识别的章节：", ", ".join(unknown))
        if not plan:
            return 3
        print(f"计划播放 {len(plan)} 个节点（run: {run_id}）")
        for c in plan:
            print(f"  - {c['num']:<8}{c['title']}")
        if args.dry_run:
            print("（--dry-run，未实际播放）")
            return 0

        records = []
        for i, chap in enumerate(plan, 1):
            log(f"=== [{i}/{len(plan)}] {chap['num']} {chap['title']}")
            rec = play_chapter(page, cfg, chap, run_dir, args, state_path)
            records.append(rec)
            (run_dir / "manifest.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
            write_state(global_state, chapter=f"{chap['num']} {chap['title']}",
                        status=rec["status"], t=rec["played"], dur=rec["duration"],
                        frames=rec["frames"], run=run_id)
            if rec["status"] == "need_login":
                print("登录失效，流程中止")
                break
            if rec["status"] in ("stopped_by_user",) and not args.continue_on_error:
                break

        # 汇总
        print("\n===== 汇总 =====")
        for r in records:
            print(f"{r['num']:<8}{r['status']:<16}{r['played']:>7.0f}/{r['duration']:<7.0f}s "
                  f"{r.get('completion', 0):>5.1f}%  帧 {r['frames']}")
        print("产物：", run_dir)
        (run_dir / "control.json").write_text(json.dumps({"action": "done"}), encoding="utf-8")
    return 0


def cmd_analyze(cfg, args):
    run_dir = Path(args.run_dir)
    ok = 0
    for fdir in sorted(run_dir.glob("frames/*")):
        if not fdir.is_dir():
            continue
        res = analyze_frames(fdir, fdir.parent.parent / "slides" / fdir.name)
        if res:
            log(f"{fdir.name}: {res[0]} 页（阈值 {res[1]:.1f}）")
            ok += 1
    if not ok:
        log("没有可用帧")
        return 1
    return 0


def cmd_control(cfg, args):
    run_dir = Path(args.run_dir)
    cp = run_dir / "control.json"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"action": args.action}), encoding="utf-8")
    print(f"已写入 {cp} -> {args.action}")
    return 0


def cmd_quiz_entry(cfg, args):
    """测验相关子命令入口，转发到 quiz 模块。"""
    import importlib
    quiz = importlib.import_module("quiz")
    if getattr(args, "require_submitted", None) is None:
        args.require_submitted = bool((cfg.get("quiz_policy") or {}).get("require_submitted", False))
    return quiz.cmd_quiz(cfg, args, sys.modules[__name__])


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="超星 / 学习通 章节视频连播与测验整理工具")
    ap.add_argument("--version", action="version", version=f"chaoxing-auto-player {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    it = sub.add_parser("init", help="导入课程（解析课程链接并写入 config.json）")
    it.add_argument("--url", help="「章节详情」页面的完整地址；不给则从已打开的标签页自动识别")
    it.set_defaults(func=cmd_init)

    b = sub.add_parser("browser", help="启动/确认浏览器")
    b.add_argument("--open", action="store_true", help="顺带打开课程页")
    b.set_defaults(func=cmd_browser)

    lg = sub.add_parser("login", help="打开登录页等待扫码")
    lg.set_defaults(func=cmd_login)

    ls = sub.add_parser("list", help="列出目录")
    ls.add_argument("--refresh", action="store_true", help="忽略缓存重新读取")
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="当前播放状态")
    st.set_defaults(func=cmd_status)

    pl = sub.add_parser("play", help="播放章节")
    pl.add_argument("--chapters", help="章节编号，逗号分隔，如 2.1,2.2 或 4")
    pl.add_argument("--from", dest="from_", help="起始章节编号")
    pl.add_argument("--to", help="结束章节编号")
    pl.add_argument("--all", action="store_true", help="目录全部节点")
    pl.add_argument("--index", nargs="+", type=int, help="按 list 的序号,#1 形式")
    pl.add_argument("--mute", action="store_true", help="静音播放")
    pl.add_argument("--rate", type=float, default=1.0, help="倍速（默认 1.0，平台可能检测）")
    pl.add_argument("--capture", dest="capture", action="store_true", default=True)
    pl.add_argument("--no-capture", dest="capture", action="store_false", help="不抽帧")
    pl.add_argument("--capture-interval", type=float, default=5.0, help="每隔多少秒抽一帧")
    pl.add_argument("--max-seconds", type=int, default=0, help="单章节最多播多少秒（调试用）")
    pl.add_argument("--continue-on-error", action="store_true")
    pl.add_argument("--dry-run", action="store_true", help="只打印计划")
    pl.add_argument("--detach", action="store_true", help="后台运行（脱离当前终端）")
    pl.add_argument("--run", help="指定 run 目录名")
    pl.set_defaults(func=cmd_play)

    an = sub.add_parser("analyze", help="把抽到的帧切成一页一图")
    an.add_argument("run_dir")
    an.set_defaults(func=cmd_analyze)

    ct = sub.add_parser("control", help="向运行中的播放写入控制指令")
    ct.add_argument("run_dir")
    ct.add_argument("action", choices=["pause", "resume", "stop"])
    ct.set_defaults(func=cmd_control)

    qz = sub.add_parser("quiz", help="章节测验：抓题 / 生成参考答案复习册")
    qzsub = qz.add_subparsers(dest="quiz_cmd", required=True)

    ql = qzsub.add_parser("list", help="列出课程里的测验节点")
    ql.set_defaults(func=cmd_quiz_entry)

    qf = qzsub.add_parser("fetch", help="抓取指定测验的题目（只读）")
    qf.add_argument("--chapters", help="测验编号，如 2.2 或 2.2,3.2")
    qf.add_argument("--from", dest="from_", help="起始编号")
    qf.add_argument("--to", help="结束编号")
    qf.add_argument("--all", action="store_true", help="全部测验节点")
    qf.add_argument("--index", nargs="+", type=int)
    qf.add_argument("--require-submitted", dest="require_submitted", action="store_true")
    qf.add_argument("--no-require-submitted", dest="require_submitted", action="store_false")
    qf.add_argument("--run", help="输出目录名")
    qf.add_argument("--dry-run", action="store_true", help="只打印要抓哪些测验，不实际抓取")
    qf.set_defaults(func=cmd_quiz_entry, require_submitted=None, from_=None, to=None,
                     all=False, index=None, chapters=None, dry_run=False)

    qr = qzsub.add_parser("report", help="用 questions.json + answers.json 生成学生版复习册")
    qr.add_argument("run_dir")
    qr.add_argument("--watermark", help="页眉用途声明")
    qr.set_defaults(func=cmd_quiz_entry)

    qd = qzsub.add_parser("docs", help="抓取指定章节的讲义整页图片，作为答题依据")
    qd.add_argument("--chapters", help="章节编号，如 5.1,5.2 或 5")
    qd.add_argument("--from", dest="from_", help="起始编号")
    qd.add_argument("--to", help="结束编号")
    qd.add_argument("--all", action="store_true", help="目录全部非测验节点")
    qd.add_argument("--index", nargs="+", type=int)
    qd.add_argument("--dry-run", action="store_true", help="只打印要抓哪些节点")
    qd.set_defaults(func=cmd_quiz_entry, from_=None, to=None,
                     all=False, index=None, chapters=None, dry_run=False)

    args = ap.parse_args()
    cfg = load_config()
    return args.func(cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
