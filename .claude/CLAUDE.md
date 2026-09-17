# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地视频自动加字幕工具：上传视频 → 本地 faster-whisper 识别语音 → DeepSeek API 翻译成中文 → 网页实时叠字幕（边看边译）或 ffmpeg 烧录进画面。纯 Flask + 原生 JS，无 Node/前端构建工具，无测试框架（TEST-REPORT.md / ACCEPTANCE.md 是手工验收记录）。

## 常用命令

```bash
# 启动服务（访问 http://127.0.0.1:5001）
./venv/bin/python app.py

# 安装依赖 —— 注意：项目目录被移动过，venv/bin/pip 的 shebang 已失效，
# 必须用 python -m pip，不能直接调用 venv/bin/pip
./venv/bin/python -m pip install -r requirements.txt

# 端口被占用时重启（Flask 非 debug 模式会缓存 Jinja 模板，改 templates/ 后必须重启才能生效）
lsof -ti :5001 | xargs kill
```

烧录功能依赖带 libass 的 ffmpeg：`brew install ffmpeg-full`（keg-only，二进制在 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`，`pipeline.burn_ffmpeg()` 会自动探测并缓存）。系统精简版 ffmpeg 仅用于抽音频。DeepSeek key 由网页传入；本地模式下可回退 `.env` 的 `DEEPSEEK_API_KEY`。

## 架构

### 两条工作流（app.py 是入口，均为后台线程 + 内存 tasks 字典，无持久化）

1. **烧录模式**：`POST /upload` → `_burn_worker` 线程 → `pipeline.process_video()`（抽音频 → 整体识别 → 整体翻译 → 写 ASS/SRT → ffmpeg 烧录）→ 前端每秒轮询 `/progress/<id>` → `/download/<id>/{video,srt}`。
2. **边看边译**：`POST /upload_realtime` 保存后立即返回可播地址（`/video/<id>` 支持 Range 拖动）→ `_realtime_worker` 用 `pipeline.transcribe_stream()` 流式识别，每攒 `REALTIME_BATCH=5` 段翻译一批 → 追加到 task 的 `segments` → `/stream/<id>` 以 SSE 逐条推给前端。SSE 事件带 `id:`，断线重连时靠 `Last-Event-ID` 续传避免字幕重复（前端还有按 index 去重兜底）。

**共享模式**（`.env` 设了 `ACCESS_PASSWORD`，用于部署给朋友）：`before_request` 做全站 HTTP Basic 认证；`translator.server_key()` 返回空，不回退站长 key；清理线程按 `FILE_TTL_HOURS` 删除过期任务及其文件——只删文件名以 `<task_id>_` 开头、且属于本进程任务的文件，绝不能改成按目录/mtime 扫删（会误删本地以前的视频）。两种模式都经 `job_slot` 信号量串行执行任务（Whisper 单例不能并发推理），排队中的任务 `waiting=True`，`/progress` 与 SSE `event: status` 会推送排队提示。部署只能单进程（`gunicorn -w 1 --threads N`）。

`tasks` 字典的读写都要经过 `tasks_lock`；改动 task 结构时两个 worker、SSE 生成器和 `/progress` 都要对齐。

### 各模块职责与关键约束

- **pipeline.py**：Whisper 模型是模块级单例（`small` + int8，为 8GB M1 控制内存，别改成每次加载）。烧录先用 `h264_videotoolbox` 硬编，失败自动回退 `libx264` 软编。ffmpeg 滤镜路径要经 `_filter_escape` 转义。`process_video` 无论成败都会清临时 .wav。
- **translator.py**：DeepSeek 按 20 条一批、JSON 严格模式翻译；批量失败重试 3 次后降级为逐句普通文本模式，逐句仍失败则返回英文原文兜底——设计原则是长视频任务绝不因个别批次中断。401/402（key 无效/欠费）直接抛 `TranslationError` 不重试。源语言检测为中文时跳过翻译（原文即中文）。
- **subtitle.py**：ASS 以 PlayResY=720 为基准由 libass 等比缩放；双语模式中文在下（ZH 样式）、英文小号在上（EN_TOP 样式）。

### 前端（templates/index.html + static/main.js + static/style.css）

- `main.js` 通过大量固定 id/class 绑定 DOM（`#drop`、`#start`、`.tab[data-tab]`、`input[name=mode]`、`input[name=dmode]`、`#sub-en/#sub-zh` 等），并会直接改写 `#start` 的 textContent 和 `#drop-text` 的 innerHTML——改版 HTML/CSS 时必须保留这些钩子。
- style.css 顶部有 `[hidden] { display: none !important; }` 守卫：因为若类选择器设了 `display:flex` 会压过 HTML `hidden` 属性，删掉它会导致烧录选项在「边看边译」页也显示。
- 全屏方案：自定义按钮把整个 `.video-wrap` 容器全屏（DOM 字幕层随之进入）；另挂一条原生 TextTrack 作为 `<video>` 单独被全屏时的兜底。
- UI 为「影院暗房风」（暖黑 + 琥珀 + Fraunces/IBM Plex Mono，CSS 变量定义在 `:root`）。前端改版须遵循 `.claude/skills/frontend-design/SKILL.md` 技能的设计准则。
