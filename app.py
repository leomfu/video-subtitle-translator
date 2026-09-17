"""本地网页服务。

两种模式：
  - 烧录模式：上传 → 后台识别/翻译/烧录 → 轮询进度 → 下载带字幕视频。
  - 边看边译：上传后立即播放，字幕在后台边生成边通过 SSE 推送，前端实时叠加。

翻译使用 DeepSeek API（网页填 key，或回退 .env 里的 key）。
"""
import json
import os
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

load_dotenv()

from pipeline import (  # noqa: E402（需先 load_dotenv）
    PipelineError, burn_ffmpeg, extract_audio, process_video,
    transcribe_stream, write_srt,
)
from translator import TranslationError, translate_segments  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts"}
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".flv": "video/x-flv", ".ts": "video/mp2t"}
REALTIME_BATCH = 5  # 边看边译：每识别到几段就翻一次，尽快出字幕

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4GB

tasks = {}  # task_id -> dict
tasks_lock = threading.Lock()


def _update(task_id, **kw):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kw)


def _get(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
        return dict(t) if t else None


# ============ 烧录模式 ============

def _burn_worker(task_id, video_path, mode, api_key):
    def report(stage, percent, message):
        _update(task_id, stage=stage, percent=round(percent, 1), message=message)

    try:
        result = process_video(video_path, mode, OUTPUT_DIR, report, api_key)
        _update(task_id, status="done", percent=100,
                message=f"完成！检测到语言：{result['language']}", result=result)
    except (PipelineError, TranslationError) as e:
        _update(task_id, status="error", error=str(e))
    except Exception as e:
        _update(task_id, status="error", error=f"处理出错：{e}")


# ============ 边看边译模式 ============

def _realtime_worker(task_id, video_path, api_key):
    base = os.path.splitext(os.path.basename(video_path))[0]
    audio_path = os.path.join(OUTPUT_DIR, f"{base}.wav")
    srt_path = os.path.join(OUTPUT_DIR, f"{base}.srt")
    try:
        extract_audio(video_path, audio_path)

        all_segs = []       # 完整段落（含 zh），用于最终写 srt
        buffer = []         # 待翻译缓冲
        lang = None

        def flush():
            nonlocal buffer
            if not buffer:
                return
            if lang == "zh":
                # 源语言本身就是中文，原文即中文，无需翻译
                zh_list = [s["en"] for s in buffer]
            else:
                zh_list = translate_segments(
                    [s["en"] for s in buffer], api_key=api_key)
            for s, zh in zip(buffer, zh_list):
                s["zh"] = zh
                idx = len(all_segs)
                s["index"] = idx
                all_segs.append(s)
                with tasks_lock:
                    if task_id in tasks:
                        tasks[task_id]["segments"].append(s)
            buffer = []

        for seg in transcribe_stream(audio_path):
            lang = seg.pop("language")
            _update(task_id, language=lang)
            buffer.append({"start": seg["start"], "end": seg["end"], "en": seg["en"]})
            if len(buffer) >= REALTIME_BATCH:
                flush()
        flush()

        # 写一份双语 srt 供下载
        write_srt(all_segs, "both", srt_path)
        _update(task_id, status="done", srt_path=srt_path,
                message=f"字幕全部生成完成（{len(all_segs)} 段）")
    except (PipelineError, TranslationError) as e:
        _update(task_id, status="error", error=str(e))
    except Exception as e:
        _update(task_id, status="error", error=f"处理出错：{e}")
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


# ============ 路由 ============

@app.route("/")
def index():
    return render_template(
        "index.html",
        has_key=bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        burn_ok=bool(burn_ffmpeg()),
    )


def _read_common_form():
    api_key = (request.form.get("api_key") or "").strip() or None
    return api_key


def _save_upload():
    """校验并保存上传文件，返回 (task_id, video_path, ext) 或 (None, error_msg, status)。"""
    file = request.files.get("video")
    if not file or not file.filename:
        return None, "请选择一个视频文件", 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return None, f"不支持的文件格式 {ext}，请上传视频文件（mp4/mov/mkv 等）", 400
    task_id = uuid.uuid4().hex[:12]
    safe_name = secure_filename(file.filename) or f"video{ext}"
    video_path = os.path.join(UPLOAD_DIR, f"{task_id}_{safe_name}")
    file.save(video_path)
    return task_id, video_path, ext


@app.route("/upload", methods=["POST"])
def upload():
    """烧录模式上传。"""
    mode = request.form.get("mode", "both")
    if mode not in ("zh", "en", "both"):
        return jsonify({"error": "无效的字幕模式"}), 400
    api_key = _read_common_form()

    if not burn_ffmpeg():
        from pipeline import BURN_UNAVAILABLE_MSG
        return jsonify({"error": BURN_UNAVAILABLE_MSG}), 400
    # 需要翻译但完全没 key（既没填也没 .env）时，提前给出清晰提示
    if mode != "en" and not api_key \
            and not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return jsonify({"error": "未填写 DeepSeek API key。请在页面上填写 key。"}), 400

    task_id, video_path, ext = _save_upload()
    if task_id is None:
        return jsonify({"error": video_path}), ext

    with tasks_lock:
        tasks[task_id] = {"type": "burn", "stage": "queued", "percent": 0,
                          "message": "已加入队列…", "status": "running"}
    threading.Thread(target=_burn_worker,
                     args=(task_id, video_path, mode, api_key),
                     daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/upload_realtime", methods=["POST"])
def upload_realtime():
    """边看边译上传：保存后立即返回可播放地址，字幕后台生成。"""
    api_key = _read_common_form()
    if not api_key and not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return jsonify({"error": "未填写 DeepSeek API key。请在页面上填写 key。"}), 400

    task_id, video_path, ext = _save_upload()
    if task_id is None:
        return jsonify({"error": video_path}), ext

    with tasks_lock:
        tasks[task_id] = {"type": "realtime", "status": "running", "segments": [],
                          "video_path": video_path, "video_ext": ext,
                          "language": None, "message": "字幕生成中…"}
    threading.Thread(target=_realtime_worker,
                     args=(task_id, video_path, api_key),
                     daemon=True).start()
    return jsonify({"task_id": task_id, "video_url": f"/video/{task_id}"})


@app.route("/video/<task_id>")
def video(task_id):
    """提供上传视频的流式播放，支持 Range 请求（<video> 拖动进度用）。"""
    task = _get(task_id)
    if not task or task.get("type") != "realtime":
        return "视频不存在", 404
    path = task["video_path"]
    if not os.path.exists(path):
        return "视频不存在", 404
    mimetype = MIME.get(task.get("video_ext", ""), "video/mp4")
    return send_file(path, mimetype=mimetype, conditional=True)


@app.route("/stream/<task_id>")
def stream(task_id):
    """SSE：把字幕段逐条推送给前端。事件 data 为 {index,start,end,en,zh}。

    每条事件带 id:（即 segment 的 index）。EventSource 断线自动重连时浏览器
    会带 Last-Event-ID 请求头，这里据此从其后继续推送，避免重发导致字幕重复。
    """
    try:
        start_from = int(request.headers.get("Last-Event-ID", "")) + 1
    except ValueError:
        start_from = 0

    def gen(sent):
        # 首个注释帧，尽快建立连接
        yield ": connected\n\n"
        while True:
            task = _get(task_id)
            if task is None:
                yield f"event: error\ndata: {json.dumps({'error': '任务不存在'})}\n\n"
                return
            with tasks_lock:
                segs = list(tasks.get(task_id, {}).get("segments", ()))
            while sent < len(segs):
                yield (f"id: {segs[sent]['index']}\n"
                       "data: " + json.dumps(segs[sent], ensure_ascii=False) + "\n\n")
                sent += 1
            status = task.get("status")
            if status == "error":
                yield "event: error\ndata: " + json.dumps(
                    {"error": task.get("error", "处理出错")}, ensure_ascii=False) + "\n\n"
                return
            if status == "done" and sent >= len(segs):
                yield "event: done\ndata: " + json.dumps(
                    {"count": len(segs), "srt": f"/download/{task_id}/srt",
                     "language": task.get("language")}, ensure_ascii=False) + "\n\n"
                return
            time.sleep(0.3)

    return Response(gen(start_from), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/progress/<task_id>")
def progress(task_id):
    task = _get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    out = {k: v for k, v in task.items() if k not in ("result", "video_path")}
    if task.get("status") == "done" and task.get("type") == "burn":
        out["files"] = {"video": f"/download/{task_id}/video",
                        "srt": f"/download/{task_id}/srt"}
    return jsonify(out)


@app.route("/download/<task_id>/<kind>")
def download(task_id, kind):
    task = _get(task_id)
    if not task:
        return "文件不存在", 404
    if kind == "srt":
        path = None
        if task.get("type") == "realtime":
            path = task.get("srt_path")
        elif task.get("status") == "done":
            path = (task.get("result") or {}).get("srt")
        if path and os.path.exists(path):
            return send_file(path, as_attachment=True)
        return "字幕还没生成好", 404
    if kind == "video" and task.get("status") == "done" and task.get("type") == "burn":
        return send_file(task["result"]["video"], as_attachment=True)
    return "文件不存在", 404


if __name__ == "__main__":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("\n  打开浏览器访问: http://127.0.0.1:5001\n")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
