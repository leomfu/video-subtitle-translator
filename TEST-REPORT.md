# v2 验收自测报告

- 日期：2026-07-12
- 环境：macOS (Apple Silicon)，Python 3.14.6（venv），系统 ffmpeg 8.x（无 libass）
- 已安装：`ffmpeg-full`（带 libass，`/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`）、`argostranslate 1.11.0` + en→zh 语言包、Whisper small（int8）
- 测试视频：`.../scratchpad/test_video.mp4`（22 秒英语合成语音，4 句）

前置修复确认：
- `brew install ffmpeg-full` 安装成功；`ffmpeg-full -filters` 含 `ass` / `subtitles`（libass）滤镜。
- `pipeline.burn_ffmpeg()` 自动选中 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`。
- `./venv/bin/python setup_local_model.py` 成功安装 en→zh 语言包，自测：`'Hello, how are you today?' -> '你好,你好吗?'`。

---

## 1. 服务启动 + 页面正常  ✅ 通过

命令：
```
./venv/bin/python app.py
curl -s -o /dev/null -w "HTTP %{http_code}" http://127.0.0.1:5001/
```
输出：`HTTP 200`。页面含全部 v2 元素（`data-tab="realtime"`、`name="engine"`、`id="key-row"`、`id="player"`、`id="sub-overlay"`）；`/static/style.css`、`/static/main.js` 均 HTTP 200。

## 2. 烧录模式（en，无需 key）产出带字幕视频  ✅ 通过

命令（完整 HTTP 流程）：
```
curl -F "video=@test_video.mp4" -F "engine=local" -F "mode=en" .../upload
# 轮询 /progress/<id> 直到 done
curl -o dl.mp4 .../download/<id>/video
```
输出：进度依次 extract→transcribe→subtitle→burn，`status=done`，`message="完成！检测到语言：en"`；
下载 `video download HTTP 200, size=660395`。抽帧（第 3 秒）确认画面底部有英文字幕
“Today we are going to learn how to make the perfect cup of coffee.”（白字黑边）。

## 3. 烧录模式双语（本地引擎，英上中下）  ✅ 通过

命令：`process_video(test_video, 'both', ..., engine='local')`（本地 argostranslate 翻译）。
输出：识别 4 句→本地翻译 4/4→烧录完成。抽帧（第 4 秒）同屏出现：
- 上：`Today we are going to learn how to make the perfect cup of coffee.`
- 下：`今天,我们将学习如何制造完美的咖啡。`
两行均白字黑边，英文在上、中文在下，符合要求。

## 4. 边看边译：SSE 逐段推送 en+zh + 视频流支持 Range  ✅ 通过

命令：
```
curl -F "video=@test_video.mp4" -F "engine=local" .../upload_realtime
curl -N .../stream/<id>
curl -D - -o /dev/null -H "Range: bytes=0-100" .../video/<id>
```
SSE 输出（节选，逐条推送）：
```
data: {"start":0.0,"end":2.88,"en":"Hello everyone, and welcome back to the channel.","zh":"大家好 欢迎回到频道","index":0}
data: {"start":2.88,"end":6.56,"en":"Today we are going...","zh":"今天,我们将学习如何制造完美的咖啡。","index":1}
data: {"start":6.56,"end":11.64,"en":"First, you need to grind...","zh":"首先,你需要磨碎豆子新鲜,然后把水加热到90度左右.","index":2}
data: {"start":11.64,"end":14.28,"en":"Finally, pour slowly...","zh":"最后,慢慢倒 并享受你的饮料。","index":3}
event: done
data: {"count":4,"srt":"/download/<id>/srt","language":"en"}
```
Range 请求输出：
```
HTTP/1.1 206 PARTIAL CONTENT
Accept-Ranges: bytes
Content-Range: bytes 0-100/169285
Content-Length: 101
```
两项均满足：SSE 逐段吐出含 en+zh 的 JSON；`/video/<id>` 对 Range 返回 206。

## 5. 本地翻译引擎（不配任何 key）  ✅ 通过

命令：`translate_segments(['Hello everyone'], engine='local')` / 直接调用本地引擎。
输出示例：
```
'Hello everyone, and welcome back to the channel.' -> '大家好 欢迎回到频道'
'Today we are going to learn how to make the perfect cup of coffee.' -> '今天,我们将学习如何制造完美的咖啡。'
```
完全离线、无需 key，译文质量可接受。

## 6. 网页 API key：DeepSeek 引擎优先用请求 key（假 key 返回 401 中文错误）  ✅ 通过

前端：选择 DeepSeek 引擎时出现密码框 `#api-key`（值存于 localStorage，随表单发送）。
命令：`curl -F engine=deepseek -F api_key=sk-faketestkey123 -F mode=zh .../upload`，轮询进度。
输出：`status=error`，`error="DeepSeek API key 无效（401），请检查填写的 key 是否正确。"`
（是 401 相关的中文错误，而非“未配置 key”，说明请求 key 被优先采用并发给了 DeepSeek。）

## 7. 错误处理：无 key 选 DeepSeek、非视频文件  ✅ 通过

- 非视频（.txt）上传（烧录 & 边看边译）：HTTP 400，
  `"不支持的文件格式 .txt，请上传视频文件（mp4/mov/mkv 等）"`。
