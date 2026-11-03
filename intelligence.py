"""Media intelligence: summaries, tags and catalog descriptions.

Enrichment pass over a finished transcript: the transcript text goes to
Muse Spark, which returns one JSON object with a short summary, topical
tags and a marketplace-ready description. Only stdlib is used.
"""

import json
import urllib.request

SPARK_URL = "https://api.meta.ai/v1/chat/completions"
SPARK_MODEL = "muse-spark-1.3"
MAX_TRANSCRIPT_CHARS = 8000
MAX_TAGS = 8

INTEL_PROMPT = (
    "You are a media cataloging assistant for a broadcast marketplace. "
    "Read the transcript below and reply with exactly one JSON object "
    "and nothing else: {\"summary\": a 2-3 sentence summary of what "
    "happens, \"tags\": 3 to 8 lowercase topical tags (subjects, places, "
    "named entities), \"description\": a 1-2 sentence neutral catalog "
    "description a TV station could print}. Transcript:\n\n")


class IntelligenceError(Exception):
    pass


def build_enrich_request(transcript, model=SPARK_MODEL):
    """JSON payload for one enrichment call (OpenAI-compatible chat)."""
    return {
        "model": model,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text":
                         INTEL_PROMPT + transcript[:MAX_TRANSCRIPT_CHARS]}]}]}


def normalize_tags(raw):
    """Raw tag list -> lowercase, deduped, capped tags."""
    tags = []
    for tag in raw if isinstance(raw, list) else []:
        cleaned = " ".join(str(tag).lower().split())[:32]
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags[:MAX_TAGS]


def parse_enrichment(text):
    """Model text -> {"summary", "tags", "description"}.

    Raises IntelligenceError when no usable object is found.
    """
    start, end = str(text).find("{"), str(text).rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            summary = " ".join(str(obj.get("summary", "")).split())[:600]
            description = " ".join(
                str(obj.get("description", "")).split())[:500]
            tags = normalize_tags(obj.get("tags", []))
            if summary or description or tags:
                return {"summary": summary, "tags": tags,
                        "description": description}
    raise IntelligenceError("unreadable enrichment: %r" % str(text)[:120])


def transcript_from_cues(cues):
    """Cue dicts -> plain transcript text (capped)."""
    return " ".join(str(c.get("text", "")) for c in cues
                    if isinstance(c, dict)).strip()[:MAX_TRANSCRIPT_CHARS]


def _response_text(payload):
    """Tolerantly pull the assistant's text out of the response."""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content
                           if isinstance(p, dict))
    return content if isinstance(content, str) else ""


QUERY_PROMPT = (
    "You turn a media buyer's request into search filters for a news "
    "marketplace. Reply with exactly one JSON object and nothing else: "
    '{"keywords": [important search words], "market": "City, ST" or null, '
    '"kind": one of package, show, podcast, irl, raw, tower, event or null, '
    '"from": "YYYY-MM-DD" or null, "to": "YYYY-MM-DD" or null}. '
    "Resolve relative dates against today (%s): 'last 30 days' means "
    "from=<today minus 30 days> to=<today>. Request:\n\n")


def parse_search_query(text, api_key, today, model=SPARK_MODEL):
    """Natural-language request -> filter dict.

    Raises IntelligenceError when the reply is unreadable or empty.
    """
    req = urllib.request.Request(
        SPARK_URL, data=json.dumps({
            "model": model, "max_tokens": 512,
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text":
                             QUERY_PROMPT % today + text}]}]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise IntelligenceError("search parse failed: %s" % e)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise IntelligenceError("search parse failed: %s" % msg)
    raw = _response_text(payload)
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(raw[start:end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            out = {
                "keywords": [str(k) for k in obj.get("keywords", [])
                             if str(k).strip()][:10],
                "market": str(obj.get("market") or "").strip() or None,
                "kind": str(obj.get("kind") or "").strip().lower() or None,
                "from": str(obj.get("from") or "").strip() or None,
                "to": str(obj.get("to") or "").strip() or None}
            if out["keywords"] or out["market"] or out["kind"] or \
                    out["from"] or out["to"]:
                return out
    raise IntelligenceError("unreadable search parse: %r" % raw[:120])


def spark_enrich(transcript, api_key, model=SPARK_MODEL):
    """Transcript text -> enrichment dict. Raises IntelligenceError."""
    req = urllib.request.Request(
        SPARK_URL, data=json.dumps(
            build_enrich_request(transcript, model)).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise IntelligenceError("intelligence request failed: %s" % e)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise IntelligenceError("intelligence failed: %s" % msg)
    text = _response_text(payload)
    if not text.strip():
        raise IntelligenceError("empty enrichment text")
    return parse_enrichment(text)
