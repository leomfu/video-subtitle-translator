# v2 独立验收报告（审核窗口）

- 验收日期：2026-07-12
- 验收方式：不采信施工方 TEST-REPORT.md 的结论，全部条目独立实测。
- 环境：服务复用施工方遗留进程（PID 27324，11:57 启动，晚于全部代码最后修改时间 11:28，确认加载的是最新代码），`./venv/bin/python app.py`，端口 5001。
- 测试视频：scratchpad/test_video.mp4（22 秒英语合成语音，4 句）。
- .env 中 `DEEPSEEK_API_KEY=` 为空，确认「无 key」错误路径为真实场景。

## 逐条验收结果

### 1. 服务启动 + 页面正常 —— ✅ 通过

- 命令：`curl -w "%{http_code}" http://127.0.0.1:5001/` 及 `/static/style.css`、`/static/main.js`
- 输出：三者均 HTTP 200；页面含 v2 全部关键元素（`data-tab="realtime"`、`name="engine"`、`id="api-key"`、`id="player"`、`id="sub-overlay"`，grep 命中 6 处）。

### 2. 烧录模式（en，无需 key）—— ✅ 通过

- 命令：`curl -F video=@test_video.mp4 -F engine=local -F mode=en /upload` → 轮询 `/progress/85be3d8e9d95` → `status=done, message="完成！检测到语言：en"` → 下载 `_subtitled.mp4`（HTTP 200，660395 字节）。
- 抽帧亲眼确认（4s、9s 两帧，Read 工具查看图片）：画面底部有白字黑边英文字幕，4s 帧为 "Today we are going to learn how to make the perfect cup of coffee."，9s 帧为两行换行的 "First, you need to grind the beans fresh, / then heat the water to about 90 degrees."。位置、样式符合电影字幕。

### 3. 烧录模式双语（本地引擎，英上中下）—— ✅ 通过

- 命令：`curl -F engine=local -F mode=both /upload`（任务 e003b9f3fbf4），本地 argostranslate 翻译，完成后下载（748903 字节）。
- 抽帧亲眼确认（4s、13s 两帧）：同屏两行，**英文在上**（稍小、略灰白）、**中文在下**（稍大、纯白），均黑边描边，位于画面底部。4s 帧："Today we are going to learn how to make the perfect cup of coffee." / "今天,我们将学习如何制造完美的咖啡。"。SRT 文件内容同步核对无误。

### 4. 边看边译：SSE + Range —— ✅ 通过

- `curl -F engine=local /upload_realtime` → 立即返回 `{"task_id":"44cf0b9f207c","video_url":"/video/44cf0b9f207c"}`。
- Range：`curl -H "Range: bytes=0-100" /video/44cf0b9f207c` → **HTTP/1.1 206 PARTIAL CONTENT**，Content-Length: 101。
- SSE：`curl -N /stream/44cf0b9f207c` 逐条吐出 4 个含 en+zh 的 JSON 事件（index 0–3，start/end/en/zh 完整），最后 `event: done` 带 srt 下载地址和 language=en。

### 5. 本地翻译引擎（不配任何 key）—— ✅ 通过

- 命令：`env -u DEEPSEEK_API_KEY ./venv/bin/python -c "translate_segments([...], engine='local')"`（.env 中 key 也为空）。
- 输出："Hello everyone, and welcome back to the channel." → "大家好 欢迎回到频道"；"First, you need to grind the beans fresh, …" → "首先,你需要磨碎豆子新鲜,然后把水加热到90度左右."。完全离线，质量可接受（个别句式生硬，属本地小模型正常水平）。

### 6. 网页 API key（假 key → 401 中文错误）—— ✅ 通过

- 前端确认：index.html 有密码框 `#api-key`（选 DeepSeek 时显示），main.js 存 localStorage 并随表单发送。
- 命令：`curl -F engine=deepseek -F api_key=sk-fake-acceptance-test-123 -F mode=zh /upload` → 轮询至 `status=error`。
- 输出：`"DeepSeek API key 无效（401），请检查填写的 key 是否正确。"` —— 是 401 相关中文错误而非「未配置 key」，证明请求里的 key 被优先采用并真实发给了 DeepSeek（后端 `_resolve_key` 逻辑也核对：请求 key 优先，其次 .env）。

