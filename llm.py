# -*- coding: utf-8 -*-
"""llm.py —— 大模型接入层（OpenAI 兼容 /chat/completions 协议）。

为什么要自己写一层，而不是直接用 openai SDK：
1. 项目不想再引入第三方依赖（国内装包慢，且 venv 已经够用）；
2. 用户要能「手动导入 / 选择模型」，SDK 反而把这件事藏起来了。

兼容范围：任何实现了 POST {base_url}/chat/completions 的服务——
DeepSeek、通义千问、Kimi、智谱、OpenAI、硅基流动、OpenRouter、Ollama、vLLM……

模型配置写在 config.json 的 llm 段（该文件已被 .gitignore 忽略，不会进版本库）：
    "llm": {
      "active": "deepseek",
      "providers": {
        "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com",
                     "api_key": "sk-...", "model": "deepseek-flash", "vision": true}
      }
    }

命令行：
    python chaoxing_player.py model list       # 看预设 + 已导入的模型
    python chaoxing_player.py model add        # 交互式导入（推荐）
    python chaoxing_player.py model use NAME   # 设为默认
    python chaoxing_player.py model test NAME  # 连通性测试
    python chaoxing_player.py model rm NAME
"""
import json
import os
import re
import time
from pathlib import Path

import netkit

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class LLMError(RuntimeError):
    """模型层异常，消息面向用户，可直接打印。"""


# 预设只是「填表用的模板」，不含任何密钥，可以安全提交到仓库。
PRESETS = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        # 官方现役只有这两个：flash 便宜且**能读图**，pro 推理更强但贵约 4 倍。
        # deepseek-chat / deepseek-reasoner 已于 2026-07-24 退役，别再写。
        # 思考模式不是独立模型，是请求参数（thinking + reasoning_effort）。
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "vision_models": ["deepseek-flash"],
        "json_mode": True,
        "vision": True,
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "dashscope": {
        "label": "阿里云百炼 / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-vl-max", "qwen-vl-plus"],
        "vision_models": ["qwen-vl-max", "qwen-vl-plus", "qwen2.5-vl-72b-instruct"],
        "json_mode": True,
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
    },
    "moonshot": {
        "label": "月之暗面 / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-latest", "moonshot-v1-8k", "moonshot-v1-32k",
                   "moonshot-v1-8k-vision-preview"],
        "vision_models": ["kimi-latest", "moonshot-v1-8k-vision-preview",
                          "moonshot-v1-32k-vision-preview"],
        "json_mode": False,
        "key_url": "https://platform.moonshot.cn/console/api-keys",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash", "glm-4v-plus"],
        "vision_models": ["glm-4v-plus", "glm-4v", "glm-4v-flash"],
        "json_mode": True,
        "key_url": "https://bigmodel.cn/usercenter/apikeys",
    },
    "openai": {
        "label": "OpenAI 官方",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        "vision_models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        "json_mode": True,
        "key_url": "https://platform.openai.com/api-keys",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2-VL-72B-Instruct",
                   "deepseek-ai/DeepSeek-V3"],
        "vision_models": ["Qwen/Qwen2-VL-72B-Instruct"],
        "json_mode": True,
        "key_url": "https://cloud.siliconflow.cn/account/ak",
    },
    "openrouter": {
        "label": "OpenRouter（多模型聚合）",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet",
                   "google/gemini-flash-1.5", "deepseek/deepseek-chat"],
        "vision_models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet",
                          "google/gemini-flash-1.5"],
        "json_mode": False,
        "key_url": "https://openrouter.ai/keys",
    },
    "ollama": {
        "label": "本地 Ollama（无需密钥）",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5:7b", "qwen2.5:14b", "qwen2.5vl:7b", "llama3.1:8b"],
        "vision_models": ["qwen2.5vl:7b", "llava:7b"],
        "json_mode": True,
        "needs_key": False,
    },
}

DEFAULTS = {
    "label": "",
    "base_url": "",
    "api_key": "",
    "api_key_env": "",
    "model": "",
    "vision": None,          # None = 按模型名自动判断
    "vision_model": "",      # 摘要讲义页时单独用的视觉模型（可选）
    "json_mode": None,       # None = 按预设/探测自动决定
    "max_tokens": 4096,
    "temperature": 0.2,
    "timeout": 180,
    "extra_headers": {},
}


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def _sec(cfg):
    if not isinstance(cfg.get("llm"), dict):
        cfg["llm"] = {}
    sec = cfg["llm"]
    sec.setdefault("providers", {})
    if not isinstance(sec["providers"], dict):
        sec["providers"] = {}
    return sec


