# chaoxing-auto-player

超星 / 学习通网页版的章节工具集。用真实浏览器打开课程页面，可以：

1. **连播**——在**平台原生播放器**里按目录顺序把选中的章节一节一节播完，无人值守；
2. **整理测验**——把章节测验的题目抓成结构化数据，**自动生成参考答案与解析**，导出成学生版复习册。

支持按单节、按整章、按区间、按全课程选择；播放时按间隔自动抽帧，
方便事后整理笔记或生成讲解内容。

> **免责声明**：本项目仅为教学与学习辅助工具。请遵守所在平台的服务协议与学校规定，
> **不要用于代看、刷课、售卖等用途**。
> 测验功能**只读**：不会点击「提交」「保存」，不会修改任何作答记录；
> 生成的答案是**参考答案**（大模型依据本课讲义 + 联网检索推断），
> 每题都标了依据与置信度，**不保证绝对正确**，考试一律以线下为准。

---

## 功能

**播放**

- 在学习通**原生播放器**里播，进度正常记录，不替换播放器、不伪造时长
- 多章节顺序连播：一节播完自动打开下一节继续播
- 选择方式：单节 `4.1` / 整章 `4` / 区间 `--from 4 --to 6` / 全课程 `--all`
- 章节测验这类没有视频的节点自动识别并跳过
- 播放中按间隔抽帧（默认 5 秒一张），自动裁成纯视频画面
- 实时状态文件 + 暂停 / 继续 / 停止指令
- 断点续播：中断后重跑会从平台记录的位置继续

**测验**

- **一条命令跑到底** `quiz auto`：抓讲义 → 抓题 → **调大模型作答** → 出复习册，
  中间不需要人工介入
- **模型自己导入自己选** `model add`：内置 DeepSeek / 通义千问 / Kimi / 智谱 / OpenAI /
  硅基流动 / OpenRouter / 本地 Ollama 预设，也支持任何 OpenAI 兼容接口；
  密钥只写本地 `config.json`（已被 `.gitignore` 忽略）
- **联网查资料**：答案解析不出来时自动检索中文题库与百科（360 / 搜狗 / 百度 / 必应，
  可选博查 / Tavily / Serper），把命中页的摘要作为可核验依据
- **两遍独立作答 + 规则化置信度**：第二遍换一套提示词从零推导，两遍不一致就降到 low；
  置信度是**算出来的**，不采信模型自评
- 自动识别文字反爬：平台会把题干汉字替换成生僻码位 + 内嵌字体还原，
  本项目用字形位图匹配自动反解（内置缓存，同一套字体只算一次）
- 抓讲义整页图到 `cache/docs/`，用视觉模型压缩成「按页编号的知识要点」，
  答案可标注到**具体页码**
- 生成**学生版单文件复习册**：答案默认折叠、正确项高亮、带来源与置信度徽标、可打印
- 附一页纸「答案速查表」，以及每题的知识点标签，方便回查讲义
- 全过程留痕：`solver_trace.md`（人看）、`solver_run.json`（审计）、`evidence.json`（检索记录）
- `doctor` 一条命令体检环境 / 模型 / 检索 / 题库；`selftest.py` 离线验证流水线


## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（依赖本机 Edge，暂不支持 macOS / Linux） |
| 浏览器 | Microsoft Edge（脚本自己启动，不需要手动开） |
| Python | 3.9 及以上 |

## 安装

```bash
git clone https://github.com/olordinary/chaoxing-auto-player.git
cd chaoxing-auto-player

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

> **国内网络**：pip 默认走 `pypi.org`，`playwright` 的 wheel 有 38 MB，常常只有十几 kB/s
> 甚至超时（`Connection timed out` 后从断点续传，看起来永远下不完）。换镜像即可，
> 实测清华源 1.6 MB/s，快 170 倍：
>
> ```bash
> pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
> pip config set global.timeout 60
> pip install -r requirements.txt
> ```
>
> 本项目用本机 Edge 走 CDP，**不需要**执行 `playwright install`（那一步才是下浏览器，更慢）。

## 三步开始

```bash
# 1) 导入课程：登录后打开任意一节的「章节详情」页，脚本会自动识别；
#    也可以直接粘贴地址栏链接
python chaoxing_player.py init
# 或
python chaoxing_player.py init --url "https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=...&courseId=..."

# 2) 登录（只在第一次需要，登录态保存在本地 profile 目录）
python chaoxing_player.py login

