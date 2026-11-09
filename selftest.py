# -*- coding: utf-8 -*-
"""selftest.py —— 离线自检：不联网、不花钱，先把「抓题 → 作答 → 出册」跑通。

为什么需要它：
    真跑一次自动作答要消耗 token，而且一旦报错很难判断是"模型答得不好"
    还是"流水线本身有 bug"。这里用一个本地假模型（实现同一套
    /chat/completions 协议）把整条链路走一遍，几十秒就能确认：
    提示词装配、JSON 解析、答案规范化、置信度规则、落盘、出册全都没问题。

用法：
    python selftest.py            # 纯离线（推荐先跑这个）
    python selftest.py --net      # 额外测一遍真实检索链路（会访问 360/搜狗）

产物在 .workbuddy/_selftest/，可以随时删。
"""
import argparse
import json
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

import llm          # noqa: E402
import netkit       # noqa: E402
import quiz         # noqa: E402
import solver       # noqa: E402
import websearch    # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f"  {detail}" if detail and not cond else ""))
    return bool(cond)


# --------------------------------------------------------------------------- #
# 假模型：实现 OpenAI 兼容的 /v1/chat/completions
# --------------------------------------------------------------------------- #
_RE_Q_HEAD = re.compile(r"^(\d+)\. \[(\w+)\] ", re.M)
_RE_OPT = re.compile(r"^\s+([A-H])\. (.+)$", re.M)
DOC_CITE = "讲义 {num} 第{page}页（秋水仙素抑制纺锤体形成，使染色体数目加倍）"


