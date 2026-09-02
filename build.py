#!/usr/bin/env python3
"""Turn script.txt plus a few hand-made clips into a finished vertical short.

Stages: script -> voice (edge-tts) -> word timings -> captions.ass ->
concat/fit clips -> burn in captions and mux audio -> out/<timestamp>.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "voice": "en-GB-RyanNeural",
    "rate": "+0%",
    "resolution": "1080x1920",
    "fps": 30,
    "captions": {
        "font": "Inter",
        "size": 90,
        "style": "word-by-word",
        "position": "center",
        "timing": "wordboundary",
        "group_size": 3,
        "color": "#FFFFFF",
        "highlight": "#FFE500",
        "outline": 6,
    },
    "music": {"enabled": False, "volume": 0.08},
}

# Homebrew's core ffmpeg formula is deliberately slim and ships without libass,
# so the caption burn-in filter simply does not exist there. ffmpeg-full has it
# but is keg-only, which means it is never symlinked onto PATH.
FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin")
TAIL_PAD = 0.4          # seconds of video held past the end of the voice
MAX_CAPTION_HOLD = 0.5  # cap on stretching a word across a silent gap
FONT_DIRS = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts")]


def die(msg: str) -> None:
    print(f"\nerror: {msg}", file=sys.stderr)
    sys.exit(1)


def log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


# --------------------------------------------------------------------------- config


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        log("config", f"{path.name} not found, using built-in defaults")
        return dict(DEFAULTS)
    try:
        import yaml
    except ImportError:
        die("PyYAML is not installed. Run: pip install -r requirements.txt")
    with path.open() as fh:
        loaded = yaml.safe_load(fh) or {}
    return deep_merge(DEFAULTS, loaded)


# --------------------------------------------------------------------------- ass helpers


def hex_to_ass(color: str) -> str:
    """#RRGGBB -> &HAABBGGRR&. ASS stores colour byte-reversed with alpha first."""
    h = color.strip().lstrip("#")
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"expected #RRGGBB, got {color!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&".upper()


