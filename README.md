# chaoxing-auto-player

超星 / 学习通网页版的章节视频连播工具。用真实浏览器打开课程页面，
在**平台原生播放器**里按目录顺序把选中的章节一节一节播完，无人值守。

支持按单节、按整章、按区间、按全课程选择；播放时按间隔自动抽帧，
方便事后整理笔记或生成讲解内容。

> **免责声明**：本项目仅为个人学习辅助工具，用于把已选课程按顺序播完、顺带留存画面资料。
> 请遵守所在平台的服务协议与学校规定，**不要用于代看、刷课、售卖等用途**。
> 本项目不包含任何绕过平台检测、破解加密、伪造学习记录的代码。

---

## 功能

- 在学习通**原生播放器**里播，进度正常记录，不替换播放器、不伪造时长
- 多章节顺序连播：一节播完自动打开下一节继续播
- 选择方式：单节 `4.1` / 整章 `4` / 区间 `--from 4 --to 6` / 全课程 `--all`
- 章节测验这类没有视频的节点自动识别并跳过
- 播放中按间隔抽帧（默认 5 秒一张），自动裁成纯视频画面
- 实时状态文件 + 暂停 / 继续 / 停止指令
- 断点续播：中断后重跑会从平台记录的位置继续

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

## 输出

```
runs/<run名>/
├─ player.log          # 运行日志
├─ state.json          # 实时进度，轮询这个
├─ control.json        # 控制指令
├─ manifest.json       # 每节的时长 / 已播 / 完成度 / 帧数
├─ frames/<章节id>/    # 抽到的帧（已裁成纯视频画面）
└─ slides/<章节id>/    # analyze 产出
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

## License

[MIT](LICENSE)
