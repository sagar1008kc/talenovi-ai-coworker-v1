"""Word-timed captions from ElevenLabs character alignment (SRT sidecar + styled ASS burn-in)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from studio.models import RenderProfile


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def words_from_alignment(alignment: dict, offset: float = 0.0) -> list[Word]:
    """Group per-character timings into words (whitespace separated)."""
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    if not (len(chars) == len(starts) == len(ends)):
        raise ValueError("Alignment arrays have different lengths")
    words: list[Word] = []
    buf, w_start, w_end = "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(Word(buf, w_start + offset, w_end + offset))
            buf, w_start, w_end = "", None, None
            continue
        if w_start is None:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        words.append(Word(buf, w_start + offset, w_end + offset))
    return words


def char_offset_times(alignment: dict, char_index: int) -> float:
    """Start time of the character at char_index (clamped)."""
    starts = alignment["character_start_times_seconds"]
    if not starts:
        return 0.0
    return float(starts[min(max(char_index, 0), len(starts) - 1)])


def build_cues(words: list[Word], max_words: int, max_chars: int = 42, max_gap: float = 0.6) -> list[Cue]:
    cues: list[Cue] = []
    group: list[Word] = []

    def flush() -> None:
        if group:
            cues.append(Cue(group[0].start, group[-1].end, " ".join(w.text for w in group)))
            group.clear()

    for word in words:
        too_long = len(" ".join(w.text for w in group + [word])) > max_chars
        gap = group and word.start - group[-1].end > max_gap
        sentence_end = group and group[-1].text[-1:] in ".!?"
        if group and (len(group) >= max_words or too_long or gap or sentence_end):
            flush()
        group.append(word)
    flush()
    # Close tiny gaps so text does not flicker; keep cues non-overlapping.
    fixed: list[Cue] = []
    for i, cue in enumerate(cues):
        end = cue.end
        if i + 1 < len(cues) and 0 < cues[i + 1].start - end < 0.25:
            end = cues[i + 1].start
        end = max(end, cue.start + 0.2)
        if i + 1 < len(cues):
            end = max(cue.start + 0.05, min(end, cues[i + 1].start))   # a short cue never overlaps the next
        fixed.append(Cue(cue.start, end, cue.text))
    return fixed


def _srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{milli:03}"


def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{c:02}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    lines = []
    for i, cue in enumerate(cues, 1):
        lines += [str(i), f"{_srt_time(cue.start)} --> {_srt_time(cue.end)}", cue.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass(cues: list[Cue], path: Path, profile: RenderProfile) -> Path:
    """Mobile-friendly styling: big bold words, thick outline, kept above platform UI zones."""
    vertical = profile.height > profile.width
    font_size = round(profile.width * (0.075 if vertical else 0.034))
    margin_v = round(profile.height * (0.30 if vertical else 0.07))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {profile.width}\nPlayResY: {profile.height}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(2, font_size // 14)},1,2,60,60,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for cue in cues:
        text = cue.text.replace("\\", "").replace("{", "(").replace("}", ")")
        if vertical:
            text = text.upper()
        events.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def parse_srt(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = (part.strip() for part in lines[1].split("-->"))

        def secs(stamp: str) -> float:
            hms, ms = stamp.split(",")
            h, m, s = (int(x) for x in hms.split(":"))
            return h * 3600 + m * 60 + s + int(ms) / 1000

        cues.append(Cue(secs(a), secs(b), " ".join(lines[2:])))
    return cues