# 3) 导入一个大模型（只在第一次需要，交互式选服务商 + 粘密钥）
python chaoxing_player.py model add

# 4) 体检一下（可选，但建议：会告诉你模型/检索/讲义哪一环不通）
python chaoxing_player.py doctor

# 5) 用起来——两个功能各一条命令
python chaoxing_player.py list                     # 看目录（播放用）
python chaoxing_player.py play --chapters 4 --mute

python chaoxing_player.py quiz list                # 看有哪些测验
python chaoxing_player.py quiz auto --chapter 4    # 抓讲义 → 抓题 → 作答 → 出复习册
```

**播放**和**测验**是两条独立的线，互不影响：播放不碰题目，测验不播放视频。
下面先说播放，再说测验；只想用其中一个，看对应那节就行。

> 不导入模型也能用：`quiz auto` 会照常抓讲义和题目，只是停在「待作答」，
> 并把 `solve_payload.md` 准备好。导入模型后重跑即可一路生成答案。


## 命令

### `init` — 导入课程

```bash
python chaoxing_player.py init                                  # 从已打开的标签页自动识别
python chaoxing_player.py init --url "<章节详情页完整链接>"        # 直接解析链接
```

课程参数写进 `config.json`。这个文件已被 `.gitignore` 忽略，不会被提交。

### `list` — 列目录

```bash
python chaoxing_player.py list
python chaoxing_player.py list --refresh     # 忽略缓存重新读取
```

输出序号、编号（`4.1` 这种）、`knowledgeid` 和标题。

### `play` — 播放

| 参数 | 说明 |
|---|---|
| `--chapters 4.1,4.3` | 按编号选；给 `4` 表示整章，自动展开成各小节 |
| `--from 4 --to 6` | 按目录顺序取区间 |
| `--all` | 全部内容节点 |
| `--index 12 13` | 按 `list` 里的序号 |
| `--mute` | 静音（挂机推荐） |
| `--rate 1.0` | 倍速，默认 1.0。不建议调高，平台可能判定异常 |
| `--capture-interval 5` | 每 5 秒抽一帧，默认 5 |
| `--no-capture` | 不抽帧，只播 |
| `--max-seconds 20` | 每节最多播 20 秒（调试用） |
| `--dry-run` | 只打印播放计划 |
| `--detach` | 脱离终端后台跑 |
| `--run NAME` | 指定本次运行的目录名 |

```bash
python chaoxing_player.py play --chapters 4            # 第 4 章所有小节
python chaoxing_player.py play --from 5 --to 8         # 第 5 到第 8 章
python chaoxing_player.py play --all --mute --detach   # 全课程后台静音跑
```

### `status` / `control` — 查看与控制

```bash
python chaoxing_player.py status
# 播放：386s / 660s (58.5%)  播放中  倍速 1  静音 True

python chaoxing_player.py control <run名> pause
python chaoxing_player.py control <run名> resume
python chaoxing_player.py control <run名> stop
```

### `analyze` — 把抽到的帧切成「一页一图」

```bash
python chaoxing_player.py analyze runs/20261027-153000
```

按画面差异分段，每段取最后一帧（画面文字最完整的那帧），输出到 `slides/<章节id>/`。

### `quiz` — 章节测验

#### 先跑这一条

```bash
python chaoxing_player.py quiz auto --chapter 5
```

它会一口气做完：

1. 读课程目录，找到第 5 章的测验（5.5）和它依据的讲义（5.1–5.4）
2. 抓讲义整页图（已有则跳过）
3. 抓题目、反解字体混淆
4. 生成 `solve_payload.md`——把依据材料、题目、作答规范打包
5. **自动作答**：视觉模型把讲义压成按页要点 → 分批判卷 → 低置信题联网检索 →
   换一套提示词独立复核 → 按规则定置信度，写出 `answers.json`
6. 出复习册 `参考答案.html` + `答案速查.md`

第 5 步需要先导入模型（`model add`）。没导入时 `auto` 会停在第 4 步，
并提示你怎么补——其余步骤不受影响。

#### 不知道该处理哪一章？先列出来

```bash
python chaoxing_player.py quiz list      # 列 9 套测验，带编号
python chaoxing_player.py quiz auto --all --dry-run   # 看全部 9 套的配对关系，不抓取
```

`--dry-run` 会打印每套测验配对到哪几节讲义，**建议先看这个再动手**：

```
第5章 植物细胞和组织培养
  依据讲义：5.1(11页) 5.2(11页) 5.3(9页) 5.4(无页)
  目标测验：5.5 章节测试  kid=1260414120
  产物目录：ch05/quiz/5.5/
