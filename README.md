# chaoxing-auto-player

超星 / 学习通网页版的章节工具集。用真实浏览器打开课程页面，可以：

1. **连播**——在**平台原生播放器**里按目录顺序把选中的章节一节一节播完，无人值守；
2. **整理测验**——把章节测验的题目抓成结构化数据，配参考答案与解析，导出成学生版复习册。

支持按单节、按整章、按区间、按全课程选择；播放时按间隔自动抽帧，
方便事后整理笔记或生成讲解内容。

> **免责声明**：本项目仅为教学与学习辅助工具。请遵守所在平台的服务协议与学校规定，
> **不要用于代看、刷课、售卖等用途**。
> 测验功能**只读**：不会点击「提交」「保存」，不会修改任何作答记录；
> 答案由教师/使用者自行整理，属于「参考答案」，不保证绝对正确，考试一律以线下为准。

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

- 自动识别文字反爬：平台会把题干汉字替换成生僻码位 + 内嵌字体还原，
  本项目用字形位图匹配自动反解（内置缓存，同一套字体只算一次）
- 抓取题型、题干、选项、分组标题，输出 `questions.json`
- `require_submitted` 可按课程开关（预习类习题允许未提交也能抓）
- 生成**学生版单文件复习册**：答案默认折叠、正确项高亮、带来源与置信度徽标、可打印
- 附一页纸「答案速查表」，以及每题的知识点标签，方便回查讲义

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

## 三步开始

```bash
# 1) 导入课程：登录后打开任意一节的「章节详情」页，脚本会自动识别；
#    也可以直接粘贴地址栏链接
python chaoxing_player.py init
# 或
python chaoxing_player.py init --url "https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=...&courseId=..."

# 2) 登录（只在第一次需要，登录态保存在本地 profile 目录）
python chaoxing_player.py login

# 3) 播放
python chaoxing_player.py list                    # 先看目录
python chaoxing_player.py play --chapters 4 --mute
```

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

### `quiz` — 章节测验：抓题 + 出参考答案复习册

```bash
# 1) 看课程里有哪些测验
python chaoxing_player.py quiz list

# 2) 先抓讲义，给答案铺依据（强烈建议，见下节）
python chaoxing_player.py quiz docs --chapters 5.1,5.2,5.3
python chaoxing_player.py quiz docs --from 5 --to 6 --dry-run   # 只看抓哪些

# 3) 抓题（只读；未提交的测验默认也允许，可用 --require-submitted 收紧）
python chaoxing_player.py quiz fetch --chapters 5.5 --run quiz-ch5

# 4) 整理答案写入 answers.json，再生成复习册
python chaoxing_player.py quiz report runs/quiz-ch5
```

`docs` 抓讲义、`fetch` 出 `questions.json`（题面、选项、题型、分组）；
`report` 需要同目录下存在 `answers.json`，两者合并生成 `参考答案.html` 与 `答案速查.md`。

`answers.json` 每题的字段：

```json
{"no": 1, "answer": ["B"], "source": "ai", "confidence": "medium",
 "explain": "……为什么选它，为什么不选其他项……",
 "cite": "讲义 5.1 第11页（细胞起始密度的定义）",
 "knowledge_tag": "细胞起始密度",
 "verify": {"pass2": "B", "agree": true, "note": "讲义无具体数值，依据来自通用经验值"}}
```

- `source`：`platform`（平台给的答案）/ `inferred`（从逐题对错反推）/ `ai`（生成）
- `confidence`：`high` / `medium` / `low`，会显示在复习册的徽标上
- `cite` 必须指向真实可查的位置（讲义第几页），没有依据时写「通用学科知识（X 无讲义）」
- `verify` 记录第二遍校验结果；两遍不一致时把 `confidence` 降级

**关于文字反爬**：平台的题干会被替换成生僻字码位，靠内嵌字体还原显示。
本项目用字形位图匹配自动反解（拿内嵌字体的字形与系统 `Noto Sans SC` 逐字比对），
实测准确率约 97%；映射按字体哈希缓存在 `cache/fonts/`，可人工校订后写回缓存。

### 参考答案是怎么来的

这一点必须说清楚，否则容易高估产物的可靠性：

**代码不生成答案。** `quiz.py` 只做三件事——抓题、反解混淆、把题与答案渲染成页面。
`answers.json` 是外部输入，由人（或对话中的模型）在 `questions.json` 基础上逐题写出。
项目里没有任何大模型 API 调用，也没有联网检索答案。

答案质量取决于**证据基础**，按可靠性分三层：

| 层级 | 依据 | 可靠性 | 怎么拿到 |
|---|---|---|---|
| L1 | 平台给出的答案 | 最高 | 教师开放「查看答案」 |
| L2 | 平台判分反推 | 高 | 测验已提交且该题答对，正确项 = 所选项 |
| L3 | 本课讲义 | 中高 | `quiz docs` 抓下来，答案可引具体页 |
| L4 | 通用模型记忆 | 中低 | 无上述依据时只能用这个，置信度必须下调 |

`quiz docs` 存在的意义就是把 L4 抬到 L3。实测对比（同一套 18 题的 5.5 测验）：
不抓讲义时全部落在 L4，只能标「通用学科知识」；抓了 5.1–5.3 共 31 页讲义后，
13 题能指向具体页，高置信度题从 0 提升到 10。

**产物里低置信度的题不要直接发给学生**，人工过一遍再定稿。

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

**测验**

```
runs/quiz-<run名>/
├─ manifest.json
└─ <章节id>/
   ├─ questions.json   # 题面 + 选项 + 题型（结构化）
   ├─ answers.json     # 参考答案 + 解析 + 依据 + 置信度（自行填写）
   ├─ 参考答案.html     # 学生版复习册（答案折叠、可打印）
   └─ 答案速查.md       # 一页纸速查
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
不是。除教师开放了「查看答案」的情况外，平台不提供答案，这里的答案是**生成的参考解**，
每题都标了来源和置信度。标 `低置信` 或 `中置信` 的题请自行核对教材。

**Q：抓题会留下作答记录吗？**
不会。`quiz fetch` 全程只读：不点击「提交」「暂时保存」，不填写任何答题框。

## License

[MIT](LICENSE)