### 7. 错误处理 —— ✅ 通过

均为清晰中文提示（JSON unicode 转义，前端解析后正常显示）：

- 非视频 .txt 上传（/upload 与 /upload_realtime）：HTTP 400，"不支持的文件格式 .txt，请上传视频文件（mp4/mov/mkv 等）"。
- DeepSeek 无 key（两种模式）：HTTP 400，"选择了 DeepSeek 引擎但未填写 API key。请填写 key，或改用「本地模型（免费）」。"
- 不选文件：HTTP 400，"请选择一个视频文件"。
- 不存在的 task_id：/progress 404 "任务不存在"；/video、/download 404；/stream 发 `event: error` + "任务不存在" 后正常关闭。

### 8. 代码质量 / 无阻塞 / README —— ✅ 通过（附改进项，见下）

- 通读 app.py、pipeline.py、translator.py、local_engine.py、subtitle.py、templates/index.html、static/main.js、static/style.css。
- 耗时操作全部在后台线程（`_burn_worker` / `_realtime_worker`，daemon 线程）；任务状态读写有 `tasks_lock`；Whisper、argos 模型均全局单例（argos 加载带双重检查锁）；`translate_segments(texts, engine, api_key, progress_cb)` 统一接口符合规格；实时小批次（5 段）/ 烧录大批次（20 段）符合规格。
- 全部测试跑完后服务仍健康（GET / 200，进程无异常），无阻塞迹象。
- README.md 已更新 v2 用法（两模式、两引擎、setup_local_model.py、ffmpeg-full 说明），与实际行为一致。
- 纯 Flask + 原生 JS，无 Node 构建工具。

## 边界测试（额外）

| 测试 | 结果 |
|---|---|
| 非视频文件上传（.txt） | 400 + 中文提示，通过 |
| 不存在的 task_id（progress/video/download/stream） | 404 / SSE error 事件，均正确 |
| 坏内容伪装 .mp4 上传 | 任务进入 error，不崩溃；但错误文案是 ffmpeg 原始 stderr（见问题 #4） |
| SSE 断开重连（带 `Last-Event-ID: 2` 重连） | 服务端**忽略 Last-Event-ID、无 id: 字段**，从头重发全部 4 条（见问题 #1） |

## 发现的问题（按严重程度）

**阻断级：无。**

**重要：**

1. **SSE 重连会导致前端字幕数组重复**。`/stream` 不输出 `id:` 字段、不读 `Last-Event-ID`，生成器每次连接都从 `sent=0` 重发全部段；而 main.js `onmessage` 无去重直接 `segments.push(...)`。EventSource 中途断线自动重连后（长视频/机器休眠时很可能发生），`segments` 会成倍重复。实测：带 `Last-Event-ID: 2` 重连仍收到全部 4 条。显示层因 `find()` 取首个匹配仍能工作，故不算阻断；建议服务端输出 `id: <index>` 并按 Last-Event-ID 续传，或前端按 `index` 去重。
2. **烧录流水线失败时泄漏临时 .wav**。`pipeline.process_video` 只在成功路径删除音频（无 try/finally）；实测假 key 失败任务遗留 `outputs/4685eb055748_test_video.wav`。长视频的 16kHz wav 可达数百 MB，多次失败会占满磁盘。实时模式 `_realtime_worker` 有 finally 清理，烧录模式应对齐。

**建议：**

3. `tasks` 字典、`uploads/`、`outputs/` 无任何过期清理，长期运行内存/磁盘持续增长（本地单用户工具可接受，建议加简单清理或说明）。
4. 坏视频文件（合法扩展名、非法内容）的报错直接暴露 ffmpeg 原始 stderr（含完整路径和编译参数），应映射为「无法读取该视频文件，请确认文件未损坏」之类的中文提示。
5. 前端 `startBurn/startRealtime` 中 `await resp.json()` 在响应非 JSON 时（如超过 4GB 触发 Werkzeug 413 HTML 页）抛出未捕获异常，UI 会卡在「正在上传视频…」且不恢复按钮。
6. 非英语源视频行为不一致：实时模式对 `lang != en` 用原文占位（合理），烧录模式 zh/both 则不管检测语言仍喂给所选引擎——本地 argos en→zh 对法语等源语言会输出乱译。建议烧录模式对非 en/zh 源语言同样降级或提示。
7. `_get()` 返回的浅拷贝仍共享 `segments` 列表引用，SSE 读取与 worker 追加依赖 CPython `list.append` 原子性；实践安全但建议在锁内取快照（`list(segs)`）更严谨。

