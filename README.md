# 🎬 视频自动加字幕工具 v2

上传视频 → 本地 Whisper 识别语音（音频不上传）→ 翻译成中文 → 实时叠字幕或烧录进画面。

**两种使用模式：**

- 👀 **边看边译**：上传后立即在网页里播放，字幕在后台边生成边推送，像看电影一样实时叠加；播放时可随时切换 双语 / 中文 / English 显示，不用重新处理。
- 🔥 **烧录模式**：把字幕永久烧进画面，产出 `_subtitled.mp4`，另附独立 `.srt`。支持 双语（英上中下）/ 仅中文 / 仅 English。

**翻译使用 DeepSeek API**：直接在网页上填入 API key（保存在浏览器本地，不落盘到服务器）。

## 使用前准备（只需一次）

1. **安装依赖**（虚拟环境 `venv/` 已装好大部分）：
   ```bash
   ./venv/bin/python -m pip install -r requirements.txt
   ```

2. **烧录功能需要带 libass 的 ffmpeg**。系统自带的精简版 ffmpeg 没有字幕滤镜，请安装：
   ```bash
   brew install ffmpeg-full
   ```
   安装后二进制在 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`，程序会自动优先使用它烧录。
   若未安装，「边看边译」和 `.srt` 下载仍可正常使用，仅烧录不可用。

3. **DeepSeek key**：可在网页上直接填写；也可写进 `.env` 的 `DEEPSEEK_API_KEY=` 作为默认值。
   key 到 https://platform.deepseek.com 申请。

4. 首次处理视频时会自动下载 Whisper 识别模型（约 460MB），之后离线可用。

## 启动

```bash
./venv/bin/python app.py
```

浏览器打开 **http://127.0.0.1:5001**：

- **边看边译**：填 key → 拖入视频 → 点「开始播放并翻译」→ 视频立即播放，字幕陆续叠上；用下方按钮切换显示语言；完成后可下载 `.srt`。
- **烧录模式**：填 key → 拖入视频 → 选字幕模式 → 点「开始烧录」→ 完成后下载带字幕视频和 `.srt`。

## 给朋友用（部署到服务器）

在 `.env` 里设置 `ACCESS_PASSWORD` 后，程序进入共享模式：

- 打开网页要输入密码（用户名随便填）。
- 不再使用服务器 `.env` 里的 DeepSeek key，每个朋友在网页上填自己的 key。
- 同一时刻只处理一个视频，其余排队，页面上会显示“排队中，前面还有 N 个任务”；排队的任务超过 `MAX_QUEUE`（默认 5）时拒绝新上传。
- 任务完成 24 小时后自动删除它的视频和字幕（`FILE_TTL_HOURS`）。只删本次运行产生的文件；服务重启前的旧文件需要手动清理。

所有可配置项见 `.env.example`。

### 部署步骤（以 Ubuntu 为例，建议至少 2 核 4GB 内存）

```bash
sudo apt install -y python3-venv ffmpeg nginx   # apt 的 ffmpeg 自带 libass，可烧录
git clone https://github.com/leomfu/video-subtitle-translator.git
cd video-subtitle-translator
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
cp .env.example .env    # 编辑 .env，设置 ACCESS_PASSWORD

# 必须 -w 1：任务状态存在进程内存里，多进程会找不到任务
./venv/bin/gunicorn -w 1 --threads 16 --timeout 0 -b 127.0.0.1:5001 app:app
```

用 nginx 对外提供访问，并务必配置 HTTPS（例如用 certbot），否则密码会明文传输。nginx 里需要这几项：

```nginx
location / {
    proxy_pass http://127.0.0.1:5001;
    client_max_body_size 4g;      # 允许上传大视频
    proxy_buffering off;          # 边看边译的字幕推送（SSE）不能被缓冲
    proxy_read_timeout 3600s;
}
```

注意：

- 服务器上没有 GPU 时用 CPU 识别，长视频会比较慢。
- 首次处理视频会自动下载约 460MB 的识别模型，服务器需要能访问外网。
- 重启服务会丢失进行中的任务。

## 说明

- 语音识别在本地进行，视频/音频不会上传到任何服务器；仅识别出的文字会发给 DeepSeek 做翻译。
- 8GB 内存的 M1 上，模型都做了单例加载，避免重复占用内存。
- 支持 mp4 / mov / mkv / avi / webm / m4v / flv / ts 等格式。
- 纯 Flask + 原生 JS，无需 Node/前端构建工具。

## 文件结构

- `app.py`：Flask 服务（上传、进度轮询、SSE 字幕推送 `/stream`、支持 Range 的视频流 `/video`）
- `pipeline.py`：抽音频 → Whisper 识别（含流式 `transcribe_stream`）→ 翻译 → 字幕 → 烧录
- `translator.py`：DeepSeek 翻译接口 `translate_segments(texts, api_key, progress_cb)`
- `subtitle.py`：生成 SRT / ASS 字幕
- `templates/`、`static/`：前端页面
