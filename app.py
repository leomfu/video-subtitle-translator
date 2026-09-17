"""本地网页服务。

两种模式：
  - 烧录模式：上传 → 后台识别/翻译/烧录 → 轮询进度 → 下载带字幕视频。
  - 边看边译：上传后立即播放，字幕在后台边生成边通过 SSE 推送，前端实时叠加。

翻译使用 DeepSeek API（网页填 key；本地使用时可回退 .env 里的 key）。

共享部署（.env 设了 ACCESS_PASSWORD）：全站需要密码、不回退站长 key、
任务排队串行处理、过期文件自动清理。
"""
import json
import os
import secrets
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
from translator import TranslationError, server_key, translate_segments  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts"}
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".flv": "video/x-flv", ".ts": "video/mp2t"}
REALTIME_BATCH = 5  # 边看边译：每识别到几段就翻一次，尽快出字幕

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()
SHARED = bool(ACCESS_PASSWORD)  # 设了访问密码即视为给朋友用的共享部署
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "5"))  # 排队 + 处理中的任务上限，超出拒绝上传
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4096"))
# 文件和任务记录保留时长（小时）；共享部署默认 24，本地默认 0 = 不清理
FILE_TTL_HOURS = float(os.environ.get("FILE_TTL_HOURS", "24" if SHARED else "0"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

tasks = {}  # task_id -> dict
tasks_lock = threading.Lock()
# Whisper 模型单例不能并发推理，CPU 也有限：同一时刻只处理一个任务，其余排队
job_slot = threading.Semaphore(1)


@app.before_request
def _require_password():
    """共享部署时全站 HTTP Basic 认证（用户名随意）。浏览器会缓存凭据，
    之后的 fetch / EventSource / <video> 同源请求都会自动带上。"""
    if not ACCESS_PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.password and secrets.compare_digest(
            auth.password.encode(), ACCESS_PASSWORD.encode()):
        return None
    return Response("需要访问密码", 401,
                    {"WWW-Authenticate": 'Basic realm="subtitle", charset="UTF-8"'})


def _update(task_id, **kw):
    if kw.get("status") in ("done", "error"):
        kw["finished"] = time.time()
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kw)


def _get(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
        return dict(t) if t else None


def _new_task(task_id, **fields):
    with tasks_lock:
        tasks[task_id] = {"status": "running", "waiting": True,
                          "created": time.time(), **fields}


def _queued(task_id, job, *args):
    """排到处理槽位后再执行 job。"""
    with job_slot:
        _update(task_id, waiting=False)
        job(task_id, *args)


def _queue_message(task_id):
    """任务还在排队时返回提示文字，否则返回 None。"""
    with tasks_lock:
        me = tasks.get(task_id)
        if not me or not me.get("waiting"):
            return None
        ahead = sum(1 for tid, t in tasks.items()
                    if tid != task_id and t["status"] == "running"
                    and (not t["waiting"] or t["created"] < me["created"]))
    return f"排队中，前面还有 {ahead} 个任务…"


def _busy():
    with tasks_lock:
        return sum(1 for t in tasks.values() if t["status"] == "running") >= MAX_QUEUE


def _cleanup():
    """删除过期任务记录及其上传/输出文件。

    只删本进程创建的任务（文件名以 "<task_id>_" 开头），目录里的其他文件一律不碰，
    避免误删以前留下的视频。
    """
    cutoff = time.time() - FILE_TTL_HOURS * 3600
    with tasks_lock:
        expired = [tid for tid, t in tasks.items() if t.get("finished", cutoff) < cutoff]
        for tid in expired:
            del tasks[tid]
    for tid in expired:
        for d in (UPLOAD_DIR, OUTPUT_DIR):
            for name in os.listdir(d):
                if name.startswith(tid + "_"):
                    try:
                        os.remove(os.path.join(d, name))
                    except OSError:
                        pass


def _cleanup_loop():
    while True:
        try:
            _cleanup()
        except Exception as e:
            print(f"清理过期文件出错：{e}")
        time.sleep(600)


if FILE_TTL_HOURS > 0:
    threading.Thread(target=_cleanup_loop, daemon=True).start()


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
        has_key=bool(server_key()),
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
    if mode != "en" and not api_key and not server_key():
        return jsonify({"error": "未填写 DeepSeek API key。请在页面上填写 key。"}), 400
    if _busy():
        return jsonify({"error": "服务器正忙，排队的任务已满，请稍后再试。"}), 429

    task_id, video_path, ext = _save_upload()
    if task_id is None:
        return jsonify({"error": video_path}), ext

    _new_task(task_id, type="burn", stage="queued", percent=0, message="已加入队列…")
    threading.Thread(target=_queued,
                     args=(task_id, _burn_worker, video_path, mode, api_key),
                     daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/upload_realtime", methods=["POST"])
def upload_realtime():
    """边看边译上传：保存后立即返回可播放地址，字幕后台生成。"""
    api_key = _read_common_form()
    if not api_key and not server_key():
        return jsonify({"error": "未填写 DeepSeek API key。请在页面上填写 key。"}), 400
    if _busy():
        return jsonify({"error": "服务器正忙，排队的任务已满，请稍后再试。"}), 429

    task_id, video_path, ext = _save_upload()
    if task_id is None:
        return jsonify({"error": video_path}), ext

    _new_task(task_id, type="realtime", segments=[], video_path=video_path,
              video_ext=ext, language=None, message="字幕生成中…")
    threading.Thread(target=_queued,
                     args=(task_id, _realtime_worker, video_path, api_key),
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
        last_note = None
        while True:
            task = _get(task_id)
            if task is None:
                yield f"event: error\ndata: {json.dumps({'error': '任务不存在'})}\n\n"
                return
            if task.get("status") == "running":
                note = _queue_message(task_id) or "字幕正在后台生成，可以直接开始播放。"
                if note != last_note:
                    last_note = note
                    yield "event: status\ndata: " + json.dumps(
                        {"message": note}, ensure_ascii=False) + "\n\n"
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
    queue_msg = _queue_message(task_id)
    if queue_msg:
        out["message"] = queue_msg
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
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5001"))
    if host not in ("127.0.0.1", "localhost") and not ACCESS_PASSWORD:
        raise SystemExit("对外开放（HOST 不是 127.0.0.1）时，必须在 .env 里设置 ACCESS_PASSWORD。")
    print(f"\n  打开浏览器访问: http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
