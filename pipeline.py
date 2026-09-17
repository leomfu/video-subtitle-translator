"""核心流水线：抽音频 → Whisper 识别 → 翻译 → 生成字幕 → ffmpeg 烧录。

烧录需要带 libass 的 ffmpeg。系统 homebrew 的精简版 ffmpeg 没有 ass 滤镜，
所以烧录优先使用 ffmpeg-full（keg-only，二进制在 /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg）。
抽音频等操作用系统 ffmpeg 即可。
"""
import os
import subprocess

from subtitle import write_ass, write_srt
from translator import translate_segments

WHISPER_MODEL = "small"  # int8 量化约 500MB 内存，适合 8GB M1

FFMPEG_FULL = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


class PipelineError(Exception):
    pass


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f"命令失败：{' '.join(cmd)}\n{proc.stderr[-800:]}")
    return proc


# ============ ffmpeg（烧录）能力检测 ============

_burn_ffmpeg = None  # 缓存：能用于烧录的 ffmpeg 路径；"" 表示不可用


def _has_ass_filter(ffmpeg_bin):
    try:
        out = subprocess.run([ffmpeg_bin, "-hide_banner", "-filters"],
                             capture_output=True, text=True).stdout
    except Exception:
        return False
    return any(f" {name} " in out for name in ("ass", "subtitles"))


def burn_ffmpeg():
    """返回可用于烧录（带 libass）的 ffmpeg 路径；找不到返回 None。"""
    global _burn_ffmpeg
    if _burn_ffmpeg is None:
        if os.path.exists(FFMPEG_FULL) and _has_ass_filter(FFMPEG_FULL):
            _burn_ffmpeg = FFMPEG_FULL
        elif _has_ass_filter("ffmpeg"):
            _burn_ffmpeg = "ffmpeg"
        else:
            _burn_ffmpeg = ""
    return _burn_ffmpeg or None


BURN_UNAVAILABLE_MSG = (
    "烧录功能不可用：当前 ffmpeg 缺少 libass（ass 字幕滤镜）。"
    "请运行 `brew install ffmpeg-full` 安装带 libass 的版本后重试；"
    "在此之前，你仍可使用「边看边译」模式实时看字幕，或下载 .srt 字幕文件自行加载。"
)


def extract_audio(video_path, audio_path):
    _run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", audio_path])
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
        raise PipelineError("视频里似乎没有声音轨道，无法识别字幕。")


def transcribe_stream(audio_path, progress_cb=None):
    """流式识别：逐段 yield {start, end, en}，并额外附带 info。

    最后一次 yield 前不含 info；调用方可用 .language / .duration。
    这里用生成器把每段尽快交给调用方（边看边译用）。
    """
    model = _get_model()
    seg_iter, info = model.transcribe(
        audio_path, language=None, vad_filter=True, beam_size=5,
    )
    duration = info.duration or 1
    yielded = False
    for seg in seg_iter:
        text = seg.text.strip()
        if progress_cb:
            progress_cb(min(seg.end / duration, 1.0))
        if text:
            yielded = True
            yield {"start": seg.start, "end": seg.end, "en": text, "language": info.language}
    if not yielded:
        raise PipelineError("没有识别到任何语音内容，请确认视频里有人声。")


def transcribe(audio_path, progress_cb=None):
    """返回 (segments, detected_language)。segments: [{start, end, en}]"""
    segments = []
    lang = None
    for seg in transcribe_stream(audio_path, progress_cb):
        lang = seg.pop("language")
        segments.append(seg)
    return segments, lang


def _filter_escape(path):
    # ffmpeg 滤镜参数里 \ : ' , [ ] 是特殊字符，需要转义（不经过 shell，无需外层引号）
    for ch in ("\\", ":", "'", ",", "[", "]"):
        path = path.replace(ch, "\\" + ch)
    return path


def burn_in(video_path, ass_path, out_path):
    ff = burn_ffmpeg()
    if not ff:
        raise PipelineError(BURN_UNAVAILABLE_MSG)
    vf = f"ass={_filter_escape(os.path.abspath(ass_path))}"
    cmd = [
        ff, "-y", "-i", video_path,
        "-vf", vf,
        "-c:v", "h264_videotoolbox", "-b:v", "5M",  # M1 硬件编码，速度快
        "-c:a", "copy", out_path,
    ]
    try:
        _run(cmd)
    except PipelineError:
        # 个别源（如奇数分辨率/特殊像素格式）硬件编码会失败，回退软件编码
        cmd_sw = [
            ff, "-y", "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264", "-crf", "20", "-preset", "fast",
            "-c:a", "copy", out_path,
        ]
        _run(cmd_sw)


def process_video(video_path, mode, work_dir, report, api_key=None):
    """完整烧录流水线。mode: zh / en / both。

    report(stage, percent, message)：进度回调。
    api_key：DeepSeek API key。
    返回 {video, srt, language}（输出文件路径）。
    """
    # 先检查烧录能力，避免识别翻译半天后才发现烧不了
    if not burn_ffmpeg():
        raise PipelineError(BURN_UNAVAILABLE_MSG)

    base = os.path.splitext(os.path.basename(video_path))[0]
    audio_path = os.path.join(work_dir, f"{base}.wav")
    ass_path = os.path.join(work_dir, f"{base}.ass")
    srt_path = os.path.join(work_dir, f"{base}.srt")
    out_path = os.path.join(work_dir, f"{base}_subtitled.mp4")

    try:
        report("extract", 0, "正在提取音频…")
        extract_audio(video_path, audio_path)

        report("transcribe", 0, "正在识别语音（首次运行需下载模型，约 460MB）…")
        segments, lang = transcribe(
            audio_path,
            progress_cb=lambda p: report("transcribe", p * 100, f"正在识别语音… {p * 100:.0f}%"),
        )

        if mode in ("zh", "both") and lang != "zh":
            report("translate", 0, "正在翻译成中文…")
            texts = [s["en"] for s in segments]
            zh_list = translate_segments(
                texts, api_key=api_key,
                progress_cb=lambda done, total: report(
                    "translate", done / total * 100, f"正在翻译… {done}/{total} 句"),
            )
            for seg, zh in zip(segments, zh_list):
                seg["zh"] = zh
        else:
            # 纯英文模式不需要翻译；源语言本身是中文时原文即中文
            for seg in segments:
                seg["zh"] = seg["en"]

        report("subtitle", 100, "正在生成字幕文件…")
        write_ass(segments, mode, ass_path)
        write_srt(segments, mode, srt_path)

        report("burn", 0, "正在把字幕烧录进视频…")
        burn_in(video_path, ass_path, out_path)
    finally:
        # 无论成功失败都清掉临时音频，失败时不留大体积 .wav
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass

    return {"video": out_path, "srt": srt_path, "language": lang}
