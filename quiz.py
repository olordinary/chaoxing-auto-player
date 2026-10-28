# -*- coding: utf-8 -*-
"""测验题抓取与参考答案复习册生成。

设计要点：
- 题目文本被平台做了字体混淆（部分汉字替换成生僻码位 + 内嵌子集字体还原）。
  这里用「字形位图匹配」自动反解：拿内嵌字体的每个字形，与系统 Noto Sans SC
  （与平台用的 Source Han Sans 同源）逐字比对，取相似度最高者为真字。
- 只读：不点击「提交」「暂时保存」，不修改任何作答。
- 参考答案由人（或模型）在 questions.json 基础上生成，写入 answers.json，
  再由 report 合并成学生版复习册。
"""
import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
CACHE_DIR = TOOL_DIR / "cache" / "fonts"

REF_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]

TYPE_BY_LABEL = [
    ("单选", "single_choice"),
    ("多选", "multi_choice"),
    ("判断", "judge"),
    ("填空", "fill_blank"),
    ("简答", "short_answer"),
    ("论述", "short_answer"),
]

JS_QUESTIONS = r"""
() => {
  const clean = (el) => el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const qs = [...document.querySelectorAll('.TiMu')].map((q, i) => {
    const stemEl = q.querySelector('.Zy_TItle > div.font-cxsecret') || q.querySelector('.Zy_TItle');
    const rows = [...q.querySelectorAll('ul.Zy_ulTop > li')];
    const opts = rows.map((li, k) => {
      const num = li.querySelector('.num_option');
      const iTag = li.querySelector('i.fl') || li.querySelector('i');
      const rawLetter = num ? (num.getAttribute('data') || clean(num)) : clean(iTag);
      const letter = (rawLetter || '').replace(/[、.．:：\s]/g, '').slice(0, 2)
                     || String.fromCharCode(65 + k);
      const a = li.querySelector('a.after') || li.querySelector('a');
      let text = a ? clean(a) : clean(li);
      text = text.replace(/^[A-Za-z][、.．:：]\s*/, '').trim();
      return { letter, text };
    });
    const myAns = clean(q.querySelector('.myAnswer .answerCon')) ||
                  clean(q.querySelector('.myAnswer')) ||
                  '';
    const scoreTxt = clean(q.querySelector('.answerScore')) ||
                     clean(q.querySelector('[class*="score"]')) || '';
    const markCls = ['.dui', '.cuo', '.right', '.wrong', '.correct', '.error']
      .map(s => q.querySelector(s)).filter(Boolean).map(e => e.className).join('|');
    return {
      idx: i + 1,
      qid: (q.parentElement ? (q.parentElement.id || '') : '').replace('question', ''),
      qtype: rows.length ? (rows[0].getAttribute('qtype') || '') : '',
      stem: clean(stemEl),
      options: opts,
      myAnswerText: myAns.replace(/^我的答案[:：]?\s*/, ''),
      scoreText: scoreTxt,
      markCls: markCls.slice(0, 80)
    };
  });
  return {
    sections: [...document.querySelectorAll('.newTestType')].map(e => clean(e)),
    questions: qs,
    pageText: (document.body ? document.body.innerText : '').slice(0, 3000),
    hasSubmit: !!([...document.querySelectorAll('a,button,div,span')]
                  .find(e => (e.innerText || '').trim() === '提交')),
    title: (document.title || '')
  };
}
"""


# --------------------------------------------------------------------------- #
# 字体反混淆
# --------------------------------------------------------------------------- #
def _ref_font(size):
    from PIL import ImageFont
    for p in REF_FONT_CANDIDATES:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    return None