def providers(cfg):
    return _sec(cfg)["providers"]


def active_name(cfg):
    sec = _sec(cfg)
    name = sec.get("active") or ""
    if name and name in sec["providers"]:
        return name
    if sec["providers"]:
        return next(iter(sec["providers"]))
    return ""


def preset_of(name, entry):
    """找到这个 provider 对应的预设（用于补默认值）。"""
    if name in PRESETS:
        return PRESETS[name]
    base = (entry.get("base_url") or "").rstrip("/").lower()
    for pname, p in PRESETS.items():
        if p["base_url"].rstrip("/").lower() == base:
            return p
    return {}


def is_vision_model(model, preset=None):
    model = (model or "").lower()
    if not model:
        return False
    if preset and preset.get("vision_models"):
        return any(model == m.lower() for m in preset["vision_models"])
    keys = ("vl", "vision", "4o", "4.1", "gemini", "claude-3", "llava", "omni")
    return any(k in model for k in keys)


def resolve_provider(cfg, name=None):
    """把配置补全成可直接用的字典；缺关键项时抛 LLMError。"""
    sec = _sec(cfg)
    name = name or active_name(cfg)
    if not name:
        raise LLMError(
            "还没有导入任何模型。先运行：\n"
            "  python chaoxing_player.py model add\n"
            "或先看内置预设：\n"
            "  python chaoxing_player.py model list")
    raw = sec["providers"].get(name)
    if raw is None:
        raise LLMError(f"config.json 里没有名为 {name!r} 的模型配置；"
                       f"可选：{', '.join(sec['providers']) or '（空）'}")

    preset = preset_of(name, raw)
    p = dict(DEFAULTS)
    p.update({k: v for k, v in raw.items() if v is not None})
    p["name"] = name
    p["label"] = p.get("label") or preset.get("label") or name
    p["_preset"] = preset

    if not p["base_url"]:
        raise LLMError(f"模型 {name} 缺少 base_url")
    if not p["model"]:
        raise LLMError(f"模型 {name} 缺少 model（要调用的模型名）")

    # 密钥解析顺序：配置里的明文 → 指定的环境变量 → 预设约定的环境变量
    key = (p.get("api_key") or "").strip()
    if not key and p.get("api_key_env"):
        key = (os.environ.get(p["api_key_env"]) or "").strip()
    if not key:
        for env in (f"{name.upper().replace('-', '_')}_API_KEY", "OPENAI_API_KEY"):
            ev = (os.environ.get(env) or "").strip()
            if ev and name != "ollama":
                key = ev
                break
    p["api_key"] = key
    needs_key = preset.get("needs_key", True)
    if needs_key and not key and not p["base_url"].startswith("http://127.0.0.1"):
        raise LLMError(f"模型 {name} 缺少 api_key。重新导入一次即可：\n"
                       f"  python chaoxing_player.py model add --preset {name} --api-key sk-xxx")

    if p["json_mode"] is None:
        p["json_mode"] = bool(preset.get("json_mode", False))
    if p["vision"] is None:
        p["vision"] = is_vision_model(p["model"], preset)
    if p["vision"] and not p.get("vision_model"):
        p["vision_model"] = p["model"]
    p["vision"] = bool(p["vision"])
    return p


def save(cfg):
    """写回 config.json（该文件已被 .gitignore 忽略，密钥不会进版本库）。"""
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #
def mask_key(k):
    if not k:
        return "（无）"
    if len(k) <= 10:
        return k[:2] + "***"
    return f"{k[:6]}…{k[-4:]}"


