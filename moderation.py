"""Obscenity detection + scene blur via Muse Spark vision.

Pipeline per video asset:
  1. ffmpeg extracts 1 frame / FRAME_EVERY_SECONDS as JPEGs
  2. each frame -> Muse Spark verdict (safe/unsafe + category)
  3. consecutive unsafe frames -> flagged ranges (>= MIN_RANGE_SECONDS)
  4. ffmpeg boxblur over flagged ranges -> derived review copy

Only stdlib is used. The Meta Model API key comes from META_API_KEY.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

SPARK_URL = "https://api.meta.ai/v1/chat/completions"
SPARK_MODEL = "muse-spark-1.3"
FRAME_EVERY_SECONDS = 2.0
MIN_RANGE_SECONDS = 2.0
MAX_MINUTES = 180

JUDGE_PROMPT = (
    "You are a broadcast standards classifier. Look at this video frame "
    "and reply with exactly one JSON object and nothing else: "
    '{"verdict": "safe" or "unsafe", "category": "none", "nudity", '
    '"sexual-activity" or "other", "ai": "real" if the frame looks like '
    'a genuine camera recording, "ai" if it looks AI-generated or '
    'synthetic, "unsure" if you cannot tell}. "Unsafe" means the frame '
    "contains nudity, sexual activity, or other obscene visual content.")
AI_MIN_JUDGED = 3
AI_THRESHOLD = 0.5
AI_FLOOR = 0.25
AI_LIKELY = 0.65
AI_CERTAIN = 0.99


class ModerationError(Exception):
    pass


class UnreadableVerdict(ModerationError):
    """One frame's verdict text could not be parsed (fail-closed-able)."""


