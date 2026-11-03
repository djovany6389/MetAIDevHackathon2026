"""Closed captioning via Muse Voice Transcribe.

Pipeline per uploaded asset file:
  1. audio -> mono 16-bit PCM at 16 kHz (stdlib wave/array for WAV,
     ffmpeg when available for compressed containers)
  2. PCM split into <=10 minute chunks (Meta file limit)
  3. each chunk -> POST https://api.meta.ai/v1/asr/transcribe
  4. transcript segments merged with time offsets -> WebVTT

Only stdlib is used. The Meta Model API key comes from META_API_KEY.
"""

import array
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import wave

TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"
TRANSCRIBE_MODE = "DIARIZATION"
SAMPLE_RATE = 16000
CHUNK_SECONDS = 600
MAX_MINUTES = 180


class CaptionError(Exception):
    pass


def vtt_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return "%02d:%02d:%06.3f" % (hours, mins, secs)


def segments_to_vtt(segments):
    """[{start, end, speaker, text}] -> WebVTT document."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        text = " ".join(str(seg.get("text", "")).split())
        if not text:
            continue
        text = text.replace("-->", "->")
        lines.append("%s --> %s" % (vtt_timestamp(start), vtt_timestamp(end)))
        lines.append(text)
        lines.append("")
    return "\n".join(lines) + "\n"


# 8-bit WAV samples are unsigned; map them to signed 8-bit first.
_XOR128 = bytes(b ^ 0x80 for b in range(256))


def _to_mono16(frames, nchan, width):
    """Raw PCM frames -> array('h') mono 16-bit (channel 0 kept).

    Byte-slice operations only, so this runs at C speed. WAV is
    little-endian, matching every realistic host.
    """
    if sys.byteorder != "little":
        raise CaptionError("only little-endian hosts are supported")
    if width == 1:
        ch0 = frames[0::nchan] if nchan > 1 else frames
        out = bytearray(2 * len(ch0))
        out[1::2] = ch0.translate(_XOR128)
        return array.array("h", bytes(out))
    if width not in (2, 3, 4):
        raise CaptionError("unsupported WAV sample width")
    stride = width * nchan
    n = len(frames) // stride
    out = bytearray(2 * n)
    out[0::2] = frames[width - 2::stride][:n]
    out[1::2] = frames[width - 1::stride][:n]
    return array.array("h", bytes(out))


def _resample(samples, in_rate, out_rate=SAMPLE_RATE):
    """array('h') mono -> resampled array('h'). Linear interpolation."""
    if in_rate == out_rate:
        return samples
    if in_rate % out_rate == 0:
        return array.array("h", samples[::in_rate // out_rate])
    n_out = int(len(samples) * out_rate / in_rate)
    step = in_rate / out_rate
    out = array.array("h", bytes(2 * n_out))
    total = len(samples)
    pos = 0.0
    for j in range(n_out):
        i = int(pos)
        if i >= total - 1:
            out[j] = samples[-1]
        else:
            frac = pos - i
            out[j] = int(samples[i] * (1.0 - frac)
                         + samples[i + 1] * frac)
        pos += step
    return out


def wav_to_pcm16_mono_16k(wav_bytes):
    """Any PCM WAV -> raw mono 16-bit 16 kHz PCM bytes. Stdlib only."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            nchan, width, rate, nframes = (w.getnchannels(),
                                           w.getsampwidth(),
                                           w.getframerate(),
                                           w.getnframes())
            frames = w.readframes(nframes)
            comptype = w.getcomptype()
    except (wave.Error, EOFError) as e:
        raise CaptionError("could not read WAV audio: %s" % e)
    if comptype != "NONE":
        raise CaptionError("compressed WAV is not supported")
    if nchan > 2:
        raise CaptionError("surround audio is not supported")
    mono = _to_mono16(frames, nchan, width)
    return _resample(mono, rate).tobytes()