def chat(cfg, messages, provider=None, model=None, json_mode=None, temperature=None,
         max_tokens=None, timeout=None, retries=3, tag=""):
    """发一次对话请求，返回 {text, usage, latency_ms, model, attempts, finish_reason}。"""
    p = resolve_provider(cfg, provider)
    url = p["base_url"].rstrip("/") + "/chat/completions"
    net_proxy = (cfg.get("net") or {}).get("proxy", "auto")

    use_json = p["json_mode"] if json_mode is None else bool(json_mode)
    payload = {
        "model": model or p["model"],
        "messages": messages,
        "stream": False,
        "temperature": p["temperature"] if temperature is None else temperature,
        "max_tokens": p["max_tokens"] if max_tokens is None else max_tokens,
    }
    if use_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {p['api_key']}"} if p["api_key"] else {}
    headers.update(p.get("extra_headers") or {})
    tmo = p["timeout"] if timeout is None else timeout

    t0 = time.time()
    attempts = 0
    dropped_json_mode = False
    last = None
    while attempts <= retries:
        attempts += 1
        try:
            r = netkit.http_request(url, "POST", data=payload, headers=headers,
                                    timeout=tmo, proxy=net_proxy, retries=0)
            break
        except netkit.NetError as e:
            msg = str(e)
            # 有的服务不认 response_format，去掉再试
            if use_json and not dropped_json_mode and ("response_format" in msg or "400" in msg):
                dropped_json_mode = True
                use_json = False
                payload.pop("response_format", None)
                continue
            if "401" in msg or "403" in msg:
                raise LLMError(f"{p['label']} 鉴权失败（{mask_key(p['api_key'])}）。"
                               f"密钥可能填错或已过期。\n原始信息：{msg[:200]}")
            if "404" in msg:
                raise LLMError(f"接口地址可能不对：{url}\n原始信息：{msg[:200]}")
            last = e
            if attempts <= retries:
                time.sleep(min(2 ** attempts, 15))
            continue

    if last is not None and attempts > retries:
        raise LLMError(f"{p['label']} 调用失败：{last}")

    latency = int((time.time() - t0) * 1000)
    try:
        body = r.json()
    except Exception:
        raise LLMError(f"{p['label']} 返回的不是 JSON（HTTP {r.status}）：{r.text[:300]}")

    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError(f"{p['label']} 返回错误：{msg}")

    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"{p['label']} 返回里没有 choices：{str(body)[:300]}")
    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""
    if not text and msg.get("reasoning_content"):
        # 推理模型偶尔把内容全放在 reasoning_content 里
        text = msg["reasoning_content"]

    return {
        "text": text or "",
        "usage": body.get("usage") or {},
        "latency_ms": latency,
        "model": body.get("model") or payload["model"],
        "attempts": attempts,
        "finish_reason": choices[0].get("finish_reason"),
        "provider": p["name"],
        "tag": tag,
    }