- DeepSeek 引擎且无任何 key（烧录 zh & 边看边译）：HTTP 400，
  `"选择了 DeepSeek 引擎但未填写 API key。请填写 key，或改用「本地模型（免费）」。"`。
- 烧录不可用时（ffmpeg 缺 libass）：`/upload` 直接返回清晰中文提示 `BURN_UNAVAILABLE_MSG`
  （当前环境已装 ffmpeg-full，故正常烧录；降级提示路径已在代码中实现并可触发）。

## 8. 代码质量 / 无阻塞 / README 更新  ✅ 通过

- 所有耗时处理都在后台线程：烧录用 `_burn_worker` 线程，边看边译用 `_realtime_worker` 线程，
  主线程/请求不被阻塞；SSE 用生成器在请求线程内轮询任务状态（`threaded=True`）。
- 模型均单例：Whisper（`pipeline._model`）、本地翻译（`local_engine._translation`，带锁），
  避免 8GB 内存重复加载。
- `translator.py` 已重构为统一接口 `translate_segments(texts, engine, api_key, progress_cb)`。
- 实时模式按小批次（每 5 段）翻译，烧录模式维持大批次（DeepSeek 20/批）。
- `README.md` 已更新 v2 用法（两种模式、两种引擎、setup 步骤、ffmpeg-full 说明）。
- 纯 Flask + 原生 JS，未引入 Node/前端构建工具。

---

## 返工修复复测（响应审核窗口 ACCEPTANCE.md，2026-07-12）

### 修复 1（重要）：SSE 重连导致字幕重复  ✅ 已修复并复测

改动：
- `app.py /stream`：每条事件输出 `id: <segment.index>`；读取 `Last-Event-ID` 请求头，
  重连时从其后继续推送；segments 快照改为在 `tasks_lock` 内 `list(...)` 拷贝。
- `static/main.js`：`onmessage` 按 `index` 去重兜底（`segments.some(s => s.index === seg.index)`）。

复测（task_id=45d48fcc9712，重启服务后新代码）：
```
$ curl -N /stream/<id>                      # 首次连接
id: 0
data: {"start":0.0,...,"index":0}
id: 1 ... id: 2 ... id: 3 ...               # 4 条均带 id:
event: done

$ curl -H "Last-Event-ID: 2" /stream/<id>   # 模拟浏览器断线重连
: connected
id: 3
data: {"start":11.64,"end":14.28,"en":"Finally, pour slowly...","zh":"最后,慢慢倒 并享受你的饮料。","index":3}
event: done                                  # 只补发 index 3，不再从头重发

$ curl -H "Last-Event-ID: 3" /stream/<id>   # 已收完
event: done                                  # 只有 done

$ curl -H "Last-Event-ID: abc" /stream/<id> # 非法值防御
（回退从头推送，4 条 id: 全量）
```

### 修复 2（重要）：烧录失败泄漏 .wav  ✅ 已修复并复测

改动：`pipeline.process_video` 主体包入 `try/finally`，无论成功失败都删除临时音频
（与实时模式 `_realtime_worker` 的 finally 对齐）；审核遗留的
`outputs/4685eb055748_test_video.wav` 已删除。

复测：
```
# 失败路径：烧录 zh + 假 key（翻译阶段 401 失败）
$ curl -F engine=deepseek -F api_key=sk-fake-rework-test -F mode=zh /upload
task_id=31d1e999ff01 → status=error（"DeepSeek API key 无效（401）…"）
$ ls outputs/ | grep 31d1e999ff01   → （该任务无任何遗留文件）
$ ls outputs/*.wav                  → outputs/ 无任何 .wav

# 成功路径回归：烧录 en 本地引擎
status=done，video download HTTP 200, size=660395，outputs/ 同样无 .wav
```

### 顺手修复（建议级 #5）：前端非 JSON 响应卡 UI  ✅ 已修复

改动：`static/main.js` 新增 `safeJson()`，`startBurn/startRealtime` 的响应解析改为
`await safeJson(resp)`，非 JSON（如 413 HTML 页）时返回 null 并显示中文错误
「服务返回异常（HTTP xxx），文件可能过大或服务出错。」，按钮恢复可用，不再卡在「正在上传视频…」。

其余建议级问题（#3 过期清理、#4 坏视频 stderr 文案、#6 非英语源烧录、#7 快照严谨性——
其中 #7 已随修复 1 顺带解决：SSE 读取改为锁内快照）按审核意见暂不动。

服务已于修复后重启（新 PID，端口 5001），以上复测均在新进程上进行。

---

## 结论

8 条验收标准全部通过，审核提出的 2 个重要问题已修复并复测通过。烧录问题已通过 ffmpeg-full 修复；本地免费翻译引擎（argostranslate）
与网页填写 DeepSeek key 两条路径均可用；边看边译（SSE + Range 视频流 + 前端字幕叠加）已实现并验证。

### 备注
- Python 3.14 下 argostranslate 可正常安装，但会连带拉入 torch/stanza（约 100MB+），首次安装耗时较长。
- 需在“安装完本地语言包之后”再启动 app.py：argostranslate 会缓存已安装语言列表，
  先启动后装包会导致该进程读不到语言包（重启服务即可）。README 的步骤顺序已据此编排。
