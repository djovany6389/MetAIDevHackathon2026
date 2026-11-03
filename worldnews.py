"""World briefing: headlines from public RSS feeds + an AI digest.

Only stdlib is used. FEEDS lists verified public RSS endpoints; set
WORLDNEWS_FEEDS ("Name|https://...,Name|https://...") to override.
"""

import json
import os
import urllib.request
import xml.etree.ElementTree as ET

SPARK_URL = "https://api.meta.ai/v1/chat/completions"
SPARK_MODEL = "muse-spark-1.3"
FEED_TIMEOUT = 10
PER_FEED_ITEMS = 8
MAX_ITEMS = 15
MAX_BRIEFING_CHARS = 6000

DEFAULT_FEEDS = (
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
)

BRIEFING_PROMPT = (
    "You are a newsroom assignment editor. Read these breaking-news "
    "headlines and reply with exactly one JSON object and nothing else: "
    '{"digest": "a tight 3-5 sentence briefing on the biggest developing '
    'stories and why they matter for coverage", "summaries": ["one 1-2 '
    'sentence plain-language summary per headline below, in the same '
    'numbered order"]}. Headlines:\n\n')


class WorldNewsError(Exception):
    pass


def feeds():
    """Configured (source, url) feeds, defaulting to DEFAULT_FEEDS."""
    raw = os.environ.get("WORLDNEWS_FEEDS", "")
    if raw.strip():
        out = []
        for part in raw.split(","):
            name, sep, url = part.partition("|")
            if sep and name.strip() and url.strip().startswith("https://"):
                out.append((name.strip(), url.strip()))
        if out:
            return out
    return list(DEFAULT_FEEDS)


def fetch_feed(url):
    """One feed URL -> raw bytes. Raises WorldNewsError."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Relay briefing fetcher)",
                      "Accept": ("application/rss+xml, application/xml, "
                                 "text/xml")})
    try:
        with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        raise WorldNewsError("feed failed %s: %s" % (url, e))


def parse_rss(data, source):
    """RSS bytes -> [{title, link, source}] (never raises)."""
    items = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    for node in root.iter("item"):
        title = " ".join((node.findtext("title") or "").split())[:200]
        link = (node.findtext("link") or "").strip()[:500]
        if not title:
            continue
        if link and not link.startswith(("http://", "https://")):
            link = ""
        desc_node = node.find("description")
        desc = " ".join("".join(
            desc_node.itertext()).split())[:300] \
            if desc_node is not None else ""
        items.append({"title": title, "link": link, "source": source,
                      "description": desc})
        if len(items) >= PER_FEED_ITEMS:
            break
    return items


def build_briefing_request(items, model=SPARK_MODEL):
    """Headline items -> JSON payload for one digest call."""
    lines = []
    for num, item in enumerate(items, 1):
        line = "%d. [%s] %s" % (num, item.get("source", ""),
                                item.get("title", ""))
        if item.get("description"):
            line += " — %s" % item["description"]
        lines.append(line)
    digest = "\n".join(lines)[:MAX_BRIEFING_CHARS]
    return {
        "model": model,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [{"type": "text",
                         "text": BRIEFING_PROMPT + digest}]}]}


def parse_briefing(text, count):
    """Model text + item count -> (digest, per-item summaries).

    Summaries are padded with "" so callers can fall back to the
    feed's own descriptions. Raises WorldNewsError when unusable.
    """
    start, end = str(text).find("{"), str(text).rfind("}")
    obj = None
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            obj = None
    if isinstance(obj, dict):
        digest = " ".join(str(obj.get("digest", "")).split())[:1500]
        raw = obj.get("summaries", [])
        if not isinstance(raw, list):
            raw = []
        summaries = [" ".join(str(s).split())[:400] for s in raw]
        summaries += [""] * (count - len(summaries))
        summaries = summaries[:count]
        if digest or any(summaries):
            return digest, summaries
    raise WorldNewsError("unreadable briefing: %r" % str(text)[:120])


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


def spark_briefing(items, api_key, model=SPARK_MODEL):
    """Headline items -> {"summary", "summaries"}. Raises WorldNewsError."""
    req = urllib.request.Request(
        SPARK_URL, data=json.dumps(
            build_briefing_request(items, model)).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise WorldNewsError("briefing request failed: %s" % e)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise WorldNewsError("briefing failed: %s" % msg)
    digest, summaries = parse_briefing(_response_text(payload), len(items))
    return {"summary": digest, "summaries": summaries}
