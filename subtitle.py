"""生成 SRT / ASS 字幕文件。三种模式：zh（仅中文）、en（仅英文）、both（双语）。"""


def _srt_time(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_time(seconds):
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt(segments, mode, path):
    """segments: [{start, end, en, zh}]；both 模式下英文一行、中文一行。"""
    lines = []
    for i, seg in enumerate(segments, 1):
        if mode == "zh":
            text = seg["zh"]
        elif mode == "en":
            text = seg["en"]
        else:
            text = f"{seg['en']}\n{seg['zh']}"
        lines.append(f"{i}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{text}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ASS 样式说明：
# - 分辨率基准 PlayResY=720，libass 会按实际视频等比缩放，各种分辨率下大小一致
# - 白字、黑色描边 + 轻微阴影，保证亮暗画面都清晰（电影字幕的通用做法）
# - 双语模式：英文用稍小的字号叠在中文上方
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,PingFang SC,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,28,1
Style: EN,Helvetica,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,28,1
Style: EN_TOP,Helvetica,30,&H00E8E8E8,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,76,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def write_ass(segments, mode, path):
    events = []
    for seg in segments:
        start, end = _ass_time(seg["start"]), _ass_time(seg["end"])
        en, zh = _ass_escape(seg["en"]), _ass_escape(seg["zh"])
        if mode == "zh":
            events.append(f"Dialogue: 0,{start},{end},ZH,,0,0,0,,{zh}")
        elif mode == "en":
            events.append(f"Dialogue: 0,{start},{end},EN,,0,0,0,,{en}")
        else:  # both：中文在下（MarginV=28），英文在上（EN_TOP MarginV=76）
            events.append(f"Dialogue: 0,{start},{end},ZH,,0,0,0,,{zh}")
            events.append(f"Dialogue: 0,{start},{end},EN_TOP,,0,0,0,,{en}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER + "\n".join(events) + "\n")