class MockLLM:
    """一个只会照套路回 JSON 的假模型。

    它做的事恰好是真实模型该做的事：读题目 → 按题号给答案 → 给依据。
    答案本身对不对不重要，重要的是**格式与字段**能不能被流水线吃下去。
    """

    def __init__(self):
        self.calls = []
        self.list_once = False      # True 时第二遍故意先返回顶层数组，模拟真实模型的坏习惯
        self._list_done = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    body = {}
                outer.calls.append(body)
                text = outer.reply(body)
                payload = {
                    "id": "mock", "object": "chat.completion", "model": "mock-1",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                              "total_tokens": 150},
                }
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # ---- 生命周期 ----
    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    # ---- 回复生成 ----
    @staticmethod
    def _user_text(body):
        for m in reversed(body.get("messages") or []):
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):        # 多模态：文字片段拼起来
                return "\n".join(x.get("text", "") for x in c
                                 if isinstance(x, dict) and x.get("type") == "text")
        return ""

    def reply(self, body):
        user = self._user_text(body)
        if "image_url" in json.dumps(body, ensure_ascii=False)[:200000] and "讲义" in user:
            # 讲义摘要请求
            nums = re.findall(r"讲义 ([\d.]+) 的第 (\d+)", user)
            num = nums[0][0] if nums else "12.1"
            pages = sorted({int(p) for _, p in nums}) or [1]
            return json.dumps({"pages": [{"page": p,
                                          "facts": ["秋水仙素抑制纺锤体形成",
                                                    "染色体数目加倍，形成多倍体"]}
                                         for p in pages]}, ensure_ascii=False)

        qs = []
        for no, qtype in _RE_Q_HEAD.findall(user):
            qs.append((int(no), qtype))
        if not qs:
            return "{}"

        # 每题的选项：按题号切段后各自取字母
        segs = re.split(r"^\d+\. \[", user, flags=re.M)[1:]
        use_doc = "【讲义摘要】" in user and "讲义 12.1" in user
        pass2 = "rationale_options" in user

        out = []
        for (no, qtype), seg in zip(qs, segs):
            letters = _RE_OPT.findall(seg)
            keys = [k for k, _ in letters]
            if qtype == "judge":
                ans = ["对"]
            elif qtype == "multi_choice" and len(keys) >= 2:
                ans = keys[:2]
            elif keys:
                ans = [keys[0]]
            else:
                ans = ["对"]
            cite = DOC_CITE.format(num="12.1", page=1) if use_doc else "通用学科知识"
            item = {"no": no, "answer": ans, "confidence": "high",
                    "explain": "据讲义所述，秋水仙素抑制纺锤体形成，使染色体不能移向两极。",
                    "cite": cite, "knowledge_tag": "染色体加倍", "doubt": ""}
            if pass2:
                item["rationale_options"] = {
                    k: ("正确，符合讲义表述" if k in ans else "错误，与讲义表述不符")
                    for k in keys or ["A", "B"]}
            out.append(item)
        if pass2 and self.list_once and not self._list_done:
            # 模拟实测到的情况：第二遍把 answers 直接当顶层数组吐出来
            self._list_done = True
            return json.dumps(out, ensure_ascii=False)
        return json.dumps({"answers": out}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 准备测试数据
# --------------------------------------------------------------------------- #
def make_fixture(root, with_docs=True):
    """造一个最小 run 目录：ch12/_flow.json + quiz/12.3/questions.json(+讲义图)。"""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    qdir = root / "ch12" / "quiz" / "12.3"
    qdir.mkdir(parents=True)

    src = TOOL_DIR / "runs" / "ch12" / "quiz" / "12.3" / "questions.json"
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
        data["questions"] = data["questions"][:4]      # 只取前 4 题，跑得快
    else:
        data = {
            "course": "测试课程", "chapter": {"num": "12.3", "title": "章节测试"},
            "policy": {"require_submitted": False,
                       "fetched_at": "2026-11-02T10:00:00"},
            "questions": [
                {"no": 1, "type": "single_choice",
                 "stem": "秋水仙素诱导染色体加倍的主要作用机理是:",
                 "options": [{"key": "A", "text": "促进染色体复制"},
                             {"key": "B", "text": "抑制纺锤体形成"},
                             {"key": "C", "text": "促进细胞分裂"}]},
                {"no": 2, "type": "multi_choice", "stem": "多倍体的特点包括:",
                 "options": [{"key": "A", "text": "器官较大"},
                             {"key": "B", "text": "结实率下降"},
                             {"key": "C", "text": "生长发育缓慢"}]},
                {"no": 3, "type": "judge",
                 "stem": "单倍体植物的隐性突变可直接表现。",
                 "options": [{"key": "对", "text": "对"}, {"key": "错", "text": "错"}]},
            ],
        }
    (qdir / "questions.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    (root / "ch12" / "_flow.json").write_text(json.dumps(
        {"chapter": {"num": "12", "title": "染色体工程"},
         "quizzes": {"12.3": {"docs": ["12.1"] if with_docs else []}}},
        ensure_ascii=False, indent=1), encoding="utf-8")

    if with_docs:
        d = root / "docs" / "12.1"
        d.mkdir(parents=True)
        try:
            from PIL import Image, ImageDraw
            for p in (1, 2):
                im = Image.new("RGB", (640, 400), "white")
                ImageDraw.Draw(im).text((20, 20), "lecture page %d" % p, fill="black")
                im.save(d / f"doc_p{p:02d}.png")
        except Exception:
            (d / "doc_p01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return qdir, len(data["questions"])


def mock_cfg(mock, vision=False):
    return {
        "llm": {"active": "mock", "providers": {"mock": {
            "label": "自检用假模型", "base_url": mock.base_url, "api_key": "test",
            "model": "mock-1", "vision": vision, "json_mode": True,
            "max_tokens": 4096, "temperature": 0, "timeout": 30}}},
        "search": {"engine": "auto", "max_results": 4},
        "net": {"proxy": "auto"},
    }


# --------------------------------------------------------------------------- #
# ① 纯函数单测
# --------------------------------------------------------------------------- #
def test_units():
    print("\n[1/5] 基础函数")

    obj = llm.extract_json('说明文字\n```json\n{"a": 1,}\n```')
    check("llm.extract_json 容忍代码围栏与尾逗号", obj == {"a": 1}, str(obj))
    check("llm.extract_json 对垃圾输入返回 None",
          llm.extract_json("完全不是 JSON") is None)

    q = {"no": 1, "type": "judge", "options": [{"key": "对"}, {"key": "错"}]}
    check("norm_answer 判断题 对/√/TRUE 归一",
          solver.norm_answer(q, ["√"])[0] == ["对"]
          and solver.norm_answer(q, ["错误"])[0] == ["错"])

    q2 = {"no": 2, "type": "multi_choice",
          "options": [{"key": "A"}, {"key": "B"}, {"key": "C"}]}
    check("norm_answer 多选题按选项顺序排序去重",
          solver.norm_answer(q2, ["C", "A", "A"])[0] == ["A", "C"])

    q3 = {"no": 3, "type": "single_choice",
          "options": [{"key": "A"}, {"key": "B"}]}
    ans, prob = solver.norm_answer(q3, ["A", "B"])
    check("norm_answer 单选题多给字母时取第一个并告警", ans == ["A"] and prob)

    a1 = {"answer": ["B"], "confidence": "high",
          "cite": "讲义 12.1 第3页（纺锤体）"}
    r_hi = solver.finalize(q3, a1, dict(a1), digest_used=True, web_used=False)
    check("finalize 两遍一致 + 引用讲义页码 → high", r_hi["confidence"] == "high",
          r_hi["confidence"])

    a2 = {"answer": ["A"], "confidence": "high", "cite": "通用学科知识"}
    r_bad = solver.finalize(q3, a1, a2, digest_used=True, web_used=False)
    check("finalize 两遍不一致 → low", r_bad["confidence"] == "low",
          r_bad["confidence"])

    fake = {"answer": ["B"], "confidence": "high", "cite": "讲义 9.9 第7页（编的）"}
    r_fake = solver.finalize(q3, fake, dict(fake), digest_used=False, web_used=False)
    check("finalize 没用讲义却编页码 → 引用被改写为通用知识",
          "通用" in r_fake["cite"], r_fake["cite"])

    web_fake = {"answer": ["B"], "confidence": "high", "cite": "检索[1]"}
    r_nw = solver.finalize(q3, web_fake, dict(web_fake), digest_used=False,
                           web_used=False)
    check("finalize 没联网却引检索 → 引用被改写",
          "通用" in r_nw["cite"], r_nw["cite"])

    page_idx = {"12.1": {1, 2, 3}}
    bad = {"answer": ["B"], "confidence": "high", "cite": "讲义 12.1 第99页（编的）"}
    r_bad_page = solver.finalize(q3, bad, dict(bad), digest_used=True, web_used=False,
                                 page_index=page_idx)
    check("引用不存在的页码会被改写", "不存在" in r_bad_page["cite"],
          r_bad_page["cite"])
    check("引用不存在的页码拿不到 high", r_bad_page["confidence"] != "high",
          r_bad_page["confidence"])
    good = {"answer": ["B"], "confidence": "high", "cite": "讲义 12.1 第2页（真有的）"}
    r_good_page = solver.finalize(q3, good, dict(good), digest_used=True, web_used=False,
                                  page_index=page_idx)
    check("引用真实页码仍然可以 high", r_good_page["confidence"] == "high",
          r_good_page["confidence"])

    check("build_query 去掉问句套话",
          "主要作用机理" not in websearch.build_query("秋水仙素诱导染色体加倍的主要作用机理是:"),
          websearch.build_query("秋水仙素诱导染色体加倍的主要作用机理是:"))
    kw = websearch.keyword_query("现在普遍认为单倍体是指:")
    check("keyword_query 压到很短且保留实词", 0 < len(kw) <= 20, kw)
    qs_variants = websearch.queries_for("秋水仙素诱导染色体加倍的主要作用机理是:")
    check("queries_for 返回 1–2 套非空且不重复的查询词",
          1 <= len(qs_variants) <= 2 and all(qs_variants)
          and len(set(qs_variants)) == len(qs_variants), str(qs_variants))

    hits = [{"title": "秋水仙素诱导染色体加倍", "snippet": "抑制纺锤体形成"}]
    check("filter_relevant 保留相关命中",
          len(websearch.filter_relevant(hits, "秋水仙素诱导染色体加倍")) == 1)
    check("filter_relevant 丢掉《庄子·秋水》这类噪声",
          len(websearch.filter_relevant(
              [{"title": "秋水（《庄子》作品）", "snippet": "庄子外篇"}],
              "秋水仙素诱导染色体加倍")) == 0)

    check("html_to_text 去脚本样式并保留正文",
          netkit.html_to_text("<style>x{}</style><p>要保留的正文</p>").strip()
          == "要保留的正文")
    check("_cjk_join 去掉汉字间多余空格",
          websearch._cjk_join("秋水仙素 诱导 染色体 加倍") == "秋水仙素诱导染色体加倍")


# --------------------------------------------------------------------------- #
# ② 端到端：无讲义（纯文本作答）
# --------------------------------------------------------------------------- #
def test_pipeline_plain(root):
    print("\n[2/5] 端到端：无讲义（纯文本两遍作答）")
    qdir, n = make_fixture(root / "plain", with_docs=False)
    solver.DOC_CACHE = root / "plain" / "docs"      # 隔离缓存，别污染真实 cache/
    quiz.DOC_CACHE = solver.DOC_CACHE

    mock = MockLLM().start()
    try:
        cfg = mock_cfg(mock)
        opts = {"batch": 2, "pass2": True, "search": "off", "vision": "auto",
                "report": True, "force": True}
        r = solver.solve_quiz(cfg, qdir, opts)
    finally:
        mock.stop()

    check("作答状态 ok", r.get("status") == "ok", str(r)[:200])
    check("全部题目都有答案", r.get("answered") == n, f"{r.get('answered')}/{n}")

    af = qdir / "answers.json"
    check("已生成 answers.json", af.exists())
    data = json.loads(af.read_text(encoding="utf-8")) if af.exists() else {"answers": []}
    ans = data.get("answers") or []
    check("answers 条数正确", len(ans) == n, str(len(ans)))
    check("每条都带 verify 双遍记录",
          all(a.get("verify", {}).get("agree") for a in ans))
    _doc_cite = re.compile(r"讲义\s*[\d.]+\s*第\s*\d+\s*页")
    check("无讲义时引用不出现编造页码",
          not any(_doc_cite.search(a.get("cite") or "") for a in ans),
          str([a.get("cite") for a in ans])[:200])
    check("置信度不会凭空给 high（无依据）",
          all(a["confidence"] != "high" for a in ans),
          str([a["confidence"] for a in ans]))

    for f in ("solver_run.json", "solver_trace.md", "参考答案.html", "答案速查.md"):
        check(f"产物 {f}", (qdir / f).exists())

    trace = (qdir / "solver_trace.md").read_text(encoding="utf-8") \
        if (qdir / "solver_trace.md").exists() else ""
    check("trace 里有逐题表格", trace.count("|") > n * 5)
    check("mock 模型确实被调用（两遍）", len(mock.calls) >= 2, str(len(mock.calls)))
    return qdir


# --------------------------------------------------------------------------- #
# ③ 端到端：有讲义 + 视觉摘要
# --------------------------------------------------------------------------- #
def test_pipeline_docs(root):
    print("\n[3/5] 端到端：有讲义（视觉摘要 → 带页码引用 → high）")
    qdir, n = make_fixture(root / "withdocs", with_docs=True)
    solver.DOC_CACHE = root / "withdocs" / "docs"
    quiz.DOC_CACHE = solver.DOC_CACHE

    mock = MockLLM().start()
    try:
        cfg = mock_cfg(mock, vision=True)
        opts = {"batch": 2, "pass2": True, "search": "off", "vision": "on",
                "report": False, "force": True, "force_digest": True}
        r = solver.solve_quiz(cfg, qdir, opts)
    finally:
        mock.stop()

    check("作答状态 ok", r.get("status") == "ok", str(r)[:200])
    dg = solver.DOC_CACHE / "12.1" / "_digest.json"
    check("讲义摘要已缓存到 _digest.json", dg.exists())

    af = qdir / "answers.json"
    data = json.loads(af.read_text(encoding="utf-8")) if af.exists() else {"answers": []}
    ans = data.get("answers") or []
    check("引用到讲义具体页码", any("讲义 12.1 第" in (a.get("cite") or "") for a in ans),
          str([a.get("cite") for a in ans])[:200])
    check("有依据 + 两遍一致 → 至少 1 题 high",
          any(a["confidence"] == "high" for a in ans),
          str([a["confidence"] for a in ans]))

    # 再跑一次应该复用摘要缓存，不再重复请求视觉模型
    mock2 = MockLLM().start()
    try:
        cfg2 = mock_cfg(mock2, vision=True)
        opts2 = dict(opts, force=True, force_digest=False)
        solver.solve_quiz(cfg2, qdir, opts2)
    finally:
        mock2.stop()
    vision_calls = sum(1 for c in mock2.calls
                       if "image_url" in json.dumps(c, ensure_ascii=False))
    check("第二次复用摘要缓存（视觉请求 0 次）", vision_calls == 0, str(vision_calls))


# --------------------------------------------------------------------------- #
# ④ 容错：模型返回数组 / 复核阶段整体失败
# --------------------------------------------------------------------------- #
def test_resilience(root):
    print("\n[4/5] 容错：模型返回数组 / 复核阶段整体失败")

    # ① 顶层数组要被收敛成答案列表（原先直接 obj.get() 会 AttributeError）
    batch = [{"no": 1, "type": "single_choice", "stem": "题干",
              "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}]}]
    data = {"chapter": {"num": "1", "title": "测试"}, "questions": batch}
    real_json = llm.chat_json

    def fake_json(*a, **kw):
        return ([{"no": 1, "answer": ["A"], "confidence": "high",
                  "cite": "通用学科知识"}],
                {"calls": [{"latency_ms": 1, "usage": {}, "attempts": 1}], "repairs": 0})

    llm.chat_json = fake_json
    try:
        got, calls = solver._call_batch({}, {"name": "mock"}, data, batch, "", "", 2)
    finally:
        llm.chat_json = real_json
    check("顶层数组也能收敛成答案（原来会 AttributeError）",
          len(got) == 1 and got[1]["answer"] == ["A"], str(got)[:120])
    check("调用记录标注了解析类型", bool(calls) and calls[0].get("parsed") == "list",
          str(calls)[:120])

    # ② 第二遍整体崩掉 → 降级为单遍，仍要出答案
    qdir, n = make_fixture(root / "degrade", with_docs=False)
    solver.DOC_CACHE = root / "degrade" / "docs"
    quiz.DOC_CACHE = solver.DOC_CACHE
    mock = MockLLM().start()
    real_run_pass = solver.run_pass

    def boom(cfg, provider, d, batches, digest_text, evidence_by_no, pass_no, **kw):
        if pass_no == 2:
            raise RuntimeError("模拟复核崩溃")
        return real_run_pass(cfg, provider, d, batches, digest_text, evidence_by_no,
                             pass_no, **kw)

    solver.run_pass = boom
    try:
        r = solver.solve_quiz(mock_cfg(mock), qdir,
                              {"batch": 2, "pass2": True, "search": "off",
                               "vision": "auto", "report": False, "force": True})
    finally:
        solver.run_pass = real_run_pass
        mock.stop()

    check("复核崩了不再整体失败", r.get("status") == "ok", str(r)[:200])
    af = qdir / "answers.json"
    ans = json.loads(af.read_text(encoding="utf-8"))["answers"] if af.exists() else []
    check("降级后答案仍然写盘且齐全", len(ans) == n, f"{len(ans)}/{n}")
    check("降级时置信度不超过 medium",
          all(a["confidence"] != "high" for a in ans),
          str([a["confidence"] for a in ans]))
    run_info = json.loads((qdir / "solver_run.json").read_text(encoding="utf-8")) \
        if (qdir / "solver_run.json").exists() else {}
    check("solver_run 记录了降级原因", bool(run_info.get("pass2_degraded")))

    # ③ 模型先吐数组、被要求重发后改对 → 自愈，不应降级
    qdir2, n2 = make_fixture(root / "heal", with_docs=False)
    solver.DOC_CACHE = root / "heal" / "docs"
    quiz.DOC_CACHE = solver.DOC_CACHE
    mock2 = MockLLM()
    mock2.list_once = True
    mock2.start()
    try:
        r2 = solver.solve_quiz(mock_cfg(mock2), qdir2,
                               {"batch": 2, "pass2": True, "search": "off",
                                "vision": "auto", "report": False, "force": True})
    finally:
        mock2.stop()
    check("模型改对之后不降级、答案齐全",
          r2.get("status") == "ok" and r2.get("answered") == n2, str(r2)[:200])
    # 4 题 / 每批 2 题 = 两遍共 4 次请求；第一遍复核那批多一次"改错重发"才是 5 次
    check("确实触发了「先给数组、被要求重发」的路径", len(mock2.calls) >= 5,
          f"调用次数 {len(mock2.calls)}")

    # ④ --limit 试跑不能覆盖已有的完整答案
    qdir3, n3 = make_fixture(root / "limit", with_docs=False)
    full = {"answers": [{"no": i, "answer": ["A"], "confidence": "medium",
                         "verify": {}} for i in range(1, n3 + 1)]}
    (qdir3 / "answers.json").write_text(json.dumps(full, ensure_ascii=False),
                                        encoding="utf-8")
    r3 = solver.solve_quiz({}, qdir3, {"limit": 2, "force": True, "search": "off"})
    kept = json.loads((qdir3 / "answers.json").read_text(encoding="utf-8"))["answers"]
    check("--limit 不会覆盖已有的完整答案",
          r3.get("status") == "limit_protected" and len(kept) == n3,
          f"{r3.get('status')} / 剩 {len(kept)} 条")

    # ⑤ 检索：连续"没相关资料"不该关闭检索；连续"引擎不可用"才停
    targets = [({"no": i, "stem": "秋水仙素诱导染色体加倍的作用机理"},
                "低置信") for i in range(1, 5)]
    real_search = websearch.search

    def fake_irrelevant(cfg, q, **kw):
        raise websearch.SearchError("检索失败：360: 无结果", kind="irrelevant")

    websearch.search = fake_irrelevant
    try:
        ev, audit, ok = solver.do_search({}, [], targets, 4, "", log=lambda *a: None)
    finally:
        websearch.search = real_search
    check("连续「没相关资料」不会关闭整段检索", ok is True and len(audit) == 4,
          f"ok={ok} audit={len(audit)}")

    def fake_blocked(cfg, q, **kw):
        raise websearch.SearchError("检索失败：baidu: 返回了安全验证页", kind="blocked")

    websearch.search = fake_blocked
    try:
        ev, audit, ok = solver.do_search({}, [], targets, 4, "", log=lambda *a: None)
    finally:
        websearch.search = real_search
    check("连续「引擎不可用」才停手", ok is False and len(audit) == 3,
          f"ok={ok} audit={len(audit)}")


# --------------------------------------------------------------------------- #
# ⑤ 真实检索链路（可选）
# --------------------------------------------------------------------------- #
def test_network():
    print("\n[5/5] 真实检索链路（--net）")
    cfg = {"search": {"engine": "auto", "max_results": 4}, "net": {"proxy": "auto"}}
    print(f"  代理：{netkit.detect_proxy() or '直连'}")

    rows = websearch.probe_engines(cfg)
    for r in rows:
        mark = PASS if r["ok"] else FAIL
        print(f"  {mark} {r['label']:<12}{r['count']:>2} 条 / 相关 {r['relevant']} 条"
              f"  {r['ms']}ms  {r['error']}")
    usable = [r for r in rows if r["ok"] and r["relevant"]]
    check("至少一个引擎可用且结果相关", bool(usable),
          "检索全挂：检查网络/代理，或配 search.bocha_api_key")

    q = "秋水仙素诱导染色体加倍的主要作用机理是:"
    got = None
    for qq in websearch.queries_for(q, "染色体工程"):
        try:
            r = websearch.search(cfg, qq, n=4)
            got = (qq, r["engine"], len(r["results"]))
            break
        except Exception as e:
            err = str(e)[:80]
    check("auto 模式能拿到证据", bool(got), err if not got else "")
    if got:
        print(f"    命中的查询词「{got[0]}」→ {got[1]}，{got[2]} 条")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="离线自检：抓题 → 作答 → 出册 全链路")
    ap.add_argument("--net", action="store_true", help="额外测真实检索链路（会联网）")
    ap.add_argument("--keep", action="store_true", help="保留 .workbuddy/_selftest 产物")
    args = ap.parse_args()

    root = TOOL_DIR / ".workbuddy" / "_selftest"
    print(f"工作目录：{root}")
    t0 = time.time()

    test_units()
    test_pipeline_plain(root)
    test_pipeline_docs(root)
    test_resilience(root)
    if args.net:
        test_network()

    bad = [n for n, ok, _ in _results if not ok]
    print(f"\n{'=' * 60}")
    print(f"共 {len(_results)} 项，通过 {len(_results) - len(bad)} 项，"
          f"失败 {len(bad)} 项，耗时 {time.time() - t0:.1f}s")
    if bad:
        print("失败项：")
        for n in bad:
            print("  -", n)
        return 1
    print("全部通过：" + ("这台机器上可以放心跑 quiz solve 了。" if args.net
                        else "流水线本身没问题；再接真模型即可。"))
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
