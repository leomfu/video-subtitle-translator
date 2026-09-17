# 视频加字幕工具 v2 升级规格（施工与验收依据）

## 现状（v1 已完成部分）

- Flask 后端 `app.py`（端口 5001）+ 前端 `templates/index.html`、`static/`
- `pipeline.py`：ffmpeg 抽音频 → faster-whisper(small, int8) 识别 → 翻译 → ASS/SRT → ffmpeg 烧录
- `translator.py`：DeepSeek API 批量翻译（key 目前只从 .env 读取）
- `subtitle.py`：三种模式（zh / en / both）的 SRT + ASS 生成
- venv 在 `./venv/`（Python 3.14，已装 flask、faster-whisper、requests、python-dotenv）
- 测试视频：`/private/tmp/claude-501/-Users-fuweiliang-claude-F-English----/9a2906a9-18f9-4eb0-b1d3-3777620a2cec/scratchpad/test_video.mp4`（22 秒英语合成语音）

### 已知问题（必须修复）

1. **烧录功能不可用**：系统 ffmpeg（homebrew `ffmpeg` 8.1.2）是精简编译，**没有 libass / ass / subtitles 滤镜**。
   解决方案：`brew install ffmpeg-full`（有 bottle，keg-only，装好后二进制在
   `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`）。pipeline 烧录处应优先用 ffmpeg-full 的路径，
   抽音频等其他操作可继续用系统 ffmpeg。若安装被拒绝/失败，烧录功能报清晰的中文错误提示，其余功能不受影响。
   注意：之前一次 brew install 尝试被用户中断过，如权限再次被拒，采用降级方案并在结果中说明。

## v2 新增需求

### A. 边看边译（实时字幕模式）

用户上传视频后**立即可以在网页里播放**，字幕在后台边生成边推送，像看电影一样实时叠加显示：

- 新增页面标签/切换：「烧录模式」（v1 功能）和「边看边译」两种模式
- 边看边译流程：上传视频 → 立即返回可播放地址（`<video>` 标签直接播原文件，Flask 提供支持 Range 请求的视频流端点）→ 后台启动识别+翻译 → 通过 **SSE**（`/stream/<task_id>`）把字幕条目（start/end/en/zh）逐段推送到前端
- 前端用 JS 在视频上方叠加字幕层（绝对定位在 video 容器底部），根据 `video.currentTime` 匹配显示当前字幕；双语模式英上中下，样式接近电影字幕（白字黑边）
- 字幕模式（中文/English/双语）在播放时可随时切换，不用重新处理
- 若播放进度超过已生成的字幕（处理没跟上），字幕区显示「字幕生成中…」小提示
- 处理完成后，页面提供「下载 .srt」按钮；也可以一键转入烧录（可选，非必须）

### B. 翻译引擎可选 + 网页输入 API key

- 前端加「翻译引擎」选择区，每次使用时选择：
  1. **本地模型（免费，推荐默认）**：完全离线翻译。用 `argostranslate`（en→zh 包）实现；
     在 venv 安装 `argostranslate` 并预下载 en→zh 语言包。如 Python 3.14 装不上 argostranslate，
     备选 `ctranslate2 + Helsinki-NLP/opus-mt-en-zh`（ct2 已随 faster-whisper 装好）。
  2. **DeepSeek API**：网页上直接输入 API key（密码框），保存在浏览器 localStorage，
     每次请求随表单发给后端，后端优先用请求里的 key，其次回退 .env。
- 两种模式（烧录、边看边译）都遵循这个引擎选择
- key 不落盘到服务器端文件；页面上显示当前引擎状态

### C. 架构调整

- `translator.py` 重构为统一接口：`translate_segments(texts, engine, api_key, progress_cb)`，
  engine ∈ {"local", "deepseek"}
- 实时模式的翻译按**小批次**（如每 5 段）进行，保证字幕尽快可用；烧录模式维持大批次
- Whisper 模型全局单例保持（避免 8GB 内存重复加载）；实时模式识别时用
  `condition_on_previous_text=False` 可选，保持默认即可

## 验收标准（审核窗口按此逐条验收）

1. `./venv/bin/python app.py` 能启动，浏览器打开 http://127.0.0.1:5001 页面正常
2. **烧录模式**：上传测试视频（en 模式，无需 key）→ 产出 `_subtitled.mp4`，抽帧确认画面上有字幕
3. **烧录模式双语**：本地引擎，产出的视频帧上同屏出现英文（上）和中文（下）
4. **边看边译**：上传测试视频后 `/stream/<task_id>` SSE 能逐段吐出含 en+zh 的 JSON 事件；
   视频流端点支持 Range（`curl -H "Range: bytes=0-100"` 返回 206）
5. **本地翻译引擎**：不配任何 API key，本地引擎能把英文段翻成中文（质量能接受即可）
6. **网页 API key**：DeepSeek 引擎时前端有 key 输入框；后端在请求带 key 时优先使用（可用假 key 验证：应返回 401 相关的中文错误，而不是"未配置 key"）
7. 错误处理：无 key 选 DeepSeek、非视频文件上传，均有清晰中文提示
8. 代码质量：无明显 bug、无阻塞主线程的长操作（处理都在后台线程）、`README.md` 已更新 v2 用法

## 施工注意

- 8GB 内存：不要同时加载多个大模型；argos/opus-mt 模型加载也做全局单例
- 所有面向用户的文案用中文
- 不要引入 Node/前端构建工具，保持纯 Flask + 原生 JS
- 完成后运行完整端到端测试并把结果写进 `TEST-REPORT.md`