## 最终结论（首轮）

**验收通过。** 8 条验收标准全部独立实测通过，烧录画面（含双语英上中下排布）经抽帧亲眼核实。发现的 2 个重要问题（SSE 重连重复、失败路径 wav 泄漏）均不影响验收标准所覆盖的功能路径，属于健壮性改进项，建议后续修复。

---

## 返工复核（2026-07-12，服务已重启为新 PID 28887）

施工方针对上述重要问题 #1、#2 返工，本节为独立复核结果（全部亲自实测，未采信其复测记录）。复核期间服务器上有用户活动任务（22d2f326d9f8_claude.mp4），全程未触碰、未重启服务、未清理目录；复核结束时该任务仍在正常运行（transcribe 17%）。

### 修复 1：SSE 重连重复 —— ✅ 确认修复

代码审查：`app.py /stream` 每条事件输出 `id: <index>`，从 `Last-Event-ID + 1` 续推（非法值 ValueError 回退 0）；segments 快照改为 `tasks_lock` 内 `list(...)` 拷贝（顺带解决建议 #7）；main.js `onmessage` 按 `index` 去重兜底。实现正确。

实测（新 realtime 任务 52fbe70d8a83）：
- 首次连接：4 条事件均带 `id: 0`–`id: 3`，随后 `event: done`。
- `Last-Event-ID: 2` 重连：**只补发 id: 3 一条** + done（修复前实测为全量重发 4 条）。
- `Last-Event-ID: 3`：只有 done。
- `Last-Event-ID: abc`（非法值防御）：回退全量 4 条，不报错。

残留 nit（建议级）：负数 `Last-Event-ID`（如 -5）未钳位，会以负下标读 segs 列表。浏览器只会回放服务端下发的非负 id，实际不可达，仅手工构造请求可触发且不崩溃；建议 `max(0, ...)` 钳位。

### 修复 2：烧录失败泄漏 .wav —— ✅ 确认修复

代码审查：`pipeline.process_video` 主体已包 `try/finally`，成功/失败均删除临时 .wav，与实时模式对齐。实现正确。首轮遗留的 `4685eb055748_test_video.wav` 已被清除。

实测：
- 失败路径：假 key 烧录 zh（任务 155c2e362677）→ `status=error`（401 中文提示）→ `ls outputs/ | grep 155c2e362677` **无任何遗留文件**。
- 成功路径回归：本地引擎烧录 en（任务 a169452f4cd5）→ done，下载 200（660395 字节），**抽帧亲眼确认**字幕正常（与首轮一致）；outputs/ 只有 .ass/.srt/_subtitled.mp4，**无 .wav**。

### 顺手修复（建议 #5、#7）—— ✅ 代码审查确认

- main.js 新增 `safeJson()`，`startBurn/startRealtime` 非 JSON 响应（如 413 HTML）返回 null 并显示「服务返回异常（HTTP xxx）…」，按钮恢复，不再卡 UI。实现正确（pollBurn 内联 `r.json()` 已有 try/catch 包裹，不受影响）。
- SSE 读取 segments 改为锁内快照，建议 #7 已随修复 1 解决。

### 复核后遗留问题

建议级：#3（无过期清理）、#4（坏视频暴露 ffmpeg stderr）、#6（非英语源烧录行为不一致）、新增 nit（负数 Last-Event-ID 未钳位）。均不影响验收。

## 最终结论

**验收通过。** 8 条验收标准全部实测通过；2 个重要问题已确认修复且成功路径回归无影响；剩余问题均为建议级。

服务状态：复核结束后服务保持运行（PID 28887，http://127.0.0.1:5001），用户活动任务 22d2f326d9f8 未受影响，可直接使用。
