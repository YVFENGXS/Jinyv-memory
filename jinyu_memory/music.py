# -*- coding: utf-8 -*-
"""本地音乐分析：ffmpeg 解码 + numpy 频谱特征，给模型一份可追溯的测量报告。

思路借鉴 dsh-soundcheck：
- 只报测得到的东西（时长、响度、峰值、BPM 候选、调性候选、能量走势、频段比例）
- 推断单独标「推测」，听不出来的直接写进「局限」，不编乐器、情绪和人声
- 默认短报告，detail 才展开候选与变化点，避免一次工具调用吃掉太多 token

不做分轨、不做 ASR、不做音符级转谱。分析在本机完成，音频不上传。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

try:
    import numpy as np
except Exception:                     # noqa: BLE001  没装 numpy 时仍可导入本模块
    np = None

SR = 22050                 # 分析采样率：覆盖到 11 kHz，又不至于让 10 分钟音频爆内存
FRAME = 2048               # 约 93 ms
HOP = 1024                 # 约 46 ms
MAX_RESULT = 780           # 中间件对工具结果硬截断 800 字，留一点余量
MAX_SECTION = 120          # 片段一次最多分析 120 秒
EPS = 1e-12

_AUDIO_EXT = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".amr", ".aiff", ".aif", ".ape",
}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_CACHE = Path(os.environ.get("RP_MUSIC_DIR") or "data/music_cache").expanduser()
_CACHE_LOCK = threading.RLock()
_QUOTA = {"day": "", "n": 0}
_CLEANED = 0.0


class MusicError(Exception):
    """可直接回给模型的失败原因。"""


# ---------------------------------------------------------------- 配置与缓存

def configure(cache_dir: Path) -> None:
    """由插件在启动时指定缓存目录（与 RP_DB 同目录）。"""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = Path(cache_dir).expanduser()


def available() -> str:
    """返回空字符串表示可用，否则返回缺什么。"""
    if np is None:
        return "音乐分析引擎未安装：缺 numpy（在机器人 venv 里装一下 numpy）"
    if not _exe("ffmpeg"):
        return "音乐分析引擎不完整：缺 ffmpeg；现在只能分析 WAV"
    return ""


def _max_seconds() -> int:
    try:
        return max(10, int(float(os.environ.get("RP_MUSIC_MAX_SECONDS") or 600)))
    except (TypeError, ValueError):
        return 600


def _max_bytes() -> int:
    try:
        return max(5, int(float(os.environ.get("RP_MUSIC_MAX_MB") or 80))) * 1024 * 1024
    except (TypeError, ValueError):
        return 80 * 1024 * 1024


def _daily() -> int:
    try:
        return max(1, int(float(os.environ.get("RP_MUSIC_DAILY") or 10)))
    except (TypeError, ValueError):
        return 10


def _cleanup() -> None:
    """顺手清掉三天前的缓存，别让歌曲越堆越多。"""
    global _CLEANED
    now = time.time()
    if now - _CLEANED < 3600:
        return
    _CLEANED = now
    try:
        for p in _CACHE.iterdir():
            if p.is_file() and now - p.stat().st_mtime > 3 * 86400:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def _audio_ext(data: bytes) -> str:
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"fLaC"):
        return ".flac"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".m4a"
    if data.startswith(b"#!AMR"):
        return ".amr"
    return ".bin"


def save_bytes(data: bytes, prefix: str = "audio") -> Path:
    """把 QQ 语音/文件字节落到缓存目录，返回路径。同一内容复用同一文件。"""
    if not data:
        raise MusicError("音频内容为空")
    if len(data) > _max_bytes():
        raise MusicError(f"音频超过 {_max_bytes() // 1024 // 1024} MB，先截短再发")
    with _CACHE_LOCK:
        _CACHE.mkdir(parents=True, exist_ok=True)
        _cleanup()
        name = f"{prefix}-{hashlib.sha1(data).hexdigest()[:16]}{_audio_ext(data)}"
        path = _CACHE / name
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return path


# ---------------------------------------------------------------- 来源解析

def _exe(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if not sys.executable:
        return ""
    base = Path(sys.executable)
    for candidate in (base.with_name(name), base.with_name(name + ".exe")):
        if candidate.is_file():
            return str(candidate)
    return ""


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _check_public(url: str) -> None:
    try:
        u = urlparse(url)
    except ValueError:
        raise MusicError("链接格式不对")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise MusicError("只支持 http/https 链接")
    host = u.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain", "metadata.google.internal"):
        raise MusicError("这个地址不允许访问")
    try:
        ip = ipaddress.ip_address(host)
        addrs = {str(ip)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            raise MusicError("链接解析不到主机")
        addrs = {info[4][0] for info in infos}
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise MusicError("链接地址不合法")
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise MusicError("这个地址不允许访问")


def _take_quota() -> None:
    today = date.today().isoformat()
    with _CACHE_LOCK:
        if _QUOTA["day"] != today:
            _QUOTA.update(day=today, n=0)
        if _QUOTA["n"] >= _daily():
            raise MusicError(f"今天下载分析的链接歌曲已到上限（{_daily()} 首），明天再听")
        _QUOTA["n"] += 1


def _cached_url(url: str) -> Optional[Path]:
    key = _url_hash(url)
    with _CACHE_LOCK:
        _CACHE.mkdir(parents=True, exist_ok=True)
        for p in _CACHE.glob(f"dl-{key}.*"):
            if p.is_file() and p.stat().st_size > 0 and p.suffix != ".part":
                return p
    return None


def _looks_audio(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _AUDIO_EXT)


def _save_download(data: bytes, url: str) -> Path:
    """按链接而不是内容存放：同一 URL 再问一次可直接命中缓存。"""
    with _CACHE_LOCK:
        _CACHE.mkdir(parents=True, exist_ok=True)
        _cleanup()
        path = _CACHE / f"dl-{_url_hash(url)}{_audio_ext(data)}"
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return path


def _direct_download(url: str) -> Path:
    """直链下载；手动跟 3 次跳转，每一跳都重新做公网校验。"""
    try:
        import httpx
    except Exception:                        # noqa: BLE001
        raise MusicError("下载组件缺失：httpx 没装")
    target = url
    for _ in range(4):
        _check_public(target)
        try:
            with httpx.stream("GET", target, headers={"User-Agent": _UA},
                              timeout=httpx.Timeout(60, connect=15),
                              follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        raise MusicError("下载跳转缺少地址")
                    target = urljoin(target, loc)
                    continue
                if resp.status_code != 200:
                    raise MusicError(f"下载失败（HTTP {resp.status_code}）")
                ctype = (resp.headers.get("content-type") or "").lower()
                if "text/html" in ctype:
                    raise MusicError("这是网页不是音频文件")
                declared = resp.headers.get("content-length") or "0"
                try:
                    if int(declared) > _max_bytes():
                        raise MusicError(f"音频超过 {_max_bytes() // 1024 // 1024} MB")
                except ValueError:
                    pass
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > _max_bytes():
                        raise MusicError(f"音频超过 {_max_bytes() // 1024 // 1024} MB")
                if not data:
                    raise MusicError("下载到空文件")
                return _save_download(bytes(data), url)
        except MusicError:
            raise
        except Exception as exc:             # noqa: BLE001
            raise MusicError(f"下载失败：{exc}") from exc
    raise MusicError("下载跳转次数过多")


def save_url(url: str) -> Path:
    """下载已知的音频直链（如 QQ 文件 url）到缓存，不再尝试网页解析。"""
    return _direct_download(url)


def _bili_cookie() -> str:
    """B站风控要登录态：复用机器人已有的 /bili login 凭据，未登录时为空。"""
    try:
        import bili_live
        return bili_live.cookie_header()
    except Exception:                        # noqa: BLE001  未登录/模块不可用
        return ""


def _ytdlp_cmd() -> list:
    exe = _exe("yt-dlp")
    if exe:
        return [exe]
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, timeout=10, check=True)
    except Exception:                        # noqa: BLE001
        return []
    return [sys.executable, "-m", "yt_dlp"]


def _ytdlp_download(url: str) -> Path:
    base = _ytdlp_cmd()
    if not base:
        raise MusicError("这个链接需要 yt-dlp，服务器上没装")
    key = _url_hash(url)
    out = _CACHE / f"dl-{key}.%(ext)s"
    cmd = base + [
        "-f", "bestaudio/best", "--no-playlist", "--no-warnings",
        "--socket-timeout", "15", "--max-filesize", str(_max_bytes()),
        "-o", str(out),
    ]
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(("bilibili.com", "b23.tv")):
        cookie = _bili_cookie()
        if cookie:
            cmd += ["--add-header", "Cookie: " + cookie]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MusicError(f"下载失败：{exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["未知原因"]
        raise MusicError("这个链接没下来：" + tail[0][:120])
    hits = [p for p in _CACHE.glob(f"dl-{key}.*")
            if p.is_file() and p.suffix != ".part"]
    if not hits:
        raise MusicError("下载完成但找不到音频文件")
    path = max(hits, key=lambda p: p.stat().st_size)
    if path.stat().st_size > _max_bytes():
        path.unlink(missing_ok=True)
        raise MusicError(f"音频超过 {_max_bytes() // 1024 // 1024} MB")
    return path


def _download(url: str) -> Path:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("music.163.com") or host.endswith("y.qq.com"):
        raise MusicError("网易云/QQ音乐链接不能直接下：先把歌曲下载成本地文件，"
                         "或发 B站/YouTube/直链音频")
    cached = _cached_url(url)
    if cached:
        return cached
    _take_quota()
    if _looks_audio(url):
        try:
            return _direct_download(url)
        except MusicError:
            return _ytdlp_download(url)
    try:
        return _ytdlp_download(url)
    except MusicError:
        return _direct_download(url)


def resolve(source: str = "", fallback: str = "") -> Tuple[Path, str]:
    """把 source / 本条消息的音频 / 上一首 解析成可读的本地文件。"""
    src = str(source or "").strip()
    if not src:
        p = Path(str(fallback or "")).expanduser()
        if p.is_file():
            return p, ""
        raise MusicError("这条消息里没有音频；发一段音频/文件，或给出可下载的歌曲链接")
    if src.startswith(("http://", "https://")):
        return _download(src), ""
    p = Path(src).expanduser()
    if not p.is_file():
        raise MusicError("找不到这个本地文件")
    if p.stat().st_size > _max_bytes():
        raise MusicError(f"音频超过 {_max_bytes() // 1024 // 1024} MB")
    return p, ""


# ---------------------------------------------------------------- 解码

def _parse_time(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return max(0.0, float(text))
        parts = text.split(":")
        if len(parts) == 2:
            return max(0.0, float(parts[0]) * 60 + float(parts[1]))
        if len(parts) == 3:
            return max(0.0, float(parts[0]) * 3600
                       + float(parts[1]) * 60 + float(parts[2]))
    except ValueError:
        raise MusicError("时间要写成秒数或 1:30")
    raise MusicError("时间要写成秒数或 1:30")


def _fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    whole = int(round(sec))
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _probe(path: Path) -> dict:
    ffprobe = _exe("ffprobe")
    if ffprobe:
        cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
               "-show_entries", "format=duration,bit_rate:stream=sample_rate,channels,codec_name",
               "-of", "json", str(path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            try:
                data = json.loads(proc.stdout or "{}")
                stream = (data.get("streams") or [{}])[0]
                fmt = data.get("format") or {}
                return {
                    "duration": float(fmt.get("duration") or 0),
                    "sample_rate": int(stream.get("sample_rate") or 0),
                    "channels": int(stream.get("channels") or 0),
                    "codec": str(stream.get("codec_name") or ""),
                    "bit_rate": int(float(fmt.get("bit_rate") or 0)),
                }
            except (ValueError, TypeError, IndexError):
                pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                return {"duration": w.getnframes() / max(1, w.getframerate()),
                        "sample_rate": w.getframerate(), "channels": w.getnchannels(),
                        "codec": "pcm", "bit_rate": 0}
        except (OSError, wave.Error):
            pass
    raise MusicError("读不出音频信息：文件损坏，或服务器缺 ffprobe")


def _wav_fallback(path: Path, start: float, length: float) -> Optional[object]:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            total = w.getnframes()
            lo = min(total, max(0, int(start * rate)))
            hi = total if length <= 0 else min(total, lo + int(length * rate))
            raw = w.readframes(max(0, hi - lo))
            dtype = {1: "u1", 2: "<i2", 4: "<i4"}.get(width)
            if dtype is None:
                return None
            arr = np.frombuffer(raw, dtype=dtype).astype(np.float32)
            if dtype == "u1":
                arr = (arr - 128.0) / 128.0
            else:
                arr /= float(2 ** (8 * width - 1))
            if channels > 1:
                arr = arr.reshape(-1, channels).mean(axis=1)
            if rate != SR and arr.size:
                target = max(1, int(arr.size * SR / rate))
                x_old = np.linspace(0, arr.size - 1, num=arr.size)
                x_new = np.linspace(0, arr.size - 1, num=target)
                arr = np.interp(x_new, x_old, arr).astype(np.float32)
            return arr
    except (OSError, wave.Error):
        return None


def _decode(path: Path, start: float, length: float) -> object:
    if np is None:
        raise MusicError("音乐分析引擎未安装：缺 numpy")
    ffmpeg = _exe("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-v", "error", "-nostdin"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(path)]
        if length > 0:
            cmd += ["-t", f"{length:.3f}"]
        cmd += ["-vn", "-ac", "1", "-ar", str(SR),
                "-f", "s16le", "pipe:1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MusicError(f"解码失败：{exc}") from exc
        if proc.returncode != 0 or not proc.stdout:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").strip()
            raise MusicError("解码失败：" + (tail.splitlines()[-1][:120] if tail else "没有音频流"))
        arr = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        if arr.size == 0:
            raise MusicError("解码后没有采样")
        return arr
    arr = _wav_fallback(path, start, length)
    if arr is None:
        raise MusicError("服务器缺 ffmpeg，只能分析 WAV；这个格式听不了")
    if arr.size == 0:
        raise MusicError("片段里没有采样")
    return arr


# ---------------------------------------------------------------- 测量

# Krumhansl-Kessler 调性模板（C 为第 0 位）：K-S 算法原文数值
_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_BANDS = [(30, 250), (250, 800), (800, 2500), (2500, 6000), (6000, 12000)]
_BAND_NAME = ["低", "中低", "中高", "高", "极高"]


def _db(value: float) -> float:
    return 20.0 * math.log10(max(EPS, value))


def _tempo(env, fps: float) -> List[Tuple[float, float]]:
    """起音包络自相关，返回 (BPM, 0-1 可信) 候选。"""
    n = len(env)
    if n < fps * 3:
        return []
    x = np.asarray(env, dtype=np.float64)
    x = x - x.mean()
    if float(np.std(x)) < 1e-6:
        return []
    size = 1
    while size < 2 * n:
        size <<= 1
    spec = np.fft.rfft(x, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:n]
    if ac[0] <= 0:
        return []
    ac = ac / ac[0]
    lo = max(2, int(round(fps * 60 / 200)))
    hi = min(n - 1, int(round(fps * 60 / 40)))
    if hi <= lo:
        return []
    seg = ac[lo:hi + 1]
    peaks = []
    for i in range(1, len(seg) - 1):
        if seg[i] > seg[i - 1] and seg[i] >= seg[i + 1] and seg[i] > 0.05:
            peaks.append((float(seg[i]), i + lo))
    if not peaks:
        best = int(np.argmax(seg)) + lo
        peaks = [(float(seg[best - lo]), best)]
    peaks.sort(reverse=True)
    out = []
    for score, lag in peaks:
        bpm = 60.0 * fps / lag
        if all(abs(bpm - b) > max(3.0, b * 0.03) for b, _ in out):
            out.append((bpm, min(1.0, max(0.0, score))))
        if len(out) >= 3:
            break
    return out


def _key(chroma) -> List[Tuple[str, float]]:
    total = float(np.sum(chroma))
    if total <= 0:
        return []
    c = np.asarray(chroma, dtype=np.float64) / total
    c = c - c.mean()
    hits = []
    for tonic in range(12):
        for mode, profile in (("大调", _MAJOR), ("小调", _MINOR)):
            p = np.roll(np.asarray(profile, dtype=np.float64), tonic)
            p = p - p.mean()
            denom = float(np.linalg.norm(c) * np.linalg.norm(p))
            score = float(np.dot(c, p) / denom) if denom > 0 else 0.0
            hits.append((f"{_NOTE[tonic]} {mode}", score))
    hits.sort(key=lambda x: x[1], reverse=True)
    out = []
    for name, score in hits:
        if all(name != n for n, _ in out):
            out.append((name, score))
        if len(out) >= 3:
            break
    return out


def _features(samples) -> dict:
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0:
        raise MusicError("没有采样")
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    peak = float(np.max(np.abs(x)))
    clip = float(np.mean(np.abs(x) >= 0.999)) * 100.0

    win = np.hanning(FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME)[::HOP]
    if len(frames) == 0:
        return {"rms_db": _db(rms), "peak_db": _db(peak), "clip": clip,
                "duration": len(x) / SR, "bands": [], "chroma": np.zeros(12),
                "tempo": [], "blocks": [], "block_sec": 16,
                "centroid": 0.0, "onset_rate": 0.0}
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)
    valid = (freqs >= 55) & (freqs <= 2000)
    pcs = ((np.floor(12 * np.log2(np.maximum(freqs, 1) / 440.0) + 69.0 + 0.5)).astype(int)) % 12
    pc_bins = [np.where(valid & (pcs == k))[0] for k in range(12)]
    band_bins = [np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in _BANDS]

    chroma = np.zeros(12, dtype=np.float64)
    band_power = np.zeros(len(_BANDS), dtype=np.float64)
    flux_all = []
    centroid_sum = 0.0
    centroid_n = 0
    prev_mag = None
    for i in range(0, len(frames), 512):
        mag = np.abs(np.fft.rfft(frames[i:i + 512] * win, axis=1))
        if prev_mag is not None:
            flux_all.append(float(np.maximum(mag[0] - prev_mag, 0).sum()))
        if len(mag) > 1:
            flux_all.extend(np.maximum(mag[1:] - mag[:-1], 0).sum(axis=1).tolist())
        # 只让能量足够高的帧参与音高/频段统计：脉冲之间的静音帧只会摊薄色度
        frame_energy = mag.sum(axis=1)
        hot = frame_energy >= 0.25 * float(frame_energy.max() or 0.0)
        use = mag[hot]
        if use.size:
            # 压掉 10% 以下的旁瓣，避免短促音符的频谱泄漏串到邻近音级
            tones = np.where(use >= 0.1 * use.max(axis=1, keepdims=True), use, 0.0)
            for k, bins in enumerate(pc_bins):
                if bins.size:
                    chroma[k] += float(tones[:, bins].sum())
            for k, bins in enumerate(band_bins):
                if bins.size:
                    band_power[k] += float(np.square(use[:, bins], dtype=np.float64).sum())
            tone_sum = tones.sum(axis=1)
            lit = tone_sum > 0
            if lit.any():
                centroid_sum += float(((tones * freqs).sum(axis=1)[lit]
                                       / tone_sum[lit]).sum())
                centroid_n += int(lit.sum())
        prev_mag = mag[-1]

    env = np.asarray(flux_all, dtype=np.float64)
    if env.size:
        env = np.maximum(env, 0.0)
        env = np.convolve(env, np.ones(3) / 3.0, mode="same")
    fps = SR / HOP
    tempo = _tempo(env, fps)
    onset_rate = 0.0
    if env.size and float(env.max()) > 0:
        thresh = 0.3 * float(env.max())
        peaks = int(np.sum(env > thresh))
        onset_rate = peaks / (len(env) / fps)

    # 走势块：长音频用 16 秒，短音频自动缩小，避免只有一块时看不出变化
    dur = len(x) / SR
    block = (16 if dur >= 32 else 4 if dur >= 8 else 2) * SR
    blocks = []
    for i in range(0, len(x), block):
        chunk = x[i:i + block]
        if chunk.size < SR:             # 最后不足 1 秒的尾巴忽略
            continue
        blocks.append((i / SR, _db(float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64)))))))

    total_power = float(band_power.sum())
    bands = [(bands_name, float(power / total_power * 100))
             for bands_name, power in zip(_BAND_NAME, band_power)] if total_power > 0 else []

    return {
        "rms_db": _db(rms), "peak_db": _db(peak), "clip": clip,
        "duration": len(x) / SR, "bands": bands, "chroma": chroma,
        "tempo": tempo, "blocks": blocks, "block_sec": block // SR,
        "centroid": centroid_sum / max(1, centroid_n), "onset_rate": onset_rate,
    }


# ---------------------------------------------------------------- 报告

def _level(f: dict) -> str:
    bits = []
    rms = f["rms_db"]
    bits.append("很满" if rms > -12 else "偏响" if rms > -18
                else "中等" if rms > -24 else "偏轻")
    if f["tempo"]:
        bpm = f["tempo"][0][0]
        bits.append("快" if bpm >= 140 else "中速" if bpm >= 100
                    else "偏慢" if bpm >= 70 else "慢")
    if f["onset_rate"]:
        bits.append("起音密" if f["onset_rate"] >= 3 else
                    "起音适中" if f["onset_rate"] >= 1 else "起音疏")
    if f["centroid"]:
        bits.append("偏亮" if f["centroid"] >= 1500
                    else "中等亮度" if f["centroid"] >= 500 else "偏暗")
    return "、".join(bits)


def _blocks_line(blocks, detail: bool, block_sec: int = 16) -> str:
    if not blocks:
        return "没有足够的分析块"
    loud = max(blocks, key=lambda b: b[1])
    quiet = min(blocks, key=lambda b: b[1])
    diffs = [(blocks[i][0], blocks[i][1] - blocks[i - 1][1])
             for i in range(1, len(blocks))]
    biggest = max(diffs, key=lambda d: abs(d[1])) if diffs else None
    tail = ""
    if len(blocks) >= 2:
        delta = blocks[-1][1] - blocks[-2][1]
        if abs(delta) >= 0.5:
            tail = f"；结尾比前一段{'低' if delta < 0 else '高'} {abs(delta):.1f} dB"
    line = (f"{block_sec} 秒块：最满 {_fmt_time(loud[0])}（{loud[1]:.1f} dB），"
            f"最空 {_fmt_time(quiet[0])}（{quiet[1]:.1f} dB）")
    if biggest and abs(biggest[1]) >= 0.5:
        line += f"，最大{'落差' if biggest[1] < 0 else '跃升'} {abs(biggest[1]):.1f} dB"
    line += tail
    if detail and diffs:
        picks = sorted(diffs, key=lambda d: abs(d[1]), reverse=True)[:4]
        if any(abs(d) >= 0.5 for _, d in picks):
            more = "；".join(f"{_fmt_time(t)} {d:+.1f} dB"
                             for t, d in picks if abs(d) >= 0.5)
            line += f"；变化点：{more}"
    return line


def _report(meta: dict, f: dict, start: float, length: float,
            notes: List[str], detail: bool, brief: bool) -> str:
    lines = []
    total = meta.get("duration") or f["duration"]
    sr = meta.get("sample_rate") or SR
    head = f"[测量] 全曲 {_fmt_time(total)} · {sr / 1000:.1f} kHz"
    if start > 0 or length < total:
        head += f" · 分析 {_fmt_time(start)}–{_fmt_time(start + length)}"
    head += (f" · RMS {f['rms_db']:.1f} dBFS · 峰值 {f['peak_db']:.1f} dBFS"
             f" · 贴顶 {f['clip']:.1f}%")
    lines.append(head)

    if f["tempo"]:
        top = f["tempo"][0]
        alt = ""
        for bpm, _ in f["tempo"][1:]:
            # 半速/倍速是同一拍层的另一种读法，不当成「另一层」重复报
            if any(abs(bpm - top[0] * m) <= max(3.0, top[0] * m * 0.03)
                   for m in (0.5, 2.0, 3.0, 1.0 / 3.0)):
                continue
            if abs(bpm - top[0]) > 3:
                alt = f"，另一层 {bpm:.0f}"
                break
        bpm_line = f"[速度] 约 {top[0]:.0f} BPM{alt}（可信 {top[1]:.2f}）"
        if detail and len(f["tempo"]) > 2:
            bpm_line += "；候选：" + " / ".join(f"{b:.0f}" for b, _ in f["tempo"])
        lines.append(bpm_line)
    else:
        lines.append("[速度] 没有稳定的节拍（可能是纯人声、环境声或自由速度）")

    if f["chroma"].sum() > 0:
        keys = _key(f["chroma"])
        if keys:
            key_line = "[调性] 候选 " + " / ".join(f"{n} {s:.2f}" for n, s in keys)
            if keys[0][1] < 0.45:
                key_line += "（不明确）"
            if detail:
                key_line += "；只到调式，不给和弦与转位"
            lines.append(key_line)
        else:
            lines.append("[调性] 调性不明确")
    else:
        lines.append("[调性] 没有足够的音高信息")

    lines.append("[走势] " + _blocks_line(f["blocks"], detail,
                                           f.get("block_sec", 16)))

    if f["bands"]:
        lines.append("[频谱] " + " / ".join(f"{n} {v:.0f}%" for n, v in f["bands"]))

    if not brief:
        guess = _level(f)
        lines.append(f"[推测] {guess}；情绪、场景与语义联想没有测量依据")
        lines.append("[局限] 无分轨模型：不判断有没有人唱、也认不出具体乐器；"
                     "调性与 BPM 是候选，旋律只有轮廓")
    else:
        lines.append("[局限] 无分轨/人声判定；具体乐器与音符级旋律测不到")

    if notes:
        lines.append("；".join(notes))
    text = "\n".join(lines)
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT - 1] + "…"
    return text


def analyze_file(path: Path, start: str = "", end: str = "",
                 detail: bool = False, brief: bool = False) -> str:
    """主入口：返回给模型的中文报告。"""
    if np is None:
        raise MusicError("音乐分析引擎未安装：缺 numpy")
    path = Path(path)
    meta = _probe(path)
    total = float(meta.get("duration") or 0)
    t0 = _parse_time(start)
    t1 = _parse_time(end)
    notes = []
    if t1 > 0 and t1 <= t0:
        raise MusicError("end 要大于 start")
    if t0 > 0 or t1 > 0:
        length = (t1 - t0) if t1 > 0 else max(0.0, total - t0)
        if total > 0:
            length = min(length, max(0.0, total - t0))
        if length > MAX_SECTION:
            notes.append(f"片段超过 {MAX_SECTION} 秒，只分析前 {MAX_SECTION} 秒")
            length = MAX_SECTION
        notes.append("片段 BPM 可能锁到别的拍层")
    else:
        length = total
        cap = _max_seconds()
        if length > cap:
            notes.append(f"超过 {cap // 60} 分钟，只分析前 {_fmt_time(cap)}")
            length = cap
    if length <= 0:
        if total <= 0:
            raise MusicError("音频时长读不出来")
        raise MusicError("这段位置超出音频时长")
    samples = _decode(path, t0, length)
    f = _features(samples)
    return _report(meta, f, t0, length, notes, detail, brief)


def analyze(source: str = "", fallback: str = "", start: str = "",
            end: str = "", detail: bool = False, brief: bool = False) -> str:
    """解析来源并分析；给工具层用的一步式入口。"""
    path, note = resolve(source, fallback)
    text = analyze_file(path, start=start, end=end, detail=detail, brief=brief)
    return f"{note}\n{text}" if note else text