```

#### 跑完得到什么

```
runs/ch05/
├── _flow.json              # 每套测验走到哪一步，重跑时看这个
└── quiz/5.5/
    ├── questions.json      # 题目
    ├── solve_payload.md    # 依据材料 + 题目 + 作答规范（人看/外部作答都行）
    ├── answers.json        # 答案（自动生成；也可手工改）
    ├── solver_trace.md     # ← 先看这个：逐题两遍结果 + 需人工复核清单
    ├── solver_run.json     # 审计：模型、tokens、耗时、每次调用
    ├── evidence.json       # 检索记录：查了什么词、命中哪些页
    ├── 参考答案.html
    └── 答案速查.md
```

#### 答案怎么来：全自动

导入模型后，`quiz auto` 直接跑到出复习册，不用你写 `answers.json`。
想单独重跑作答（比如换模型、改参数），用 `quiz solve`：

```bash
# 对已抓到的题重新作答（会跳过已有 answers.json，要覆盖加 --force）
python chaoxing_player.py quiz solve --chapters 5.5 --force

# 只看计划、不发请求
python chaoxing_player.py quiz solve --all --dry-run

# 换一个模型作答（先用 model list 看有哪些）
python chaoxing_player.py quiz solve --chapters 5.5 --model qwen --force
```

`quiz solve` **全程只读本地文件，不需要开浏览器**，所以可以离线慢慢跑。

`solve_payload.md` 仍在生成，它的用途变成两种：一是给你自己核对"模型看到了什么"，
二是你想手动改答案时的底稿——改完 `answers.json` 后 `quiz auto --report-only` 重出册。


#### 两种 `--all` 完全不同，别搞混

| 命令 | `--all` 的范围 | 会做什么 |
|---|---|---|
| `play --all` | 目录里**全部播放节点**（视频、文档都算） | 逐个播放，抽帧，耗时按小时计 |
| `quiz auto --all` | **只有测验节点**（通常 9 套） | 抓讲义 + 抓题 + 作答 + 出册，不播放任何视频 |

`quiz auto --all` 也不会重复抓已有的讲义和题目，所以第二次跑很快；
已有 `answers.json` 的套不会被覆盖（要重答加 `--solve --force`）。

#### 常用参数

| 参数 | 作用 |
|---|---|
| `--chapter 5` / `--chapters 5,6` | 指定章（推荐，明确） |
| `--all` | 全部测验，见上表 |
| `--dry-run` | 只看配对计划，不抓取 |
| `--solve` / `--no-solve` | 强制 / 禁止本次自动作答（默认：导入过模型就答） |
| `--model NAME` | 本次用哪个已导入的模型作答 |
| `--search auto\|on\|off` | 联网策略：默认只查低置信题，`on` 全查，`off` 不查 |
| `--report-only` | 跳过抓取，只重出报告（改完答案用这个） |
| `--force` | 忽略已有讲义/题目重抓 |
| `--require-submitted` | 只处理已提交的测验（默认不限制） |
| `--docs-for 5.5=5.1,5.2` | 同章有多套测验、自动配对判断不了时人工指定；多组用 `;` 隔开 |

#### 分步命令（想手动控制时用）

`auto` 是这几步的封装，单独跑也行：

| 命令 | 做什么 |
|---|---|
| `quiz docs --chapters 5.1,5.2` | 只抓讲义 |
| `quiz fetch --chapters 5.5 --run myrun` | 只抓题 |
| `quiz solve --chapters 5.5` | 只作答（读本地题目，不开浏览器） |
| `quiz report runs/myrun` | 只出报告 |

#### `answers.json` 字段

自动作答会按这个结构写出；你也可以直接改，改完 `--report-only` 重出册。

```json
{"no": 1, "answer": ["B"], "source": "ai", "confidence": "medium",
 "explain": "……为什么选它，为什么不选其他项……",
 "cite": "讲义 5.1 第11页（细胞起始密度的定义）",
 "knowledge_tag": "细胞起始密度",
 "verify": {"pass1": ["B"], "pass2": ["B"], "agree": true,
            "note": "两遍一致，依据为讲义页码", "doubt": ""}}
