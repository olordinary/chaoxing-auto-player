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
  const typeOf = (q) => {
    const rows = [...q.querySelectorAll('ul.Zy_ulTop > li')];
    const qt = rows.length ? (rows[0].getAttribute('qtype') || '') : '';
    return qt;
  };
  const qs = [...document.querySelectorAll('.TiMu')].map((q, i) => {
    const stemEl = q.querySelector('.Zy_TItle > div.font-cxsecret') || q.querySelector('.Zy_TItle');
    const opts = [...q.querySelectorAll('ul.Zy_ulTop > li')].map(li => {
      const num = li.querySelector('.num_option');
      const after = li.querySelector('a.after');
      return {
        letter: (num ? (num.getAttribute('data') || clean(num)) : '').trim(),
        text: clean(after)
      };
    });
    return {
      idx: i + 1,
      qid: (q.parentElement ? (q.parentElement.id || '') : '').replace('question', ''),
      qtype: typeOf(q),
      stem: clean(stemEl),
      options: opts
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
    tmp.write_bytes(font_bytes)
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
        i = int(np.argmax(sims))
        conf = float(sims[i])
        if conf < min_conf:
            low.append({"cp": hex(cp), "char": chr(cp), "guess": cands[i], "conf": round(conf, 3)})
            continue
        mapping[cp] = cands[i]

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
    return {
        "no": q["idx"],
        "qid": q.get("qid", ""),
        "type": qtype,
        "stem": stem,
        "options": opts,
        "my_answer": None,
        "is_correct": None,
        "score": None,
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
    """quiz 子命令入口（list / fetch / report）。"""
    sub = args.quiz_cmd
    if sub == "list":
        return quiz_list(cfg, cp)
    if sub == "fetch":
        return quiz_fetch(cfg, args, cp)
    if sub == "report":
        return quiz_report(cfg, args)
    return 1


def quiz_list(cfg, cp):
    from playwright.sync_api import sync_playwright
    cp.ensure_browser(cfg)
    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)
        rows = [c for c in cat if re.search(r"(测验|测试|作业|习题)", c["name"]) and c["level"] == 2]
        print(f"{'编号':<8}{'knowledgeid':<14}标题")
        print("-" * 60)
        for c in rows:
            print(f"{c['num']:<8}{c['kid']:<14}{c['title']}")
        print("-" * 60)
        print(f"共 {len(rows)} 个测验/作业节点（共 {len(cat)} 个目录节点）")
    return 0


def quiz_fetch(cfg, args, cp):
    from playwright.sync_api import sync_playwright
    cp.ensure_browser(cfg)
    run_dir = Path(cfg["output_dir"]) / (args.run or f"quiz-{datetime.now():%Y%m%d-%H%M%S}")
    run_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = cp.connect(p, cfg).contexts[0]
        page = cp.get_course_page(ctx, cfg)
        cat = cp.get_catalog(page, cfg)
        plan, unknown = cp.build_plan(cat, args) if hasattr(cp, "build_plan") else ([], [])
        if not plan:
            print("请用 --chapters 指定章节（例如 2.2）")
            return 3
        if unknown:
            print("未识别:", unknown)

        policy_required = args.require_submitted
        records = []
        for chap in plan:
            print(f"\n→ {chap['num']} {chap['title']}")
            page.evaluate("""(kid) => {
                const el = document.getElementById('cur'+kid) || document.getElementById(kid);
                if (el) { (el.querySelector('.posCatalog_name') || el).click(); }
            }""", chap["kid"])
            page.wait_for_timeout(7000)
            wf = next((f for f in page.frames if "doHomeWorkNew" in (f.url or "")), None)
            if wf is None:
                print("   没有测验内容，跳过")
                records.append({"kid": chap["kid"], "num": chap["num"], "status": "no_work"})
                continue

            info = wf.evaluate(JS_QUESTIONS)
            submitted = detect_submitted(info)
            if policy_required and not submitted:
                print("   未提交，按策略跳过（--no-require-submitted 可放开）")
                records.append({"kid": chap["kid"], "num": chap["num"], "status": "not_submitted"})
                continue

            html = grab_page_html(page, wf)
            fonts = extract_embedded_fonts(html)
            print(f"   内嵌字体 {len(fonts)} 个，题目 {len(info['questions'])} 道")
            mapping = {}
            lows = []
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
            out = run_dir / chap["kid"]
            out.mkdir(parents=True, exist_ok=True)
            (out / "questions.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
            print(f"   已保存 {out / 'questions.json'}（submitted={submitted}）")
            records.append({"kid": chap["kid"], "num": chap["num"], "status": "ok",
                            "count": len(qs), "submitted": submitted})
        (run_dir / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
        print("\n产物:", run_dir)
    return 0


def quiz_report(cfg, args):
    run_dir = Path(args.run_dir)
    made = 0
    for qf in sorted(run_dir.glob("*/questions.json")):
        d = json.loads(qf.read_text(encoding="utf-8"))
        af = qf.parent / "answers.json"
        if not af.exists():
            print(f"跳过 {qf.parent.name}：缺少 answers.json")
            continue
        a = json.loads(af.read_text(encoding="utf-8"))
        title = f"{d['chapter']['num']} {d['chapter']['title']}"
        meta = (f"{d.get('course','')} · {len(d['questions'])} 题 · "
                f"生成于 {d['policy']['fetched_at']} · "
                f"策略 require_submitted={d['policy']['require_submitted']}")
        wm = args.watermark or "参考答案 · 仅供预习与复习，考试以线下为准"
        out = qf.parent / "参考答案.html"
        build_report(d, a, out, wm, title, meta)
        md = build_answer_key(d, a, qf.parent / "答案速查.md", title)
        print("已生成", out, "和", md.name)
        made += 1
    return 0 if made else 1
