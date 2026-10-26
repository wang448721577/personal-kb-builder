from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..config import Config
from ..textutil import clean_title
from ..utils import find_ffmpeg, find_ffprobe, quick_hash
from .base import Block, ParsedDoc


def probe_media(path: Path) -> dict:
    ffprobe = find_ffprobe()
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout.decode("utf-8", errors="ignore"))
    except Exception:
        return {}


def parse_media(path: Path, cfg: Config) -> ParsedDoc:
    info = probe_media(path)
    fmt = info.get("format") or {}
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        pass
    width = height = 0
    codec = ""
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video":
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            codec = str(stream.get("codec_name") or "")
            break

    minutes = duration / 60
    text = f"音视频：{path.stem}"
    if duration:
        text += f"\n时长：{int(duration // 60)}分{int(duration % 60)}秒"
    if width and height:
        text += f"\n分辨率：{width} x {height}"
    if codec:
        text += f"\n编码：{codec}"

    meta = {
        "duration_sec": round(duration, 1),
        "width": width,
        "height": height,
        "codec": codec,
        "transcribe": bool(cfg.audio.enabled and duration and minutes <= cfg.audio.max_duration_min),
    }
    if cfg.audio.enabled and duration and minutes > cfg.audio.max_duration_min:
        meta["transcribe_skip"] = f"超过 {cfg.audio.max_duration_min} 分钟限制"

    return ParsedDoc(title=clean_title(path.stem, path.name), page_count=1,
                     blocks=[Block(text=text, kind="media", title_path="音视频信息")],
                     meta=meta, parser="media")


_whisper_models: dict[str, object] = {}


def _get_whisper(cfg: Config):
    from faster_whisper import WhisperModel
    key = f"{cfg.audio.model}_{cfg.audio.compute_type}"
    model = _whisper_models.get(key)
    if model is None:
        model = WhisperModel(
            cfg.audio.model,
            device="cpu",
            compute_type=cfg.audio.compute_type,
            download_root=str(cfg.model_dir / "whisper"),
            cpu_threads=max(2, min(4, cfg.workers)),
        )
        _whisper_models[key] = model
    return model


def extract_audio(path: Path, cfg: Config) -> Path | None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    out_dir = cfg.cache_dir / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{quick_hash(path)}.wav"
    if out.exists() and out.stat().st_size > 1024:
        return out
    cmd = [ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=7200,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    return out if out.exists() and out.stat().st_size > 1024 else None


def _read_wav(path: Path):
    import wave
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise RuntimeError("音频格式不是 16k/mono/16bit WAV")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _parse_vtt(path: Path) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    start = end = None
    cur_text: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("Kind:") \
                or line.startswith("Language:"):
            continue
        if "-->" in line:
            if start is not None and cur_text:
                out.append(("\n".join(cur_text), start, end if end is not None else start))
            try:
                a, b = line.split("-->")[:2]
                start = _vtt_time(a.strip())
                end = _vtt_time(b.strip())
            except Exception:
                start = end = None
            cur_text = []
            continue
        if start is None:
            continue
        cur_text.append(line)
    if start is not None and cur_text:
        out.append(("\n".join(cur_text), start, end if end is not None else start))
    return out


def _vtt_time(s: str) -> int:
    parts = s.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        h, m, sec = 0, int(parts[0]), float(parts[1])
    return int((h * 3600 + m * 60 + sec) * 1000)


def _transcribe_gpu(path: Path, cfg: Config) -> list[tuple[str, int, int]] | None:
    if (cfg.audio.device or "gpu").lower() != "gpu":
        return None
    exe = Path(__file__).resolve().parents[2] / "bin" / "whisper-gpu" / "main.exe"
    model = cfg.model_dir / "whisper" / "ggml-small.bin"
    if not exe.exists() or not model.exists():
        return None
    lang = cfg.audio.language if cfg.audio.language not in ("", "auto") else "zh"
    from .. import devices
    with devices.slot("gpu", getattr(cfg, "gpu_percent", 10.0), est=5.0):
        try:
            subprocess.run(
                [str(exe), "-gpu", "0", "-m", str(model), "-l", lang, "-t", "4",
                 "-ovtt", "-otxt", str(path)],
                capture_output=True, timeout=7200,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return None
    vtt = path.with_suffix(".vtt")
    if not vtt.exists():
        return None
    segs = _parse_vtt(vtt)
    vtt.unlink(missing_ok=True)
    path.with_suffix(".txt").unlink(missing_ok=True)
    if not segs:
        return None
    filtered: list[tuple[str, int, int]] = []
    seen: dict[str, int] = {}
    for text, start, end in segs:
        if not text.strip():
            continue
        if filtered and filtered[-1][0] == text:
            continue
        n = seen.get(text, 0) + 1
        seen[text] = n
        cap = 5 if len(text) >= 3 else 15
        if n > cap:
            continue
        filtered.append((text, start, end))
    return filtered or None


def transcribe_media(path: Path, cfg: Config) -> list[tuple[str, int, int]]:
    source = extract_audio(path, cfg)
    if source is None:
        raise RuntimeError("无法提取音频（缺少 ffmpeg 或音频轨）")
    gpu = _transcribe_gpu(source, cfg)
    if gpu is not None:
        segs = gpu
    else:
        model = _get_whisper(cfg)
        language = None if cfg.audio.language in ("", "auto") else cfg.audio.language
        audio = _read_wav(source)
        segs, _info = model.transcribe(
            audio,
            language=language,
            vad_filter=cfg.audio.vad,
            beam_size=2,
            condition_on_previous_text=False,
        )
        segs = [(seg.text.strip(), int(seg.start * 1000), int(seg.end * 1000))
                for seg in segs if (seg.text or "").strip()]
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    start = end = 0
    chars = 0
    for text, t0, t1 in segs:
        if not text:
            continue
        if not buf:
            start = t0
        buf.append(text)
        end = t1
        chars += len(text)
        if chars >= 500 or (end - start) >= 90000:
            out.append(("\n".join(buf), start, end))
            buf, chars = [], 0
    if buf:
        out.append(("\n".join(buf), start, end))
    try:
        source.unlink(missing_ok=True)
    except Exception:
        pass
    return out