```

- `source`：`ai`（模型生成）/ `platform`（平台给的答案）/ `inferred`（从逐题对错反推），
  后两种只在确实拿到平台信息时才出现
- `confidence`：`high` / `medium` / `low`，**由规则算出**（见下节），会显示在复习册徽标上
- `cite` 必须指向真实可查的位置（讲义第几页 / 检索第几条 / 通用学科知识）；
  没有讲义却写页码、没联网却写「检索[N]」，都会被自动改写并降级
- `verify` 记录两遍的结果与一致性说明；不一致时 `confidence` 直接降到 `low`

判断题 `answer` 写 `["对"]` 或 `["错"]`；多选题写全部字母如 `["A","C"]`。

**关于文字反爬**：平台的题干会被替换成生僻字码位，靠内嵌字体还原显示。
本项目用字形位图匹配自动反解（拿内嵌字体的字形与系统 `Noto Sans SC` 逐字比对），
实测准确率约 97%；映射按字体哈希缓存在 `cache/fonts/`，可人工校订后写回缓存。
题干里若出现明显形近错字（「体细一」=「体细胞」），提示词已要求模型按学科常识纠正后再作答。

### 自动作答是怎么工作的

`quiz solve` 一次跑五步，每步都有留痕，方便你事后审核：

| 步骤 | 做什么 | 不通时的降级 |
|---|---|---|
| ① 讲义摘要 | 把 `cache/docs/<编号>/` 的整页图交给**视觉模型**，压成「按页编号的知识要点」，缓存到 `_digest.json` | 没有视觉模型就跳过，答案只能引检索资料或通用知识 |
| ② 第一遍作答 | 按批（默认 5 题）让模型给答案，强制 JSON 输出；答案必须带 cite | 单批失败自动二分重试，单题仍失败则记错误不中断 |
| ③ 联网检索 | 只对第一遍**不是 high**的题发起，命中结果进 `evidence.json` | 连续 3 题查不到就整段关掉，不拖时间 |
| ④ 第二遍独立复核 | 换一套提示词，要求**逐项判断每个选项对错**再从零汇总，避免确认偏误 | `--no-pass2` 可关闭；关了最高只能到 medium |
| ⑤ 定稿 | 两遍一致性 + 依据等级 → 置信度（规则算），写 `answers.json` / `solver_trace.md` | — |

**置信度是规则算出来的，不是模型自评**：

| 条件 | 置信度 |
|---|---|
| 两遍一致 + 引用到讲义具体页码 | `high` |
| 两遍一致 + 有讲义/检索依据但没标到页码 | `medium` |
| 两遍一致 + 只有通用学科知识 | `medium`（模型自称 high）或 `low` |
| 两遍**不一致** / 只跑了一遍 / 答案格式不合法 | `low` |

依据来源的可靠性顺序（提示词里也按这个优先级要求）：

| 层级 | 依据 | 可靠性 |
|---|---|---|
| L1 | 平台给出的答案 | 最高（本工具不采集，保持只读） |
| L2 | 本课讲义，能指到具体页 | 高 |
| L3 | 联网检索到的题库/百科（含 360 的 AI 解题卡） | 中 |
| L4 | 通用模型记忆 | 最低，必须标 low/medium |

实测（同一套 18 题的测验）：只给题目、不给讲义也不联网时，全部落在 L4；
抓了讲义并用视觉模型摘要后，多数题能指到具体页，`high` 大幅增加。
**`solver_trace.md` 末尾有「需要人工复核的题」清单，产物里低置信度的题不要直接发给学生。**

### 联网检索用的是哪些引擎

自动作答时会去中文题库/百科找依据。默认**不需要任何密钥**：

| 引擎 | 说明 |
|---|---|
| 360 搜索 | 首选。中文题目命中率最高，不弹验证码；首条常是「AI 智能解题」卡片，会作为线索收进来（会标注“他站 AI 作答”） |
| 搜狗 | 题库站（作业帮、知了好学等）经常直接命中同款题 |
| 百度 | 质量最好，但小流量必弹「安全验证」，需先拿 cookie；被拦会冷却 15 分钟 |
| 必应 | 中文长句会退化成词典结果（搜「秋水仙素…」返回《庄子·秋水》），只吃关键词版查询，兜底用 |
| DuckDuckGo | 走代理容易 502，最后兜底 |

想更稳可以配付费检索（中文质量更好、有配额保障），写进 `config.json` 的 `search` 段：

```json
"search": {"engine": "auto", "max_results": 5,
           "bocha_api_key": "", "tavily_api_key": "", "serper_api_key": "",
           "searx_url": ""}