def _bitmap(font, ch, S=64):
    """渲染单字 → 归一化二值向量。"""
    import numpy as np
    from PIL import Image, ImageDraw
    img = Image.new("L", (S * 2, S * 2), 255)
    d = ImageDraw.Draw(img)
    try:
        d.text((S, S), ch, font=font, fill=0, anchor="mm")
    except Exception:
        return None
    ink = 255.0 - np.asarray(img, dtype=np.float32)
    if ink.max() < 10:
        return None
    ys, xs = np.nonzero(ink > 40)
    if len(ys) == 0:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = Image.fromarray(ink[y0:y1, x0:x1].astype("uint8")).resize((S, S), Image.BILINEAR)
    v = (np.asarray(crop, dtype=np.float32).ravel() > 90).astype(np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def extract_embedded_fonts(html):
    """取出页面里所有 @font-face 的 base64 字体。"""
    out = []
    for m in re.finditer(r"@font-face\s*{[^}]*?base64,([A-Za-z0-9+/=\s]{500,})", html, re.S):
        raw = re.sub(r"\s+", "", m.group(1))
        try:
            data = base64.b64decode(raw)
        except Exception:
            continue
        if data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
            out.append(data)
    return out


def build_mapping(font_bytes, min_conf=0.58):
    """自动反解：内嵌字体字形 → 真字。返回 (mapping, low_conf)。"""
    import numpy as np
    from fontTools.ttLib import TTFont

    key = hashlib.sha1(font_bytes).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            return {int(k): v for k, v in d["map"].items()}, d.get("low", [])
        except Exception:
            pass

    tmp = CACHE_DIR / f"{key}.ttf"
    tmp.write_bytes(font_bytes)  # 保留字体文件，便于事后复核/重新生成对照表
    cmap = TTFont(str(tmp)).getBestCmap()
    cps = sorted(cmap.keys())

    from PIL import ImageFont
    gfont = ImageFont.truetype(str(tmp), 80)
    rfont = _ref_font(80)
    if rfont is None:
        return {}, [chr(c) for c in cps]

    cands, vecs = [], []
    for cp in range(0x4E00, 0x9FA6):
        ch = chr(cp)
        try:
            ch.encode("gb2312")
        except Exception:
            continue
        v = _bitmap(rfont, ch)
        if v is not None:
            cands.append(ch)
            vecs.append(v)
    if not vecs:
        return {}, [chr(c) for c in cps]
    M = np.vstack(vecs)

    mapping, low = {}, []
    for cp in cps:
        v = _bitmap(gfont, chr(cp))
        if v is None:
            continue
        sims = M @ v
        order = np.argsort(sims)[::-1]
        i, j = int(order[0]), int(order[1]) if len(order) > 1 else int(order[0])
        conf = float(sims[i])
        margin = float(sims[i] - sims[j])
        # 置信度低、或与第二名太接近（易混字）：先按最优猜测解码，同时登记待核对
        mapping[cp] = cands[i]
        if conf < min_conf or margin < 0.045:
            low.append({"cp": hex(cp), "char": chr(cp), "guess": cands[i],
                        "conf": round(conf, 3), "margin": round(margin, 3),
                        "runner_up": cands[j]})

    cache.write_text(json.dumps({"map": {str(k): v for k, v in mapping.items()},
                                 "low": low}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    try:
        tmp.unlink()
    except Exception:
        pass
    return mapping, low


def decode(text, mapping):
    if not text:
        return text
    return "".join(mapping.get(ord(c), c) for c in text)


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #
def detect_submitted(info):
    """粗判是否已提交/已作答：出现成绩、查看答案、已完成等字样。"""
    t = info.get("pageText") or ""
    if re.search(r"(得分|成绩|查看答案|已提交|已完成|正确答案)", t) and not re.search(r"待完成", t):
        return True
    return False


def normalize_question(q, mapping):
    stem = decode(q["stem"], mapping)
    stem = re.sub(r"^【(单选题|多选题|判断题|填空题|简答题)】\s*", "", stem)
    qtype = None
    for label, key in TYPE_BY_LABEL:
        if label in q["stem"] or label in stem:
            qtype = key
            break
    if qtype is None:
        qtype = {"0": "single_choice", "1": "multi_choice", "3": "judge"}.get(str(q.get("qtype")), "unknown")
    opts = []
    for i, o in enumerate(q["options"]):
        key = (o["letter"] or "").strip()
        if not key or len(key) > 2:
            key = chr(ord("A") + i)
        opts.append({"key": key, "text": decode(o["text"], mapping)})
    if qtype == "judge":
        opts = [{"key": "对", "text": "对"}, {"key": "错", "text": "错"}]

    # 已提交视图会带「我的答案」与本题得分
    my_raw = decode((q.get("myAnswerText") or "").strip(), mapping)
    if qtype == "judge":
        if "对" in my_raw or "正确" in my_raw:
            my_ans = ["对"]
        elif "错" in my_raw or "错误" in my_raw:
            my_ans = ["错"]
        else:
            my_ans = None
    else:
        my_ans = [c for c in my_raw.upper() if c in "ABCDEFGH"] or None
    score_txt = (q.get("scoreText") or "").strip()
    score = None
    m = re.search(r"(-?\d+(?:\.\d+)?)", score_txt)
    if m:
        score = float(m.group(1))
    return {
        "no": q["idx"],
        "qid": q.get("qid", ""),
        "type": qtype,
        "stem": stem,
        "options": opts,
        "my_answer": my_ans,
        "is_correct": None if score is None else (score > 0),
        "score": score,
        "score_text": score_txt,
    }


def fetch_frame_quiz(wf):
    """在 work frame 里取题目结构（原始，未解码）。"""
    return wf.evaluate(JS_QUESTIONS)


def grab_page_html(page, wf):
    """取包含 @font-face 的 HTML：优先 work frame 自身文档，其次顶层页面。"""
    for src in (wf, page):
        try:
            html = src.content()
            if "font-face" in html or "base64," in html:
                return html
        except Exception:
            continue
    try:
        return wf.content()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 复习册
# --------------------------------------------------------------------------- #
HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 参考答案</title>
<style>
:root{{--ink:#1b1f24;--ink2:#5b6472;--line:#e3e6eb;--bg:#f5f6f8;--card:#fff;
--brand:#2563eb;--ok:#0f7b4f;--warn:#8a5a00;--bad:#b42318;--soft:#eef2f8}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
line-height:1.65;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:860px;margin:0 auto;padding:32px 20px 64px}}
.head{{border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:8px}}
h1{{font-size:20px;margin:0 0 8px;letter-spacing:.2px}}
.meta{{font-size:12.5px;color:var(--ink2)}}
.wm{{margin-top:10px;font-size:12.5px;color:var(--warn);background:#fff8e6;
border:1px solid #f0dfae;border-radius:8px;padding:8px 12px}}
.sec{{margin:26px 0 10px;font-size:14px;font-weight:600;color:var(--ink2);
letter-spacing:.5px;border-left:3px solid var(--brand);padding-left:9px}}
.q{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin-bottom:14px}}
.qhead{{display:flex;gap:10px;align-items:baseline;margin-bottom:10px}}
.no{{font-weight:600;color:var(--brand);flex:0 0 auto}}
.stem{{font-size:15px}}
.opt{{display:flex;gap:9px;padding:3px 0 3px 30px;font-size:14.5px;color:#333}}
.opt b{{flex:0 0 18px;color:var(--ink2);font-weight:600}}
.opt.right{{color:var(--ok);font-weight:600}}
.opt.right b{{color:var(--ok)}}
.ans{{margin-top:12px;border-top:1px dashed var(--line);padding-top:12px}}
.badge{{display:inline-block;font-size:11.5px;border-radius:999px;padding:2px 8px;margin-right:6px}}
.b-ai{{background:var(--soft);color:var(--brand)}}
.b-hi{{background:#e7f6ee;color:var(--ok)}}
.b-mid{{background:#fff6e5;color:var(--warn)}}
.b-low{{background:#fdecea;color:var(--bad)}}
.key{{font-size:15px;font-weight:600;color:var(--ok);margin:6px 0}}
details{{margin-top:4px}}
summary{{cursor:pointer;font-size:13.5px;color:var(--brand);outline:none}}
.exp{{font-size:14px;color:#333;margin-top:8px;padding-left:2px}}
.cite{{font-size:12.5px;color:var(--ink2);margin-top:6px}}
.idx{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-top:26px}}
.idx h3{{font-size:13px;margin:0 0 10px;color:var(--ink2);font-weight:600}}
.chip{{display:inline-block;font-size:12.5px;background:#f1f4f9;border-radius:999px;
padding:3px 10px;margin:0 6px 6px 0;color:#33415c}}
.fold .ans{{display:none}}
@media print{{
  body{{background:#fff}} .q{{break-inside:avoid;border:1px solid #ddd}}
  details[open] .exp{{display:block}} .hint{{display:none}}
}}
</style></head><body><div class="wrap">
<div class="head">
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  <div class="wm">{watermark}</div>
</div>
{body}
<div class="idx"><h3>知识点索引</h3>{index}</div>
<div class="meta" style="margin-top:26px;font-size:12px">
  本材料由教师整理，答案标注了来源与置信度：<b>平台答案</b> &gt; <b>反推</b> &gt; <b>AI 生成</b>。
  AI 生成且置信度低的题目，请以你的课堂笔记和教材为准。
</div>
</div>
<script>
document.querySelectorAll('details').forEach(d=>d.open=false);
</script>
</body></html>
"""


def build_answer_key(quiz_data, answers, out_path, title):
    """一页纸速查表。"""
    by_no = {a["no"]: a for a in answers.get("answers", [])}
    lines = [f"# {title} · 参考答案速查", ""]
    lines.append("> 参考答案，仅供预习与复习；考试以线下为准。")
    lines.append("")
    for q in quiz_data["questions"]:
        a = by_no.get(q["no"], {})
        key = a.get("answer") or []
        key_s = "、".join(key) if isinstance(key, list) else str(key)
        conf = {"high": "高", "medium": "中", "low": "低"}.get(a.get("confidence", ""), "")
        tag = a.get("knowledge_tag", "")
        stem = q["stem"]
        if len(stem) > 42:
            stem = stem[:42] + "…"
        lines.append(f"{q['no']}. {stem}")
        lines.append(f"   **{key_s}**" + (f"　（{conf}置信）" if conf else "") +
                     (f"　`{tag}`" if tag else ""))
        lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def build_report(quiz_data, answers, out_path, watermark, title, meta):
    qs = quiz_data["questions"]
    by_no = {a["no"]: a for a in answers.get("answers", [])}
    body_parts, tag_counter = [], {}
    cur_sec = None
    for q in qs:
        a = by_no.get(q["no"], {})
        stem = q["stem"]
        # 题型分组标题
        label = {"single_choice": "单选题", "multi_choice": "多选题", "judge": "判断题",
                 "fill_blank": "填空题", "short_answer": "简答题"}.get(q["type"], "其他")
        if label != cur_sec:
            cur_sec = label
            body_parts.append(f'<div class="sec">{label}</div>')
        key = a.get("answer") or []
        key_s = "、".join(key) if isinstance(key, list) else str(key)
        opts_html = []
        for o in q.get("options", []):
            cls = "opt right" if o["key"] in (key if isinstance(key, list) else []) else "opt"
            opts_html.append(f'<div class="{cls}"><b>{o["key"]}</b><span>{o["text"]}</span></div>')
        conf = a.get("confidence", "")
        src = a.get("source", "ai")
        src_label = {"platform": "平台答案", "inferred": "反推", "ai": "AI 生成"}.get(src, src)
        badges = f'<span class="badge b-ai">{src_label}</span>'
        if conf:
            cmap = {"high": ("b-hi", "高置信"), "medium": ("b-mid", "中置信"), "low": ("b-low", "低置信")}
            cls, txt = cmap.get(conf, ("b-mid", conf))
            badges += f'<span class="badge {cls}">{txt}</span>'
        exp = a.get("explain", "")
        cite = a.get("cite", "")
        cite_html = f'<div class="cite">依据：{cite}</div>' if cite else ""
        tag = a.get("knowledge_tag")
        if tag:
            tag_counter.setdefault(tag, []).append(q["no"])
        body_parts.append(f"""<div class="q">
  <div class="qhead"><span class="no">{q['no']}.</span><span class="stem">{stem}</span></div>
  {''.join(opts_html)}
  <div class="ans">
    {badges}
    <div class="key">参考答案：{key_s}</div>
    <details><summary>展开解析</summary><div class="exp">{exp}</div>{cite_html}</details>
  </div>
</div>""")
    idx = "".join(f'<span class="chip">{t}（第{",".join(map(str, v))}题）</span>'
                  for t, v in tag_counter.items()) or "—"
    html = HTML_TPL.format(title=title, meta=meta, watermark=watermark,
                           body="\n".join(body_parts), index=idx)
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path


def cmd_quiz(cfg, args, cp):
    """quiz 子命令入口（list / fetch / report / docs / auto）。"""
    sub = args.quiz_cmd
    if sub == "list":
        return quiz_list(cfg, cp)
    if sub == "fetch":
        return quiz_fetch(cfg, args, cp)
    if sub == "report":
        return quiz_report(cfg, args)
    if sub == "docs":
        return quiz_docs(cfg, args, cp)
    if sub == "auto":
        return quiz_auto(cfg, args, cp)
    return 1


def quiz_list(cfg, cp):
    from playwright.sync_api import sync_playwright
    cp.ensure_browser(cfg)
    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)
        rows = [c for c in quiz_nodes(cat) if c["level"] == 2]
        print(f"{'编号':<8}{'knowledgeid':<14}标题")
        print("-" * 60)
        for c in rows:
            print(f"{c['num']:<8}{c['kid']:<14}{c['title']}")
        print("-" * 60)
        print(f"共 {len(rows)} 个测验/作业节点（共 {len(cat)} 个目录节点）")
    return 0


WORK_URL_HINTS = ("doHomeWorkNew", "selectWorkQuestionYiPiYue", "work/doHomeWork",
                  "selectWorkQuestion")


def find_work_frame(page):
    """找承载测验内容的 frame（不同入口端点不一样，不要写死）。"""
    for f in page.frames:
        u = f.url or ""
        if any(h in u for h in WORK_URL_HINTS):
            return f
    for f in page.frames:  # 兜底：看有没有题目容器
        try:
            if f.evaluate("() => !!document.querySelector('.TiMu')"):
                return f
        except Exception:
            continue
    return None


QUIZ_NAME_RE = re.compile(r"(测验|测试|作业|习题)")


def quiz_nodes(cat):
    """只保留测验/作业类节点，供选择解析用。"""
    return [c for c in cat if QUIZ_NAME_RE.search(c["name"])]


def _norm_num(num, qcat):
    """把 '2' 归一成 '2.2' 这种真实存在的编号，便于 --from/--to 匹配。"""
    if not num:
        return num
    if any(c["num"] == num for c in qcat):
        return num
    for c in qcat:
        if c["num"].startswith(num + "."):
            return c["num"]
    return num


JS_CLICK_KID = r"""
(kid) => {
  const el = document.getElementById('cur'+kid) || document.getElementById(kid);
  if (el) { (el.querySelector('.posCatalog_name') || el).click(); }
}
"""


def fetch_one_quiz(page, cfg, chap, out_dir, policy_required):
    """抓单套测验并写入 out_dir/questions.json。

    fetch 与 auto 共用这一份实现，避免两处逻辑漂移。
    返回状态 dict（status: ok / no_work / not_submitted）。
    """
    page.evaluate(JS_CLICK_KID, chap["kid"])
    page.wait_for_timeout(7000)
    wf = find_work_frame(page)
    if wf is None:
        print("   没有测验内容，跳过")
        return {"status": "no_work"}

    info = wf.evaluate(JS_QUESTIONS)
    submitted = detect_submitted(info)
    if policy_required and not submitted:
        print("   未提交，按策略跳过（--no-require-submitted 可放开）")
        return {"status": "not_submitted"}

    html = grab_page_html(page, wf)
    fonts = extract_embedded_fonts(html)
    print(f"   内嵌字体 {len(fonts)} 个，题目 {len(info['questions'])} 道")
    mapping, lows = {}, []
    for fb in fonts:
        m, low = build_mapping(fb)
        mapping.update(m)
        lows.extend(low)
    if lows:
        print(f"   ⚠ {len(lows)} 个字形置信度低，需人工核对："
              + ", ".join(x["char"] for x in lows[:12]))

    qs = [normalize_question(q, mapping) for q in info["questions"]]
    data = {
        "course": (cfg.get("course") or {}).get("name", ""),
        "chapter": {"kid": chap["kid"], "num": chap["num"], "title": chap["title"]},
        "work": {"submitted": submitted, "count": len(qs)},
        "policy": {"require_submitted": policy_required,
                   "fetched_at": datetime.now().isoformat(timespec="seconds")},
        "low_confidence_glyphs": lows,
        "sections": [decode(s, mapping) for s in info["sections"]],
        "questions": qs,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "questions.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"   已保存 {out_dir / 'questions.json'}（submitted={submitted}）")
    return {"status": "ok", "count": len(qs), "submitted": submitted,
            "low_glyphs": len(lows)}


def quiz_fetch(cfg, args, cp):
    from playwright.sync_api import sync_playwright
    cp.ensure_browser(cfg)
    run_dir = Path(cfg["output_dir"]) / (args.run or f"quiz-{datetime.now():%Y%m%d-%H%M%S}")
    run_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)

        # 关键：选择只在「测验节点」范围内解析，语义才符合直觉
        qcat = quiz_nodes(cat)
        if not qcat:
            print("这门课的目录里没有找到测验/作业节点")
            return 3
        args.from_ = _norm_num(args.from_, qcat)
        args.to = _norm_num(args.to, qcat)
        plan, unknown = cp.build_plan(qcat, args)
        if not plan:
            print("请用 --chapters 指定测验（例如 2.2），或 --all 抓全部")
            return 3
        if unknown:
            print("未识别:", unknown)

        print(f"计划抓取 {len(plan)} 套测验（课程共 {len(qcat)} 套）：")
        for c in plan:
            print(f"  - {c['num']:<8}{c['title']}")
        if getattr(args, "dry_run", False):
            print("（--dry-run，未实际抓取）")
            return 0

        policy_required = args.require_submitted
        records = []
        for chap in plan:
            print(f"\n→ {chap['num']} {chap['title']}")
            rec = fetch_one_quiz(page, cfg, chap, run_dir / chap["kid"], policy_required)
            rec.update({"kid": chap["kid"], "num": chap["num"]})
            records.append(rec)
        (run_dir / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
        print("\n产物:", run_dir)
    return 0


def quiz_report(cfg, args):
    run_dir = Path(args.run_dir)
    made = 0
    for qf in sorted(run_dir.glob("**/questions.json")):
        if report_one(qf, getattr(args, "watermark", None)):
            made += 1
    if not made:
        print("没有生成任何报告（检查 answers.json 是否存在）")
    return 0 if made else 1


# --------------------------------------------------------------------------- #
# 讲义抓取：给答案提供可核验的课程依据
# --------------------------------------------------------------------------- #
DOC_CACHE = TOOL_DIR / "cache" / "docs"

JS_DOC_IMGS = r"""
() => [...document.querySelectorAll('img')]
  .map(i => i.src || '')
  .filter(s => s.includes('/thumb/'))
"""


def doc_roots(page):
    """从文档查看器里取出整页图片的根地址（去掉 thumb/N.png 尾巴）。

    平台把每页渲染成 s3.cldisk.com/.../thumb/N.png，剥掉尾部即可按页遍历。
    """
    out = []
    for f in page.frames:
        try:
            srcs = f.evaluate(JS_DOC_IMGS)
        except Exception:
            continue
        for s in srcs or []:
            m = re.match(r"^(https?://[^?#]+?)/thumb/\d+\.(?:png|jpg|webp)$", s)
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


def grab_doc(ctx, root, out_dir, tag="doc", max_pages=200):
    """按页抓取讲义图片，遇到非 200 / 非图片就停。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i in range(1, max_pages + 1):
        for ext in ("png", "jpg"):
            u = f"{root}/thumb/{i}.{ext}"
            try:
                r = ctx.request.get(u, timeout=30000)
            except Exception:
                continue
            if r.status != 200:
                continue
            body = r.body()
            if len(body) < 2000:
                continue
            if ext == "png" and body[1:4] != b"PNG":
                continue
            if ext == "jpg" and body[:2] != b"\xff\xd8":
                continue
            (out_dir / f"{tag}_p{i:02d}.{ext}").write_bytes(body)
            n += 1
            break
        else:
            break
    return n


def quiz_docs(cfg, args, cp):
    """抓取选中章节的讲义整页图，存到 cache/docs/<章节号>/。

    这是提升答案准确率的关键一步：有讲义时答案可引用具体页，
    没有讲义时只能靠通用知识，置信度必须下调。
    """
    from playwright.sync_api import sync_playwright

    cp.ensure_browser(cfg)
    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)

        plan, unknown = cp.build_plan(cat, args)
        if unknown:
            print("未识别：", unknown)
        plan = [c for c in plan if not QUIZ_NAME_RE.search(c["name"])]
        if not plan:
            print("没有可抓讲义的章节节点（测验节点已排除）")
            return 3

        print(f"计划抓取 {len(plan)} 节的讲义：")
        for c in plan:
            print(f"  - {c['num']:<6}{c['title']}")
        if getattr(args, "dry_run", False):
            return 0

        index = {}
        for c in plan:
            page.goto(cp.course_url(cfg, c["kid"]), wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)
            roots = doc_roots(page)
            out_dir = DOC_CACHE / str(c["num"])
            if not roots:
                print(f"  {c['num']} {c['title']}: 无讲义（纯视频节点）")
                index[c["num"]] = {"title": c["title"], "kid": c["kid"],
                                   "pages": 0, "roots": []}
                continue
            total = 0
            for k, root in enumerate(roots, 1):
                tag = "doc" if len(roots) == 1 else f"doc{k}"
                got = grab_doc(ctx, root, out_dir, tag)
                print(f"    源{k}: {got} 页 {root[:80]}")
                total += got
            print(f"  {c['num']} {c['title']}: 共 {total} 页 -> {out_dir}")
            index[c["num"]] = {"title": c["title"], "kid": c["kid"],
                               "pages": total, "roots": roots}

        DOC_CACHE.mkdir(parents=True, exist_ok=True)
        (DOC_CACHE / "_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        have = sum(1 for v in index.values() if v["pages"])
        print(f"完成：{have}/{len(index)} 节有讲义，共 "
              f"{sum(v['pages'] for v in index.values())} 页 -> {DOC_CACHE}")
    return 0


# --------------------------------------------------------------------------- #
# 一键流水线：scan → docs → fetch → pack → report
# --------------------------------------------------------------------------- #
def chapter_key(num):
    """'5' -> 'ch05'，保证目录排序稳定。"""
    try:
        return f"ch{int(str(num).split('.')[0]):02d}"
    except Exception:
        return f"ch_{num}"


def scan_chapters(cat):
    """把目录按章分组：{章号: {num,title,kid,nodes:[...]}}"""
    out, cur = {}, None
    for c in cat:
        if c.get("level") == 1:
            cur = str(c["num"]).strip()
            out[cur] = {"num": cur, "title": c["name"], "kid": c.get("kid"), "nodes": []}
        elif cur is not None:
            out[cur]["nodes"].append(c)
    return out


def _minor(num):
    """取小节序号：'5.4' -> 4.0；取不到给一个大数（排到最后）。"""
    try:
        return float(str(num).split(".")[1])
    except Exception:
        return 999.0


def link_quiz_docs(chapter):
    """推断「测验 N.M 的依据 = 同章下编号小于 M 的非测验节点」。"""
    quizzes = [n for n in chapter["nodes"] if QUIZ_NAME_RE.search(n["name"])]
    lessons = [n for n in chapter["nodes"] if not QUIZ_NAME_RE.search(n["name"])]
    pairs = []
    for q in quizzes:
        qm = _minor(q["num"])
        deps = [l for l in lessons if _minor(l["num"]) < qm]
        pairs.append((q, deps))
    return pairs


def parse_docs_for(raw):
    """把 --docs-for 解析成 {测验编号: [讲义编号...]}。

    支持 `5.5=5.1,5.2`，多组用 `;` 隔开：`5.5=5.1,5.2;5.6=5.3`。
    全角符号也认，省得切换输入法。
    """
    out = {}
    if not raw:
        return out
    for part in str(raw).replace("；", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"--docs-for 格式错误：{part}（应写成 5.5=5.1,5.2）")
        qnum, nums = part.split("=", 1)
        out[qnum.strip()] = [n.strip() for n in nums.replace("，", ",").split(",") if n.strip()]
    return out


def resolve_deps(nums, chapters, cat):
    """把讲义编号还原成目录节点，返回 (nodes, 找不到的编号)。"""
    pool = {}
    for ch in chapters.values():
        for n in ch["nodes"]:
            if not QUIZ_NAME_RE.search(n["name"]):
                pool.setdefault(str(n["num"]), n)
    for c in cat:
        if not QUIZ_NAME_RE.search(c.get("name") or ""):
            pool.setdefault(str(c.get("num")), c)
    nodes, missing = [], []
    for num in nums:
        n = pool.get(num)
        if n is None:
            missing.append(num)
        else:
            nodes.append(n)
    return nodes, missing


def load_flow(run_dir):
    f = Path(run_dir) / "_flow.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"quizzes": {}}


def save_flow(run_dir, flow):
    flow["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (Path(run_dir) / "_flow.json").write_text(
        json.dumps(flow, ensure_ascii=False, indent=1), encoding="utf-8")


def docs_pages(num):
    """cache/docs/<num>/ 下已有的讲义页数。"""
    d = DOC_CACHE / str(num)
    if not d.exists():
        return 0
    return len([p for p in d.iterdir() if p.suffix.lower() in (".png", ".jpg")])


def grab_docs_for(page, cfg, cp, node, force=False):
    """抓单个节点的讲义到 cache/docs/<章节号>/，已有则跳过。返回页数。"""
    num = str(node["num"])
    out_dir = DOC_CACHE / num
    if not force:
        have = docs_pages(num)
        if have:
            print(f"   {num} 讲义已存在（{have} 页），跳过")
            return have
    page.goto(cp.course_url(cfg, node["kid"]), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(12000)
    roots = doc_roots(page)
    if not roots:
        print(f"   {num} 无讲义（纯视频节点）")
        out_dir.mkdir(parents=True, exist_ok=True)
        return 0
    total = 0
    for k, root in enumerate(roots, 1):
        tag = "doc" if len(roots) == 1 else f"doc{k}"
        total += grab_doc(page.context, root, out_dir, tag)
    print(f"   {num} 讲义 {total} 页")
    return total


def build_solve_payload(quiz_dir, data, docs_meta):
    """生成 solve_payload.md：把题目 + 依据材料打包成一份「待作答」文件。"""
    qdir = Path(quiz_dir)
    ch = data["chapter"]
    qs = data["questions"]
    L = []
    L.append(f"# 待作答：{ch['num']} {ch['title']}（{len(qs)} 题）\n")
    L.append("## 依据材料\n")
    for num, pages in docs_meta.items():
        if pages:
            L.append(f"- `{num}`：{pages} 页，见 `cache/docs/{num}/doc_p01.png` 起")
        else:
            L.append(f"- `{num}`：**无讲义**（纯视频节点），涉及它的题 confidence 必须标 `low`")
    L.append("")
    L.append("## 作答要求\n")
    L.append("1. 优先依据上面列出的讲义页；讲义未覆盖时用通用学科知识，但必须如实标注。")
    L.append("2. `cite` 写成「讲义 X.Y 第N页（该页讲了什么）」；无依据写「通用学科知识（X.Y 无讲义）」。")
    L.append("3. 每题做两遍：先正向定答案，再逐项否定干扰项；两遍不一致就把 `confidence` 降级。")
    L.append("4. 不确定的题不要硬凑，标 `low` 并在 `verify.note` 里写明分歧点。")
    L.append("5. 只写答案，不改题目。\n")
    L.append("## 题目\n")
    for q in qs:
        L.append(f"### {q['no']}. {q['stem']}")
        L.append("")
        for o in q["options"]:
            L.append(f"- {o['key']}. {o['text']}")
        L.append("")
    L.append("## 输出格式\n")
    L.append("写入同目录 `answers.json`：\n")
    L.append("```json")
    L.append('{\n "generated_by": "AI 生成，逐题对照讲义双重校验",')
    L.append(f' "policy": {{"course": "", "chapter": "{ch["num"]}", "purpose": "preview"}},')
    L.append(' "answers": [')
    L.append('  {"no": 1, "answer": ["B"], "source": "ai", "confidence": "high",')
    L.append('   "explain": "为什么选它、为什么不选其他项",')
    L.append('   "cite": "讲义 5.1 第3页（xxx）", "knowledge_tag": "知识点",')
    L.append('   "verify": {"pass2": "B", "agree": true, "note": "两遍一致"}}')
    L.append(" ]\n}")
    L.append("```\n")
    L.append("> 判断题 `answer` 写 `[\"对\"]` 或 `[\"错\"]`；多选题写全部字母如 `[\"A\",\"C\"]`。")
    out = qdir / "solve_payload.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def report_one(qf, watermark=None):
    """用单个 questions.json + answers.json 出复习册。返回是否生成。"""
    qf = Path(qf)
    d = json.loads(qf.read_text(encoding="utf-8"))
    af = qf.parent / "answers.json"
    if not af.exists():
        print(f"   {qf.parent.name}：缺少 answers.json，跳过出报告")
        return False
    a = json.loads(af.read_text(encoding="utf-8"))
    title = f"{d['chapter']['num']} {d['chapter']['title']}"
    meta = (f"{d.get('course', '')} · {len(d['questions'])} 题 · "
            f"生成于 {d['policy']['fetched_at']} · "
            f"策略 require_submitted={d['policy']['require_submitted']}")
    wm = watermark or "参考答案 · 仅供预习与复习，考试以线下为准"
    out = qf.parent / "参考答案.html"
    build_report(d, a, out, wm, title, meta)
    build_answer_key(d, a, qf.parent / "答案速查.md", title)
    print(f"   已生成 {out.name} + 答案速查.md")
    return True


def quiz_auto(cfg, args, cp):
    """一键：抓讲义 → 抓题 → 打包作答载荷 →（有答案时）出复习册。

    与 play --all 的区别：play 的 --all 是整个目录的播放节点；
    这里的 --all 只处理测验节点，且不会播放任何视频。
    """
    from playwright.sync_api import sync_playwright

    cp.ensure_browser(cfg)
    run_root = Path(cfg["output_dir"])
    force = getattr(args, "force", False)

    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)
        qcat = quiz_nodes(cat)
        if not qcat:
            print("目录里没有测验节点")
            return 3

        # ---- 选择目标测验 ----
        if args.all:
            targets = qcat
        else:
            args.from_ = _norm_num(getattr(args, "from_", None), qcat)
            args.to = _norm_num(getattr(args, "to", None), qcat)
            targets, unknown = cp.build_plan(qcat, args)
            if unknown:
                print("未识别：", unknown)
            if not targets:
                print("请用 --chapter/--chapters 指定章节，或 --all 处理全部测验")
                return 3

        # ---- 按章建立计划 ----
        chapters = scan_chapters(cat)
        docs_for = parse_docs_for(getattr(args, "docs_for", None))
        jobs, blocked = [], []
        for q in targets:
            ch_num = str(q["num"]).split(".")[0]
            ch = chapters.get(ch_num)
            if not ch:
                blocked.append((q, "目录里找不到所属章"))
                continue
            pairs = link_quiz_docs(ch)
            dep_map = {qq["kid"]: deps for qq, deps in pairs}
            override = docs_for.get(str(q["num"]))
            if override:
                deps, missing = resolve_deps(override, chapters, cat)
                if missing:
                    blocked.append((q, f"--docs-for 里的讲义编号不存在：{','.join(missing)}"))
                    continue
            else:
                deps = dep_map.get(q["kid"], [])
                if len(pairs) > 1:
                    guess = ",".join(str(l["num"]) for l in ch["nodes"]
                                     if not QUIZ_NAME_RE.search(l["name"])
                                     and _minor(l["num"]) < _minor(q["num"]))
                    hint = f"（可用 --docs-for {q['num']}={guess or '5.1,5.2'} 指定）"
                    blocked.append((q, f"该章有 {len(pairs)} 套测验，无法自动判断依据{hint}"))
                    continue
            jobs.append({"chapter": ch, "quiz": q, "deps": deps})

        unused = [k for k in docs_for if k not in {str(t["num"]) for t in targets}]
        if unused:
            print(f"  ⚠ --docs-for 里的 {','.join(unused)} 不在本次目标内，已忽略\n")

        print(f"计划处理 {len(jobs)} 套测验：\n")
        for j in jobs:
            ch, q = j["chapter"], j["quiz"]
            rdir = run_root / chapter_key(ch["num"])
            flow = load_flow(rdir)
            st = (flow["quizzes"].get(q["num"]) or {}).get("state", "todo")
            deps_txt = " ".join(
                f"{d['num']}({docs_pages(d['num']) or '无'}页)" for d in j["deps"]
            ) or "（无讲义节点）"
            print(f"  第{ch['num']}章 {ch['title']}")
            print(f"    依据讲义：{deps_txt}")
            print(f"    目标测验：{q['num']} {q['title']}  kid={q['kid']}")
            print(f"    产物目录：{rdir.relative_to(run_root)}/quiz/{q['num']}/")
            print(f"    当前状态：{st}")
            print()
        if blocked:
            for q, why in blocked:
                print(f"  ⚠ 跳过 {q['num']}：{why}")
            print()
        if getattr(args, "dry_run", False):
            print("（--dry-run，未实际抓取）")
            return 0
        if not jobs:
            return 3

        # ---- 执行 ----
        policy_required = args.require_submitted
        summary = []
        for j in jobs:
            ch, q, deps = j["chapter"], j["quiz"], j["deps"]
            rdir = run_root / chapter_key(ch["num"])
            qdir = rdir / "quiz" / str(q["num"])
            qdir.mkdir(parents=True, exist_ok=True)
            flow = load_flow(rdir)
            flow["chapter"] = {"num": ch["num"], "title": ch["title"]}
            rec = flow["quizzes"].setdefault(str(q["num"]), {
                "kid": q["kid"], "title": q["title"],
                "dir": str(qdir.relative_to(run_root)).replace("\\", "/"),
                "docs": [d["num"] for d in deps], "state": "todo"})
            print(f"\n=== {q['num']} {q['title']} ===")

            try:
                # ① 讲义
                pages = {}
                if not getattr(args, "report_only", False):
                    for d in deps:
                        pages[str(d["num"])] = grab_docs_for(page, cfg, cp, d, force=force)
                    rec["docs_pages"] = pages
                    rec["state"] = "docs_ready"
                    if any(v == 0 for v in pages.values()):
                        rec["state"] = "no_docs" if not any(pages.values()) else "docs_ready"
                    save_flow(rdir, flow)

                    # ② 抓题
                    qf = qdir / "questions.json"
                    if qf.exists() and not force:
                        print("   题目已存在，跳过抓取")
                        rec["count"] = len(json.loads(qf.read_text(encoding="utf-8"))["questions"])
                    else:
                        r = fetch_one_quiz(page, cfg, q, qdir, policy_required)
                        if r["status"] != "ok":
                            rec["state"] = r["status"]
                            save_flow(rdir, flow)
                            summary.append((q["num"], r["status"]))
                            continue
                        rec["count"] = r["count"]
                        rec["submitted"] = r["submitted"]
                    rec["state"] = "fetched"
                    save_flow(rdir, flow)

                    # ③ 打包作答载荷
                    data = json.loads(qf.read_text(encoding="utf-8"))
                    payload = build_solve_payload(qdir, data, pages or {
                        str(k): v for k, v in (rec.get("docs_pages") or {}).items()})
                    rec["state"] = "packed"
                    save_flow(rdir, flow)
                    print(f"   已生成作答载荷 {payload.name}（{rec['count']} 题）")

                # ④ 出报告
                qf = qdir / "questions.json"
                if qf.exists():
                    made = report_one(qf)
                    rec["has_answers"] = (qdir / "answers.json").exists()
                    rec["has_report"] = made
                    if made:
                        rec["state"] = "reported"
                    elif rec["has_answers"] is False:
                        rec["state"] = "answers_pending"
                save_flow(rdir, flow)
                summary.append((q["num"], rec["state"]))
            except Exception as e:
                print(f"   ✗ 失败：{repr(e)[:160]}")
                rec["state"] = "failed"
                rec["error"] = repr(e)[:200]
                save_flow(rdir, flow)
                summary.append((q["num"], "failed"))

        print("\n=== 汇总 ===")
        for num, st in summary:
            print(f"  {num:<8}{st}")
        pending = [n for n, s in summary if s in ("packed", "answers_pending")]
        if pending:
            print(f"\n待作答：{', '.join(pending)}")
            print("  这些套的 solve_payload.md 已就绪，作答后跑 "
                  "`quiz auto --chapters <编号> --report-only` 出复习册")
    return 0
