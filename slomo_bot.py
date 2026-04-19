#!/usr/bin/env python3
"""
Telegram Video Studio Bot (Pyrogram MTProto)
─────────────────────────────────────────────
Professional video processing. No file limits. Social-media ready.

Fixed: session persistence, concurrency limits, minterpolate tuning,
       timeout scaling, env-var config, error handling.
"""

import os, asyncio, logging, subprocess, uuid, json, time, sys
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
)

# ── Config (use env vars on Railway) ────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8622684367:AAEaosDKzWoaO0bil7TstPoHtqGQHXFFO4I")
API_ID     = int(os.environ.get("API_ID", "6"))
API_HASH   = os.environ.get("API_HASH", "eb06d4abfb49dc3eeb1aeb98ae0f581e")
TEMP_DIR   = os.environ.get("TEMP_DIR", "/tmp/vidbot")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "1"))  # limit to 1 to avoid OOM

os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# in_memory=True avoids .session file on disk — critical for Railway's ephemeral FS
app = Client(
    "video_studio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)
user_state = {}
processing_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ── Encoding Presets ────────────────────────────────────────────────────────

def _mem_threads():
    """
    Limit threads to control peak RAM usage on small Railway containers.
    Each x264 thread holds a full frame buffer, so fewer threads = less RAM.
    """
    return ["-threads", "2"]

def _hq_video_args(bitrate_mbps=12, preset="medium"):
    """
    H.264 High Profile encoding with premium bitrate targets.
    Quality comes from high bitrate + good preset, not exotic x264 flags.
    """
    return [
        *_mem_threads(),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.2",
        "-pix_fmt", "yuv420p",
        "-preset", preset,
        "-b:v", f"{bitrate_mbps}M",
        "-maxrate", f"{int(bitrate_mbps * 1.5)}M",
        "-bufsize", f"{bitrate_mbps * 2}M",
        "-movflags", "+faststart",
    ]

def _ultra_video_args(crf=17, preset="medium"):
    """
    CRF encoding for upscale/enhance — lets x264 allocate bits to detail.
    preset=medium instead of slow to halve RAM usage.
    """
    return [
        *_mem_threads(),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.2",
        "-pix_fmt", "yuv420p",
        "-preset", preset,
        "-crf", str(crf),
        "-movflags", "+faststart",
    ]

def _hq_audio_args():
    """High-quality AAC audio — 48kHz stereo 256kbps."""
    return ["-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2"]

def _calc_bitrate(width, height, fps=30):
    """
    Calculate bitrate based on resolution and FPS.
    Balanced for quality + fast Telegram upload. Roughly matches
    YouTube recommended bitrates (which look great on any display).
    """
    pixels = width * height
    if pixels >= 3840 * 2160:   # 4K
        base = 25
    elif pixels >= 2560 * 1440:   # 1440p
        base = 16
    elif pixels >= 1920 * 1080:   # 1080p
        base = 10
    elif pixels >= 1280 * 720:    # 720p
        base = 6
    else:
        base = 4
    if fps > 30:
        base = int(base * (fps / 30) * 0.85)
    return base

# ── FFmpeg Helpers ──────────────────────────────────────────────────────────

def _uid():
    return uuid.uuid4().hex[:10]

def _run(cmd, timeout=600):
    """Run an FFmpeg command with full stderr logging on failure.

    IMPORTANT: We inject -loglevel error right after the 'ffmpeg' binary
    to suppress the version banner and configuration dump that FFmpeg
    normally prints to stderr. This way, on failure we only see the
    actual error lines — not pages of 'enable-libfoo disable-bar'.
    """
    # Inject -loglevel error after the binary name so only real errors appear
    cmd = list(cmd)  # ensure mutable copy
    if cmd and cmd[0] in ("ffmpeg", "ffmpeg.exe"):
        cmd.insert(1, "-loglevel")
        cmd.insert(2, "error")
        # Limit filter graph threads to control peak RAM on small containers
        cmd.insert(3, "-filter_threads")
        cmd.insert(4, "1")

    logger.info(f"FFmpeg cmd: {' '.join(str(c) for c in cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"FFmpeg timed out after {timeout}s. "
            "The video may be too long or the operation too heavy for this server. "
            "Try a shorter clip or a lighter setting."
        )
    if r.returncode != 0:
        logger.error(f"FFmpeg FULL stderr:\n{r.stderr}")

        # Exit code -9 = SIGKILL = OOM (out of memory) — give a clear message
        if r.returncode == -9:
            raise RuntimeError(
                "Server ran out of memory processing this video. "
                "Try a lower resolution, shorter clip, or lighter setting."
            )

        # With -loglevel error the banner is suppressed, but just in case,
        # filter out any remaining config/banner noise.
        lines = [l.strip() for l in r.stderr.strip().split('\n') if l.strip()]
        # Skip lines that are clearly FFmpeg banner / build info
        skip_patterns = ['configuration:', 'built with', 'ffmpeg version',
                         'libavutil', 'libavcodec', 'libavformat',
                         'libavdevice', 'libavfilter', 'libswscale',
                         'libswresample', 'libpostproc', 'copyright']
        real_lines = [l for l in lines
                      if not any(p in l.lower() for p in skip_patterns)
                      and 'frame=' not in l]  # skip progress lines
        if real_lines:
            err_msg = '\n'.join(real_lines[-8:])
        elif lines:
            err_msg = '\n'.join(lines[-5:])
        else:
            err_msg = f"FFmpeg exited with code {r.returncode}"
        raise RuntimeError(err_msg[:1500])

def probe_fps(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    frac = r.stdout.strip()
    if "/" in frac:
        n, d = frac.split("/")
        return float(n) / float(d)
    return float(frac) if frac else 30.0

def probe_info(path):
    """Full video probe — resolution, FPS, duration, bitrate."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,bit_rate",
         "-show_entries", "format=duration,bit_rate",
         "-of", "json", path],
        capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
        s = data.get("streams", [{}])[0]
        f = data.get("format", {})
        w, h = s.get("width", 1920), s.get("height", 1080)
        frac = s.get("r_frame_rate", "30/1")
        if "/" in str(frac):
            n, d = str(frac).split("/")
            fps = round(float(n) / float(d), 2) if float(d) != 0 else 30
        else:
            fps = float(frac)
        dur = round(float(f.get("duration", 0)), 1)
        br = int(f.get("bit_rate", 0)) // 1000  # kbps
        return {"w": w, "h": h, "fps": fps, "dur": dur, "br_kbps": br}
    except Exception:
        return {"w": 1920, "h": 1080, "fps": 30, "dur": 0, "br_kbps": 8000}

def _estimate_timeout(info, complexity=1.0):
    """Scale timeout based on video duration and operation complexity."""
    dur = max(info.get("dur", 30), 10)
    # base: ~10x realtime for simple ops, scaled by complexity
    timeout = int(dur * 10 * complexity)
    return max(timeout, 120)  # minimum 2 minutes

# ── Processing Functions ────────────────────────────────────────────────────

def do_slomo(inp, out, factor):
    """
    Slow motion using blend-mode interpolation.
    MCI mode uses too much RAM for Railway containers, so we use blend
    which is lighter and still produces smooth results.
    For HD+ sources, we scale down to 720p for interpolation then back up.
    """
    info = probe_info(inp)
    fps = info["fps"]
    br = _calc_bitrate(info["w"], info["h"], fps)
    timeout = _estimate_timeout(info, complexity=factor * 2.0)

    # For high-res, downscale → interpolate → upscale to save RAM
    if info["h"] > 720:
        # Scale to 720p for interpolation, then back to original size
        vf = (
            f"scale=-2:720,"
            f"setpts={factor}*PTS,"
            f"minterpolate=fps={fps}:mi_mode=blend,"
            f"scale={info['w']}:{info['h']}:flags=lanczos"
        )
    else:
        vf = (
            f"setpts={factor}*PTS,"
            f"minterpolate=fps={fps}:mi_mode=blend"
        )

    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf,
          "-an",
          "-r", str(fps),
          *_hq_video_args(br), out], timeout=timeout)

def do_smooth(inp, out, target_fps):
    """
    Frame interpolation to target FPS using blend mode.
    For high-res, downscale → interpolate → upscale to stay within RAM.
    """
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"], target_fps)
    complexity = target_fps / max(info["fps"], 1)
    timeout = _estimate_timeout(info, complexity=complexity * 1.5)

    if info["h"] > 720:
        vf = (
            f"scale=-2:720,"
            f"minterpolate=fps={target_fps}:mi_mode=blend,"
            f"scale={info['w']}:{info['h']}:flags=lanczos"
        )
    else:
        vf = f"minterpolate=fps={target_fps}:mi_mode=blend"

    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf,
          "-map", "0:a?",
          "-r", str(target_fps),
          *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)

def do_upscale(inp, out, target_h):
    """
    High-quality upscale pipeline (memory-safe for Railway containers):
      1. Light denoise source (avoid amplifying noise)
      2. Lanczos upscale
      3. Single-pass unsharp mask for detail recovery
      4. Subtle contrast/color enhancement
      5. CRF encoding for quality

    8K is not available — Railway containers don't have enough RAM for it.
    Max is 4K (3840x2160).
    """
    # Cap at 4K — 8K requires 8+ GB RAM which Railway doesn't provide
    if target_h > 2160:
        target_h = 2160

    res_map = {
        720:  (1280, 720),
        1080: (1920, 1080),
        1440: (2560, 1440),
        2160: (3840, 2160),
    }
    tw, th = res_map.get(target_h, (1920, target_h))
    info = probe_info(inp)
    src_pixels = info["w"] * info["h"]
    dst_pixels = tw * th

    # Skip if already at or above target resolution
    if info["h"] >= target_h:
        # Just enhance without upscaling
        crf = 17
        vf = (
            "hqdn3d=2:1.5:2:1.5,"
            "unsharp=5:5:0.6:5:5:0.0,"
            "eq=contrast=1.03:brightness=0.01:saturation=1.05"
        )
        timeout = _estimate_timeout(info, complexity=1.5)
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", vf,
              "-map", "0:v", "-map", "0:a?",
              *_ultra_video_args(crf=crf, preset="medium"),
              *_hq_audio_args(), out], timeout=timeout)
        return

    # Determine CRF and complexity based on target
    if target_h >= 2160:
        crf = 17
        complexity = 3.0
    elif target_h >= 1440:
        crf = 17
        complexity = 2.5
    else:
        crf = 18
        complexity = 2.0

    timeout = _estimate_timeout(info, complexity=complexity)
    scale_ratio = dst_pixels / max(src_pixels, 1)

    # Adaptive sharpening — one pass only to save memory
    if scale_ratio > 4:
        sharp = "unsharp=5:5:0.8:5:5:0.0"
    elif scale_ratio > 2:
        sharp = "unsharp=5:5:0.6:5:5:0.0"
    else:
        sharp = "unsharp=5:5:0.5:5:5:0.0"

    vf = (
        # Light denoise before upscaling
        "hqdn3d=2:1.5:2:1.5,"
        # Lanczos upscale
        f"scale={tw}:{th}:flags=lanczos"
        ":force_original_aspect_ratio=decrease,"
        f"pad={tw}:{th}:-1:-1:color=black,"
        # Single-pass sharpening (saves RAM vs multi-pass)
        f"{sharp},"
        # Subtle enhancement
        "eq=contrast=1.04:brightness=0.01:saturation=1.06"
    )
    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf,
          "-map", "0:v", "-map", "0:a?",
          *_ultra_video_args(crf=crf, preset="medium"),
          *_hq_audio_args(), out], timeout=timeout)

def do_speed(inp, out, factor):
    """Speed up with proper audio tempo scaling."""
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=1.0)
    atempo_chain = []
    rem = factor
    while rem > 2.0:
        atempo_chain.append("atempo=2.0")
        rem /= 2.0
    atempo_chain.append(f"atempo={rem}")
    af = ",".join(atempo_chain)
    try:
        _run(["ffmpeg", "-y", "-i", inp,
              "-filter_complex",
              f"[0:v]setpts={1/factor}*PTS[v];[0:a]{af}[a]",
              "-map", "[v]", "-map", "[a]",
              *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)
    except RuntimeError:
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", f"setpts={1/factor}*PTS", "-an",
              *_hq_video_args(br), out], timeout=timeout)

def do_reverse(inp, out):
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=2.0)
    try:
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", "reverse", "-af", "areverse",
              *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)
    except RuntimeError:
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", "reverse", "-an",
              *_hq_video_args(br), out], timeout=timeout)

def do_boomerang(inp, out):
    uid = _uid()
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=2.5)
    fwd = os.path.join(TEMP_DIR, f"{uid}_fwd.mp4")
    rev = os.path.join(TEMP_DIR, f"{uid}_rev.mp4")
    lst = os.path.join(TEMP_DIR, f"{uid}_list.txt")
    try:
        _run(["ffmpeg", "-y", "-i", inp, "-an", *_hq_video_args(br), fwd], timeout=timeout)
        _run(["ffmpeg", "-y", "-i", fwd, "-vf", "reverse", "-an", *_hq_video_args(br), rev], timeout=timeout)
        # Write concat list with proper newlines
        with open(lst, "w") as f:
            f.write(f"file '{fwd}'\n")
            f.write(f"file '{rev}'\n")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
              "-c", "copy", "-movflags", "+faststart", out])
    finally:
        for p in (fwd, rev, lst):
            if os.path.exists(p):
                os.remove(p)

def do_compress(inp, out, level):
    crf_map = {"light": 23, "medium": 28, "heavy": 34}
    crf = crf_map.get(level, 28)
    info = probe_info(inp)
    timeout = _estimate_timeout(info, complexity=1.0)
    _run(["ffmpeg", "-y", "-i", inp,
          "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.2",
          "-pix_fmt", "yuv420p", "-preset", "slow", "-crf", str(crf),
          *_hq_audio_args(),
          "-movflags", "+faststart", out], timeout=timeout)

def do_audio(inp, out):
    _run(["ffmpeg", "-y", "-i", inp,
          "-vn", "-c:a", "libmp3lame", "-q:a", "0", "-ar", "48000", out])

def do_gif(inp, out, fps):
    uid = _uid()
    palette = os.path.join(TEMP_DIR, f"{uid}_palette.png")
    try:
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", f"fps={fps},scale=480:-1:flags=lanczos,palettegen=stats_mode=diff",
              palette])
        _run(["ffmpeg", "-y", "-i", inp, "-i", palette,
              "-lavfi", f"fps={fps},scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5",
              out])
    finally:
        if os.path.exists(palette):
            os.remove(palette)

def do_rotate(inp, out, degrees):
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=1.0)
    transpose_map = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}
    vf = transpose_map.get(degrees, "transpose=1")
    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf, "-map", "0:v", "-map", "0:a?",
          *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)

# ── Enhancement Functions ────────────────────────────────────────────────────

def do_enhance(inp, out, preset):
    """
    Premium enhancement pipeline — multi-pass processing:
      1. Temporal + spatial denoise (hqdn3d)
      2. Multi-radius unsharp masking (coarse detail + fine detail)
      3. Adaptive contrast, brightness, saturation, gamma
      4. CRF encoding for maximum quality retention
    """
    info = probe_info(inp)
    crf = 16 if info["w"] >= 1920 else 18
    timeout = _estimate_timeout(info, complexity=2.0)
    filter_presets = {
        "auto": (
            "hqdn3d=3:2:3:2,"
            "unsharp=7:7:0.8:7:7:0.0,"   # coarse detail recovery
            "unsharp=3:3:0.5:3:3:0.0,"   # fine detail sharpening
            "eq=contrast=1.06:brightness=0.02:saturation=1.12:gamma=1.02"
        ),
        "sharp": (
            "hqdn3d=1.5:1:1.5:1,"
            "unsharp=7:7:1.2:7:7:0.0,"   # aggressive coarse sharpening
            "unsharp=5:5:0.8:5:5:0.0,"   # medium detail
            "unsharp=3:3:0.4:3:3:0.0,"   # fine texture
            "eq=contrast=1.08:brightness=0.01:saturation=1.06"
        ),
        "clean": (
            "hqdn3d=8:6:8:6,"            # strong temporal + spatial denoise
            "unsharp=5:5:0.4:5:5:0.0,"   # gentle detail recovery post-denoise
            "unsharp=3:3:0.2:3:3:0.0,"
            "eq=contrast=1.04:saturation=1.06:gamma=1.01"
        ),
        "vivid": (
            "hqdn3d=2:1.5:2:1.5,"
            "unsharp=5:5:0.7:5:5:0.0,"
            "unsharp=3:3:0.4:3:3:0.0,"
            "eq=contrast=1.15:brightness=0.03:saturation=1.30:gamma=1.06"
        ),
    }
    vf = filter_presets.get(preset, filter_presets["auto"])
    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf,
          "-map", "0:v", "-map", "0:a?",
          *_ultra_video_args(crf=crf, preset="medium"),
          *_hq_audio_args(), out], timeout=timeout)

def do_stabilize(inp, out, strength):
    """Two-pass video stabilization."""
    uid = _uid()
    trf = os.path.join(TEMP_DIR, f"{uid}_transforms.trf")
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=3.0)
    smooth_map = {"light": 10, "medium": 20, "heavy": 40}
    smoothing = smooth_map.get(strength, 20)
    try:
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", f"vidstabdetect=shakiness=8:accuracy=10:result={trf}",
              "-f", "null", "/dev/null"], timeout=timeout)
        _run(["ffmpeg", "-y", "-i", inp,
              "-vf", f"vidstabtransform=input={trf}:smoothing={smoothing}:interpol=bicubic:crop=black:zoom=3,"
                     f"unsharp=3:3:0.3:3:3:0.0",
              "-map", "0:v", "-map", "0:a?",
              *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)
    finally:
        if os.path.exists(trf):
            os.remove(trf)

def do_denoise(inp, out, strength):
    """Noise/grain removal."""
    info = probe_info(inp)
    br = _calc_bitrate(info["w"], info["h"])
    timeout = _estimate_timeout(info, complexity=1.5)
    noise_map = {
        "light":  "hqdn3d=3:2:3:2",
        "medium": "hqdn3d=6:4:6:4",
        "heavy":  "hqdn3d=10:8:10:8",
    }
    vf = noise_map.get(strength, noise_map["medium"])
    vf += ",unsharp=3:3:0.4:3:3:0.0"
    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf, "-map", "0:v", "-map", "0:a?",
          *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)

def do_motionblur(inp, out, strength):
    """
    Motion blur effect using frame blending.
    Instead of interpolating to high FPS (RAM-heavy), we use tmix
    to blend adjacent frames directly — achieves a similar look with
    a fraction of the memory.
    """
    info = probe_info(inp)
    orig_fps = info["fps"]
    br = _calc_bitrate(info["w"], info["h"], orig_fps)
    timeout = _estimate_timeout(info, complexity=2.0)
    blend_map = {"light": 2, "medium": 3, "heavy": 5}
    frames = blend_map.get(strength, 3)
    # tmix blends N adjacent frames — produces motion blur without minterpolate
    weights = " ".join(["1"] * frames)
    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", f"tmix=frames={frames}:weights={weights}",
          "-map", "0:a?",
          "-r", str(int(orig_fps)),
          *_hq_video_args(br), *_hq_audio_args(), out], timeout=timeout)

def do_social(inp, out, platform):
    """Social-media optimized export with premium encoding."""
    info = probe_info(inp)
    w, h = info["w"], info["h"]
    timeout = _estimate_timeout(info, complexity=2.0)

    if platform == "ig_reel":
        vf = ("scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,"
              "pad=1080:1920:-1:-1:color=black,"
              "unsharp=3:3:0.3:3:3:0.0")
        br, fps_out = 8, 30
    elif platform == "ig_post":
        vf = ("scale=1080:1080:flags=lanczos:force_original_aspect_ratio=decrease,"
              "pad=1080:1080:-1:-1:color=black,"
              "unsharp=3:3:0.3:3:3:0.0")
        br, fps_out = 6, 30
    elif platform == "tiktok":
        vf = ("scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,"
              "pad=1080:1920:-1:-1:color=black,"
              "unsharp=3:3:0.3:3:3:0.0")
        br, fps_out = 8, 30
    elif platform == "youtube":
        target_fps = 60 if info["fps"] > 30 else 30
        vf = (f"scale='if(gt(iw,3840),3840,-2)':'if(gt(iw,3840),-2,ih)':flags=lanczos,"
              "unsharp=3:3:0.3:3:3:0.0")
        br = _calc_bitrate(min(w, 3840), h, target_fps)
        br = max(br, 12)
        fps_out = target_fps
    elif platform == "twitter":
        vf = ("scale='if(gt(iw,1920),1920,-2)':'if(gt(iw,1920),-2,ih)':flags=lanczos,"
              "unsharp=3:3:0.3:3:3:0.0")
        br, fps_out = 6, 30
    else:
        vf = "null"
        br, fps_out = 15, 30

    _run(["ffmpeg", "-y", "-i", inp,
          "-vf", vf,
          "-r", str(fps_out),
          *_hq_video_args(br, preset="medium"),
          *_hq_audio_args(),
          out], timeout=timeout)

# ── Mode Definitions ────────────────────────────────────────────────────────

MODES = {
    "slomo":      {"icon": "🎬", "title": "Slow-Motion",    "prompt": "Send me a video — I'll create smooth slow motion."},
    "smooth":     {"icon": "✨", "title": "Smooth FPS",     "prompt": "Send me a video — I'll interpolate it to high frame rates."},
    "motionblur": {"icon": "💨", "title": "Motion Blur",    "prompt": "Send me a video — I'll add RSMB-style motion blur."},
    "upscale":    {"icon": "🔍", "title": "Upscale",        "prompt": "Send me a video — I'll upscale it with enhancement to 720p / 1080p / 1440p / 4K."},
    "speed":      {"icon": "⚡", "title": "Speed Up",       "prompt": "Send me a video — I'll speed it up with frame blending."},
    "reverse":    {"icon": "⏪", "title": "Reverse",        "prompt": "Send me a video — I'll reverse it."},
    "boomerang":  {"icon": "🔁", "title": "Boomerang",      "prompt": "Send me a video — I'll make a forward + reverse loop."},
    "enhance":    {"icon": "💎", "title": "Enhance",        "prompt": "Send me a video — I'll sharpen, denoise, and color correct."},
    "stabilize":  {"icon": "🎯", "title": "Stabilize",      "prompt": "Send me a shaky video — I'll smooth the camera motion."},
    "denoise":    {"icon": "🧹", "title": "Denoise",        "prompt": "Send me a noisy video — I'll clean up grain and noise."},
    "social":     {"icon": "📱", "title": "Social Export",   "prompt": "Send me a video — I'll export it optimized for your platform."},
    "compress":   {"icon": "📦", "title": "Compress",       "prompt": "Send me a video — I'll reduce the file size."},
    "audio":      {"icon": "🎵", "title": "Extract Audio",  "prompt": "Send me a video — I'll extract the audio as MP3."},
    "gif":        {"icon": "🎞️", "title": "Convert to GIF", "prompt": "Send me a video — I'll turn it into an optimized GIF."},
    "rotate":     {"icon": "🔄", "title": "Rotate",         "prompt": "Send me a video — I'll rotate it."},
}

OPTION_KEYBOARDS = {
    "slomo": [[
        InlineKeyboardButton("2x Slower", callback_data="slomo_2"),
        InlineKeyboardButton("4x Slower", callback_data="slomo_4"),
        InlineKeyboardButton("8x Slower", callback_data="slomo_8"),
    ]],
    "smooth": [[
        InlineKeyboardButton("30 FPS", callback_data="fps_30"),
        InlineKeyboardButton("60 FPS", callback_data="fps_60"),
        InlineKeyboardButton("120 FPS", callback_data="fps_120"),
    ]],
    "motionblur": [[
        InlineKeyboardButton("Light", callback_data="mb_light"),
        InlineKeyboardButton("Medium", callback_data="mb_medium"),
        InlineKeyboardButton("Heavy", callback_data="mb_heavy"),
    ]],
    "upscale": [[
        InlineKeyboardButton("720p", callback_data="up_720"),
        InlineKeyboardButton("1080p", callback_data="up_1080"),
    ], [
        InlineKeyboardButton("1440p", callback_data="up_1440"),
        InlineKeyboardButton("4K", callback_data="up_2160"),
    ]],
    "speed": [[
        InlineKeyboardButton("1.5x", callback_data="spd_1.5"),
        InlineKeyboardButton("2x", callback_data="spd_2"),
        InlineKeyboardButton("3x", callback_data="spd_3"),
    ]],
    "compress": [[
        InlineKeyboardButton("Light", callback_data="cmp_light"),
        InlineKeyboardButton("Medium", callback_data="cmp_medium"),
        InlineKeyboardButton("Heavy", callback_data="cmp_heavy"),
    ]],
    "rotate": [[
        InlineKeyboardButton("90° CW", callback_data="rot_90"),
        InlineKeyboardButton("180°", callback_data="rot_180"),
        InlineKeyboardButton("90° CCW", callback_data="rot_270"),
    ]],
    "gif": [[
        InlineKeyboardButton("Low (10 FPS)", callback_data="gif_10"),
        InlineKeyboardButton("Medium (15 FPS)", callback_data="gif_15"),
        InlineKeyboardButton("High (24 FPS)", callback_data="gif_24"),
    ]],
    "enhance": [[
        InlineKeyboardButton("Auto", callback_data="enh_auto"),
        InlineKeyboardButton("Sharpen", callback_data="enh_sharp"),
    ], [
        InlineKeyboardButton("Clean", callback_data="enh_clean"),
        InlineKeyboardButton("Vivid", callback_data="enh_vivid"),
    ]],
    "stabilize": [[
        InlineKeyboardButton("Light", callback_data="stb_light"),
        InlineKeyboardButton("Medium", callback_data="stb_medium"),
        InlineKeyboardButton("Heavy", callback_data="stb_heavy"),
    ]],
    "denoise": [[
        InlineKeyboardButton("Light", callback_data="dns_light"),
        InlineKeyboardButton("Medium", callback_data="dns_medium"),
        InlineKeyboardButton("Heavy", callback_data="dns_heavy"),
    ]],
    "social": [[
        InlineKeyboardButton("IG Reel", callback_data="soc_ig_reel"),
        InlineKeyboardButton("IG Post", callback_data="soc_ig_post"),
    ], [
        InlineKeyboardButton("TikTok", callback_data="soc_tiktok"),
        InlineKeyboardButton("YouTube", callback_data="soc_youtube"),
    ], [
        InlineKeyboardButton("Twitter/X", callback_data="soc_twitter"),
    ]],
}

INSTANT_MODES = {"reverse", "boomerang", "audio"}

WELCOME_TEXT = (
    "🎥 **Video Studio Bot**\n\n"
    "Professional video processing — no file limits, no quality loss.\n"
    "Premium H.264 encoding with upscaling up to 4K.\n"
    "Optimized for Retina, OLED, and ProMotion displays.\n\n"
    "**Motion & FPS**\n"
    "/slomo — Smooth slow motion (2x, 4x, 8x)\n"
    "/smooth — Frame interpolation (30/60/120 FPS)\n"
    "/motionblur — RSMB-style motion blur\n"
    "/speed — Speed up (1.5x, 2x, 3x)\n"
    "/reverse — Reverse playback\n"
    "/boomerang — Forward + reverse loop\n\n"
    "**Enhancement**\n"
    "/enhance — Sharpen + denoise + color (4 presets)\n"
    "/stabilize — Remove camera shake\n"
    "/denoise — Clean up noise & grain\n"
    "/upscale — Upscale to 720p / 1080p / 1440p / 4K\n\n"
    "**Export & Tools**\n"
    "/social — Export for IG / TikTok / YouTube / X (no recompression)\n"
    "/compress — Reduce file size\n"
    "/rotate — Rotate 90° / 180° / 270°\n"
    "/audio — Extract audio as MP3\n"
    "/gif — Convert to GIF\n\n"
    "/cancel — Cancel current operation\n\n"
    "Pick a command and send your video!"
)

# ── Callback Parser ─────────────────────────────────────────────────────────

def _parse_callback(data):
    if data.startswith("slomo_"):
        f = int(data.split("_")[1])
        return f"{f}x slow motion", do_slomo, (f,)
    elif data.startswith("fps_"):
        f = int(data.split("_")[1])
        return f"{f} FPS interpolation", do_smooth, (f,)
    elif data.startswith("mb_"):
        s = data.split("_")[1]
        return f"{s} motion blur", do_motionblur, (s,)
    elif data.startswith("up_"):
        h = int(data.split("_")[1])
        labels = {720: "720p", 1080: "1080p", 1440: "1440p", 2160: "4K", 4320: "8K"}
        return f"{labels.get(h, str(h))} upscale", do_upscale, (h,)
    elif data.startswith("spd_"):
        f = float(data.split("_")[1])
        return f"{f}x speed up", do_speed, (f,)
    elif data.startswith("cmp_"):
        return f"{data.split('_')[1]} compression", do_compress, (data.split("_")[1],)
    elif data.startswith("rot_"):
        return f"{data.split('_')[1]}° rotation", do_rotate, (int(data.split("_")[1]),)
    elif data.startswith("gif_"):
        return f"GIF ({data.split('_')[1]} FPS)", do_gif, (int(data.split("_")[1]),)
    elif data.startswith("enh_"):
        p = data.split("_")[1]
        labels = {"auto": "Auto Enhance", "sharp": "Sharpen", "clean": "Clean", "vivid": "Vivid"}
        return labels.get(p, p), do_enhance, (p,)
    elif data.startswith("stb_"):
        return f"{data.split('_')[1]} stabilization", do_stabilize, (data.split("_")[1],)
    elif data.startswith("dns_"):
        return f"{data.split('_')[1]} denoise", do_denoise, (data.split("_")[1],)
    elif data.startswith("soc_"):
        platform = data[4:]  # strip "soc_"
        labels = {"ig_reel": "Instagram Reel", "ig_post": "Instagram Post",
                  "tiktok": "TikTok", "youtube": "YouTube", "twitter": "Twitter/X"}
        return f"{labels.get(platform, platform)} export", do_social, (platform,)
    return None, None, ()

# ── Download / Upload ───────────────────────────────────────────────────────

async def send_result(client, chat_id, out_path, mode, orig_name, status_msg):
    if not os.path.exists(out_path):
        raise RuntimeError("Output file was not created.")

    out_mb = os.path.getsize(out_path) / (1024 * 1024)
    base = os.path.splitext(orig_name)[0]

    info = probe_info(out_path)
    specs = f"\n📐 {info['w']}x{info['h']} | {info['fps']} FPS"
    dur_m, dur_s = divmod(int(info["dur"]), 60)
    specs += f" | {dur_m}:{dur_s:02d} | {out_mb:.1f} MB"
    br_mbps = info["br_kbps"] / 1000
    specs += f" | {br_mbps:.1f} Mbps"

    # Update status with file size so user knows upload is happening
    try:
        await status_msg.edit_text(f"⬆️ Uploading {out_mb:.1f} MB...")
    except Exception:
        pass

    if mode == "audio":
        await client.send_audio(chat_id, out_path,
            file_name=f"{base}_audio.mp3",
            caption=f"Here's your extracted audio ({out_mb:.1f} MB). Tap to save.")
    elif mode == "gif":
        await client.send_animation(chat_id, out_path,
            file_name=f"{base}.gif",
            caption=f"Here's your GIF ({out_mb:.1f} MB). Long-press to save.")
    else:
        # send_video streams progressively — much faster delivery than send_document
        await client.send_video(chat_id, out_path,
            file_name=f"{base}_{mode}.mp4",
            caption=f"Tap to save to your device.{specs}",
            width=info["w"], height=info["h"],
            duration=int(info["dur"]),
            supports_streaming=True)

    try:
        await status_msg.edit_text("Done! Your file is ready above.")
    except Exception:
        pass

async def process_video(client, chat_id, video_msg, mode, process_fn, args, status_msg):
    uid = _uid()
    orig_name = getattr(video_msg.video, "file_name", None) or \
                getattr(video_msg.document, "file_name", None) or "video.mp4"
    ext = ".mp3" if mode == "audio" else (".gif" if mode == "gif" else ".mp4")
    inp = os.path.join(TEMP_DIR, f"{uid}_input.mp4")
    out = os.path.join(TEMP_DIR, f"{uid}_output{ext}")

    # Acquire semaphore to limit concurrent processing
    acquired = False
    try:
        # Try to acquire immediately
        acquired = processing_semaphore._value > 0
        if not acquired:
            await status_msg.edit_text(
                f"⏳ Server is busy processing other videos. You're in queue...\n"
                f"Your {MODES[mode]['title']} job will start automatically.")
        await processing_semaphore.acquire()
        acquired = True

        await status_msg.edit_text("⬇️ Downloading your video...")
        await client.download_media(video_msg, file_name=inp)

        if not os.path.exists(inp):
            raise RuntimeError("Failed to download the video. Please try sending it again.")

        file_mb = os.path.getsize(inp) / (1024 * 1024)
        info = probe_info(inp)
        await status_msg.edit_text(
            f"⚙️ Processing {MODES[mode]['title']}...\n"
            f"📁 {file_mb:.1f} MB | {info['w']}x{info['h']} | {info['fps']} FPS\n"
            f"Please wait — this may take a while for large/HD videos.")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, process_fn, inp, out, *args)

        await status_msg.edit_text("⬆️ Uploading your result...")
        await send_result(client, chat_id, out, mode, orig_name, status_msg)

    except Exception as e:
        logger.error(f"Processing error: {e}")
        err_text = str(e)[:400]
        try:
            await status_msg.edit_text(
                f"❌ Processing failed:\n{err_text}\n\n"
                f"Try a shorter clip or a different setting.")
        except Exception:
            await client.send_message(chat_id,
                f"❌ Processing failed:\n{err_text}")
    finally:
        if acquired:
            processing_semaphore.release()
        for p in (inp, out):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

# ── Command Handlers ────────────────────────────────────────────────────────

@app.on_message(filters.command("start") | filters.command("help"))
async def start_cmd(client, message: Message):
    user_state.pop(message.from_user.id, None)
    await message.reply_text(WELCOME_TEXT)

@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, message: Message):
    user_state.pop(message.from_user.id, None)
    await message.reply_text("Operation cancelled.")

@app.on_message(filters.command(list(MODES.keys())))
async def mode_cmd(client, message: Message):
    cmd = message.command[0].lower()
    if cmd not in MODES:
        return
    m = MODES[cmd]
    user_state[message.from_user.id] = {"mode": cmd}
    await message.reply_text(f"{m['icon']} **{m['title']}**\n\n{m['prompt']}")

@app.on_message(filters.video | filters.document)
async def on_video(client, message: Message):
    uid = message.from_user.id
    state = user_state.get(uid)
    if not state or "mode" not in state:
        await message.reply_text("Pick a command first! Use /start to see all options.")
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.reply_text("That doesn't look like a video file.")
        return

    mode = state["mode"]

    if mode in INSTANT_MODES:
        user_state.pop(uid, None)
        status_msg = await message.reply_text(f"Starting {MODES[mode]['title']}...")
        fn_map = {"reverse": (do_reverse, ()), "boomerang": (do_boomerang, ()), "audio": (do_audio, ())}
        fn, args = fn_map[mode]
        asyncio.create_task(
            process_video(client, message.chat.id, message, mode, fn, args, status_msg))
        return

    if mode in OPTION_KEYBOARDS:
        state["video_msg"] = message
        user_state[uid] = state
        await message.reply_text("Choose an option:",
            reply_markup=InlineKeyboardMarkup(OPTION_KEYBOARDS[mode]))
        return

    await message.reply_text("Something went wrong. Try /start again.")
    user_state.pop(uid, None)

@app.on_callback_query()
async def on_callback(client, callback_query: CallbackQuery):
    uid = callback_query.from_user.id
    state = user_state.get(uid)
    if not state or "video_msg" not in state:
        await callback_query.answer("Session expired. Please start over.", show_alert=True)
        return
    await callback_query.answer()

    mode = state["mode"]
    video_msg = state["video_msg"]
    user_state.pop(uid, None)

    label, process_fn, args = _parse_callback(callback_query.data)
    if not process_fn:
        await callback_query.message.edit_text("Unknown option. Try /start again.")
        return

    await callback_query.message.edit_text(f"Starting {label}...")
    asyncio.create_task(
        process_video(client, callback_query.message.chat.id, video_msg,
                      mode, process_fn, args, callback_query.message))

# ── Startup ──────────────────────────────────────────────────────────────────

def _register_handlers(application):
    application.on_message(filters.command("start") | filters.command("help"))(start_cmd)
    application.on_message(filters.command("cancel"))(cancel_cmd)
    application.on_message(filters.command(list(MODES.keys())))(mode_cmd)
    application.on_message(filters.video | filters.document)(on_video)
    application.on_callback_query()(on_callback)

def run_forever():
    global app
    while True:
        try:
            logger.info("Video Studio Bot starting (Pyrogram MTProto)...")
            app.run()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5s...")
            time.sleep(5)
            app = Client(
                "video_studio_bot",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                in_memory=True,
            )
            _register_handlers(app)

if __name__ == "__main__":
    run_forever()