```

`engine` 设成 `auto` 时按「已配密钥的付费引擎 → 360 → 搜狗 → 百度 → 必应 → DuckDuckGo」
依次试，某引擎返回的结果和查询词毫无交集就换下一个（这条判据很关键，
否则必应对中文长句返回的词典页会一直挡住后面的引擎）。
检索结果缓存 7 天，同一套题反复跑不会重复请求。


### `model` — 导入 / 选择大模型

答案由大模型生成，所以先得告诉它用哪个模型。配置写在 `config.json` 的 `llm` 段
（该文件已被 `.gitignore` 忽略，密钥不会进版本库）。

```bash
python chaoxing_player.py model list        # 看已导入的 + 内置预设
python chaoxing_player.py model add         # 交互式：选服务商 → 选模型 → 粘密钥
python chaoxing_player.py model use qwen    # 设为默认
python chaoxing_player.py model test qwen   # 测连通性
python chaoxing_player.py model show qwen   # 看完整配置（密钥打码）
python chaoxing_player.py model rm qwen
```

非交互导入（适合脚本化 / 一次性）：

```bash
python chaoxing_player.py model add --preset deepseek --api-key sk-xxx
python chaoxing_player.py model add --preset dashscope --model qwen-vl-max --set-active
python chaoxing_player.py model add --preset ollama        # 本地模型，无需密钥
```

内置预设：DeepSeek、阿里云百炼（通义千问）、月之暗面（Kimi）、智谱 GLM、OpenAI、
硅基流动、OpenRouter、本地 Ollama。**任何 OpenAI 兼容接口**都能用
（`--base-url https://xxx/v1 --model 模型名`），包括 vLLM、LM Studio、one-api 之类的中转。

两个和能力相关的点：

- **读图能力**：讲义是整页图片，要转成可引用的文字要点必须有视觉模型。
  主模型不支持读图时，`solve` 会自动在**已导入的模型里找一个能读图的**来读讲义
  （也可以用 `--digest-model` 指定）。一个能读图的都没有，就退化成纯文本作答。
- **JSON 模式**：DeepSeek / 通义 / 智谱 / OpenAI 等支持 `response_format`；
  Kimi、OpenRouter 不支持，程序会自动去掉这个字段重试。

### `doctor` — 体检

一条命令检查「环境 / 配置 / 模型 / 检索 / 讲义 / 题库 / 流水线状态」，
每项给出 ✓ 或 ✗，失败项后面直接跟该敲什么命令修：

```bash
python chaoxing_player.py doctor            # 全量（会调一次模型、试探各引擎）
python chaoxing_player.py doctor --no-llm   # 不调模型（省钱）
python chaoxing_player.py doctor --no-net   # 不测检索
```

典型输出：

```
—— 大模型 ————————————————
  ✓ 已导入模型：2 个，当前默认 deepseek
      * deepseek    deepseek-chat                纯文本
        qwen        qwen-vl-max                  读图
  ✓ deepseek 可用（812ms，模型 deepseek-chat）

—— 联网检索 ————————————————
  ✓ 360 搜索      3 条/相关 3 条  540ms
  ✓ 搜狗          3 条/相关 3 条  950ms
  ✗ 百度          0 条/相关 0 条  743ms
       → 检索失败：返回了安全验证页（触发反爬，已冷却 15 分钟）
```

### `selftest.py` — 离线自检流水线

不联网、不花钱，用一个本地假模型把「抓题 → 作答 → 出册」整条链路跑一遍，
几十秒就能确认不是代码问题：

```bash
python selftest.py           # 纯离线（建议改完代码先跑这个）
python selftest.py --net     # 额外测真实检索链路
```

它覆盖：JSON 容错解析、答案规范化、置信度规则（包括"编造页码""没联网却引检索"这两种
必须被改写的作弊情形）、讲义视觉摘要与缓存复用、`answers.json`/`trace`/复习册产物完整性。


## 输出

**播放**

```
runs/<run名>/
├─ player.log          # 运行日志
├─ state.json          # 实时进度，轮询这个
├─ control.json        # 控制指令
├─ manifest.json       # 每节的时长 / 已播 / 完成度 / 帧数
├─ frames/<章节id>/    # 抽到的帧（已裁成纯视频画面）
└─ slides/<章节id>/    # analyze 产出
```