# --------------------------------------------------------------------------- #
# JSON 抽取与修复
# --------------------------------------------------------------------------- #
def _balanced(text, start):
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text):
    """从模型输出里挖出第一个合法 JSON（容忍代码围栏、前后废话、尾随逗号）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    for i, ch in enumerate(t):
        if ch in "{[":
            seg = _balanced(t, i)
            if not seg:
                continue
            for candidate in (seg, re.sub(r",\s*([}\]])", r"\1", seg)):
                try:
                    return json.loads(candidate)
                except Exception:
                    continue
    return None


REPAIR_PROMPT = ("上一条回复不是合法 JSON，无法解析。请只输出一个 JSON 对象，"
                 "不要任何解释文字、不要 Markdown 代码围栏、不要注释。")


def chat_json(cfg, messages, provider=None, model=None, max_repair=1, **kw):
    """要 JSON 的对话：解析失败会自动让模型重发一次。返回 (obj, meta)。"""
    meta = {"calls": [], "repairs": 0}
    resp = chat(cfg, messages, provider=provider, model=model, json_mode=True, **kw)
    meta["calls"].append(resp)
    obj = extract_json(resp["text"])
    if obj is not None:
        return obj, meta

    convo = list(messages) + [{"role": "assistant", "content": resp["text"][:4000]},
                              {"role": "user", "content": REPAIR_PROMPT}]
    for _ in range(max_repair):
        meta["repairs"] += 1
        resp = chat(cfg, convo, provider=provider, model=model, json_mode=True,
                    temperature=0, **kw)
        meta["calls"].append(resp)
        obj = extract_json(resp["text"])
        if obj is not None:
            return obj, meta
        convo += [{"role": "assistant", "content": resp["text"][:2000]},
                  {"role": "user", "content": REPAIR_PROMPT}]
    raise LLMError(f"模型连续 {1 + max_repair} 次都没给出合法 JSON。"
                   f"最后一段输出：{resp['text'][:300]}")


def test_provider(cfg, name=None):
    p = resolve_provider(cfg, name)
    t0 = time.time()
    resp = chat(cfg, [{"role": "user", "content": "只回复两个字：可用"}],
                provider=p["name"], max_tokens=32, temperature=0, retries=1)
    return {"provider": p["name"], "label": p["label"], "model": resp["model"],
            "reply": (resp["text"] or "").strip()[:40],
            "latency_ms": resp["latency_ms"], "wall_ms": int((time.time() - t0) * 1000),
            "usage": resp["usage"]}


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _pick(prompt, items, allow_custom=True):
    """编号选择器。返回 (值, 是否自定义)。"""
    for i, (label, _) in enumerate(items, 1):
        print(f"  {i:>2}) {label}")
    if allow_custom:
        print("   0) 自定义（手动输入）")
    while True:
        try:
            s = input(f"{prompt} [1]: ").strip()
        except EOFError:
            return (items[0][1] if items else None), False
        if not s:
            return (items[0][1] if items else None), False
        if s in ("0", "custom", "c") and allow_custom:
            return input("  请输入名称：").strip(), True
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1][1], False
        # 直接输入值
        return s, True


def _collect_interactive(cfg):
    names = list(PRESETS)
    print("可用预设（都是 OpenAI 兼容接口）：\n")
    items = [(f"{PRESETS[n]['label']:<26}{PRESETS[n]['base_url']}", n) for n in names]
    print("选择服务商：")
    preset_name, custom = _pick("服务商", items, allow_custom=True)
    if custom and preset_name not in PRESETS:
        preset = {}
        base_url = input("  base_url（形如 https://xxx/v1）：").strip()
        models = []
    else:
        preset = PRESETS[preset_name]
        base_url = preset["base_url"]
        models = list(preset.get("models") or [])
        print(f"\n  base_url: {base_url}")
        if preset.get("key_url"):
            print(f"  申请密钥: {preset['key_url']}")

    if models:
        print("\n选择模型：")
        model, mc = _pick("模型", [(m, m) for m in models])
        # mc=True 表示用户手输了列表外的名字，直接用；否则用选中的
    else:
        model = input("  模型名（官方文档里的 model 值，如 deepseek-flash）：").strip()

    needs_key = preset.get("needs_key", True)
    api_key, api_key_env = "", ""
    if needs_key:
        print("\n密钥可以直接存 config.json（本地文件，已被 .gitignore 忽略），")
        print("也可以只存环境变量名，密钥本身放系统环境变量里（更安全）。")
        mode = input("  1) 直接输入密钥  2) 存环境变量名  [1]: ").strip() or "1"
        if mode == "2":
            default_env = f"{preset_name.upper().replace('-', '_')}_API_KEY"
            api_key_env = input(f"  环境变量名 [{default_env}]: ").strip() or default_env
            if not os.environ.get(api_key_env):
                print(f"  ⚠ 当前环境里读不到 {api_key_env}，请在系统环境变量里设置后重启终端。")
        else:
            import getpass
            try:
                api_key = getpass.getpass("  粘贴密钥（输入时不回显）：").strip()
            except Exception:
                api_key = input("  粘贴密钥：").strip()
            if not api_key:
                print("  ⚠ 没输入密钥，先保存配置，之后再补：model add --preset ... --api-key ...")

    vision = is_vision_model(model, preset)
    if not vision and preset.get("vision_models"):
        alt = [m for m in preset["vision_models"] if m in models] or preset["vision_models"]
        print(f"\n  提示：{model} 未识别为读图模型。要让模型直接看讲义页，"
              f"可换用 {alt[0]}，或在 config.json 里手动把 vision 设为 true。")

    name = preset_name
    got = input(f"\n  配置名（命令行里用它指定模型，不能含空格）[{preset_name}]: ").strip()
    name = got.replace(" ", "") or preset_name

    return {
        "name": name,
        "label": preset.get("label") or name,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "model": model,
        # 只有「确认能读图」才写 true；认不出来写 null，交给 resolve_provider
        # 每次现算（预设更新后不必重新导入）。写死 false 会把能力永久钉死。
        "vision": True if vision else None,
        "vision_model": model if vision else "",
        "json_mode": preset.get("json_mode", False),
    }


def _vision_label(v):
    """配置里 vision 有三种状态：true（确认能读图）/ false（确认不能）/ null（未定，运行时现算）。"""
    if v is None:
        return "自动"
    return "是" if v else "否"


def cmd_model(cfg, args):
    sub = args.model_cmd
    sec = _sec(cfg)

    if sub == "list":
        print(f"{'配置名':<14}{'模型':<32}{'读图':<6}{'JSON':<6}接口")
        print("-" * 96)
        for name, raw in sec["providers"].items():
            p = dict(DEFAULTS)
            p.update(raw)
            mark = " *" if name == active_name(cfg) else ""
            print(f"{(name + mark):<14}{str(p.get('model'))[:30]:<32}"
                  f"{_vision_label(p.get('vision')):<6}"
                  f"{'是' if p.get('json_mode') else '否':<6}{p.get('base_url')}")
        if not sec["providers"]:
            print("（还没有导入任何模型）")
        print("-" * 96)
        print(f"当前默认：{active_name(cfg) or '未设置'}")
        print("\n内置预设（用 model add --preset <名字> 导入）：")
        for n, p in PRESETS.items():
            print(f"  {n:<14}{p['label']:<28}{p['base_url']}")
        return 0

    if sub == "add":
        if args.preset or args.api_key or args.model:
            name = args.name or args.preset or "custom"
            # 预设要按 --preset 查，不能按配置名查：--preset deepseek --name myds
            # 时若拿 myds 去查表就查不到预设，base_url 变空、直接报错。
            preset = PRESETS.get(args.preset or name, {})
            if args.preset and args.preset not in PRESETS and not args.base_url:
                print(f"没有名为 {args.preset!r} 的内置预设。可选：{', '.join(PRESETS)}")
                print("或者用 --base-url 指定完整的 OpenAI 兼容地址。")
                return 1
            base_url = args.base_url or preset.get("base_url") or ""
            model = args.model or (preset.get("models") or [""])[0]
            if not base_url or not model:
                print("非交互导入需要 --base-url 和 --model（或用 --preset 指定预设）。")
                return 1
            vision = is_vision_model(model, preset)
            entry = {
                "label": args.label or preset.get("label") or name,
                "base_url": base_url,
                "api_key": args.api_key or "",
                "api_key_env": args.api_key_env or "",
                "model": model,
                "vision": True if vision else None,
                "vision_model": model if vision else "",
                "json_mode": preset.get("json_mode", False),
            }
        else:
            entry = _collect_interactive(cfg)
            name = entry.pop("name")
        if name in sec["providers"]:
            print(f"（覆盖已有同名配置：{name}）")
        sec["providers"][name] = entry
        if args.set_active or not sec.get("active"):
            sec["active"] = name
        save(cfg)
        print(f"\n已保存模型配置：{name}")
        print(f"  模型：{entry['model']}   读图：{_vision_label(entry.get('vision'))}")
        print(f"  接口：{entry['base_url']}")
        print(f"  默认：{active_name(cfg)}")
        if not args.no_test:
            print("\n正在测试连通性 ...")
            try:
                r = test_provider(cfg, name)
                print(f"  ✓ 可用（{r['latency_ms']} ms）：{(r['reply'] or '').strip()[:30]}")
            except Exception as e:
                print(f"  ✗ 测试失败：{e}")
                print("    配置已保存，修好密钥后可用 `model test` 重试。")
        return 0

    if sub == "use":
        name = args.name
        if name not in sec["providers"]:
            print(f"没有名为 {name!r} 的配置。先 `model list` 看看有哪些。")
            return 1
        sec["active"] = name
        save(cfg)
        print(f"默认模型已切换为：{name}（{sec['providers'][name].get('model')}）")
        return 0

    if sub == "test":
        try:
            r = test_provider(cfg, args.name)
        except LLMError as e:
            print("✗", e)
            return 1
        except Exception as e:
            print("✗ 测试失败：", e)
            return 1
        print(f"✓ {r['label']} / {r['model']}")
        print(f"  延迟 {r['latency_ms']} ms   回复：{(r['reply'] or '').strip()[:40]}")
        if r["usage"]:
            print(f"  tokens：{r['usage']}")
        return 0

    if sub == "show":
        try:
            p = resolve_provider(cfg, args.name)
        except LLMError as e:
            print(e)
            return 1
        show = {k: v for k, v in p.items() if not k.startswith("_")}
        show["api_key"] = mask_key(p["api_key"])
        print(json.dumps(show, ensure_ascii=False, indent=2))
        return 0

    if sub == "rm":
        name = args.name
        if name not in sec["providers"]:
            print(f"没有名为 {name!r} 的配置")
            return 1
        sec["providers"].pop(name)
        if sec.get("active") == name:
            sec["active"] = next(iter(sec["providers"]), "")
        save(cfg)
        print(f"已删除 {name}；当前默认：{active_name(cfg) or '未设置'}")
        return 0

    print("未知的 model 子命令")
    return 1