def ass_time(seconds: float) -> str:
    """Seconds -> H:MM:SS.cc (ASS uses centiseconds)."""
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    h, rem = divmod(centis, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def esc_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph argument."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# --------------------------------------------------------------------------- preflight


def resolve_ffmpeg() -> tuple[str, str]:
    """Find an ffmpeg that actually has libass, or explain how to get one."""
    candidates = []
    if os.environ.get("FFMPEG"):
        candidates.append(Path(os.environ["FFMPEG"]))
    candidates.append(FFMPEG_FULL / "ffmpeg")
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))

    tried = []
    for cand in candidates:
        if not cand.exists():
            continue
        tried.append(str(cand))
        try:
            filters = subprocess.run(
                [str(cand), "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if re.search(r"^\s*\S*\s+ass\s", filters, re.M):
            probe = cand.parent / "ffprobe"
            return str(cand), str(probe if probe.exists() else "ffprobe")

    detail = f"checked: {', '.join(tried)}" if tried else "no ffmpeg binary found"
    die(
        "no ffmpeg with libass support (needed to burn in captions).\n"
        f"  {detail}\n"
        "  Homebrew's core 'ffmpeg' formula is slim and has no libass. Install the full build:\n"
        "      brew install ffmpeg-full\n"
        "  It is keg-only; this script looks for it at /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg.\n"
        "  Or point $FFMPEG at any ffmpeg built with --enable-libass."
    )


def find_font_dir(font_name: str) -> Path | None:
    needle = font_name.replace(" ", "").lower()
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.suffix.lower() in {".ttf", ".otf", ".ttc"} and needle in entry.stem.replace(" ", "").lower():
                return directory
    return None


def run(cmd: list[str], stage: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        die(f"{stage} failed (ffmpeg exit {proc.returncode}):\n{tail}")


def probe_duration(ffprobe: str, path: Path) -> float:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        die(f"could not read duration of {path}")
    return float(proc.stdout.strip())


# --------------------------------------------------------------------------- stages


def read_script(path: Path) -> str:
    if not path.exists():
        die(f"script not found: {path}")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("#")]
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not text:
        die(f"{path} is empty (only comments or blank lines)")
    return text


async def _synth(text: str, voice: str, rate: str, audio_path: Path) -> list[dict]:
    import edge_tts

    words: list[dict] = []
    # edge-tts defaults to SentenceBoundary; word-level highlighting needs the
    # per-word events, which only arrive when boundary is set explicitly.
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, boundary="WordBoundary")
    with audio_path.open("wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # edge-tts reports offsets in 100-nanosecond ticks.
                start = chunk["offset"] / 1e7
                words.append({
                    "text": chunk["text"],
                    "start": start,
                    "end": start + chunk["duration"] / 1e7,
                })
    return words


def synth_voice(text: str, cfg: dict, work: Path) -> tuple[Path, list[dict]]:
    audio_path = work / "voice.mp3"
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        die("edge-tts is not installed. Run: pip install -r requirements.txt")

    try:
        words = asyncio.run(_synth(text, cfg["voice"], cfg["rate"], audio_path))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim below
        if "403" in str(exc):
            die(
                "edge-tts was rejected with HTTP 403.\n"
                "  Microsoft rotates the Sec-MS-GEC token and pinned versions go stale.\n"
                "  Fix: pip install -U edge-tts"
            )
        die(f"edge-tts synthesis failed: {exc}")

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        die("edge-tts produced no audio. Check the voice name with: edge-tts --list-voices")

    (work / "words.json").write_text(json.dumps(words, indent=2), encoding="utf-8")
    return audio_path, words


def transcribe(audio_path: Path) -> list[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper is not installed. Run: pip install -r requirements.txt")
    log("timing", "running faster-whisper (base, int8, CPU)")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True)
    words: list[dict] = []
    for seg in segments:
        for word in (seg.words or []):
            token = word.word.strip()
            if token:
                words.append({"text": token, "start": word.start, "end": word.end})
    return words


def resolve_words(cfg: dict, tts_words: list[dict], audio_path: Path, work: Path) -> list[dict]:
    mode = cfg["captions"].get("timing", "wordboundary")
    if mode == "wordboundary":
        if tts_words:
            log("timing", f"{len(tts_words)} words from edge-tts WordBoundary events")
            return tts_words
        log("timing", "no WordBoundary events from this voice, falling back to faster-whisper")
    words = transcribe(audio_path)
    if not words:
        die("no word timings from either edge-tts or faster-whisper")
    (work / "words.json").write_text(json.dumps(words, indent=2), encoding="utf-8")
    log("timing", f"{len(words)} words from faster-whisper")
    return words


def mark_sentence_ends(words: list[dict], script: str) -> None:
    """Flag words that end a sentence, in place.

    edge-tts strips punctuation from WordBoundary events, so "faster." arrives as
    "faster" and a caption group happily straddles a full stop. The original
    script still has the punctuation, and the synthesiser speaks it in order, so
    walking the two in parallel recovers the breaks. Best-effort: on any mismatch
    the word is simply left unflagged rather than resyncing wrongly.
    """
    def norm(token: str) -> str:
        return re.sub(r"[^\w']", "", token).lower()

    tokens = script.split()
    cursor = 0
    for word in words:
        target = norm(word["text"])
        if not target:
            continue
        # Look a little way ahead: the synthesiser may expand a token (a number,
        # say) into several spoken words.
        for probe in range(cursor, min(cursor + 4, len(tokens))):
            if norm(tokens[probe]) == target:
                word["sentence_end"] = bool(re.search(r"""[.!?\u2026]['"\u201d\u2019]?$""", tokens[probe]))
                cursor = probe + 1
                break


def group_words(words: list[dict], group_size: int, max_chars: int = 24) -> list[list[dict]]:
    """Chunk words into on-screen runs, bounded by count, line width and sentences."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        candidate = current + [word]
        width = sum(len(w["text"]) for w in candidate) + len(candidate) - 1
        if current and (len(candidate) > group_size or width > max_chars):
            groups.append(current)
            current = [word]
        else:
            current = candidate
        # Never let a group run past a full stop into the next sentence.
        if word.get("sentence_end"):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def build_ass(words: list[dict], cfg: dict, width: int, height: int, out_path: Path,
              script: str = "") -> None:
    caps = cfg["captions"]
    if script:
        mark_sentence_ends(words, script)
    primary = hex_to_ass(caps["color"])
    highlight = hex_to_ass(caps["highlight"])
    font, size, outline = caps["font"], int(caps["size"]), int(caps["outline"])

    position = caps.get("position", "center")
    y = {"center": height // 2, "lower-third": int(height * 0.72), "top": int(height * 0.25)}.get(position)
    if y is None:
        die(f"unknown captions.position {position!r} (use center, lower-third or top)")
    pos_tag = rf"{{\an5\pos({width // 2},{y})}}"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{primary},{primary},&H00000000&,&H00000000&,-1,0,0,0,100,100,0,0,1,{outline},0,5,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = []
    flat = 0
    groups = group_words(words, int(caps.get("group_size", 3)))
    plain = caps.get("style") != "word-by-word"

    for group in groups:
        if plain:
            text = pos_tag + " ".join(ass_escape(w["text"]) for w in group)
            lines.append(f"Dialogue: 0,{ass_time(group[0]['start'])},{ass_time(group[-1]['end'])},Main,,0,0,0,,{text}")
            flat += len(group)
            continue

        for idx, word in enumerate(group):
            # Hold each word until the next one starts so inter-word gaps do not
            # flash blank, but never linger through a long silence. The next word
            # is taken from the flat list, not the group: clamping per-group would
            # let a group's last event overlap the next group's first one and draw
            # two lines on top of each other at the same \pos.
            nxt = words[flat + 1]["start"] if flat + 1 < len(words) else None
            end = word["end"] + MAX_CAPTION_HOLD
            end = min(end, nxt) if nxt is not None else min(end, word["end"] + TAIL_PAD)
            end = max(end, word["start"] + 0.02)
            flat += 1

            parts = []
            for j, other in enumerate(group):
                token = ass_escape(other["text"])
                if j == idx:
                    # Explicitly restore instead of \r, which would also drop \an5\pos.
                    parts.append(rf"{{\c{highlight}\fscx112\fscy112}}{token}{{\c{primary}\fscx100\fscy100}}")
                else:
                    parts.append(token)
            lines.append(
                f"Dialogue: 0,{ass_time(word['start'])},{ass_time(end)},Main,,0,0,0,,{pos_tag}{' '.join(parts)}"
            )

    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    log("captions", f"{len(lines)} events across {len(groups)} groups -> {out_path.name}")


def build_video(ffmpeg: str, clips: list[Path], cfg: dict, target: float, work: Path) -> Path:
    width, height = (int(v) for v in cfg["resolution"].lower().split("x"))
    fps = int(cfg["fps"])
    out_path = work / "video.mp4"

    chains = []
    for i in range(len(clips)):
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps}[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(clips)))
    chains.append(f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[cat]")
    # Match the video to the voice, never the other way round: hold the last
    # frame if the clips run short, trim if they run long.
    # Pad generously with a clone of the last frame, then let -t cut back to the
    # target. Padding by the full target is always enough however short the clips
    # are, and costs nothing when they are already long enough.
    chains.append(f"[cat]tpad=stop_mode=clone:stop_duration={target:.3f}[out]")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "[out]", "-an", "-t", f"{target:.3f}",
        "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run(cmd, "clip concat")
    return out_path


def assemble(ffmpeg: str, video: Path, voice: Path, ass_path: Path, cfg: dict,
             music_path: Path | None, total: float, font_dir: Path | None, out_path: Path) -> None:
    ass_arg = f"ass=filename='{esc_filter_path(str(ass_path.relative_to(ROOT)))}'"
    if font_dir:
        ass_arg += f":fontsdir='{esc_filter_path(str(font_dir))}'"

    chains = [f"[0:v]{ass_arg}[v]", "[1:a]apad[voice]"]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(voice)]

    if music_path:
        cmd += ["-i", str(music_path)]
        volume = float(cfg["music"]["volume"])
        chains.append(f"[2:a]aloop=loop=-1:size=2147483647,volume={volume}[music]")
        # normalize=0 keeps amix from halving both inputs.
        chains.append("[voice][music]amix=inputs=2:duration=first:normalize=0[a]")
        audio_out = "[a]"
    else:
        audio_out = "[voice]"

    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "[v]", "-map", audio_out,
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        # edge-tts returns 24 kHz mono; resample so the file matches what
        # social players and editors expect.
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run(cmd, "assemble")


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a vertical short from a script and clips.")
    parser.add_argument("--script", default="script.txt")
    parser.add_argument("--clips", default="clips/")
    parser.add_argument("--music", default="music/")
    parser.add_argument("--out", default="out/")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--voice", help="override config.yaml voice, e.g. en-US-BrianNeural")
    parser.add_argument("--keep-work", action="store_true", help="keep intermediates in work/")
    args = parser.parse_args()

    cfg = load_config(ROOT / args.config)
    if args.voice:
        cfg["voice"] = args.voice

    try:
        width, height = (int(v) for v in cfg["resolution"].lower().split("x"))
    except ValueError:
        die(f"bad resolution {cfg['resolution']!r}, expected WIDTHxHEIGHT")

    ffmpeg, ffprobe = resolve_ffmpeg()
    log("preflight", f"ffmpeg: {ffmpeg}")

    font_dir = find_font_dir(cfg["captions"]["font"])
    if not font_dir:
        print(
            f"warning: font {cfg['captions']['font']!r} not found; libass will silently "
            f"substitute another face.\n         Install it with: brew install --cask font-inter",
            file=sys.stderr,
        )

    clips_dir = ROOT / args.clips
    clips = sorted(p for p in clips_dir.glob("*.mp4") if p.is_file())
    if not clips:
        die(f"no .mp4 clips in {clips_dir}. Drop in 01.mp4, 02.mp4, ... (ordered by filename)")
    log("preflight", f"{len(clips)} clip(s): {', '.join(c.name for c in clips)}")

    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    text = read_script(ROOT / args.script)
    log("script", f"{len(text.split())} words")

    log("voice", f"synthesising with {cfg['voice']} at rate {cfg['rate']}")
    voice_path, tts_words = synth_voice(text, cfg, work)
    voice_dur = probe_duration(ffprobe, voice_path)
    log("voice", f"{voice_dur:.2f}s -> work/voice.mp3")

    words = resolve_words(cfg, tts_words, voice_path, work)

    ass_path = work / "captions.ass"
    build_ass(words, cfg, width, height, ass_path, script=text)

    total = voice_dur + TAIL_PAD
    video = build_video(ffmpeg, clips, cfg, total, work)
    log("clips", f"concatenated and fitted to {total:.2f}s -> work/video.mp4")

    music_path = None
    if cfg["music"].get("enabled"):
        tracks = sorted(
            p for p in (ROOT / args.music).iterdir()
            if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
        ) if (ROOT / args.music).is_dir() else []
        if tracks:
            music_path = tracks[0]
            log("music", f"{music_path.name} at volume {cfg['music']['volume']}")
        else:
            print(f"warning: music.enabled is true but no tracks in {args.music}", file=sys.stderr)

    out_path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.mp4"
    log("assemble", "burning captions and muxing audio")
    assemble(ffmpeg, video, voice_path, ass_path, cfg, music_path, total, font_dir, out_path)

    if not args.keep_work:
        (work / "video.mp4").unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / 1e6
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:  # --out pointed outside the project
        shown = out_path
    print(f"\n  {shown}  ({width}x{height}, {total:.1f}s, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