def extract_frames(asset_path, outdir):
    """Video -> [(timestamp, jpeg_path)] at FRAME_EVERY_SECONDS cadence."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ModerationError("install ffmpeg to scan video files")
    pattern = os.path.join(outdir, "frame_%06d.jpg")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", asset_path,
           "-vf", "fps=1/%.0f" % FRAME_EVERY_SECONDS, pattern]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=1800, check=True)
    except subprocess.CalledProcessError:
        raise ModerationError("ffmpeg could not read the video track")
    except subprocess.TimeoutExpired:
        raise ModerationError("frame extraction timed out")
    names = sorted(n for n in os.listdir(outdir) if n.endswith(".jpg"))
    if not names:
        raise ModerationError("no frames could be extracted")
    return [(i * FRAME_EVERY_SECONDS, os.path.join(outdir, n))
            for i, n in enumerate(names)]


def build_verdict_request(image_bytes, model=SPARK_MODEL):
    """JSON payload for one frame verdict (OpenAI-compatible chat)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    # Reasoning models spend most of the budget thinking (observed ~250
    # reasoning tokens per verdict), so the cap needs headroom beyond the
    # ~50 tokens the JSON answer itself takes.
    return {
        "model": model,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": JUDGE_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + b64}}]}]}


def extract_text(payload):
    """Tolerantly pull the assistant's text out of the response."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        first = choices[0]
        content = (first.get("message") or {}).get("content", "")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            joined = " ".join(p.get("text", "") for p in content
                              if isinstance(p, dict))
            if joined:
                return joined
        if isinstance(first.get("text"), str) and first["text"]:
            return first["text"]
    for key in ("output_text", "text", "response", "output"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def judge_text(text):
    """Model text -> (unsafe, category, ai). Raises on unreadable ones."""
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            verdict = str(obj.get("verdict", obj.get("label",
                                 obj.get("classification",
                                         obj.get("result", ""))))).lower()
            if verdict in ("unsafe", "explicit", "nsfw", "flagged"):
                unsafe = True
            elif verdict in ("safe", "clean", "none", "no"):
                unsafe = False
            else:
                unsafe = None
            if unsafe is not None:
                category = "".join(
                    c for c in str(obj.get("category", "")).lower()
                    if c.isalnum() or c == "-")[:40] or "none"
                ai = str(obj.get("ai", "unsure")).lower()
                if ai not in ("ai", "real", "unsure"):
                    ai = "unsure"
                return unsafe, "none" if not unsafe else category, ai
    lowered = text.lower()
    if "unsafe" in lowered:
        return True, "none", "unsure"
    if "safe" in lowered:
        return False, "none", "unsure"
    raise UnreadableVerdict("unreadable verdict: %r" % text[:120])


def spark_verdict(image_bytes, api_key, model=SPARK_MODEL):
    """One JPEG frame -> (unsafe, category). Raises ModerationError."""
    req = urllib.request.Request(
        SPARK_URL, data=json.dumps(
            build_verdict_request(image_bytes, model)).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise ModerationError("moderation request failed: %s" % e)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise ModerationError("moderation failed: %s" % msg)
    text = extract_text(payload)
    if not text.strip():
        reason = ""
        try:
            reason = payload["choices"][0].get("finish_reason") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            reason = ""
        if reason == "length":
            raise ModerationError(
                "verdict was cut off before answering "
                "(finish_reason=length)")
        if isinstance(payload, dict):
            keys: object = sorted(payload.keys())
            sample = json.dumps(payload)[:500]
        else:
            keys = type(payload).__name__
            sample = repr(payload)[:500]
        raise UnreadableVerdict(
            "empty verdict text (keys: %s; raw: %s)" % (keys, sample))
    return judge_text(text)


def evaluate_frames(stamps, judge):
    """[(timestamp, jpeg_bytes)] -> [(t, unsafe, category, ai)].

    Isolated unreadable verdicts fail closed (flagged for review); a run
    where nothing is readable aborts so systematic breakage surfaces.
    Transport and API errors are fatal and keep their message.
    """
    out = []
    last_failure = ""
    for stamp, image in stamps:
        try:
            unsafe, category, ai = judge(image)
        except UnreadableVerdict as e:
            unsafe, category, ai = True, "unverified", "unsure"
            last_failure = str(e)
        out.append((stamp, unsafe, category, ai))
    if out and all(c == "unverified" for _, _, c, _ in out):
        raise ModerationError("no moderation verdict was readable" + (
            "; last failure: %s" % last_failure if last_failure else ""))
    return out


def ai_label_for(score, judged):
    """(score, judged) -> (label, tone); (None, None) shows no badge."""
    if judged < AI_MIN_JUDGED or score < AI_FLOOR:
        return None, None
    if score >= AI_CERTAIN:
        return "AI GENERATED VIDEO", "red"
    if score >= AI_LIKELY:
        return "LIKELY AI-MADE", "red"
    return "POSSIBLY AI-MADE", "amber"


def aggregate_ai(labels):
    """[ai-label...] -> file-level call with verdict, score and badge."""
    judged = [v for v in labels if v in ("ai", "real")]
    if len(judged) < AI_MIN_JUDGED:
        return {"verdict": "uncertain", "score": 0.0,
                "judged": len(judged), "label": None, "tone": None}
    score = round(sum(1 for v in judged if v == "ai") / len(judged), 2)
    label, tone = ai_label_for(score, len(judged))
    return {"verdict": "ai" if score >= AI_THRESHOLD else "real",
            "score": score, "judged": len(judged),
            "label": label, "tone": tone}


def merge_ranges(flags, frame_every=FRAME_EVERY_SECONDS,
                 min_seconds=MIN_RANGE_SECONDS):
    """[(t, unsafe, category)] (ascending) -> [{start, end, category}].

    One safe frame inside a flagged stretch is tolerated so the blur does
    not flicker; stretches shorter than min_seconds are dropped.
    """
    ranges = []
    start = end = category = None
    for stamp, unsafe, cat, *_ in sorted(flags):
        if not unsafe:
            continue
        if start is None:
            start, end, category = stamp, stamp + frame_every, cat
        elif stamp - end <= frame_every:
            end = stamp + frame_every
            if category == "unverified" and cat != "unverified":
                category = cat
        else:
            ranges.append((start, end, category))
            start, end, category = stamp, stamp + frame_every, cat
    if start is not None:
        ranges.append((start, end, category))
    return [{"start": round(s, 2), "end": round(e, 2),
             "category": c} for s, e, c in ranges
            if e - s >= min_seconds]


def blur_filter(spans):
    """[(start, end)] -> ffmpeg -vf value blurring exactly those spans."""
    terms = ["between(t,%.2f,%.2f)" % (s, e) for s, e in spans]
    return ("boxblur=luma_radius=20:luma_power=1:enable='"
            + "+".join(terms) + "'")


def blur_command(asset_path, spans, out_path, ffmpeg=None):
    ff = ffmpeg or shutil.which("ffmpeg")
    if not ff:
        raise ModerationError("install ffmpeg to blur flagged scenes")
    return [ff, "-y", "-v", "error", "-i", asset_path,
            "-vf", blur_filter(spans), "-c:a", "copy", out_path]


def run_blur(asset_path, spans, out_path):
    try:
        subprocess.run(blur_command(asset_path, spans, out_path),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=1800, check=True)
    except subprocess.CalledProcessError:
        raise ModerationError("ffmpeg could not blur the flagged scenes")
    except subprocess.TimeoutExpired:
        raise ModerationError("scene blur timed out")


def scan_video(asset_path, api_key, judge=None):
    """Full detect pass -> {"flags": [...], "frames": n}."""
    judge = judge or (lambda img: spark_verdict(img, api_key))
    tmp = tempfile.mkdtemp(prefix="relay-mod-")
    try:
        stamps = extract_frames(asset_path, tmp)
        if len(stamps) > MAX_MINUTES * 60 / FRAME_EVERY_SECONDS:
            raise ModerationError(
                "video longer than %d minutes is not supported"
                % MAX_MINUTES)
        loaded = []
        for stamp, path in stamps:
            with open(path, "rb") as f:
                loaded.append((stamp, f.read()))
        evaluated = evaluate_frames(loaded, judge)
        flags = merge_ranges(evaluated)
        ai = aggregate_ai([row[3] for row in evaluated])
        return {"flags": flags, "frames": len(stamps), "ai": ai}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
