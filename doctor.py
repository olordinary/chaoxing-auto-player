# -*- coding: utf-8 -*-
"""doctor.py —— 体检：把「跑不起来」拆成一条条能看懂、能自己修的检查项。

为什么单独做这个命令：
    自动作答依赖四样外部东西——浏览器、大模型、检索、讲义缓存。
    任何一样不通，报错都会长得像"模型答不出"，很难定位。
    doctor 把它们逐项试一遍，直接告诉你哪一环坏了、该敲什么命令修。

用法：
    python chaoxing_player.py doctor              # 全量体检（会联网、会调模型）
    python chaoxing_player.py doctor --no-llm     # 不调模型（省钱）
    python chaoxing_player.py doctor --no-net     # 不测检索
"""
import importlib
import json
import os
import sys
import time
from pathlib import Path

import llm

TOOL_DIR = Path(__file__).resolve().parent
OK = "✓"
BAD = "✗"
WARN = "!"


def _importable(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def _rel(p):
    try:
        return str(Path(p).relative_to(TOOL_DIR)).replace("\\", "/")
    except Exception:
        return str(p)


def check_env(cfg, out):
    out.section("运行环境")
    ver = sys.version_info
    out.item((ver.major, ver.minor) >= (3, 9),
             f"Python {sys.version.split()[0]}", "需要 3.9+")
    for mod, why in (("playwright", "浏览器自动化（抓讲义/抓题必需）"),
                     ("PIL", "讲义图压缩 + 复习册缩略图"),
                     ("numpy", "字形位图匹配（反解字体混淆）"),
                     ("fontTools", "读取内嵌字体")):
        out.item(_importable(mod), f"依赖 {mod}", why + "；装：pip install -r requirements.txt")
    out.item(_importable("winreg"), "Windows 注册表可读", "仅 Windows 支持")

    edge = None
    for cand in (cfg.get("edge"), os.environ.get("EDGE_PATH"),
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if cand and Path(cand).exists():
            edge = cand
            break
    out.item(bool(edge), f"Edge：{edge or '未找到'}", "装 Edge，或在 config.json 里写 edge 路径")


def check_config(cfg, out):
    out.section("配置")
    cf = TOOL_DIR / "config.json"
    out.item(cf.exists(), f"配置文件 {_rel(cf) if cf.exists() else '（不存在）'}",
             "先跑 python chaoxing_player.py init")
    course = cfg.get("course") or {}
    out.item(bool(course.get("courseId")), "已导入课程 courseId",
             "运行 python chaoxing_player.py init")
    od = Path(cfg.get("output_dir") or (TOOL_DIR / "runs"))
    out.item(od.exists() or od.parent.exists(), f"输出目录 {_rel(od)}",
             "目录不存在，跑一次 quiz auto 会自动建")
    prof = Path(cfg.get("profile_dir") or (TOOL_DIR / "profile"))
    out.info(f"浏览器配置目录：{_rel(prof)}（登录态存在这里，别删）")
    out.info(f"出网代理：{cfg.get('net', {}).get('proxy', 'auto')}")


def check_llm(cfg, out, do_call=True):
    out.section("大模型")
    provs = (cfg.get("llm") or {}).get("providers") or {}
    active = llm.active_name(cfg)
    if not provs:
        out.item(False, "已导入模型：0 个", "python chaoxing_player.py model add")
        return
    out.item(True, f"已导入模型：{len(provs)} 个，当前默认 {active or '未设置'}")
    for name, raw in provs.items():
        mark = "*" if name == active else " "
        # 用解析后的结果，而不是配置里的原始值：
        # 配置写 null 时能力是运行时现算的，看 raw 会误报成"纯文本"。
        try:
            vis = "读图" if llm.resolve_provider(cfg, name).get("vision") else "纯文本"
        except llm.LLMError:
            vis = "配置不全"
        out.info(f"  {mark} {name:<16}{raw.get('model', ''):<28}{vis}")

    if not do_call:
        out.info("（--no-llm，跳过连通性测试）")
        return
    for name in provs:
        t0 = time.time()
        try:
            r = llm.test_provider(cfg, name)      # 与 model test 共用一套探针，避免两处口径不一致
            out.item(True, f"{name} 可用（{r['latency_ms']}ms，模型 {r['model']}）")
        except Exception as e:
            msg = str(e).splitlines()[0][:90]
            out.item(False, f"{name} 调不通", f"{msg}；用 model test {name} 复查")
        finally:
            if time.time() - t0 > 60:
                out.info(f"  （{name} 这次等了 {int(time.time() - t0)}s，偏慢）")


def check_search(cfg, out, do_net=True):
    out.section("联网检索")
    sc = cfg.get("search") or {}
    engines = []
    try:
        import websearch
        engines = websearch.available_engines(cfg)
    except Exception as e:
        out.item(False, "检索模块加载失败", str(e)[:80])
        return
    if not engines:
        out.item(False, "没有可用检索引擎", "配 search.bocha_api_key 或检查网络")
        return
    out.info(f"引擎：{'、'.join(engines)}（engine={sc.get('engine', 'auto')}）")
    if not do_net:
        out.info("（--no-net，跳过实测）")
        return

    import websearch
    rows = websearch.probe_engines(cfg)
    good = 0
    for r in rows:
        ok = r["ok"] and r["relevant"] > 0
        good += 1 if ok else 0
        detail = f"{r['count']} 条/相关 {r['relevant']} 条  {r['ms']}ms"
        out.item(ok, f"{r['label']:<12}{detail}",
                 r["error"] or "结果与查询不相关（中文长句常被搜索站切碎）")
    out.item(good > 0, f"可用引擎 {good}/{len(rows)}",
             "全都不可用：检查代理，或改用付费检索（search.bocha_api_key）")


def check_assets(cfg, out):
    import solver

    out.section("讲义缓存")
    docs = solver.DOC_CACHE
    if not docs.exists():
        out.item(False, "还没有讲义缓存", "python chaoxing_player.py quiz docs --chapters 12.1")
    else:
        rows = []
        for d in sorted(docs.iterdir()):
            if not d.is_dir():
                continue
            imgs = [p for p in d.iterdir() if p.suffix.lower() in (".png", ".jpg")]
            if imgs:
                dg = d / "_digest.json"
                rows.append((d.name, len(imgs), dg.exists()))
        out.item(bool(rows), f"讲义 {len(rows)} 份，共 {sum(r[1] for r in rows)} 页")
        for name, n, has_dg in rows:
            out.info(f"  {name:<8}{n:>3} 页  摘要缓存：{'有' if has_dg else '无'}")
        if rows and not any(r[2] for r in rows):
            out.info("  摘要缓存为空：首次 solve 时会用视觉模型生成（需要能读图的模型）")

    out.section("题库 / 产出")
    root = Path(cfg.get("output_dir") or (TOOL_DIR / "runs"))
    qfs = sorted(root.glob("ch*/quiz/*/questions.json"))
    if not qfs:
        out.item(False, "还没有抓到题目", "python chaoxing_player.py quiz auto --chapter N")
        return
    out.item(True, f"已抓取 {len(qfs)} 套测验")
    for qf in qfs:
        d = qf.parent
        try:
            n = len(json.loads(qf.read_text(encoding="utf-8")).get("questions") or [])
        except Exception:
            n = -1
        has_a = (d / "answers.json").exists()
        has_r = (d / "参考答案.html").exists()
        tag = []
        if has_a:
            try:
                ans = json.loads((d / "answers.json").read_text(encoding="utf-8"))["answers"]
                hi = sum(1 for a in ans if a.get("confidence") == "high")
                tag.append(f"答案 {len(ans)} 题（高 {hi}）")
            except Exception:
                tag.append("答案存在但读不出")
        if has_r:
            tag.append("有复习册")
        out.info(f"  {_rel(d):<26}{n:>3} 题  " + ("；".join(tag) or "待作答"))


def check_flow(cfg, out):
    """看 _flow.json 里有没有卡在「跑到一半断了」的套。

    注意 answers_pending / packed 是正常中间态（抓完题、还没作答），
    不算故障，不在这里报。
    """
    root = Path(cfg.get("output_dir") or (TOOL_DIR / "runs"))
    stuck = []
    for f in sorted(root.glob("ch*/_flow.json")):
        try:
            flow = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for num, rec in (flow.get("quizzes") or {}).items():
            st = str(rec.get("state") or "")
            if st in ("failed", "llm_error", "solving"):
                stuck.append((f.parent.name, num, st))
    out.section("流水线状态")
    if not stuck:
        out.item(True, "没有中断/失败的套")
    else:
        for ch, num, st in stuck:
            out.item(False, f"{ch} {num} 状态 {st}", f"重跑：quiz solve --chapters {num} --force")


class Report:
    def __init__(self):
        self.rows = []
        self.fails = 0
        self.warns = 0

    def section(self, name):
        print(f"\n—— {name} " + "—" * max(0, 46 - len(name)))

    def item(self, ok, text, hint=""):
        self.rows.append((bool(ok), text))
        if not ok:
            self.fails += 1
        print(f"  {OK if ok else BAD} {text}")
        if not ok and hint:
            print(f"       → {hint}")

    def info(self, text):
        print(f"    {text}")


def cmd_doctor(cfg, args):
    rep = Report()
    print("chaoxing-auto-player 体检\n" + "=" * 56)
    check_env(cfg, rep)
    check_config(cfg, rep)
    check_llm(cfg, rep, do_call=not getattr(args, "no_llm", False))
    check_search(cfg, rep, do_net=not getattr(args, "no_net", False))
    check_assets(cfg, rep)
    check_flow(cfg, rep)

    print("\n" + "=" * 56)
    total = len(rep.rows)
    if rep.fails == 0:
        print(f"全部 {total} 项通过。可以直接跑："
              f"\n  python chaoxing_player.py quiz auto --chapter <章号>")
        return 0
    print(f"{total} 项中有 {rep.fails} 项需要注意（见上面的 → 提示）。")
    print("修完重跑本命令即可确认；只想验证流水线本身，用 python selftest.py")
    return 1