**测验（`quiz auto`，推荐）**

```
runs/ch05/                     # 按章组织，一个章一个目录
├─ _flow.json                  # 每套测验的进度、配对到哪些讲义
└─ quiz/5.5/                   # 按测验编号，不用记 knowledge id
   ├─ questions.json           # 题面 + 选项 + 题型（结构化）
   ├─ solve_payload.md         # 依据材料 + 题目 + 作答规范
   ├─ answers.json             # 参考答案 + 解析 + 依据 + 置信度（自动生成）
   ├─ solver_trace.md          # 逐题两遍结果 + 需人工复核清单（← 先看这个）
   ├─ solver_run.json          # 审计：模型、每次调用、tokens、耗时
   ├─ evidence.json            # 检索记录：查询词、命中页、引擎
   ├─ 参考答案.html             # 学生版复习册（答案折叠、可打印）
   └─ 答案速查.md               # 一页纸速查
```

讲义单独缓存在 `cache/docs/<章节号>/`，跨 run 复用，不重复下载；
视觉模型生成的按页要点存在同目录的 `_digest.json`，页面没变就一直复用。

**测验（`quiz fetch --run`，手动模式）**

```
runs/quiz-<run名>/
├─ manifest.json
└─ <章节id>/
   ├─ questions.json
   ├─ answers.json
   ├─ 参考答案.html
   └─ 答案速查.md
```

`manifest.json` 示例：

```json
[{"kid":"...","num":"4.1","title":"原代培养","status":"finished",
  "played":659.8,"duration":659.8,"frames":132,"completion":100.0}]
```

`status` 取值：`finished` / `no_video` / `truncated` / `stopped_by_user` /
`need_login` / `cannot_play` / `player_lost`。

## 实现要点

如果你也想改这个脚本，这几条能少走弯路：

1. **不能复用默认 Edge 配置做自动化**
   Edge 136+ 禁止对默认 User Data 目录开远程调试，`--remote-debugging-port` 无效，
   Playwright 的 `launch_persistent_context` 会直接卡死。
   所以脚本用**独立的 profile 目录**（`profile/`）启动一个新窗口，登录一次长期有效。

2. **不能靠复制配置目录「借用」登录态**
   新版 Edge 的 Cookies 是 App-Bound 加密，绑定配置目录路径，拷到别处解不开。

3. **本地调试端口要走直连**
   脚本里强制 `NO_PROXY=127.0.0.1,localhost`，否则系统里的 http 代理会把 CDP 请求拦成 502。

4. **播放按钮必须真实点击**
   播放器是 videojs，`.vjs-play-control` 用 JS `click()` 无效，得用鼠标坐标点。
   脚本带重试，并在检测到异常暂停时自动恢复。

5. **抽帧要按「画面绘制区 ∩ 容器裁剪区」算**
   页面缩放（DPR）不为 1，且视频元素常被右侧目录栏覆盖，
   直接按元素 bbox 截图会带进侧边栏。脚本先在页面里算出真实可见矩形，再按比例裁剪。

6. **测验题干的字体反爬怎么破**
   平台把部分汉字换成生僻码位（集中在子字旁 U+5A18–U+5B67），页面内嵌一个 base64
   子集字体把字形画回来，所以「看着正常、源码是乱的」。字体 `post` 表是 format 3.0、
   字形名等于假码位本身，读不出真字。可行解法是**字形位图比对**：
   把内嵌字体每个字形渲染成二值位图，与系统字体的候选字逐一比相似度。
   参考字体必须用 `NotoSansSC-VF.ttf`（与平台用的 Source Han Sans 同源），
   换成微软雅黑准确率会从 97% 掉到 84%。候选集限制在 GB2312 可排除日文变体。

7. **平台自身的一个渲染 bug**
   `孢`（U+5B62）恰好落在混淆字体覆盖的码位范围内，会被渲染成 `甲`，
   于是页面显示「细菌芽甲」。抓取后建议对这类明显的错字做一次校正。