def extract_pcm(asset_path, mime):
    """Asset file -> mono 16-bit 16 kHz PCM bytes.

    WAV is converted with the stdlib; anything else needs ffmpeg.
    """
    ext = os.path.splitext(asset_path)[1].lower()
    if ext == ".wav" or (mime or "").lower() in ("audio/wav", "audio/x-wav"):
        with open(asset_path, "rb") as f:
            return wav_to_pcm16_mono_16k(f.read())
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CaptionError(
            "install ffmpeg to caption %s files (WAV uploads need none)"
            % (ext or "this format"))
    out = asset_path + ".cap16k.wav"
    cmd = [ffmpeg, "-y", "-v", "error", "-i", asset_path,
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", out]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=1800, check=True)
        with open(out, "rb") as f:
            return wav_to_pcm16_mono_16k(f.read())
    except subprocess.CalledProcessError:
        raise CaptionError("ffmpeg could not read the audio track")
    except subprocess.TimeoutExpired:
        raise CaptionError("audio extraction timed out")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def chunk_pcm(pcm):
    """Raw PCM -> [(offset_seconds, chunk_bytes)] of <= CHUNK_SECONDS."""
    stride = SAMPLE_RATE * 2 * CHUNK_SECONDS
    total_minutes = len(pcm) / (SAMPLE_RATE * 2 * 60)
    if total_minutes > MAX_MINUTES:
        raise CaptionError("audio longer than %d minutes is not supported"
                           % MAX_MINUTES)
    chunks = []
    for i in range(0, max(len(pcm), 1), stride):
        offset = i / (SAMPLE_RATE * 2)
        chunks.append((offset, pcm[i:i + stride]))
    return chunks


def pcm_to_wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def build_multipart(wav_bytes):
    """(body, content_type) for the file endpoint.

    Fields follow Meta's transcribe cookbook: a `request` JSON part
    (mode/model/audioEncoding; language omitted so it auto-detects)
    plus the `audio` WAV part.
    """
    boundary = "----relaycap%x" % abs(hash((len(wav_bytes),
                                               wav_bytes[:64],
                                               wav_bytes[-64:])))
    request_part = json.dumps({"mode": TRANSCRIBE_MODE,
                               "model": TRANSCRIBE_MODEL,
                               "audioEncoding": "WAV"}).encode()
    body = b"\r\n".join([
        ("--%s" % boundary).encode(),
        b'Content-Disposition: form-data; name="request"',
        b"Content-Type: application/json",
        b"",
        request_part,
        ("--%s" % boundary).encode(),
        b'Content-Disposition: form-data; name="audio"; filename="audio.wav"',
        b"Content-Type: audio/wav",
        b"",
        wav_bytes,
        ("--%s--" % boundary).encode(),
        b""])
    return body, "multipart/form-data; boundary=%s" % boundary


def parse_segments(payload):
    """Tolerantly pull [{start, end, speaker, text}] from the response.

    The cookbook shape is `turns` with startMs/endMs plus `transcript`;
    second-based and legacy shapes pass through unchanged. A turn with
    no end runs until the next turn starts (or the audio ends).
    """
    if not isinstance(payload, dict):
        return []
    turns = payload.get("segments") or payload.get("turns") or []
    if isinstance(turns, dict):
        turns = turns.get("segments", [])
    if not isinstance(turns, list):
        return []
    try:
        total = float(payload.get("audioDurationMs")) / 1000
    except (TypeError, ValueError):
        total = None
    out = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        if "startMs" in t or "endMs" in t:
            try:
                start = float(t.get("startMs", 0)) / 1000
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(t["endMs"]) / 1000 if "endMs" in t else None
            except (TypeError, ValueError):
                end = None
        else:
            start = t.get("start", t.get("start_time", t.get("from", 0)))
            end = t.get("end", t.get("end_time", t.get("to", start)))
        speaker = t.get("speaker", t.get("speaker_label",
                                         t.get("speaker_id", "")))
        text = t.get("text", t.get("transcript", ""))
        out.append({"start": start, "end": end,
                    "speaker": speaker, "text": text})
    for i, seg in enumerate(out):
        if seg["end"] is None:
            if i + 1 < len(out):
                try:
                    seg["end"] = float(out[i + 1]["start"])
                except (TypeError, ValueError):
                    seg["end"] = seg["start"]
            elif total is not None:
                seg["end"] = total
            else:
                seg["end"] = seg["start"]
    return out


def parse_vtt_timestamp(s):
    """'HH:MM:SS.mmm' (hours optional) -> seconds. Raises ValueError."""
    parts = str(s or "").strip().split(":")
    if len(parts) == 3:
        hours, mins, secs = parts
    elif len(parts) == 2:
        hours, mins, secs = 0, parts[0], parts[1]
    else:
        raise ValueError("bad timestamp %r" % s)
    return int(hours) * 3600 + int(mins) * 60 + float(secs)


def vtt_to_cues(vtt_text):
    """Our own WebVTT -> [{start, end, speaker, text}] cue dicts.

    One-way backfill for transcripts saved before cues were stored
    as JSON. 'SPEAKER: line' payloads split back into speaker/text;
    anything unparseable is skipped, never fatal.
    """
    cues = []
    stamp, payload = None, []

    def flush():
        if stamp is None or not payload:
            return
        text = " ".join(" ".join(payload).split())
        speaker = ""
        if ": " in text:
            maybe, rest = text.split(": ", 1)
            if maybe and len(maybe) <= 40:
                speaker, text = maybe, rest
        if text:
            cues.append({"start": stamp[0], "end": max(stamp[1], stamp[0]),
                         "speaker": speaker, "text": text})

    for line in str(vtt_text or "").split("\n"):
        line = line.strip()
        if not line:
            flush()
            stamp, payload = None, []
            continue
        if "-->" in line:
            flush()
            try:
                first, second = line.split("-->", 1)
                start = parse_vtt_timestamp(first)
                end = parse_vtt_timestamp(second.split()[0])
            except (ValueError, IndexError):
                stamp = None
                continue
            stamp, payload = (start, end), []
            continue
        if stamp is None or line == "WEBVTT":
            continue
        payload.append(line)
    flush()
    return cues


def muse_transcribe(wav_bytes, api_key):
    """One WAV chunk -> segments. Raises CaptionError on failure."""
    body, content_type = build_multipart(wav_bytes)
    req = urllib.request.Request(
        TRANSCRIBE_URL, data=body, method="POST",
        headers={"Content-Type": content_type,
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise CaptionError(
            "transcription request failed: HTTP Error %s: %s%s"
            % (e.code, e.reason, ": %s" % detail if detail else ""))
    except Exception as e:
        raise CaptionError("transcription request failed: %s" % e)
    segments = parse_segments(payload)
    if not segments and isinstance(payload, dict) and payload.get("error"):
        raise CaptionError("transcription failed: %s" % payload["error"])
    return segments
