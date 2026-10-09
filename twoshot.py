"""Two-shot podcast compositor.

Turns a completed HeyGen dialogue job (one single-speaker clip per turn) into a
single "same room" two-shot: each host is fixed to one half of a 16:9 frame,
the two halves are joined with a feathered seam so the desk and back wall read
as one continuous studio, and whoever is not speaking runs a looping muted
"listening" clip instead of freezing.

Pure ffmpeg — no network. The provider calls that fetch the speaking clips and
generate the idle clips live in video_service, which owns provider access.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

W, H = 1920, 1080
OVERLAP = 36  # px of feathered blend at the seam. Narrow reads as a furniture
# edge where the halves' backgrounds differ (shelf vs wall); wide reads as haze.

# Per-side framing trim so the desk line meets at the seam: `zoom` scales the
# source before the half-crop, `dx`/`dy` shift the crop window (+right/+down,
# source pixels). Calibrated against the current studio looks — retune these
# whenever the persona looks change (compare desk height at the seam).
ALIGN = {"left": {"zoom": 1.08, "dx": 0, "dy": 40},
         "right": {"zoom": 1.055, "dx": 0, "dy": -39}}
# Longer hum sequence → a longer idle source clip → more raw material for the
# humanized listening track (a short loop repeats visibly). Muted in the mix.
IDLE_SCRIPT = "Mm-hmm...  Mmm...  Mm-hmm...  Hmm...  Mm...  Mm-hmm."
IDLE_VERSION = 3  # bump to invalidate cached idle clips when IDLE_* changes

XFADE = 0.2      # s of video/audio crossfade at each turn boundary (short enough
                 # that same-host pose dissolves read as motion blur, not ghosting)
DESK_Y = 888     # output y of the desk back edge at the seam — boundary between
                 # the wall and desk color-correction zones; recalibrate with ALIGN
HEAD_PAD = 0.2   # s of the speaker's lead-in idle kept before their first word
TAIL_PAD = 0.35  # s kept after their last word
# Keeps the listener's hands parked on the desk — without this the photo avatar
# gestures as if mid-speech, which reads wrong next to the actual speaker.
IDLE_MOTION_PROMPT = ("sitting completely still, listening to his co-host. His "
                      "hands stay clasped on the desk, perfectly motionless, for "
                      "the entire video — no hand or arm movement whatsoever. "
                      "Only breathing, slow blinks and an occasional very slight "
                      "head nod. Does not speak.")


class StitchError(Exception):
    pass


def ffmpeg_bin() -> tuple[str, str]:
    """Locate ffmpeg/ffprobe: $FFMPEG_BIN dir, then bundled tools/, then PATH."""
    candidates = []
    env_dir = (os.environ.get("FFMPEG_BIN") or "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(__file__).resolve().parent / "tools" / "ffmpeg")
    for d in candidates:
        ff, fp = d / "ffmpeg.exe", d / "ffprobe.exe"
        if ff.exists() and fp.exists():
            return str(ff), str(fp)
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return ff, fp
    raise StitchError(
        "ffmpeg/ffprobe not found. Put them in tools/ffmpeg/ under the project, "
        "set FFMPEG_BIN to their folder, or add them to PATH.")


def _run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise StitchError(f"ffmpeg failed:\n{r.stderr[-1800:]}")


def probe_duration(path: Path) -> float:
    _, fp = ffmpeg_bin()
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ai-intelekt-engine/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def make_idle_loop(ff: str, src: Path, dst: Path):
    """Forward+reversed ping-pong so a short idle clip loops seamlessly, muted."""
    _run([ff, "-hide_banner", "-v", "error", "-y", "-i", str(src),
          "-filter_complex",
          "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0,"
          "fps=25,format=yuv420p[v]",
          "-map", "[v]", "-an", "-c:v", "libx264", "-crf", "18",
          "-preset", "fast", str(dst)])


def speech_bounds(path: Path) -> tuple[float, float]:
    """(start, end) of actual speech in a clip, padded by HEAD_PAD/TAIL_PAD.
    HeyGen clips open and close with dead idle air; stacked at every turn
    boundary it reads as an awkward pause, so the stitch trims to speech."""
    ff, _ = ffmpeg_bin()
    r = subprocess.run([ff, "-hide_banner", "-i", str(path),
                        "-af", "silencedetect=n=-32dB:d=0.3", "-f", "null", "-"],
                       capture_output=True, text=True)
    dur = probe_duration(path)
    silences, open_start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            open_start = float(m.group(1))
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and open_start is not None:
            silences.append((open_start, float(m.group(1))))
            open_start = None
    if open_start is not None:  # trailing silence runs to EOF
        silences.append((open_start, dur))
    start, end = 0.0, dur
    if silences and silences[0][0] <= 0.05:
        start = max(0.0, silences[0][1] - HEAD_PAD)
    if silences and silences[-1][1] >= dur - 0.05:
        end = min(dur, silences[-1][0] + TAIL_PAD)
    if end - start < 1.0:  # implausible — keep the clip whole rather than mangle it
        return 0.0, dur
    return start, end


def _motion_scores(src: Path) -> list[tuple[float, float]]:
    """Per-frame inter-frame motion, measured on the central subject region
    (outer borders are wall/desk and only dilute the face/hand signal)."""
    ff, _ = ffmpeg_bin()
    r = subprocess.run([ff, "-hide_banner", "-i", str(src), "-vf",
                        "crop=iw*0.6:ih*0.7:iw*0.2:ih*0.08,"
                        "tblend=all_mode=difference,signalstats,metadata=print",
                        "-f", "null", "-"], capture_output=True, text=True)
    frames, t = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", line)
        if m and t is not None:
            frames.append((t, float(m.group(1))))
            t = None
    return frames[1:]  # first diff frame compares against nothing real


# Best-window motion above this means the idle clip is never truly still
# (measured: a clean hold scores ~0.23, a continuously humming host ~0.34) —
# fall back to a frozen closed-lips frame with synthetic breathing instead.
STILL_THRESH = 0.30


def stillest_window(src: Path) -> tuple[float, float, float]:
    """Find the span with the least motion in an idle clip, returning
    (start, end, mean motion score). Prompting the provider for a motionless
    avatar is unreliable — hands drift and the hum script moves the mouth — so
    stillness is selected after the fact. Several window lengths are tried:
    when the clip hums every couple of seconds, a short window between hums
    beats a long window that would have to span one."""
    frames = _motion_scores(src)
    if len(frames) < 10:
        return 0.0, min(1.0, probe_duration(src)), 0.0
    scores = [s for _, s in frames]
    best = None  # (mean score, -length, start index, n_win)
    for w in (1.6, 1.1, 0.7):
        n_win = max(2, round(w * 25))
        if n_win >= len(scores):
            continue
        cur = sum(scores[:n_win])
        for i in range(len(scores) - n_win):
            cand = (cur / n_win, -w, i, n_win)
            if best is None or cand < best:
                best = cand
            cur += scores[i + n_win] - scores[i]
    if best is None:
        return 0.0, min(1.0, probe_duration(src)), 0.0
    score, neg_w, i, n_win = best
    start = frames[i][0]
    return start, min(start + (-neg_w), frames[-1][0]), score


def _closed_lips_frame(ff: str, src: Path, frames: list) -> float:
    """Timestamp of the best freeze frame: among the lowest-motion frames
    (spaced apart), pick the one whose mouth region is brightest — an open
    mouth shows a dark gap, closed lips are skin-toned."""
    candidates, taken = [], []
    for t, _ in sorted(frames, key=lambda f: f[1]):
        if all(abs(t - u) >= 0.3 for u in taken):
            taken.append(t)
            if len(taken) >= 12:
                break
    best_t, best_luma = taken[0], -1.0
    for t in taken:
        r = subprocess.run([ff, "-hide_banner", "-v", "error",
                            "-ss", f"{t:.3f}", "-i", str(src),
                            "-vf", "crop=iw*0.24:ih*0.12:iw*0.38:ih*0.42,"
                                   "scale=1:1:flags=area,format=rgb24",
                            "-frames:v", "1", "-f", "rawvideo", "-"],
                           capture_output=True)
        if len(r.stdout) >= 3:
            px = r.stdout[-3:]
            luma = 0.299 * px[0] + 0.587 * px[1] + 0.114 * px[2]
            if luma > best_luma:
                best_luma, best_t = luma, t
    return best_t


def build_still_idle(ff: str, src: Path, dst: Path, target_seconds: float, seed: str):
    """The listening track. Preferred: the stillest window of the idle clip,
    stretched to ~3x slow motion and ping-ponged (forward/reverse passes at
    slightly varied speeds) — residual micro-motion reads as slow breathing.
    If the clip is never actually still (best window above STILL_THRESH), a
    frozen closed-lips frame with a slow synthetic breathing zoom is used
    instead: absolute stillness beats authentic-but-moving."""
    ws, we, score = stillest_window(src)
    if score > STILL_THRESH:
        t = _closed_lips_frame(ff, src, _motion_scores(src))
        png = dst.with_suffix(".freeze.png")
        _run([ff, "-hide_banner", "-v", "error", "-y", "-ss", f"{t:.3f}",
              "-i", str(src), "-frames:v", "1", str(png)])
        # Fully static — no synthetic breathing. Any animated zoom quantizes to
        # visible micro-hops on high-contrast edges, and a periodic twitch reads
        # worse than a perfectly held pose next to an animated speaker.
        _run([ff, "-hide_banner", "-v", "error", "-y",
              "-loop", "1", "-framerate", "25", "-i", str(png),
              "-t", f"{target_seconds + 1:.2f}",
              "-vf", "scale=1080:1080,setsar=1,format=yuv420p",
              "-an", "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(dst)])
        return
    span = we - ws
    if span <= 0.2:
        raise StitchError(f"idle source too short for a still window: {src}")
    rng = random.Random(seed)
    chunks, total, forward = [], 0.0, True
    while total < target_seconds or len(chunks) < 2:
        speed = rng.uniform(0.30, 0.42)  # ~2.4-3.3x slow motion
        chunks.append((forward, speed))
        total += span / speed
        forward = not forward
    n = len(chunks)
    parts = [f"[0:v]split={n}" + "".join(f"[i{k}]" for k in range(n))]
    for k, (fwd, speed) in enumerate(chunks):
        ops = f"trim=start={ws:.3f}:end={we:.3f},setpts=PTS-STARTPTS,"
        ops += "" if fwd else "reverse,"
        parts.append(f"[i{k}]{ops}setpts=PTS/{speed:.3f}[c{k}]")
    fc = (";".join(parts) + ";" + "".join(f"[c{k}]" for k in range(n))
          + f"concat=n={n}:v=1:a=0,fps=25,format=yuv420p[v]")
    _run([ff, "-hide_banner", "-v", "error", "-y", "-i", str(src),
          "-filter_complex", fc, "-map", "[v]", "-an",
          "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(dst)])


def _half(src: str, cw: int, side: str, out: str, gain: dict | None = None) -> str:
    """Filter for one side: scale to full height (plus that side's trim zoom),
    crop to `cw` with the side's dx/dy shift, then color-correct. A single gain
    can't equalize wall AND desk at once (they mismatch differently), so the
    desk gain is applied to the whole half and the wall zone above DESK_Y gets
    a relative correction, blended in with a soft vertical ramp."""
    a = ALIGN.get(side) or {"zoom": 1.0, "dx": 0, "dy": 0}
    sh = round((H + 20) * a["zoom"])
    y = (sh - H) // 2 + a["dy"]
    base = (f"{src}scale=-2:{sh},setsar=1,"
            f"crop={cw}:{H}:(iw-{cw})/2+{a['dx']}:{max(0, y)}")
    if not gain:
        return base + out
    tag = side[0]
    dg, wg = gain["desk"], gain["wall"]
    rel = tuple(min(1.3, max(0.75, w / d)) for w, d in zip(wg, dg))
    wz, ramp = DESK_Y - 30, 80
    return (
        base + f",colorchannelmixer=rr={dg[0]:.4f}:gg={dg[1]:.4f}:bb={dg[2]:.4f}[{tag}d];"
        f"[{tag}d]split=2[{tag}b][{tag}w0];"
        f"[{tag}w0]crop={cw}:{wz}:0:0,"
        f"colorchannelmixer=rr={rel[0]:.4f}:gg={rel[1]:.4f}:bb={rel[2]:.4f},"
        f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='if(lt(Y,{wz - ramp}),255,255*({wz}-Y)/{ramp})'[{tag}wz];"
        f"[{tag}b][{tag}wz]overlay=x=0:y=0" + out
    )


def _sample_rgb(ff: str, clip: Path, side: str, cw: int,
                x: int, y: int, w: int, h: int) -> tuple[float, float, float]:
    """Mean RGB of a patch of one side, in half-crop output coordinates."""
    vf = (_half("", cw, side, "")
          + f",crop={w}:{h}:{x}:{y},scale=1:1:flags=area,format=rgb24")
    r = subprocess.run([ff, "-hide_banner", "-v", "error", "-ss", "0.5",
                        "-i", str(clip), "-vf", vf, "-frames:v", "1",
                        "-f", "rawvideo", "-"], capture_output=True)
    if len(r.stdout) < 3:
        raise StitchError(f"could not sample color from {clip}")
    px = r.stdout[-3:]
    return float(max(1, px[0])), float(max(1, px[1])), float(max(1, px[2]))


def _meet_in_middle(left, right, lo, hi):
    target = [(a * b) ** 0.5 for a, b in zip(left, right)]
    return (tuple(min(hi, max(lo, t / c)) for t, c in zip(target, left)),
            tuple(min(hi, max(lo, t / c)) for t, c in zip(target, right)))


def color_gains(ff: str, left_clip: Path, right_clip: Path, cw: int) -> dict:
    """Per-side, per-zone RGB gains meeting both halves at their geometric mean.
    Desk sampled well away from the seam and hands (with a fallback region and
    a brightness sanity check — sampling a shadow would poison the gains);
    wall sampled from the panel area above the heads' shoulder line."""
    # Average a near-seam patch (the halves must match where they MEET) with a
    # mid-desk patch (wood grain varies spatially; one patch alone skews the
    # per-channel target and leaves a residual tint step at the join).
    def _desk_sample(clip, side):
        near = _sample_rgb(ff, clip, side,
                           cw, cw - 300 if side == "left" else 100, 920, 200, 80)
        mid = _sample_rgb(ff, clip, side,
                          cw, cw - 560 if side == "left" else 360, 960, 200, 70)
        if sum(near) < 180:
            return mid
        if sum(mid) < 180:
            return near
        return tuple((a + b) / 2 for a, b in zip(near, mid))

    ld = _desk_sample(left_clip, "left")
    rd = _desk_sample(right_clip, "right")
    if min(sum(ld), sum(rd)) < 180:
        dl = dr = (1.0, 1.0, 1.0)  # can't find wood on both sides — do no harm
    else:
        dl, dr = _meet_in_middle(ld, rd, 0.85, 1.18)

    lw = _sample_rgb(ff, left_clip, "left", cw, cw - 450, 140, 300, 180)
    rw = _sample_rgb(ff, right_clip, "right", cw, 150, 140, 300, 180)
    if not (30 <= sum(lw) <= 420 and 30 <= sum(rw) <= 420):
        wl, wr = dl, dr  # implausible wall sample — no differential correction
    else:
        wl, wr = _meet_in_middle(lw, rw, 0.80, 1.25)
    return {"left": {"desk": dl, "wall": wl}, "right": {"desk": dr, "wall": wr}}


def _feathered_turn(ff: str, speaker_clip: Path, idle_track: Path,
                    speaker_side: str, bounds: tuple[float, float],
                    idle_offset: float, gains: dict, out: Path):
    """Composite one turn: the speaker's clip (trimmed to speech) on their half,
    the listener's still idle track (seeked to a varied phase) on the other,
    joined by a feathered seam. Audio comes from the speaker."""
    cw = (W + OVERLAP) // 2
    rx = W - cw
    ss, ee = bounds
    turn_dur = ee - ss
    trim = (f"[0:v]trim=start={ss:.3f}:end={ee:.3f},setpts=PTS-STARTPTS[tv];"
            f"[0:a]atrim=start={ss:.3f}:end={ee:.3f},asetpts=PTS-STARTPTS[ta];"
            # in-graph trim, not an input -ss: keyframe seeking can drop the
            # first frame(s), which rendered as a black half at t=0
            f"[1:v]trim=start={idle_offset:.3f},setpts=PTS-STARTPTS[iv];")
    if speaker_side == "left":
        left_src, right_src = "[tv]", "[iv]"
    else:
        left_src, right_src = "[iv]", "[tv]"
    fc = (
        trim
        + _half(left_src, cw, "left", "[sl]", gains.get("left")) + ";"
        + _half(right_src, cw, "right", "[sr]", gains.get("right")) + ";"
        + f"[sl]pad={W}:{H}:0:0:color=black[base];"
        + f"[sr]format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        + f"a='clip(X/{OVERLAP}*255,0,255)'[rr];"
        + f"[base][rr]overlay=x={rx}:y=0,fps=25,format=yuv420p[v]"
    )
    _run([ff, "-hide_banner", "-v", "error", "-y",
          "-i", str(speaker_clip), "-i", str(idle_track),
          "-filter_complex", fc, "-map", "[v]", "-map", "[ta]",
          "-t", f"{turn_dur:.3f}",
          "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)])


def render(turns: list[dict], seg_clips: dict, idle_sources: dict,
           side_of: dict, out_path: Path) -> dict:
    """Build the full two-shot.

    turns        ordered [{"index": int, "speaker": str}, ...]
    seg_clips    {segment index: Path to that speaker's downloaded clip}
    idle_sources {speaker: Path to that speaker's raw idle clip}
    side_of      {speaker: "left"|"right"}

    Speaker clips are trimmed to speech, listeners run a slowed stillest-window
    idle track seeked to a different phase every turn, both halves are
    color-matched via the shared desk wood, and consecutive turns are joined
    with a video+audio crossfade instead of a hard cut.
    """
    ff, _ = ffmpeg_bin()
    work = out_path.parent / "_turns"
    work.mkdir(parents=True, exist_ok=True)
    seed = out_path.parent.name  # deterministic per job → reruns are identical

    bounds = {t["index"]: speech_bounds(seg_clips[t["index"]]) for t in turns}
    total = sum(e - s for s, e in bounds.values())

    first_clip_of = {}
    for t in turns:
        first_clip_of.setdefault(side_of[t["speaker"]], seg_clips[t["index"]])
    cw = (W + OVERLAP) // 2
    gains = color_gains(ff, first_clip_of["left"], first_clip_of["right"], cw)

    idle_tracks = {}
    for spk, src in idle_sources.items():
        track = work / f"idle_{spk}.mp4"
        build_still_idle(ff, src, track, total + 15, f"{seed}:{spk}")
        idle_tracks[spk] = (track, probe_duration(track))

    rng = random.Random(seed)
    turn_files = []
    for t in turns:
        spk = t["speaker"]
        listener = next(s for s in side_of if s != spk)
        track, track_dur = idle_tracks[listener]
        turn_dur = bounds[t["index"]][1] - bounds[t["index"]][0]
        offset = rng.uniform(0, max(0.0, track_dur - turn_dur - 0.5))
        dst = work / f"turn{t['index']}.mp4"
        _feathered_turn(ff, seg_clips[t["index"]], track, side_of[spk],
                        bounds[t["index"]], offset, gains, dst)
        turn_files.append(dst)

    durs = [probe_duration(f) for f in turn_files]
    inputs, fc = ["-i", str(turn_files[0])], ""
    vprev, aprev, offset = "[0:v]", "[0:a]", 0.0
    for i in range(1, len(turn_files)):
        inputs += ["-i", str(turn_files[i])]
        offset += durs[i - 1] - XFADE
        fc += (f"{vprev}[{i}:v]xfade=transition=fade:duration={XFADE}"
               f":offset={offset:.3f}[vx{i}];"
               f"{aprev}[{i}:a]acrossfade=d={XFADE}[ax{i}];")
        vprev, aprev = f"[vx{i}]", f"[ax{i}]"
    # Pin 8-bit 4:2:0 — without this the xfade graph negotiates yuv444p and
    # players reject the resulting High 4:4:4 profile (e.g. Windows Media Player).
    fc += f"{vprev}format=yuv420p[vout]"
    _run([ff, "-hide_banner", "-v", "error", "-y", *inputs,
          "-filter_complex", fc, "-map", "[vout]", "-map", aprev,
          "-c:v", "libx264", "-crf", "18", "-preset", "medium",
          "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)])
    shutil.rmtree(work, ignore_errors=True)
    return {"path": str(out_path), "durationSeconds": round(probe_duration(out_path), 2)}


def assign_sides(turns: list[dict]) -> dict:
    """First speaker to appear takes the left half, the other the right."""
    order = []
    for t in turns:
        if t["speaker"] not in order:
            order.append(t["speaker"])
    sides = {}
    for i, spk in enumerate(order[:2]):
        sides[spk] = "left" if i == 0 else "right"
    return sides