8. **检索解析跟着站点走，非常容易退化**
   写这套解析时踩到的几个坑，改代码前先看一眼：
   - 搜狗的结果标题是 `<h3 class="vr-title  ">`，**class 后面带尾随空格**，
     精确匹配 `class="vr-title"` 会一个字都取不到，表现成"搜到 0 条"而不是报错；
   - 百度对小流量直接返回「安全验证」页，必须先访问首页拿 `BAIDUID` cookie
     （`netkit` 里有进程级 cookie 罐 + `warmup`）；被拦要判定成"引擎不可用"、
     换下一个引擎，而不是"这题没资料"；
   - 必应对中文长句会退化成词典结果（搜「秋水仙素诱导染色体加倍」返回《庄子·秋水》），
     所以查询词要做两套：整句版给中文引擎，关键词版给必应/兜底；
   - 360 的「AI 智能解题」卡片（`<p class="mh-answer">答案：A.…</p>`）经常直接给出答案，
     但那是别家模型生成的，只能当线索，提示词里必须标成"他站 AI 作答、须自行核对"。

9. **答案质量靠流程约束，不靠提示词许愿**
   三件事是硬约束：① 引用必须能被程序校验（讲义页码 / 检索序号），编的会被改写降级；
   ② 第二遍换提示词、要求逐项判断选项，避免两遍其实是同一条思路；
   ③ 置信度按规则算，模型自评只作为降级因素、不作为升级依据。

## 常见问题

**Q：必须一直开着那个浏览器窗口吗？**
是。窗口关掉播放就断，重跑会从平台记录的位置继续。

**Q：能拖进度条吗？**
平台规定「观看时长 ≥ 总时长 90%」才算完成，**完成前不允许拖拽**，所以只能线性播。
已完成的章节可以拖。

**Q：为什么有的节点显示 `no_video`？**
章节测验、讨论、作业这类节点没有视频，脚本会跳过，不影响后面的排期。

**Q：抽帧数量比预期少？**
如果播放期间窗口被移动或页面被滚走，视频会跑出视野导致截图失败。
脚本已做处理：连续失败会自动把视频滚回视野中心，并在日志里提示 `capture_misses`。

**Q：能用倍速吗？**
能设，但默认 1.0。平台对学习时长有校验，倍速有被判异常的风险，自行斟酌。

**Q：抓下来的题干里有生僻字（比如「孁菌」）？**
说明该套题的内嵌字体没被正确匹配。先看日志有没有「字形置信度低」的提示；
`cache/fonts/` 下按字体哈希存着映射，可以人工改对后写回缓存（`"verified_by": "manual"`），
下次同一套字体直接命中。

**Q：答案是标准答案吗？**
不是。平台不提供答案，这里的答案是大模型依据**本课讲义 + 联网检索**推断出的参考解，
每题都标了来源（`cite`）和置信度。标 `low` 的题请自行核对教材；
`solver_trace.md` 末尾列出了所有需要人工复核的题。

**Q：自动作答会不会花很多钱？**
一套 15 题的测验大约 6 次模型请求（3 批 × 2 遍）+ 若干次检索，检索免费。
主要成本在**讲义摘要**：有 30 页讲义且用视觉模型时，会按 3 页一批发 10 次请求。
摘要结果按页面签名缓存在 `_digest.json`，同一套讲义只算一次。
想先看规模：`quiz solve --all --dry-run` 会打印预计请求数。

**Q：不导入模型能用吗？**
能，只是停在「待作答」：`quiz auto` 会抓讲义、抓题、生成 `solve_payload.md`。
之后你可以自己写 `answers.json`，或者导入模型后 `quiz solve --chapters 5.5`。

**Q：模型答得不准怎么办？**
先看 `solver_trace.md`：`两遍不一致`通常说明题目本身有歧义或题干有错字；
`只有通用学科知识支撑`说明这一节没抓到讲义（`quiz docs --chapters 5.x` 补抓）。
换更强的模型也行：`quiz solve --chapters 5.5 --model xxx --force`。

**Q：检索一个都连不上？**
`doctor --no-llm` 会逐个引擎报状态。国内网络建议直接保持免密钥引擎可用状态；
如果所在网络访问不到这几个站点，配一个 `search.bocha_api_key`（中文检索、有配额）最省事。
检索在作答流程里是可降级的——查不到就不当依据，答案会相应降低置信度。

**Q：抓题会留下作答记录吗？**
不会。`quiz fetch` 与 `quiz auto` 全程只读：不点击「提交」「暂时保存」，不填写任何答题框。
`quiz solve` 更是只读本地文件，连浏览器都不开。

## License

[MIT](LICENSE)
