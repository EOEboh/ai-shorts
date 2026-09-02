# ai-shorts

Turns `script.txt` plus a few hand-generated clips into a finished vertical short
(1080x1920, H.264, faststart): neutral AI voice, burned-in word-by-word captions,
optional ducked music.

Per video you do two things — write the script, generate 2-3 clips. The rest is
one command.

## Setup (macOS)

```bash
brew install python@3.13 ffmpeg-full
brew install --cask font-inter
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`ffmpeg-full`, not `ffmpeg`. Homebrew's core ffmpeg formula is deliberately slim
and ships **without libass**, so caption burn-in is impossible with it. Because
`ffmpeg-full` is keg-only it is never put on `PATH`; `build.py` looks for it at
`/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`, and `$FFMPEG` overrides that. The
build refuses to start if it cannot find an ffmpeg with libass.

Pick a voice:

```bash
.venv/bin/edge-tts --list-voices | grep -E "en-GB|en-US"
```

## Use

Drop clips into `clips/` named `01.mp4`, `02.mp4`, ... — they are ordered by
filename and nothing is auto-detected, so the naming *is* the edit. Write
`script.txt` (lines starting with `#` are ignored). Then:

```bash
.venv/bin/python build.py
```

Overrides:

```bash
.venv/bin/python build.py --script script.txt --clips clips/ --voice en-GB-RyanNeural --out out/
```

`--config` points at a different YAML, `--keep-work` leaves the intermediates in
`work/` (`voice.mp3`, `words.json`, `captions.ass`, `video.mp4`).

## How it runs

1. Read `script.txt`.
2. edge-tts synthesises `work/voice.mp3`, emitting a `WordBoundary` event per word.
3. Those events give word timings directly. `captions.timing: whisper` instead
   transcribes the audio with faster-whisper (base, int8, CPU); this is also the
   automatic fallback if a voice emits no word boundaries.
4. Write `work/captions.ass`.
5. Scale/crop every clip to fill 1080x1920, concat, and fit the result to the
   **voice** length — hold the final frame if the clips run short, trim if long.
6. Burn in the captions, mux voice and optional music, write `out/<timestamp>.mp4`.

Timing comes from the synthesiser rather than from transcription because the words
are already known: edge-tts reports exactly when it said each one, so there is
nothing to infer. faster-whisper stays available for audio that did not come from
edge-tts.

## config.yaml

```yaml
voice: en-GB-RyanNeural
rate: "+0%"
resolution: "1080x1920"
fps: 30
captions:
  font: "Inter"
  size: 90              # px at the configured resolution
  style: word-by-word   # word-by-word | plain
  position: center      # center | lower-third | top
  timing: wordboundary  # wordboundary | whisper
  group_size: 3         # words on screen at once
  color: "#FFFFFF"
  highlight: "#FFE500"
  outline: 6
music:
  enabled: false
  volume: 0.08
```

`size` is literal pixels: the ASS file pins `PlayResX/PlayResY` to the output
resolution, so one unit is one pixel. Word runs are capped by `group_size` *and*
by line width, so long words split rather than overflow the frame.

Music is optional; the first file in `music/` is looped to length and mixed at
`music.volume` under the voice.

## Troubleshooting

**`no ffmpeg with libass support`** — install `ffmpeg-full` (see Setup).

**edge-tts fails with HTTP 403** — Microsoft rotates the `Sec-MS-GEC` token and
pinned versions go stale. `pip install -U edge-tts`.

**Captions are in the wrong font** — libass substitutes silently when a font is
missing. The build warns at startup; install Inter with
`brew install --cask font-inter`.
