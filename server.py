#!/usr/bin/env python3
"""Relay Network app server (Python standard library only).

Frontend in ./public plus a JSON API with login and role permissions.

Roles: station, contributor, partner.
  - Station:     /discover /request /requests /booking /bookings /expenses /station
  - Contributor: /contributor — apply to briefs, publish finished work
  - Partner:     /partner — manage own tower-cam feeds, see usage/earnings

Auth: PBKDF2-hashed passwords in data/users.json, random session tokens in an
HttpOnly cookie. Sessions persist in data/sessions.json (30-day expiry).

Data dir override for tests:  RELAY_DATA=/tmp/xyz python server.py

Run:  python server.py        (or PORT=4200 python server.py)
Open: http://127.0.0.1:4173/
"""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import threading
import urllib.parse

import captions
import intelligence
import moderation
import worldnews
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load KEY=VALUE lines from .env (project root) into the environment.

    Real environment variables win; missing file means defaults.
    """
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

PUBLIC = os.path.join(ROOT, "public")
DATA_DIR = os.environ.get("RELAY_DATA") or os.path.join(ROOT, "data")

HOMES = {"station": "/discover", "contributor": "/contributor",
         "partner": "/partner"}
ROLE_ALIASES = {"creator": "contributor"}
HOURLY_KINDS = ("tower", "irl", "event")
FIXED_KINDS = ("package", "show", "podcast", "raw")
PUBLISH_KINDS = {"package": "Package", "show": "TV show",
                 "podcast": "Podcast", "irl": "IRL live",
                 "raw": "Raw footage"}
PARTNER_SHARE = 0.60
WORLDNEWS_TTL = 1800
WORLDNEWS_RETRY = 300
_MUX_BBB = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
_UNIFIED_TOS = ("https://demo.unified-streaming.com/k8s/features/stable/video/"
                "tears-of-steel/tears-of-steel.ism/.m3u8")
DEFAULT_STREAMS = {
    "irl": "https://stream.playzi.tv/live/fast/playlist.m3u8",
    "tower": _MUX_BBB,
    "package": _UNIFIED_TOS,
    "show": "https://test-streams.mux.dev/tos_ismc/main.m3u8",
    "podcast": "https://media.w3.org/2010/05/sintel/trailer.mp4",
    "event": ("https://devstreaming-cdn.apple.com/videos/streaming/examples/"
              "img_bipbop_adv_example_ts/master.m3u8"),
}
FALLBACK_STREAMS = {
    "tower": _UNIFIED_TOS,
    "package": _MUX_BBB,
    "show": _UNIFIED_TOS,
    "podcast": _MUX_BBB,
    "event": _MUX_BBB,
}
SESSION_DAYS = 30
MAX_UPLOAD = 5 * 1024 * 1024
UPLOAD_EXTS = {"pdf": "application/pdf", "jpg": "image/jpeg",
               "jpeg": "image/jpeg", "png": "image/png"}
MAX_ASSET = 2 * 1024 * 1024 * 1024
ASSET_EXTS = {"mp4": "video/mp4", "webm": "video/webm",
              "mov": "video/quicktime", "mkv": "video/x-matroska",
              "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


EST = timezone(timedelta(hours=-5))  # platform time: US Eastern, no DST

META_API_KEY = os.environ.get("META_API_KEY", "")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_date(s):
    m = DATE_RE.match((s or "").strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc).date()
    except ValueError:
        return None


def parse_time(s):
    m = TIME_RE.match((s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


def window_bounds(date_s, from_s, until_s):
    d = parse_date(date_s)
    a = parse_time(from_s)
    b = parse_time(until_s)
    if not d or not a or not b:
        return None
    start = datetime(d.year, d.month, d.day, a[0], a[1], tzinfo=EST)
    end = datetime(d.year, d.month, d.day, b[0], b[1], tzinfo=EST)
    if end <= start or (end - start) > timedelta(hours=24):
        return None
    return start, end


def money(n):
    return "$%.2f" % n


def parse_range(asked, size):
    """Return (start, end, ranged) for a Range header value and file size."""
    start, end, ranged = 0, size - 1, False
    m = re.match(r"bytes=(\d*)-(\d*)$", (asked or "").strip())
    if m and size:
        a, b = m.groups()
        if a:
            start = min(int(a), size - 1)
        if b:
            end = min(int(b), size - 1)
        if start <= end:
            ranged = True
    return start, end, ranged


def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 200_000).hex()
    return {"salt": salt, "hash": digest}


def check_pw(password, record):
    try:
        expect = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), record["salt"].encode(),
            200_000).hex()
    except (KeyError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(expect, record["hash"])


def clean(s, limit=500):
    return (s or "").strip()[:limit]


def _flag_stamp(flag):
    start = float(flag.get("start", 0))
    end = float(flag.get("end", start))
    return "%d:%02d-%d:%02d" % (start // 60, start % 60,
                                end // 60, end % 60)


_caption_queue = queue.Queue()
_caption_worker_started = False
_caption_worker_lock = threading.Lock()
_mod_queue = queue.Queue()
_mod_worker_started = False
_mod_worker_lock = threading.Lock()
_intel_queue = queue.Queue()
_intel_worker_started = False
_intel_worker_lock = threading.Lock()
_wn_queue = queue.Queue()
_wn_worker_started = False
_wn_worker_lock = threading.Lock()


def process_caption_job(store, aid, transcribe_fn=None):
    """Run one caption job synchronously (worker calls this per job)."""
    def transcribe(wav):
        if transcribe_fn is not None:
            return transcribe_fn(wav)
        return captions.muse_transcribe(wav, META_API_KEY)

    try:
        asset = store.asset_for(aid)
        if asset is None:
            raise captions.CaptionError("file not found")
        path = os.path.join(store.dir, "assets", asset["stored"])
        pcm = captions.extract_pcm(path, asset.get("mime", ""))
        segments = []
        for offset, chunk in captions.chunk_pcm(pcm):
            for seg in transcribe(captions.pcm_to_wav_bytes(chunk)):
                seg = dict(seg)
                try:
                    seg["start"] = float(seg.get("start", 0)) + offset
                    seg["end"] = float(seg.get("end", 0)) + offset
                except (TypeError, ValueError):
                    continue
                segments.append(seg)
        vtt = captions.segments_to_vtt(segments)
        cues = []
        for seg in segments:
            try:
                start = float(seg.get("start", 0))
                end = float(seg.get("end", start))
            except (TypeError, ValueError):
                continue
            text = " ".join(str(seg.get("text", "")).split())
            if not text:
                continue
            cues.append({"start": start, "end": max(end, start),
                         "speaker": str(seg.get("speaker", "")).strip(),
                         "text": text})
        cues.sort(key=lambda c: (c["start"], c["end"]))
        os.makedirs(os.path.join(store.dir, "captions"), exist_ok=True)
        with open(os.path.join(store.dir, "captions", aid + ".vtt"),
                  "w", encoding="utf-8") as f:
            f.write(vtt)
        with open(os.path.join(store.dir, "captions", aid + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump({"cues": cues}, f)
        store._set_caption(aid, "ready")
        store._maybe_auto_intel(aid)
    except captions.CaptionError as e:
        store._set_caption(aid, "error", str(e))
    except Exception as e:  # never kill the worker thread
        store._set_caption(aid, "error", "captioning failed: %s" % e)


def _caption_worker():
    while True:
        store, aid = _caption_queue.get()
        try:
            process_caption_job(store, aid)
        finally:
            _caption_queue.task_done()


def enqueue_caption(store, aid):
    global _caption_worker_started
    with _caption_worker_lock:
        if not _caption_worker_started:
            thread = threading.Thread(target=_caption_worker, daemon=True)
            thread.start()
            _caption_worker_started = True
    _caption_queue.put((store, aid))


def process_moderation_job(store, aid, judge=None):
    """Run one moderation job synchronously (worker calls this per job)."""
    try:
        asset = store.asset_for(aid)
        if asset is None:
            raise moderation.ModerationError("file not found")
        path = os.path.join(store.dir, "assets", asset["stored"])
        result = moderation.scan_video(path, META_API_KEY, judge=judge)
        flags = result["flags"]
        if not flags:
            store._set_moderation(aid, {"status": "clean", "flags": [],
                                        "frames": result["frames"],
                                        "ai": result["ai"]})
            return
        os.makedirs(os.path.join(store.dir, "moderation"), exist_ok=True)
        out = os.path.join(store.dir, "moderation", aid + ".blur.mp4")
        moderation.run_blur(path, [(f["start"], f["end"]) for f in flags],
                            out)
        store._set_moderation(aid, {"status": "flagged", "flags": flags,
                                    "frames": result["frames"],
                                    "ai": result["ai"],
                                    "review": aid + ".blur.mp4"})
    except moderation.ModerationError as e:
        store._set_moderation(aid, {"status": "error", "error": str(e)})
    except Exception as e:  # never kill the worker thread
        store._set_moderation(aid, {"status": "error",
                                    "error": "moderation failed: %s" % e})


def _moderation_worker():
    while True:
        store, aid = _mod_queue.get()
        try:
            process_moderation_job(store, aid)
        finally:
            _mod_queue.task_done()


def enqueue_moderation(store, aid):
    global _mod_worker_started
    with _mod_worker_lock:
        if not _mod_worker_started:
            thread = threading.Thread(target=_moderation_worker,
                                      daemon=True)
            thread.start()
            _mod_worker_started = True
    _mod_queue.put((store, aid))


def process_intelligence_job(store, aid, enrich_fn=None):
    """Run one enrichment job synchronously (worker calls this per job)."""
    try:
        asset = store.asset_for(aid)
        if asset is None:
            raise intelligence.IntelligenceError("file not found")
        try:
            with open(os.path.join(store.dir, "captions", aid + ".json"),
                      encoding="utf-8") as f:
                cues = json.load(f).get("cues", [])
        except (OSError, ValueError):
            raise intelligence.IntelligenceError("transcript not ready")
        transcript = intelligence.transcript_from_cues(cues)
        if not transcript:
            raise intelligence.IntelligenceError("transcript is empty")
        enrich = enrich_fn or intelligence.spark_enrich
        result = enrich(transcript, META_API_KEY)
        store._set_intelligence(aid, {"status": "ready",
                                      "summary": result.get("summary", ""),
                                      "tags": result.get("tags", []),
                                      "description": result.get(
                                          "description", "")})
    except intelligence.IntelligenceError as e:
        store._set_intelligence(aid, {"status": "error", "error": str(e)})
    except Exception as e:  # never kill the worker thread
        store._set_intelligence(aid, {"status": "error",
                                      "error": "intelligence failed: %s" % e})


def _intelligence_worker():
    while True:
        store, aid = _intel_queue.get()
        try:
            process_intelligence_job(store, aid)
        finally:
            _intel_queue.task_done()


def enqueue_intelligence(store, aid):
    global _intel_worker_started
    with _intel_worker_lock:
        if not _intel_worker_started:
            thread = threading.Thread(target=_intelligence_worker,
                                      daemon=True)
            thread.start()
            _intel_worker_started = True
    _intel_queue.put((store, aid))


def process_worldnews_job(store, fetch_fn=None, summarize_fn=None):
    """Refresh the cached world briefing (worker calls this per job)."""
    fetch = fetch_fn or worldnews.fetch_feed
    try:
        items = []
        for source, url in worldnews.feeds():
            try:
                items.extend(worldnews.parse_rss(fetch(url), source))
            except worldnews.WorldNewsError:
                continue
            if len(items) >= worldnews.MAX_ITEMS:
                break
        items = items[:worldnews.MAX_ITEMS]
        if not items:
            raise worldnews.WorldNewsError("all news feeds failed")
        summary, summaries = "", [""] * len(items)
        if META_API_KEY:
            try:
                summarize = summarize_fn or worldnews.spark_briefing
                result = summarize(items, META_API_KEY)
                summary = result.get("summary", "")
                got = result.get("summaries", [])
                for i in range(min(len(items), len(got))):
                    if got[i]:
                        summaries[i] = got[i]
            except worldnews.WorldNewsError:
                pass  # headlines still saved below
        for i, item in enumerate(items):
            item["summary"] = summaries[i] or item.get("description", "")
        store._set_worldnews({"status": "ready", "summary": summary,
                              "items": items, "error": ""})
    except worldnews.WorldNewsError as e:
        store._set_worldnews({"status": "error", "error": str(e)})
    except Exception as e:  # never kill the worker thread
        store._set_worldnews({"status": "error",
                              "error": "world briefing failed: %s" % e})


def _worldnews_worker():
    while True:
        store = _wn_queue.get()
        try:
            process_worldnews_job(store)
        finally:
            _wn_queue.task_done()


def enqueue_worldnews(store):
    global _wn_worker_started
    with _wn_worker_lock:
        if not _wn_worker_started:
            thread = threading.Thread(target=_worldnews_worker,
                                      daemon=True)
            thread.start()
            _wn_worker_started = True
    _wn_queue.put(store)


class Store:
    """All Relay state with one lock. Pure enough for socketless tests."""

    def __init__(self, data_dir=None):
        self.dir = data_dir or DATA_DIR
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(os.path.join(self.dir, "uploads"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "assets"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "captions"), exist_ok=True)
        self.lock = threading.Lock()
        self.users = self._load("users.json", [])
        self.content = self._load("content.json", [])
        self.feeds = self._load("feeds.json", [])
        self.bookings = self._load("bookings.json", [])
        self.requests = self._load("requests.json", [])
        self.pitches = self._load("pitches.json", [])
        self.saved = self._load("saved.json", {})
        self.settings = self._load("settings.json", {})
        self.uploads = self._load("uploads.json", [])
        self.assets = self._load("assets.json", [])
        self.notif_seen = self._load("notif_seen.json", {})
        self.ifb = self._load("ifb.json", {})
        self.captions = self._load("captions.json", {})
        self.moderation = self._load("moderation.json", {})
        self.intelligence = self._load("intelligence.json", {})
        self.worldnews = self._load("worldnews.json", {})
        self.folders = self._load("folders.json", {})
        self.counters = self._load("counters.json", {})
        self.sessions = self._load("sessions.json", {})
        self.secret = self._secret()
        self._migrate()
        self._prune_sessions()

    # ---------- persistence ----------
    def _path(self, name):
        return os.path.join(self.dir, name)

    def _load(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default

    def _save(self, name, obj):
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, self._path(name))

    def _secret(self):
        path = self._path(".secret")
        try:
            with open(path, encoding="utf-8") as f:
                if len(f.read().strip()) >= 32:
                    f.seek(0)
                    return f.read().strip()
        except FileNotFoundError:
            pass
        value = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return value

    def _migrate(self):
        """Normalize records written by older prototype versions."""
        changed = False
        for f in self.feeds:
            if "code" not in f:
                f["code"] = (f.get("id") or "CAM").upper().replace("_", "-")
                changed = True
            f.setdefault("market", "")
            f.setdefault("coords", "")
            f.setdefault("uplink", "")
            f.setdefault("signal", "")
            f.setdefault("thumb", "img/harbor.svg")
            try:
                f["rate"] = float(f.get("rate", 0))
            except (TypeError, ValueError):
                f["rate"] = 0
            f["net"] = round(f["rate"] * PARTNER_SHARE, 2)
            if f.get("status") not in ("live", "offline", "pending"):
                f["status"] = "offline"
                changed = True
            f.pop("hoursUsed", None)
        for b in self.bookings:
            if b.get("status") in ("confirmed", "reserved", "live"):
                b["status"] = "upcoming"
                changed = True
            total = b.get("total")
            if isinstance(total, str):
                try:
                    b["total"] = round(float(total.replace("$", "").replace(",", "")), 2)
                except ValueError:
                    b["total"] = 0
                changed = True
            if not b.get("invoice"):
                b["invoice"] = self._next_invoice()
                changed = True
            b.setdefault("created", iso(utcnow()))
        if "next_invoice" not in self.counters:
            peak = 1000
            for b in self.bookings:
                m = re.match(r"INV-(\d+)$", b.get("invoice") or "")
                if m:
                    peak = max(peak, int(m.group(1)))
            self.counters["next_invoice"] = peak + 1
            changed = True
        if changed:
            self._save("feeds.json", self.feeds)
            self._save("bookings.json", self.bookings)
            self._save("counters.json", self.counters)
        for aid, record in self.captions.items():
            if not isinstance(record, dict) or \
                    record.get("status") != "ready":
                continue
            target = os.path.join(self.dir, "captions", aid + ".json")
            if os.path.isfile(target):
                continue
            try:
                with open(os.path.join(self.dir, "captions", aid + ".vtt"),
                          encoding="utf-8") as f:
                    cues = captions.vtt_to_cues(f.read())
            except OSError:
                continue
            try:
                with open(target, "w", encoding="utf-8") as f:
                    json.dump({"cues": cues}, f)
            except OSError:
                pass

    def _next_invoice(self):
        n = int(self.counters.get("next_invoice", 1001))
        self.counters["next_invoice"] = n + 1
        self._save("counters.json", self.counters)
        return "INV-%d" % n

    @staticmethod
    def _uid(prefix, existing):
        while True:
            uid = "%s_%s" % (prefix, secrets.token_hex(4))
            if uid not in existing:
                return uid

    # ---------- users & sessions ----------
    def find_user(self, email):
        email = (email or "").strip().lower()
        return next((u for u in self.users if u["email"] == email), None)

    @staticmethod
    def public_user(user):
        return {"email": user["email"], "name": user.get("name", ""),
                "role": user["role"], "roles": user.get("roles", []),
                "org": user.get("org", "")}

    @staticmethod
    def display_name(user):
        return user.get("org") or user.get("name") or user["email"]

    def _prune_sessions(self):
        now = utcnow()
        dead = [t for t, s in self.sessions.items()
                if s.get("expires", "") < iso(now)]
        for t in dead:
            del self.sessions[t]
        if dead:
            self._save("sessions.json", self.sessions)

    def login_token(self, user):
        with self.lock:
            self._prune_sessions()
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {
                "email": user["email"], "created": iso(utcnow()),
                "expires": iso(utcnow() + timedelta(days=SESSION_DAYS))}
            self._save("sessions.json", self.sessions)
            return token

    def user_for_token(self, token):
        if not token:
            return None
        with self.lock:
            sess = self.sessions.get(token)
            if not sess or sess.get("expires", "") < iso(utcnow()):
                if sess:
                    del self.sessions[token]
                    self._save("sessions.json", self.sessions)
                return None
            return self.find_user(sess["email"])

    def logout(self, token):
        with self.lock:
            if token in self.sessions:
                del self.sessions[token]
                self._save("sessions.json", self.sessions)

    def signup(self, payload):
        email = (payload.get("email") or "").strip().lower()
        password = payload.get("password") or ""
        role = ROLE_ALIASES.get((payload.get("role") or "").strip().lower(),
                                (payload.get("role") or "").strip().lower())
        name = clean(payload.get("name"), 120)
        org = clean(payload.get("org"), 120)
        if not EMAIL_RE.match(email) or role not in HOMES or not name:
            return 400, {"error": "name, valid email and role are required"}
        if role in ("station", "partner") and not org:
            return 400, {"error": "station name is required" if role == "station"
                         else "property / business name is required"}
        if len(password) < 8:
            return 400, {"error": "password must be at least 8 characters"}
        with self.lock:
            if self.find_user(email):
                return 409, {"error": "that email is already registered"}
            roles = payload.get("roles", []) if role == "contributor" else []
            roles = [clean(r, 40) for r in roles if clean(r, 40)][:12]
            user = {"email": email, "name": name, "role": role, "roles": roles,
                    "org": org if role != "contributor" else "",
                    "based": clean(payload.get("based"), 120),
                    "specialties": clean(payload.get("specialties"), 200),
                    "languages": clean(payload.get("languages"), 120),
                    "pass": hash_pw(password), "created": iso(utcnow())}
            self.users.append(user)
            self._save("users.json", self.users)
        return 201, {"ok": True, "role": role,
                     "token": self.login_token(user)}

    def login(self, payload):
        user = self.find_user(payload.get("email"))
        if not user or not check_pw(payload.get("password") or "",
                                    user.get("pass", {})):
            return 401, {"error": "wrong email or password"}
        data = self.public_user(user)
        data["ok"] = True
        data["token"] = self.login_token(user)
        return 200, data

    def set_password(self, user, payload):
        current = payload.get("current") or ""
        new = payload.get("new") or ""
        if not check_pw(current, user.get("pass", {})):
            return 401, {"error": "current password is wrong"}
        if len(new) < 8:
            return 400, {"error": "new password must be at least 8 characters"}
        with self.lock:
            user["pass"] = hash_pw(new)
            self._save("users.json", self.users)
        return 200, {"ok": True}

    # ---------- catalog (content + partner feeds, feeds win on id clash) ----------
    def _feed_item(self, feed):
        owner = self.find_user(feed.get("owner"))
        owner_name = self.display_name(owner) if owner else "Relay partner"
        status = feed.get("status", "offline")
        label = {"live": "Live", "offline": "Offline",
                 "pending": "Signal check"}.get(status, "Offline")
        return {
            "id": feed["id"], "kind": "tower", "kindLabel": "Tower cam",
            "code": feed.get("code", ""), "title": feed.get("name", "Tower feed"),
            "market": feed.get("market", "") or feed.get("location", ""),
            "owner": owner_name, "ownerEmail": feed.get("owner", ""),
            "price": feed.get("rate", 0), "unit": "hour",
            "status": status, "statusLabel": label,
            "bookable": status == "live",
            "meta": "1080p · SRT / HLS",
            "filed": "%s · %s" % (feed.get("location", ""), owner_name),
            "tech": feed.get("signal") or "Signal details pending",
            "thumb": feed.get("thumb", "img/harbor.svg"),
            "description": "Live tower camera at %s (%s). Book by the hour; "
                           "connection details release before your window."
                           % (feed.get("location", "partner site"),
                              feed.get("coords") or "coords on file"),
            "license": "United States · Non-exclusive · Editorial only",
            "permitted": "Linear TV + simultaneous web broadcast",
            "restrictions": "No VOD, resale, archive or advertising use",
        }

    def catalog(self):
        items = {c["id"]: dict(c, bookable=c.get("kind") != "tower")
                 for c in self.content
                 if c.get("id") and not c.get("unpublished")}
        for f in self.feeds:
            if f.get("id"):
                items[f["id"]] = self._feed_item(f)
        complete_assets = {a["id"]: a for a in self.assets
                           if a.get("complete")}
        for item in items.values():
            self._enrich_item(item, complete_assets)
        return sorted(items.values(), key=lambda c: c.get("title", ""))

    def own_listings(self, user):
        """Contributor's own listings, including unpublished ones."""
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        complete_assets = {a["id"]: a for a in self.assets
                           if a.get("complete")}
        rows = []
        for c in self.content:
            if c.get("ownerEmail") != user["email"]:
                continue
            item = dict(c, bookable=c.get("kind") != "tower")
            self._enrich_item(item, complete_assets)
            rows.append(item)
        rows.sort(key=lambda c: c.get("title", ""))
        return 200, rows

    def _enrich_item(self, item, complete_assets):
        asset = complete_assets.get(item.get("asset") or "")
        if not item.get("stream") and asset is not None:
            item["stream"] = "/assets/" + item["asset"]
            item["assetName"] = asset["name"]
        default = DEFAULT_STREAMS.get(item.get("kind"))
        if default and not item.get("stream"):
            item["stream"] = default
        fallback = FALLBACK_STREAMS.get(item.get("kind"))
        if fallback and not item.get("streamFallback"):
            item["streamFallback"] = fallback
        item["assetMime"] = asset["mime"] if asset else ""
        mod = self.moderation.get(item.get("asset") or "")
        if mod is None:
            item["moderation"] = "none"
            item["moderationAi"] = {}
            item["moderationFlags"] = 0
            item["reviewStream"] = ""
        else:
            item["moderation"] = mod.get("status", "none")
            item["moderationAi"] = mod.get("ai") or {}
            flags = mod.get("flags", []) \
                if mod.get("status") == "flagged" else []
            item["moderationFlags"] = len(flags)
            review = ""
            if mod.get("status") == "flagged":
                name = mod.get("review") or \
                    (item["asset"] + ".blur.mp4")
                if os.path.isfile(os.path.join(
                        self.dir, "moderation", name)):
                    review = "/assets/%s?review=1" % item["asset"]
            item["reviewStream"] = review

    @staticmethod
    def _search_tokens(text):
        return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
                if t]

    def _item_transcript(self, aid):
        try:
            with open(os.path.join(self.dir, "captions", aid + ".json"),
                      encoding="utf-8") as f:
                cues = json.load(f).get("cues", [])
        except (OSError, ValueError):
            return ""
        return " ".join(c.get("text", "") for c in cues
                        if isinstance(c, dict))

    @staticmethod
    def _search_score(item, keywords, tags, transcript):
        if not keywords:
            return 1
        fields = [(item.get("title", ""), 5), (" ".join(tags), 4),
                  (item.get("description", ""), 3),
                  (item.get("market", ""), 2), (transcript, 1)]
        lowered = [(str(t).lower(), w) for t, w in fields]
        score = 0
        for kw in keywords:
            kw = str(kw).lower()
            if not kw:
                continue
            score += max([w for t, w in lowered if kw in t] or [0])
        return score

    def search_content(self, user, payload, parse_fn=None):
        """Ranked marketplace search for stations.

        Natural-language q is parsed into keywords + filters (with a
        plain keyword fallback); explicit market/kind/from/to win over
        parsed values. Keywords match titles, AI tags, descriptions,
        markets and transcripts, in that weight order.
        """
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        payload = payload or {}
        q = clean(payload.get("q"), 500)
        kinds = set(PUBLISH_KINDS) | {"tower", "event"}
        explicit = {
            "market": clean(payload.get("market"), 120) or None,
            "kind": clean(payload.get("kind"), 20).lower() or None,
            "from": clean(payload.get("from"), 20) or None,
            "to": clean(payload.get("to"), 20) or None}
        if explicit["kind"] not in kinds:
            explicit["kind"] = None
        for key in ("from", "to"):
            if explicit[key] and not parse_date(explicit[key]):
                explicit[key] = None
        parsed = {"keywords": [], "market": None, "kind": None,
                  "from": None, "to": None}
        fallback = False
        if q:
            parse = parse_fn or intelligence.parse_search_query
            try:
                today = datetime.now(EST).strftime("%Y-%m-%d")
                result = parse(q, META_API_KEY, today)
                parsed = {"keywords": [str(k) for k in
                                       result.get("keywords", [])][:10],
                          "market": result.get("market"),
                          "kind": result.get("kind"),
                          "from": result.get("from"),
                          "to": result.get("to")}
            except intelligence.IntelligenceError:
                fallback = True
                parsed["keywords"] = self._search_tokens(q)
        filters = {k: explicit[k] or parsed[k]
                   for k in ("market", "kind", "from", "to")}
        if filters["kind"] not in kinds:
            filters["kind"] = None
        for key in ("from", "to"):
            if filters[key] and not parse_date(filters[key]):
                filters[key] = None
        transcripts = {}
        scored = []
        for item in self.catalog():
            if filters["market"] and \
                    item.get("market") != filters["market"]:
                continue
            if filters["kind"] and item.get("kind") != filters["kind"]:
                continue
            rec = (item.get("recorded") or "").strip()
            if (filters["from"] or filters["to"]) and not parse_date(rec):
                continue
            if filters["from"] and rec < filters["from"]:
                continue
            if filters["to"] and rec > filters["to"]:
                continue
            aid = item.get("asset") or ""
            if aid not in transcripts:
                tags = self.intelligence.get(aid, {}).get("tags", []) \
                    if aid else []
                transcripts[aid] = (
                    [t for t in tags if isinstance(t, str)],
                    self._item_transcript(aid) if aid else "")
            tags, transcript = transcripts[aid]
            score = self._search_score(item, parsed["keywords"],
                                       tags, transcript)
            if parsed["keywords"] and not score:
                continue
            scored.append((score, item))
        scored.sort(key=lambda s: (-s[0], s[1].get("title", "")))
        parsed["market"] = filters["market"]
        parsed["kind"] = filters["kind"]
        parsed["from"] = filters["from"]
        parsed["to"] = filters["to"]
        return 200, {"parsed": parsed, "fallback": fallback,
                     "results": [item for _, item in scored]}

    def _set_worldnews(self, body):
        with self.lock:
            merged = dict(self.worldnews)
            merged.update(body)
            merged["updated"] = iso(utcnow())
            self.worldnews = merged
            self._save("worldnews.json", self.worldnews)

    @staticmethod
    def _worldnews_stale(cache, ttl):
        try:
            stamp = datetime.fromisoformat(cache.get("updated", ""))
        except (TypeError, ValueError):
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (utcnow() - stamp).total_seconds() > ttl

    def worldnews_status(self, user):
        """Cached world briefing for any signed-in user.

        Fresh cache is served as-is; otherwise a background refresh is
        queued and the stale briefing (if any) is served meanwhile.
        """
        cache = self.worldnews or {}
        if cache.get("status") == "ready" and \
                not self._worldnews_stale(cache, WORLDNEWS_TTL):
            return 200, dict(cache)
        if cache.get("status") == "error" and \
                not self._worldnews_stale(cache, WORLDNEWS_RETRY):
            return 200, dict(cache)
        if cache.get("status") != "pending":
            with self.lock:
                self.worldnews["status"] = "pending"
                self._save("worldnews.json", self.worldnews)
            enqueue_worldnews(self)
        return 200, dict(self.worldnews)

    def content_item(self, cid):
        for c in self.catalog():
            if c["id"] == cid:
                return c
        return None

    def content_record(self, cid):
        """Raw content/feed record by id, ignoring unpublish state.

        For historical rows (invoices, expenses) that must keep their
        titles after a listing leaves the marketplace.
        """
        for c in self.content:
            if c.get("id") == cid:
                return c
        for f in self.feeds:
            if f.get("id") == cid:
                return self._feed_item(f)
        return None

    def publish_content(self, user, payload):
        if user["role"] not in ("contributor", "partner"):
            return 403, {"error": "contributors and partners only"}
        kind = (payload.get("kind") or "").strip().lower()
        if user["role"] == "partner":
            return 403, {"error": "partners publish through tower-cam feeds"}
        if kind not in PUBLISH_KINDS:
            return 400, {"error": "kind must be one of " +
                                  ", ".join(sorted(PUBLISH_KINDS))}
        title = clean(payload.get("title"), 140)
        desc = clean(payload.get("description"), 2000)
        market = clean(payload.get("market"), 120)
        try:
            price = round(float(payload.get("price", 0)), 2)
        except (TypeError, ValueError):
            price = -1
        if not title or not desc or not market or price <= 0:
            return 400, {"error": "title, description, market and a price "
                                  "above zero are required"}
        unit = "hour" if kind in HOURLY_KINDS else (
            "episode" if kind == "show" else "license")
        stream = clean(payload.get("stream"), 500)
        fallback = clean(payload.get("streamFallback"), 500)
        for url in (stream, fallback):
            if url and not url.startswith(("http://", "https://")):
                return 400, {"error": "stream URLs must start with http(s)"}
        recorded = clean(payload.get("recorded"), 20)
        if recorded and not parse_date(recorded):
            return 400, {"error": "recorded must be a date (YYYY-MM-DD)"}
        asset_id = clean(payload.get("assetId"), 40)
        asset_mime = ""
        if asset_id:
            asset = self.asset_for(asset_id)
            if asset is None or asset["owner"] != user["email"]:
                return 400, {"error": "unknown or unfinished upload"}
            asset_mime = asset["mime"]
        with self.lock:
            item = {
                "id": self._uid("ct", {c.get("id") for c in self.content}),
                "kind": kind, "kindLabel": PUBLISH_KINDS[kind],
                "title": title, "description": desc, "market": market,
                "owner": self.display_name(user), "ownerEmail": user["email"],
                "price": price, "unit": unit,
                "stream": stream if kind in DEFAULT_STREAMS else "",
                "streamFallback": fallback if kind in DEFAULT_STREAMS else "",
                "asset": asset_id,
                "status": "live" if kind in HOURLY_KINDS else "ready",
                "statusLabel": ("Live" if kind in HOURLY_KINDS else
                                "Ready to license" if kind == "raw" else
                                "Ready to air"),
                "meta": clean(payload.get("meta"), 120) or "Independent production",
                "filed": "%s · %s" % (market, self.display_name(user)),
                "tech": clean(payload.get("tech"), 160) or "Delivery specs on file",
                "thumb": "img/reporter.svg" if kind == "irl" else (
                    "img/solar.svg" if kind == "package" else (
                        "img/docks.svg" if kind == "show" else (
                            "img/forum.svg" if kind == "raw"
                            else "img/podcast.svg"))),
                "license": "United States · Non-exclusive · Editorial only",
                "permitted": clean(payload.get("permitted"), 200) or
                              ("Linear TV + simultaneous web broadcast"
                               if kind in HOURLY_KINDS else "Linear TV + VOD + web"),
                "restrictions": "No resale or advertising use",
                "unpublished": False, "recorded": recorded,
                "created": iso(utcnow())}
            self.content.append(item)
            self._save("content.json", self.content)
        if asset_id and asset_mime.startswith("video"):
            self._maybe_auto_moderate(asset_id)
        return 201, item

    def update_content(self, user, cid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        scan_aid = ""
        with self.lock:
            item = next((c for c in self.content
                         if c.get("id") == cid and
                         c.get("ownerEmail") == user["email"]), None)
            if item is None:
                return 404, {"error": "not found"}
            data = payload or {}
            if "title" in data:
                title = clean(data.get("title"), 140)
                if not title:
                    return 400, {"error": "title is required"}
                item["title"] = title
            if "description" in data:
                desc = clean(data.get("description"), 2000)
                if not desc:
                    return 400, {"error": "description is required"}
                item["description"] = desc
            if "market" in data:
                market = clean(data.get("market"), 120)
                if not market:
                    return 400, {"error": "market is required"}
                item["market"] = market
                item["filed"] = "%s · %s" % (
                    market, self.display_name(user))
            if "price" in data:
                try:
                    price = round(float(data.get("price", 0)), 2)
                except (TypeError, ValueError):
                    price = -1
                if price <= 0:
                    return 400, {"error": "price must be above zero"}
                item["price"] = price
            if "recorded" in data:
                recorded = clean(data.get("recorded"), 20)
                if recorded and not parse_date(recorded):
                    return 400, {
                        "error": "recorded must be a date (YYYY-MM-DD)"}
                item["recorded"] = recorded
            if "assetId" in data:
                asset_id = clean(data.get("assetId"), 40)
                if asset_id:
                    asset = self.asset_for(asset_id)
                    if asset is None or asset["owner"] != user["email"]:
                        return 400, {"error": "unknown or unfinished upload"}
                    item["asset"] = asset_id
                    if (asset.get("mime") or "").startswith("video"):
                        scan_aid = asset_id
                else:
                    item["asset"] = ""
            self._save("content.json", self.content)
            result = dict(item)
        if scan_aid:
            self._maybe_auto_moderate(scan_aid)
        return 200, result

    def set_content_visibility(self, user, cid, published):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            item = next((c for c in self.content
                         if c.get("id") == cid and
                         c.get("ownerEmail") == user["email"]), None)
            if item is None:
                return 404, {"error": "not found"}
            item["unpublished"] = not published
            self._save("content.json", self.content)
        return 200, {"ok": True, "id": cid,
                     "unpublished": item["unpublished"]}

    # ---------- bookings ----------
    @staticmethod
    def booking_live(booking, now=None):
        if booking.get("status") == "cancelled":
            return False
        bounds = window_bounds(booking.get("date"), booking.get("from"),
                               booking.get("until"))
        if not bounds:
            return False
        now = now or utcnow()
        return bounds[0] - timedelta(minutes=5) <= now <= bounds[1]

    def booking_view(self, booking):
        view = dict(booking)
        view["liveNow"] = self.booking_live(booking)
        view["totalText"] = money(booking.get("total", 0))
        return view

    def create_booking(self, user, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        item = self.content_item(payload.get("contentId"))
        if item is None:
            return 404, {"error": "content not found"}
        if not item.get("bookable", True):
            return 409, {"error": "this feed is offline and cannot be booked"}
        hourly = item.get("unit") == "hour"
        qty = 1
        hours = 0
        date_s = clean(payload.get("date"), 20)
        from_s = clean(payload.get("from"), 10)
        until_s = clean(payload.get("until"), 10)
        if hourly:
            bounds = window_bounds(date_s, from_s, until_s)
            if not bounds:
                return 400, {"error": "date (YYYY-MM-DD) and a same-day "
                                      "from/until window (HH:MM) are required"}
            hours = round((bounds[1] - bounds[0]).total_seconds() / 3600, 2)
        else:
            if not parse_date(date_s):
                return 400, {"error": "date (YYYY-MM-DD) is required"}
            try:
                qty = int(payload.get("qty", 1))
            except (TypeError, ValueError):
                qty = 0
            if qty < 1 or qty > 99:
                return 400, {"error": "qty must be between 1 and 99"}
        last4 = re.sub(r"\D", "", str(payload.get("cardLast4") or ""))[-4:]
        if payload.get("cardLast4") and len(last4) != 4:
            return 400, {"error": "card last 4 must be 4 digits"}
        unit_price = item["price"]
        total = round(unit_price * (hours if hourly else qty), 2)
        with self.lock:
            booking = {
                "id": self._uid("bk", {b.get("id") for b in self.bookings}),
                "station": user["email"], "stationName": self.display_name(user),
                "contentId": item["id"], "status": "upcoming" if hourly else "licensed",
                "date": date_s, "from": from_s if hourly else "",
                "until": until_s if hourly else "",
                "qty": qty if not hourly else 1, "hours": hours,
                "unitPrice": unit_price, "total": total,
                "invoice": self._next_invoice(),
                "cardLast4": last4,
                "rights": "%s · %s" % (item.get("license", ""),
                                       item.get("permitted", "")),
                "created": iso(utcnow())}
            self.bookings.append(booking)
            self._save("bookings.json", self.bookings)
        return 201, self.booking_view(booking)

    def list_bookings(self, user):
        return [self.booking_view(b) for b in self.bookings
                if b.get("station") == user["email"]]

    def _own_booking(self, user, bid):
        return next((b for b in self.bookings
                     if b.get("id") == bid and b.get("station") == user["email"]),
                    None)

    def cancel_booking(self, user, bid):
        with self.lock:
            booking = self._own_booking(user, bid)
            if booking is None:
                return 404, {"error": "not found"}
            if booking.get("status") == "cancelled":
                return 409, {"error": "already cancelled"}
            bounds = window_bounds(booking.get("date"), booking.get("from"),
                                   booking.get("until"))
            if bounds and utcnow() > bounds[1]:
                return 409, {"error": "this window already ended"}
            booking["status"] = "cancelled"
            self._save("bookings.json", self.bookings)
        return 200, {"ok": True, "id": bid}

    def invoice_text(self, user, bid):
        booking = self._own_booking(user, bid)
        if booking is None:
            return None
        item = self.content_record(booking.get("contentId")) or {}
        window = booking.get("date", "")
        if booking.get("from"):
            window += " %s-%s EST" % (booking["from"], booking["until"])
        lines = [
            "RELAY NETWORK · INVOICE %s" % booking.get("invoice", "pending"),
            "Booking %s · %s" % (bid, item.get("title", booking["contentId"])),
            "Station: %s <%s>" % (booking.get("stationName", ""),
                                  booking.get("station", "")),
            "Window: %s" % window,
            "Rate: %s x %s = %s USD" % (
                money(booking.get("unitPrice", 0)),
                ("%s hours" % booking["hours"]) if booking.get("hours")
                else ("%s x %s" % (booking.get("qty", 1), item.get("unit", ""))),
                money(booking.get("total", 0))),
            "Paid with card ending %s (local billing record, no external charge)."
            % (booking.get("cardLast4") or "----"),
            "",
            booking.get("rights", ""),
            "Thank you for booking with Relay."]
        return "\n".join(lines) + "\n"

    def endpoints(self, user, bid, host="127.0.0.1:4173"):
        booking = self._own_booking(user, bid)
        if booking is None:
            return 404, {"error": "not found"}
        if not self.booking_live(booking):
            return 403, {"error": "endpoints release five minutes before "
                                  "the window and close when it ends"}
        token = hmac.new(self.secret.encode(), bid.encode(),
                         hashlib.sha256).hexdigest()[:16]
        cid = booking.get("contentId", "")
        bare = host.split(":")[0]
        return 200, {
            "srt": "srt://%s:9001/%s?token=%s" % (bare, cid, token),
            "hls": "http://%s/hls/%s/index.m3u8?token=%s" % (host, cid, token),
            "validUntil": "%s %s EST" % (booking["date"], booking["until"])}

    # ---------- coverage requests ----------
    def create_request(self, user, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        title = clean(payload.get("title"), 140)
        brief = clean(payload.get("brief"), 4000)
        if not title or not brief:
            return 400, {"error": "title and brief are required"}
        attachments = payload.get("attachments", [])
        if not isinstance(attachments, list):
            return 400, {"error": "attachments must be a list"}
        with self.lock:
            known = {u["id"] for u in self.uploads}
            for a in attachments:
                if not isinstance(a, str) or a not in known:
                    return 400, {"error": "unknown attachment %r" % (a,)}
            record = {
                "id": self._uid("rq", {r.get("id") for r in self.requests}),
                "station": user["email"], "stationName": self.display_name(user),
                "title": title, "brief": brief,
                "type": clean(payload.get("type"), 60) or "News coverage",
                "deliverable": clean(payload.get("deliverable"), 160),
                "language": clean(payload.get("language"), 80),
                "assets": [clean(a, 40) for a in payload.get("assets", [])][:10],
                "roles": [clean(r, 40) for r in payload.get("roles", [])][:12],
                "arrangement": clean(payload.get("arrangement"), 40),
                "location": clean(payload.get("location"), 160),
                "starts": clean(payload.get("starts"), 80),
                "due": clean(payload.get("due"), 80),
                "budget": clean(payload.get("budget"), 80),
                "exclusivity": clean(payload.get("exclusivity"), 60),
                "rights": [clean(r, 40) for r in payload.get("rights", [])][:10],
                "attachments": attachments,
                "status": "open", "applications": [],
                "created": iso(utcnow())}
            self.requests.append(record)
            self._save("requests.json", self.requests)
        return 201, record

    def list_requests(self, user):
        if user["role"] == "station":
            return [r for r in self.requests
                    if r.get("station") == user["email"]]
        mine = set(user.get("roles", []))
        out = []
        for r in self.requests:
            assigned_to_me = (r.get("assignee") or {}).get("email") == \
                user["email"]
            if r.get("status") != "open" and not assigned_to_me:
                continue
            view = dict(r)
            need = set(r.get("roles", []))
            view["match"] = bool(mine & need) if need else True
            view["applied"] = any(a.get("email") == user["email"]
                                  for a in r.get("applications", []))
            view["mine"] = assigned_to_me
            out.append(view)
        return out

    def list_assets(self, user):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        used = {}
        for c in self.content:
            if c.get("asset"):
                used.setdefault(c["asset"], []).append(c.get("title", c["id"]))
        rows = []
        for a in self.assets:
            if a.get("owner") != user["email"]:
                continue
            rows.append({"id": a["id"], "name": a["name"], "size": a["size"],
                         "mime": a["mime"], "created": a["created"],
                         "captions": self.captions.get(
                             a["id"], {}).get("status", "none"),
                         "moderation": self.moderation.get(
                             a["id"], {}).get("status", "none"),
                         "moderationAi": self.moderation.get(
                             a["id"], {}).get("ai", {}),
                         "folder": a.get("folder") or "uploads",
                         "complete": a.get("complete", False),
                         "listings": used.get(a["id"], [])})
        rows.sort(key=lambda r: r["created"], reverse=True)
        return 200, rows

    def sales_for(self, user):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        mine = {c["id"]: c for c in self.content
                if c.get("ownerEmail") == user["email"]}
        rows = []
        for b in self.bookings:
            item = mine.get(b.get("contentId"))
            if item is None or b.get("status") == "cancelled":
                continue
            hourly = item.get("unit") == "hour"
            rows.append({"bookingId": b["id"], "contentId": item["id"],
                         "title": item.get("title", ""),
                         "station": b.get("stationName", b.get("station", "")),
                         "date": b.get("date", ""),
                         "from": b.get("from", "") if hourly else "",
                         "until": b.get("until", "") if hourly else "",
                         "kind": item.get("kind", ""),
                         "kindLabel": item.get("kindLabel", ""),
                         "liveNow": self.booking_live(b),
                         "stationIfb": self.ifb.get(
                             b.get("station", ""), {}).get("url", "")
                         if item.get("kind") == "irl" else "",
                         "total": b.get("total", 0),
                         "created": b.get("created", "")})
        rows.sort(key=lambda r: r["created"], reverse=True)
        gross = round(sum(r["total"] for r in rows), 2)
        return 200, {"rows": rows, "gross": gross, "count": len(rows)}

    def get_ifb(self, user):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        return 200, {"url": self.ifb.get(user["email"], {}).get("url", "")}

    def put_ifb(self, user, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        url = clean((payload or {}).get("url"), 500)
        if url and not url.startswith(("http://", "https://")):
            return 400, {"error": "IFB must be an http(s) audio stream URL"}
        with self.lock:
            self.ifb[user["email"]] = {"url": url,
                                       "updated": iso(utcnow())}
            self._save("ifb.json", self.ifb)
            return 200, {"url": url}

    def expenses_for(self, user):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        rows = []
        for b in self.bookings:
            if b.get("station") != user["email"]:
                continue
            if b.get("status") == "cancelled":
                continue
            item = self.content_record(b.get("contentId")) or {}
            rows.append({
                "bookingId": b["id"], "contentId": b.get("contentId", ""),
                "title": item.get("title", b.get("contentId", "")),
                "kind": item.get("kind", ""),
                "kindLabel": item.get("kindLabel", ""),
                "vendor": item.get("owner", ""),
                "date": b.get("date", ""),
                "window": ("%s–%s" % (b.get("from", ""), b.get("until", ""))
                           if b.get("from") else b.get("date", "")),
                "invoice": b.get("invoice", ""),
                "status": b.get("status", ""),
                "total": b.get("total", 0),
                "created": b.get("created", "")})
        rows.sort(key=lambda r: r["created"], reverse=True)
        total = round(sum(r["total"] for r in rows), 2)
        return 200, {"rows": rows, "total": total, "count": len(rows)}

    def update_profile(self, user, payload):
        with self.lock:
            name = clean(payload.get("name"), 120)
            if name:
                user["name"] = name
            user["based"] = clean(payload.get("based"), 120)
            user["specialties"] = clean(payload.get("specialties"), 200)
            user["languages"] = clean(payload.get("languages"), 120)
            if user["role"] == "contributor" and "roles" in payload:
                roles = [clean(r, 40) for r in payload.get("roles", [])
                         if clean(r, 40)][:12]
                user["roles"] = roles
            if user["role"] in ("station", "partner") and payload.get("org"):
                user["org"] = clean(payload.get("org"), 120)
            self._save("users.json", self.users)
            view = self.public_user(user)
            view.update({"based": user.get("based", ""),
                         "specialties": user.get("specialties", ""),
                         "languages": user.get("languages", "")})
            return 200, view

    def apply(self, user, rid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            req = next((r for r in self.requests if r.get("id") == rid
                        and r.get("status") == "open"), None)
            if req is None:
                return 404, {"error": "brief not found or closed"}
            if any(a.get("email") == user["email"]
                   for a in req.get("applications", [])):
                return 409, {"error": "already applied"}
            req.setdefault("applications", []).append({
                "email": user["email"], "name": user.get("name", ""),
                "roles": user.get("roles", []),
                "note": clean((payload or {}).get("note"), 1000),
                "at": iso(utcnow())})
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": rid}

    def _own_request(self, user, rid):
        return next((r for r in self.requests
                     if r.get("id") == rid and r.get("station") == user["email"]),
                    None)

    def update_request(self, user, rid, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            req = self._own_request(user, rid)
            if req is None:
                return 404, {"error": "not found"}
            payload = payload or {}
            if "title" in payload:
                title = clean(payload.get("title"), 140)
                if not title:
                    return 400, {"error": "title cannot be empty"}
                req["title"] = title
            if "brief" in payload:
                brief = clean(payload.get("brief"), 4000)
                if not brief:
                    return 400, {"error": "brief cannot be empty"}
                req["brief"] = brief
            for key, limit in [("type", 60), ("deliverable", 160),
                               ("language", 80), ("arrangement", 40),
                               ("location", 160), ("starts", 80), ("due", 80),
                               ("budget", 80), ("exclusivity", 60)]:
                if key in payload:
                    req[key] = clean(payload.get(key), limit)
            for key, limit, cap in [("assets", 40, 10), ("roles", 40, 12),
                                    ("rights", 40, 10)]:
                if key in payload:
                    vals = payload.get(key)
                    if not isinstance(vals, list):
                        return 400, {"error": key + " must be a list"}
                    req[key] = [clean(v, limit) for v in vals][:cap]
            self._save("requests.json", self.requests)
        return 200, req

    def accept_application(self, user, rid, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            req = self._own_request(user, rid)
            if req is None:
                return 404, {"error": "not found"}
            if req.get("status") != "open":
                return 409, {"error": "brief is no longer open"}
            email = ((payload or {}).get("email") or "").strip().lower()
            applicant = next((a for a in req.get("applications", [])
                              if a.get("email") == email), None)
            if applicant is None:
                return 404, {"error": "applicant not found"}
            req["status"] = "assigned"
            req["assignee"] = {"email": applicant["email"],
                               "name": applicant.get("name", "")}
            req["assignedAt"] = iso(utcnow())
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": rid}

    def assign_direct(self, user, rid, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            req = self._own_request(user, rid)
            if req is None:
                return 404, {"error": "not found"}
            if req.get("status") != "open":
                return 409, {"error": "brief is no longer open"}
            email = ((payload or {}).get("email") or "").strip().lower()
            person = self.find_user(email)
            if person is None or person["role"] != "contributor":
                return 404, {"error": "contributor not found"}
            req["status"] = "assigned"
            req["assignee"] = {"email": person["email"],
                               "name": person.get("name", "")}
            req["assignedAt"] = iso(utcnow())
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": rid}

    def directory(self, user):
        if user["role"] == "station":
            return 200, [{"name": u.get("name", ""), "email": u["email"],
                          "roles": u.get("roles", []),
                          "specialties": u.get("specialties", "")}
                         for u in self.users if u["role"] == "contributor"]
        if user["role"] == "contributor":
            return 200, [{"org": self.display_name(u), "email": u["email"]}
                         for u in self.users if u["role"] == "station"]
        return 403, {"error": "stations and contributors only"}

    def create_pitch(self, user, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        email = ((payload or {}).get("station") or "").strip().lower()
        station = self.find_user(email)
        if station is None or station["role"] != "station":
            return 404, {"error": "station not found"}
        title = clean(payload.get("title"), 140)
        brief = clean(payload.get("brief"), 4000)
        if not title or not brief:
            return 400, {"error": "title and brief are required"}
        asset_id = clean((payload or {}).get("assetId"), 40)
        asset_mime = ""
        if asset_id:
            asset = self.asset_for(asset_id)
            if asset is None or asset["owner"] != user["email"]:
                return 400, {"error": "unknown or unfinished upload"}
            asset_mime = asset["mime"]
        with self.lock:
            pitch = {"id": self._uid("pt", {p.get("id") for p in self.pitches}),
                     "station": station["email"],
                     "stationName": self.display_name(station),
                     "pitcher": user["email"], "pitcherName": user.get("name", ""),
                     "title": title, "brief": brief,
                     "budget": clean(payload.get("budget"), 80),
                     "assetId": asset_id, "assetMime": asset_mime,
                     "status": "pending",
                     "created": iso(utcnow())}
            self.pitches.append(pitch)
            self._save("pitches.json", self.pitches)
        if asset_id and asset_mime.startswith("video"):
            self._maybe_auto_moderate(asset_id)
        return 201, pitch

    def list_pitches(self, user):
        if user["role"] == "station":
            rows = [p for p in self.pitches
                    if p.get("station") == user["email"]]
        elif user["role"] == "contributor":
            rows = [p for p in self.pitches
                    if p.get("pitcher") == user["email"]]
        else:
            return 403, {"error": "stations and contributors only"}
        rows.sort(key=lambda r: r["created"], reverse=True)
        return 200, rows

    def decide_pitch(self, user, pid, accept):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            pitch = next((p for p in self.pitches
                          if p.get("id") == pid
                          and p.get("station") == user["email"]), None)
            if pitch is None:
                return 404, {"error": "not found"}
            if pitch.get("status") != "pending":
                return 409, {"error": "pitch already decided"}
            pitch["status"] = "accepted" if accept else "declined"
            pitch["decidedAt"] = iso(utcnow())
            if accept:
                record = {
                    "id": self._uid("rq", {r.get("id") for r in self.requests}),
                    "station": user["email"],
                    "stationName": self.display_name(user),
                    "title": pitch["title"], "brief": pitch["brief"],
                    "type": "Commissioned pitch",
                    "budget": pitch.get("budget", ""),
                    "roles": [], "assets": [], "rights": [],
                    "status": "assigned",
                    "assignee": {"email": pitch["pitcher"],
                                 "name": pitch.get("pitcherName", "")},
                    "assignedAt": iso(utcnow()),
                    "pitchAsset": pitch.get("assetId", ""),
                    "applications": [], "deliveries": [], "notes": [],
                    "created": iso(utcnow())}
                self.requests.append(record)
                self._save("requests.json", self.requests)
                pitch["requestId"] = record["id"]
            self._save("pitches.json", self.pitches)
        return 200, {"ok": True, "id": pid, "status": pitch["status"]}

    def submit_delivery(self, user, rid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            req = next((r for r in self.requests if r.get("id") == rid), None)
            if req is None or (req.get("assignee") or {}).get("email") != \
                    user["email"]:
                return 404, {"error": "assignment not found"}
            if req.get("status") != "assigned":
                return 409, {"error": "nothing to submit right now"}
            asset_id = clean((payload or {}).get("assetId"), 40)
            asset_name, asset_mime = "", ""
            if asset_id:
                asset = self.asset_for(asset_id)
                if asset is None or asset["owner"] != user["email"]:
                    return 400, {"error": "unknown or unfinished upload"}
                asset_name, asset_mime = asset["name"], asset["mime"]
            req["status"] = "submitted"
            version = {"v": len(req.get("deliveries", [])) + 1,
                       "assetId": asset_id,
                       "assetName": asset_name, "assetMime": asset_mime,
                       "note": clean((payload or {}).get("note"), 1000),
                       "at": iso(utcnow())}
            req.setdefault("deliveries", []).append(version)
            req["delivery"] = version
            self._save("requests.json", self.requests)
        if asset_id and asset_mime.startswith("video"):
            self._maybe_auto_moderate(asset_id)
        return 200, {"ok": True, "id": rid}

    def approve_delivery(self, user, rid):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            req = self._own_request(user, rid)
            if req is None:
                return 404, {"error": "not found"}
            if req.get("status") != "submitted":
                return 409, {"error": "no delivery to approve"}
            req["status"] = "completed"
            req["completedAt"] = iso(utcnow())
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": rid}

    def _assignment_party(self, user, rid):
        req = next((r for r in self.requests if r.get("id") == rid), None)
        if req is None or req.get("status") == "open":
            return None
        if user["email"] in (req.get("station"),
                             (req.get("assignee") or {}).get("email")):
            return req
        return None

    def post_note(self, user, rid, payload):
        with self.lock:
            req = self._assignment_party(user, rid)
            if req is None:
                return 404, {"error": "assignment not found"}
            text = clean((payload or {}).get("text"), 1000)
            tc = clean((payload or {}).get("tc"), 12)
            if not text:
                return 400, {"error": "note text is required"}
            if tc and not re.match(r"^\d{1,3}:[0-5]\d(:[0-5]\d)?$", tc):
                return 400, {"error": "timecode must look like 01:12"}
            note = {"id": self._uid("nt", {n.get("id")
                                           for n in req.get("notes", [])}),
                    "author": user.get("name", ""), "role": user["role"],
                    "text": text, "tc": tc, "at": iso(utcnow()),
                    "resolved": False}
            req.setdefault("notes", []).append(note)
            self._save("requests.json", self.requests)
        return 201, note

    def resolve_note(self, user, rid, nid):
        with self.lock:
            req = self._assignment_party(user, rid)
            if req is None:
                return 404, {"error": "assignment not found"}
            note = next((n for n in req.get("notes", [])
                         if n.get("id") == nid), None)
            if note is None:
                return 404, {"error": "note not found"}
            note["resolved"] = not note.get("resolved", False)
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": nid, "resolved": note["resolved"]}

    def notifications_for(self, user):
        if user["role"] == "station":
            items = self._station_notifications(user)
        elif user["role"] == "contributor":
            items = self._contributor_notifications(user)
        elif user["role"] == "partner":
            items = self._partner_notifications(user)
        else:
            return 403, {"error": "unknown role"}
        items.sort(key=lambda i: i["at"], reverse=True)
        items = items[:20]
        seen = self.notif_seen.get(user["email"], "")
        for i in items:
            i["unread"] = bool(i["at"]) and i["at"] > seen
        return 200, {"items": items,
                     "unread": sum(1 for i in items if i["unread"])}

    def _station_notifications(self, user):
        items = []
        for r in self.requests:
            if r.get("station") != user["email"]:
                continue
            for a in r.get("applications", []):
                items.append({
                    "id": "app:%s:%s" % (r["id"], a.get("email", "")),
                    "kind": "application",
                    "title": "%s applied to “%s”" % (
                        a.get("name") or a.get("email", ""), r.get("title")),
                    "detail": a.get("note", ""),
                    "at": a.get("at") or r.get("created", ""),
                    "link": "/requests"})
            for v in r.get("deliveries", []):
                items.append({
                    "id": "del:%s:%s" % (r["id"], v.get("v")),
                    "kind": "delivery",
                    "title": "Delivery v%s of “%s” is ready" % (
                        v.get("v"), r.get("title")),
                    "detail": v.get("note", ""),
                    "at": v.get("at", ""),
                    "link": "/requests"})
            for v in r.get("deliveries", []):
                mod = self.moderation.get(v.get("assetId") or "")
                if not mod:
                    continue
                if mod.get("status") == "flagged":
                    flags = mod.get("flags", [])
                    items.append({
                        "id": "mod:%s:%s" % (r["id"], v.get("v")),
                        "kind": "flagged",
                        "title": "Delivery v%s of “%s” was auto-blurred" % (
                            v.get("v"), r.get("title")),
                        "detail": "%d scene%s: %s" % (
                            len(flags), "" if len(flags) == 1 else "s",
                            ", ".join(_flag_stamp(f) for f in flags[:4])),
                        "at": mod.get("updated", ""),
                        "link": "/requests"})
                ai = mod.get("ai") or {}
                if ai.get("verdict") == "ai":
                    items.append({
                        "id": "aimod:%s:%s" % (r["id"], v.get("v")),
                        "kind": "ai",
                        "title": "Delivery v%s of “%s” may be AI-generated"
                        % (v.get("v"), r.get("title")),
                        "detail": "%d%% of %d checked frames look synthetic"
                        % (int(round(ai.get("score", 0) * 100)),
                           ai.get("judged", 0)),
                        "at": mod.get("updated", ""),
                        "link": "/requests"})
            for n in r.get("notes", []):
                if (n.get("role") or "") != "contributor":
                    continue
                items.append({
                    "id": "note:%s:%s" % (r["id"], n.get("id")),
                    "kind": "note",
                    "title": "%s noted on “%s”" % (
                        n.get("author", ""), r.get("title")),
                    "detail": n.get("text", ""),
                    "at": n.get("at", ""),
                    "link": "/requests"})
        for p in self.pitches:
            if p.get("station") != user["email"]:
                continue
            items.append({
                "id": "pitch:%s" % p["id"],
                "kind": "pitch",
                "title": "%s pitched “%s”" % (
                    p.get("pitcherName", ""), p.get("title")),
                "detail": (p.get("brief", "") or "")[:140],
                "at": p.get("created", ""),
                "link": "/requests"})
            mod = self.moderation.get(p.get("assetId") or "")
            if mod and mod.get("status") == "flagged":
                flags = mod.get("flags", [])
                items.append({
                    "id": "pitchmod:%s" % p["id"],
                    "kind": "flagged",
                    "title": "Pitch “%s” sample was auto-blurred" % (
                        p.get("title")),
                    "detail": "%d scene%s: %s" % (
                        len(flags), "" if len(flags) == 1 else "s",
                        ", ".join(_flag_stamp(f) for f in flags[:4])),
                    "at": mod.get("updated", ""),
                    "link": "/requests"})
            ai = (mod or {}).get("ai") or {}
            if ai.get("verdict") == "ai":
                items.append({
                    "id": "pitchai:%s" % p["id"],
                    "kind": "ai",
                    "title": "Pitch “%s” sample may be AI-generated" % (
                        p.get("title")),
                    "detail": "%d%% of %d checked frames look synthetic"
                    % (int(round(ai.get("score", 0) * 100)),
                       ai.get("judged", 0)),
                    "at": mod.get("updated", ""),
                    "link": "/requests"})
        return items

    def _contributor_notifications(self, user):
        items = []
        mine = set(user.get("roles", []))
        for r in self.requests:
            assigned_to_me = (r.get("assignee") or {}).get("email") == \
                user["email"]
            if r.get("status") == "open" and not assigned_to_me:
                applied = any(a.get("email") == user["email"]
                              for a in r.get("applications", []))
                need = set(r.get("roles", []))
                if not applied and (bool(mine & need) if need else True):
                    items.append({
                        "id": "brief:%s" % r["id"],
                        "kind": "brief",
                        "title": "New brief: “%s”" % r.get("title"),
                        "detail": "%s · %s" % (r.get("stationName", ""),
                                               r.get("budget", "")),
                        "at": r.get("created", ""),
                        "link": "/contributor"})
                continue
            if not assigned_to_me:
                continue
            if r.get("assignedAt"):
                items.append({
                    "id": "assigned:%s" % r["id"],
                    "kind": "assigned",
                    "title": "“%s” was assigned to you" % r.get("title"),
                    "detail": r.get("stationName", ""),
                    "at": r.get("assignedAt", ""),
                    "link": "/assignments"})
            if r.get("feedback"):
                items.append({
                    "id": "changes:%s" % r["id"],
                    "kind": "changes",
                    "title": "Changes requested on “%s”" % r.get("title"),
                    "detail": r.get("feedback", ""),
                    "at": r.get("feedbackAt", ""),
                    "link": "/assignments"})
            if r.get("status") == "completed":
                items.append({
                    "id": "approved:%s" % r["id"],
                    "kind": "approved",
                    "title": "Your delivery for “%s” was approved" % (
                        r.get("title")),
                    "detail": "",
                    "at": r.get("completedAt", ""),
                    "link": "/assignments"})
            latest = r.get("delivery") or {}
            mod = self.moderation.get(latest.get("assetId") or "")
            if mod and mod.get("status") == "flagged":
                flags = mod.get("flags", [])
                items.append({
                    "id": "modme:%s" % r["id"],
                    "kind": "flagged",
                    "title": "Your delivery for “%s” was flagged" % (
                        r.get("title")),
                    "detail": "%d scene%s auto-blurred: %s" % (
                        len(flags), "" if len(flags) == 1 else "s",
                        ", ".join(_flag_stamp(f) for f in flags[:4])),
                    "at": mod.get("updated", ""),
                    "link": "/assignments"})
            for n in r.get("notes", []):
                if (n.get("role") or "") != "station":
                    continue
                items.append({
                    "id": "note:%s:%s" % (r["id"], n.get("id")),
                    "kind": "note",
                    "title": "%s noted on “%s”" % (
                        n.get("author", ""), r.get("title")),
                    "detail": n.get("text", ""),
                    "at": n.get("at", ""),
                    "link": "/assignments"})
        for p in self.pitches:
            if p.get("pitcher") != user["email"]:
                continue
            if p.get("status") == "accepted":
                items.append({
                    "id": "pitchdec:%s" % p["id"],
                    "kind": "pitch",
                    "title": "Your pitch “%s” was accepted" % p.get("title"),
                    "detail": "",
                    "at": p.get("decidedAt", ""),
                    "link": "/assignments"})
            elif p.get("status") == "declined":
                items.append({
                    "id": "pitchdec:%s" % p["id"],
                    "kind": "pitch",
                    "title": "Your pitch “%s” was declined" % p.get("title"),
                    "detail": "",
                    "at": p.get("decidedAt", ""),
                    "link": "/assignments"})
        return items

    def _partner_notifications(self, user):
        items = []
        mine = {f["id"]: f for f in self.feeds
                if f.get("owner") == user["email"]}
        for b in self.bookings:
            feed = mine.get(b.get("contentId"))
            if feed is None or b.get("status") == "cancelled":
                continue
            window = b.get("date", "")
            if b.get("from"):
                window += " %s–%s EST" % (b.get("from"), b.get("until"))
            items.append({
                "id": "book:%s" % b["id"],
                "kind": "booking",
                "title": "%s booked “%s”" % (
                    b.get("stationName", b.get("station", "")),
                    feed.get("name", "")),
                "detail": window,
                "at": b.get("created", ""),
                "link": "/partner#usage"})
        return items

    def mark_notifications_seen(self, user):
        with self.lock:
            self.notif_seen[user["email"]] = iso(utcnow())
            self._save("notif_seen.json", self.notif_seen)
        return 200, {"ok": True}

    def request_changes(self, user, rid, payload):
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        with self.lock:
            req = self._own_request(user, rid)
            if req is None:
                return 404, {"error": "not found"}
            if req.get("status") != "submitted":
                return 409, {"error": "no delivery to review"}
            req["status"] = "assigned"
            req["feedback"] = clean((payload or {}).get("feedback"), 1000)
            req["feedbackAt"] = iso(utcnow())
            self._save("requests.json", self.requests)
        return 200, {"ok": True, "id": rid}

    # ---------- partner feeds ----------
    def list_feeds(self, user):
        out = []
        for f in self.feeds:
            if f.get("owner") != user["email"]:
                continue
            view = dict(f)
            rows = [b for b in self.bookings
                    if b.get("contentId") == f["id"]
                    and b.get("status") != "cancelled"]
            view["hoursUsed"] = round(sum(b.get("hours", 0) for b in rows), 2)
            view["bookings"] = len(rows)
            out.append(view)
        return out

    def create_feed(self, user, payload):
        if user["role"] != "partner":
            return 403, {"error": "partners only"}
        name = clean(payload.get("name"), 140)
        location = clean(payload.get("location"), 160)
        uplink = clean(payload.get("uplink"), 500)
        if uplink and not re.match(r"^(https?|srt|rtsp|rtmp)://.{3,}$", uplink):
            return 400, {"error": "uplink must be an http(s), srt, rtsp or "
                                  "rtmp URL"}
        try:
            rate = round(float(payload.get("rate", 0)), 2)
        except (TypeError, ValueError):
            rate = 0
        if not name or not location or rate <= 0:
            return 400, {"error": "feed name, install location and an hourly "
                                  "rate above zero are required"}
        with self.lock:
            codes = {f.get("code") for f in self.feeds}
            n = len(self.feeds) + 1
            while "CAM-%02d" % n in codes:
                n += 1
            feed = {
                "id": self._uid("f", {f.get("id") for f in self.feeds}),
                "code": "CAM-%02d" % n, "name": name, "location": location,
                "market": clean(payload.get("market"), 120),
                "coords": clean(payload.get("coords"), 60),
                "uplink": uplink,
                "rate": rate, "net": round(rate * PARTNER_SHARE, 2),
                "status": "pending", "signal": "Awaiting signal check",
                "thumb": "img/harbor.svg", "owner": user["email"],
                "created": iso(utcnow())}
            self.feeds.append(feed)
            self._save("feeds.json", self.feeds)
        return 201, feed

    def feed_status(self, user, fid, payload):
        if user["role"] != "partner":
            return 403, {"error": "partners only"}
        status = (payload.get("status") or "").strip().lower()
        if status not in ("live", "offline"):
            return 400, {"error": "status must be live or offline"}
        with self.lock:
            feed = next((f for f in self.feeds
                         if f.get("id") == fid and f.get("owner") == user["email"]),
                        None)
            if feed is None:
                return 404, {"error": "not found"}
            feed["status"] = status
            if status == "live":
                feed.pop("note", None)
                if feed.get("signal") in ("", "Awaiting signal check"):
                    feed["signal"] = "1080p29.97 · SRT / HLS"
            else:
                note = clean(payload.get("note"), 160)
                if note:
                    feed["note"] = note
            self._save("feeds.json", self.feeds)
        return 200, {"ok": True, "id": fid, "status": status}

    # ---------- usage & earnings, derived from real bookings ----------
    def usage_for(self, user):
        mine = {f["id"]: f for f in self.feeds
                if f.get("owner") == user["email"]}
        rows = []
        for b in self.bookings:
            feed = mine.get(b.get("contentId"))
            if feed is None or b.get("status") == "cancelled":
                continue
            hours = b.get("hours", 0) or 0
            rows.append({
                "bookingId": b["id"], "feedId": feed["id"],
                "feed": "%s · %s" % (feed.get("code", ""), feed.get("name", "")),
                "station": b.get("stationName", b.get("station", "")),
                "date": b.get("date", ""),
                "window": "%s–%s" % (b.get("from", ""), b.get("until", ""))
                          if b.get("from") else b.get("date", ""),
                "hours": hours,
                "earnings": round(hours * feed.get("rate", 0) * PARTNER_SHARE, 2),
                "created": b.get("created", "")})
        rows.sort(key=lambda r: (r["date"], r["window"]), reverse=True)
        return rows

    # ---------- saved content ----------
    def get_saved(self, user):
        ids = self.saved.get(user["email"], [])
        return {"saved": [i for i in ids if self.content_item(i)]}

    def add_saved(self, user, payload):
        cid = payload.get("contentId")
        if not self.content_item(cid):
            return 404, {"error": "content not found"}
        with self.lock:
            ids = self.saved.setdefault(user["email"], [])
            if cid not in ids:
                ids.append(cid)
            self._save("saved.json", self.saved)
            return 200, {"saved": list(ids)}

    def remove_saved(self, user, cid):
        with self.lock:
            ids = self.saved.get(user["email"], [])
            if cid in ids:
                ids.remove(cid)
                self._save("saved.json", self.saved)
            return 200, {"saved": list(ids)}

    # ---------- partner settings ----------
    @staticmethod
    def _default_settings():
        return {"bank": "", "notif": "Email me on signal loss"}

    def get_settings(self, user):
        if user["role"] != "partner":
            return 403, {"error": "partners only"}
        merged = self._default_settings()
        merged.update(self.settings.get(user["email"], {}))
        return 200, merged

    def put_settings(self, user, payload):
        if user["role"] != "partner":
            return 403, {"error": "partners only"}
        notif = clean(payload.get("notif"), 60)
        if notif not in ("Email me on signal loss", "Email + SMS on signal loss",
                         "Muted"):
            return 400, {"error": "unknown notification preference"}
        with self.lock:
            self.settings[user["email"]] = {
                "bank": clean(payload.get("bank"), 120), "notif": notif}
            self._save("settings.json", self.settings)
            return 200, self.settings[user["email"]]

    # ---------- brief attachments ----------
    def upload(self, user, payload):
        name = clean(payload.get("name"), 120)
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        if ext not in UPLOAD_EXTS:
            return 400, {"error": "only PDF, JPG or PNG attachments"}
        try:
            raw = base64.b64decode(payload.get("data") or "", validate=True)
        except (ValueError, TypeError):
            return 400, {"error": "invalid file data"}
        if not raw or len(raw) > MAX_UPLOAD:
            return 400, {"error": "file must be between 1 byte and 5 MB"}
        with self.lock:
            uid = self._uid("up", {u["id"] for u in self.uploads})
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            stored = "%s_%s" % (uid, safe)
            with open(os.path.join(self.dir, "uploads", stored), "wb") as f:
                f.write(raw)
            record = {"id": uid, "name": name, "stored": stored,
                      "size": len(raw), "mime": UPLOAD_EXTS[ext],
                      "owner": user["email"], "created": iso(utcnow())}
            self.uploads.append(record)
            self._save("uploads.json", self.uploads)
        return 201, {"id": uid, "name": name, "size": len(raw)}

    def upload_path(self, uid):
        record = next((u for u in self.uploads if u["id"] == uid), None)
        if record is None:
            return None, None
        path = os.path.join(self.dir, "uploads", record["stored"])
        if not os.path.isfile(path):
            return None, None
        return path, record

    # ---------- contributor media assets (chunked upload) ----------
    def _purge_stale_assets(self):
        cutoff = iso(utcnow() - timedelta(hours=24))
        stale = [a for a in self.assets
                 if not a.get("complete") and a.get("created", "") < cutoff]
        for a in stale:
            try:
                os.remove(os.path.join(self.dir, "assets", a["stored"]))
            except OSError:
                pass
            self.assets.remove(a)
        if stale:
            self._save("assets.json", self.assets)

    def asset_init(self, user, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        name = clean(payload.get("name"), 160)
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        try:
            size = int(payload.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if ext not in ASSET_EXTS:
            return 400, {"error": "only video or audio files (mp4, webm, mov, "
                                  "mkv, mp3, wav, m4a)"}
        if size < 1 or size > MAX_ASSET:
            return 400, {"error": "file must be between 1 byte and 2 GB"}
        with self.lock:
            self._purge_stale_assets()
            uid = self._uid("as", {a["id"] for a in self.assets})
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            stored = "%s_%s.part" % (uid, safe)
            open(os.path.join(self.dir, "assets", stored), "wb").close()
            record = {"id": uid, "name": name, "stored": stored,
                      "size": size, "received": 0, "chunks": 0,
                      "mime": ASSET_EXTS[ext],
                      "owner": user["email"], "created": iso(utcnow()),
                      "folder": "uploads", "complete": False}
            self.assets.append(record)
            self._save("assets.json", self.assets)
        return 201, {"id": uid, "name": name, "size": size,
                     "chunkSize": 1024 * 1024}

    def _own_asset(self, user, aid):
        return next((a for a in self.assets
                     if a["id"] == aid and a.get("owner") == user["email"]),
                    None)

    def asset_chunk(self, user, aid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        try:
            raw = base64.b64decode(payload.get("data") or "", validate=True)
        except (ValueError, TypeError):
            return 400, {"error": "invalid chunk data"}
        if not raw or len(raw) > 4 * 1024 * 1024:
            return 400, {"error": "chunk must be between 1 byte and 4 MB"}
        try:
            index = int(payload.get("index", -1))
        except (TypeError, ValueError):
            return 400, {"error": "chunk index is required"}
        with self.lock:
            asset = self._own_asset(user, aid)
            if asset is None:
                known = any(a["id"] == aid for a in self.assets)
                return (403 if known else 404), {"error": "not found"}
            if asset.get("complete"):
                return 409, {"error": "upload already complete"}
            expected = asset.get("chunks", 0)
            if index != expected:
                return 409, {"error": "expected chunk %d" % expected}
            if asset["received"] + len(raw) > asset["size"]:
                return 400, {"error": "upload exceeds declared size"}
            with open(os.path.join(self.dir, "assets", asset["stored"]),
                      "ab") as f:
                f.write(raw)
            asset["received"] += len(raw)
            asset["chunks"] = expected + 1
            self._save("assets.json", self.assets)
            return 200, {"received": asset["received"], "size": asset["size"]}

    def asset_complete(self, user, aid):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            asset = self._own_asset(user, aid)
            if asset is None:
                known = any(a["id"] == aid for a in self.assets)
                return (403 if known else 404), {"error": "not found"}
            if asset.get("complete"):
                return 200, {"id": aid, "name": asset["name"],
                             "size": asset["size"]}
            if asset.get("received", 0) != asset["size"]:
                return 400, {"error": "upload incomplete (%d of %d bytes)" % (
                    asset.get("received", 0), asset["size"])}
            final = asset["stored"]
            if final.endswith(".part"):
                final = final[:-len(".part")]
                os.replace(os.path.join(self.dir, "assets", asset["stored"]),
                           os.path.join(self.dir, "assets", final))
                asset["stored"] = final
            asset["complete"] = True
            self._save("assets.json", self.assets)
            return 200, {"id": aid, "name": asset["name"],
                         "size": asset["size"]}

    def delete_asset(self, user, aid):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            asset = self._own_asset(user, aid)
            if asset is None:
                known = any(a["id"] == aid for a in self.assets)
                return (403 if known else 404), {"error": "not found"}
            used = [c.get("title", c["id"]) for c in self.content
                    if c.get("asset") == aid]
            if used:
                return 409, {"error": "“%s” is still live on the "
                                      "marketplace in: %s"
                             % (asset["name"], ", ".join(used[:3]))}
            if self._asset_in_deliveries(aid):
                return 409, {"error": "“%s” was delivered to a station and "
                                      "must stay for their records"
                             % asset["name"]}
            if any(p.get("assetId") == aid for p in self.pitches):
                return 409, {"error": "“%s” is attached to a pitch sample"
                             % asset["name"]}
            self.assets = [a for a in self.assets if a["id"] != aid]
            self._save("assets.json", self.assets)
            self.captions.pop(aid, None)
            self._save("captions.json", self.captions)
            self.moderation.pop(aid, None)
            self._save("moderation.json", self.moderation)
            self.intelligence.pop(aid, None)
            self._save("intelligence.json", self.intelligence)
            stored = asset["stored"]
        for path in (os.path.join(self.dir, "assets", stored),
                     os.path.join(self.dir, "captions", aid + ".vtt"),
                     os.path.join(self.dir, "moderation",
                                  aid + ".blur.mp4")):
            try:
                os.remove(path)
            except OSError:
                pass
        return 200, {"ok": True, "id": aid}

    def _user_folders(self, email):
        fresh = email not in self.folders
        folders = self.folders.setdefault(email, [])
        if fresh:
            now = iso(utcnow())
            folders.extend([
                {"id": "uploads", "name": "Uploads", "created": now,
                 "system": True},
                {"id": "raw", "name": "Raw footage", "created": now},
                {"id": "finals", "name": "Finals", "created": now}])
        elif not any(f.get("id") == "uploads" for f in folders):
            folders.insert(0, {"id": "uploads", "name": "Uploads",
                               "created": iso(utcnow()), "system": True})
        return folders

    def list_folders(self, user):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            folders = self._user_folders(user["email"])
            counts = {}
            for a in self.assets:
                if a.get("owner") == user["email"]:
                    fid = a.get("folder") or "uploads"
                    counts[fid] = counts.get(fid, 0) + 1
            rows = [dict(f, files=counts.get(f["id"], 0))
                    for f in folders]
            self._save("folders.json", self.folders)
        return 200, rows

    def create_folder(self, user, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        name = clean((payload or {}).get("name"), 60)
        if not name:
            return 400, {"error": "folder name is required"}
        with self.lock:
            folders = self._user_folders(user["email"])
            if any(f["name"].lower() == name.lower() for f in folders):
                return 409, {"error": "a folder with that name "
                                      "already exists"}
            known = {f["id"] for fs in self.folders.values() for f in fs}
            folder = {"id": self._uid("fld", known), "name": name,
                      "created": iso(utcnow())}
            folders.append(folder)
            self._save("folders.json", self.folders)
        return 201, folder

    def rename_folder(self, user, fid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        name = clean((payload or {}).get("name"), 60)
        if not name:
            return 400, {"error": "folder name is required"}
        with self.lock:
            folders = self._user_folders(user["email"])
            folder = next((f for f in folders if f.get("id") == fid),
                          None)
            if folder is None:
                return 404, {"error": "not found"}
            if folder.get("system"):
                return 400, {"error": "the Uploads folder cannot "
                                      "be renamed"}
            if any(f["name"].lower() == name.lower() and
                   f["id"] != fid for f in folders):
                return 409, {"error": "a folder with that name "
                                      "already exists"}
            folder["name"] = name
            self._save("folders.json", self.folders)
        return 200, folder

    def delete_folder(self, user, fid):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            folders = self._user_folders(user["email"])
            folder = next((f for f in folders if f.get("id") == fid),
                          None)
            if folder is None:
                return 404, {"error": "not found"}
            if folder.get("system"):
                return 400, {"error": "the Uploads folder cannot "
                                      "be deleted"}
            self.folders[user["email"]] = [
                f for f in folders if f["id"] != fid]
            moved = 0
            for a in self.assets:
                if a.get("owner") == user["email"] and \
                        a.get("folder") == fid:
                    a["folder"] = "uploads"
                    moved += 1
            self._save("folders.json", self.folders)
            self._save("assets.json", self.assets)
        return 200, {"ok": True, "moved": moved}

    def move_asset(self, user, aid, payload):
        if user["role"] != "contributor":
            return 403, {"error": "contributors only"}
        with self.lock:
            asset = self._own_asset(user, aid)
            if asset is None:
                known = any(a["id"] == aid for a in self.assets)
                return (403 if known else 404), {"error": "not found"}
            fid = clean((payload or {}).get("folder"), 40)
            folders = self._user_folders(user["email"])
            if not any(f.get("id") == fid for f in folders):
                return 404, {"error": "folder not found"}
            asset["folder"] = fid
            self._save("assets.json", self.assets)
            self._save("folders.json", self.folders)
        return 200, {"ok": True, "folder": fid}

    def asset_for(self, aid):
        return next((a for a in self.assets
                     if a["id"] == aid and a.get("complete")), None)

    def _asset_in_deliveries(self, aid):
        return any(aid == v.get("assetId")
                   for r in self.requests for v in r.get("deliveries", []))

    def _asset_in_listings(self, aid):
        return any(c.get("asset") == aid and not c.get("unpublished")
                   for c in self.content)

    def _asset_in_unpublished_listings(self, aid):
        return any(c.get("asset") == aid and c.get("unpublished")
                   for c in self.content)

    def asset_access(self, user, aid):
        """(record, None) if user may fetch, else (None, status).

        A marketplace listing doubles as its file's public preview, so
        every station can stream it. Files delivered to a coverage
        request stay private to that project: only the commissioning
        station and the file owner may fetch them. Files attached only
        to unpublished listings are hidden from stations.
        """
        asset = self.asset_for(aid)
        if asset is None:
            return None, 404
        if user["email"] == asset["owner"]:
            return asset, None
        if user["role"] != "station":
            return None, 403
        if self._asset_in_listings(aid):
            return asset, None
        mine = [v.get("assetId") for r in self.requests
                if r.get("station") == user["email"]
                for v in r.get("deliveries", [])]
        if aid in mine:
            return asset, None
        if self._asset_in_deliveries(aid) or \
                self._asset_in_unpublished_listings(aid):
            return None, 403
        return asset, None

    def request_captions(self, user, aid, payload=None):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        if not META_API_KEY:
            return 503, {"error": "captioning is not configured "
                                  "(META_API_KEY is missing)"}
        auto = bool((payload or {}).get("auto"))
        if auto and os.environ.get("CAPTIONS_AUTO", "1") == "0":
            return 200, {"status": "disabled"}
        with self.lock:
            current = self.captions.get(aid, {}).get("status")
            if current == "ready" and not os.path.isfile(
                    os.path.join(self.dir, "captions", aid + ".json")):
                current = None  # transcript without cues: regenerate below
            if current == "ready":
                return 200, {"status": "ready"}
            if current == "pending":
                return 202, {"status": "pending"}
            self.captions[aid] = {"status": "pending",
                                  "updated": iso(utcnow())}
            self._save("captions.json", self.captions)
        enqueue_caption(self, aid)
        return 202, {"status": "pending"}

    def caption_status(self, user, aid):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        return 200, self.captions.get(aid, {"status": "none"})

    def caption_cues(self, user, aid):
        """Timed cues for the roll-up player. Same gates as caption_status,
        plus the transcript must be ready and on disk."""
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        if self.captions.get(aid, {}).get("status") != "ready":
            return 404, {"error": "no captions yet"}
        try:
            with open(os.path.join(self.dir, "captions", aid + ".json"),
                      encoding="utf-8") as f:
                cues = json.load(f).get("cues", [])
        except (OSError, ValueError):
            return 404, {"error": "no captions yet"}
        return 200, {"cues": cues if isinstance(cues, list) else []}

    def _set_caption(self, aid, status, error=""):
        with self.lock:
            self.captions[aid] = {"status": status, "error": error,
                                  "updated": iso(utcnow())}
            self._save("captions.json", self.captions)

    def _set_intelligence(self, aid, body):
        with self.lock:
            body = dict(body)
            body["updated"] = iso(utcnow())
            self.intelligence[aid] = body
            self._save("intelligence.json", self.intelligence)

    def _maybe_auto_intel(self, aid):
        if os.environ.get("INTEL_AUTO", "1") == "0":
            return
        if not META_API_KEY:
            return
        with self.lock:
            if self.intelligence.get(aid, {}).get("status") in (
                    "pending", "ready"):
                return
            self.intelligence[aid] = {"status": "pending",
                                      "updated": iso(utcnow())}
            self._save("intelligence.json", self.intelligence)
        enqueue_intelligence(self, aid)

    def request_intelligence(self, user, aid):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        if self.captions.get(aid, {}).get("status") != "ready":
            return 409, {"error": "transcript is not ready yet"}
        if not META_API_KEY:
            return 503, {"error": "intelligence is not configured "
                                  "(META_API_KEY is missing)"}
        with self.lock:
            current = self.intelligence.get(aid, {}).get("status")
            if current == "ready":
                return 200, {"status": "ready"}
            if current == "pending":
                return 202, {"status": "pending"}
            self.intelligence[aid] = {"status": "pending",
                                      "updated": iso(utcnow())}
            self._save("intelligence.json", self.intelligence)
        enqueue_intelligence(self, aid)
        return 202, {"status": "pending"}

    def intelligence_status(self, user, aid):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        if user["role"] != "station":
            return 403, {"error": "stations only"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        return 200, self.intelligence.get(aid, {"status": "none"})

    def request_moderation(self, user, aid):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        if user["role"] != "station":
            return 403, {"error": "only the buying station can run checks"}
        if not (asset.get("mime") or "").startswith("video"):
            return 400, {"error": "moderation scans video files only"}
        if not META_API_KEY:
            return 503, {"error": "moderation is not configured "
                                  "(META_API_KEY is missing)"}
        with self.lock:
            current = self.moderation.get(aid, {}).get("status")
            if current in ("clean", "flagged"):
                return 200, {"status": current}
            if current == "pending":
                return 202, {"status": "pending"}
            self.moderation[aid] = {"status": "pending",
                                    "updated": iso(utcnow())}
            self._save("moderation.json", self.moderation)
        enqueue_moderation(self, aid)
        return 202, {"status": "pending"}

    def moderation_status(self, user, aid):
        asset = self.asset_for(aid)
        if asset is None:
            return 404, {"error": "not found"}
        _, denied = self.asset_access(user, aid)
        if denied is not None:
            return denied, {"error": "not found" if denied == 404
                            else "you cannot watch this file"}
        return 200, self.moderation.get(aid, {"status": "none"})

    def _set_moderation(self, aid, body):
        with self.lock:
            body = dict(body)
            body["updated"] = iso(utcnow())
            self.moderation[aid] = body
            self._save("moderation.json", self.moderation)

    def _maybe_auto_moderate(self, aid):
        if os.environ.get("MOD_AUTO", "1") == "0":
            return
        if not META_API_KEY:
            return
        with self.lock:
            if self.moderation.get(aid, {}).get("status") in (
                    "pending", "clean", "flagged"):
                return
            self.moderation[aid] = {"status": "pending",
                                    "updated": iso(utcnow())}
            self._save("moderation.json", self.moderation)
        enqueue_moderation(self, aid)

    def moderation_review_path(self, user, aid):
        """(status, path) for the blurred review copy, else (4xx, None)."""
        asset, denied = self.asset_access(user, aid)
        if asset is None:
            return denied, None
        mod = self.moderation.get(aid, {})
        if mod.get("status") != "flagged":
            return 404, None
        path = os.path.join(self.dir, "moderation",
                             mod.get("review") or (aid + ".blur.mp4"))
        if not os.path.isfile(path):
            return 404, None
        return 200, path

    def asset_download_path(self, user, aid):
        """(status, path) for downloads; stations get the blurred copy."""
        if not self.asset_downloadable(user, aid):
            return 403, None
        asset = self.asset_for(aid)
        path = os.path.join(self.dir, "assets", asset["stored"])
        mod = self.moderation.get(aid, {})
        if user["email"] != asset["owner"] and \
                mod.get("status") == "flagged":
            review = os.path.join(
                self.dir, "moderation",
                mod.get("review") or (aid + ".blur.mp4"))
            if os.path.isfile(review):
                path = review
        if not os.path.isfile(path):
            return 404, None
        return 200, path

    def asset_downloadable(self, user, aid):
        """True when user may download (not just stream) an asset.

        The file owner may always download their own file. A station may
        download a delivery only once the coverage is approved; before
        that, previews stream but do not download.
        """
        asset = self.asset_for(aid)
        if asset is None:
            return False
        if user["email"] == asset["owner"]:
            return True
        if user["role"] != "station":
            return False
        return any(aid in [v.get("assetId") for v in r.get("deliveries", [])]
                   and r.get("status") == "completed"
                   for r in self.requests
                   if r.get("station") == user["email"])


STORE = Store()

ROUTES = {
    "/": "/index.html",
    "/signup": "/index.html",
    "/login": "/login.html",
    "/discover": "/discover.html",
    "/request": "/request.html",
    "/requests": "/requests.html",
    "/booking": "/booking.html",
    "/bookings": "/bookings.html",
    "/expenses": "/expenses.html",
    "/station": "/station.html",
    "/contributor": "/contributor.html",
    "/publish": "/publish.html",
    "/listings": "/listings.html",
    "/assignments": "/assignments.html",
    "/pitches": "/pitches.html",
    "/media": "/media.html",
    "/sales": "/sales.html",
    "/profile": "/profile.html",
    "/partner": "/partner.html",
}
PAGE_ROLES = {
    "/discover": "station",
    "/request": "station",
    "/requests": "station",
    "/booking": "station",
    "/bookings": "station",
    "/expenses": "station",
    "/station": "station",
    "/contributor": "contributor",
    "/publish": "contributor",
    "/listings": "contributor",
    "/assignments": "contributor",
    "/pitches": "contributor",
    "/media": "contributor",
    "/sales": "contributor",
    "/profile": "contributor",
    "/partner": "partner",
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self._set_cookie = None
        super().__init__(*args, directory=PUBLIC, **kwargs)

    def log_message(self, *args):
        print("[%s] %s" % (utcnow().strftime("%H:%M:%S"), args[0] % args[1:]))

    def end_headers(self):
        if self._set_cookie:
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8",
                   filename=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length == 0:
            return {}
        if length < 0 or length > 8_000_000:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def _token(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookies.get("relay_session")
        return morsel.value if morsel else None

    def _user(self):
        return STORE.user_for_token(self._token())

    def _login_cookie(self, token):
        self._set_cookie = ("relay_session=%s; HttpOnly; Path=/; SameSite=Lax; "
                            "Max-Age=%d" % (token, SESSION_DAYS * 86400))

    def _logout_cookie(self):
        self._set_cookie = ("relay_session=; Expires=Thu, 01 Jan 1970 "
                            "00:00:00 GMT; Path=/")

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/"):
            return self._api_get(path)
        if path.startswith("/uploads/"):
            return self._serve_upload(path[len("/uploads/"):])
        if path.startswith("/assets/") and path.endswith("/captions.vtt"):
            return self._serve_captions(
                path[len("/assets/"):-len("/captions.vtt")])
        if path.startswith("/assets/"):
            query = urllib.parse.parse_qs(parsed.query)
            return self._serve_asset(path[len("/assets/"):],
                                     "download" in query,
                                     "review" in query)
        if path in ROUTES:
            need = PAGE_ROLES.get(path)
            if need:
                user = self._user()
                if not user:
                    return self._redirect("/login?next=" +
                                          urllib.parse.quote(self.path))
                if user["role"] != need:
                    return self._redirect(HOMES[user["role"]] + "?denied=1")
            self.path = ROUTES[path]
        return super().do_GET()

    def _serve_upload(self, uid):
        if not self._user():
            return self._send_json({"error": "sign in required"}, 401)
        path, record = STORE.upload_path(uid)
        if path is None:
            return self._send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", record["mime"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_captions(self, aid):
        user = self._user()
        if not user:
            return self._send_json({"error": "sign in required"}, 401)
        asset, denied = STORE.asset_access(user, aid)
        if asset is None:
            return self._send_json({"error": "not found" if denied == 404
                                    else "stations and the file owner only"},
                                   denied)
        if STORE.captions.get(aid, {}).get("status") != "ready":
            return self._send_json({"error": "no captions yet"}, 404)
        path = os.path.join(STORE.dir, "captions", aid + ".vtt")
        if not os.path.isfile(path):
            return self._send_json({"error": "no captions yet"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, aid, download=False, review=False):
        user = self._user()
        if not user:
            return self._send_json({"error": "sign in required"}, 401)
        if review:
            status, path = STORE.moderation_review_path(user, aid)
            if path is None:
                return self._send_json({"error": "not found"}, status)
            asset = STORE.asset_for(aid)
        else:
            asset, denied = STORE.asset_access(user, aid)
            if asset is None:
                return self._send_json(
                    {"error": "not found" if denied == 404
                     else "stations and the file owner only"}, denied)
            if download:
                if not STORE.asset_downloadable(user, aid):
                    return self._send_json(
                        {"error": "downloads go to the commissioning "
                                  "station and the file owner only"}, 403)
                status, path = STORE.asset_download_path(user, aid)
                if path is None:
                    return self._send_json({"error": "not found"}, status)
            else:
                path = os.path.join(STORE.dir, "assets", asset["stored"])
                if not os.path.isfile(path):
                    return self._send_json({"error": "not found"}, 404)
        size = os.path.getsize(path)
        start, end, ranged = parse_range(self.headers.get("Range"), size)
        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", asset["mime"])
        self.send_header("Accept-Ranges", "bytes")
        if download:
            name = re.sub(r'[\x00-\x1f\x7f"]', "_",
                          asset.get("name", aid)) or aid
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % name)
        if ranged:
            self.send_header("Content-Range", "bytes %d-%d/%d" %
                             (start, end, size))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_get(self, path):
        if path == "/api/me":
            user = self._user()
            if not user:
                return self._send_json({"error": "not signed in"}, 401)
            view = Store.public_user(user)
            view.update({"based": user.get("based", ""),
                         "specialties": user.get("specialties", ""),
                         "languages": user.get("languages", "")})
            return self._send_json(view)
        user = self._user()
        if not user:
            return self._send_json({"error": "sign in required"}, 401)
        if path == "/api/content":
            return self._send_json(STORE.catalog())
        if path == "/api/listings":
            status, body = STORE.own_listings(user)
            return self._send_json(body, status)
        if path.startswith("/api/content/"):
            item = STORE.content_item(path.rsplit("/", 1)[-1])
            if item is None:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(item)
        if path == "/api/bookings":
            if user["role"] != "station":
                return self._send_json({"error": "stations only"}, 403)
            return self._send_json(STORE.list_bookings(user))
        if path.startswith("/api/bookings/") and path.endswith("/invoice"):
            bid = path[len("/api/bookings/"):-len("/invoice")]
            text = STORE.invoice_text(user, bid)
            if text is None:
                return self._send_json({"error": "not found"}, 404)
            booking = next((b for b in STORE.bookings if b.get("id") == bid), {})
            return self._send_text(text, filename="%s.txt" % booking.get(
                "invoice", bid))
        if path.startswith("/api/bookings/") and path.endswith("/endpoints"):
            bid = path[len("/api/bookings/"):-len("/endpoints")]
            host = self.headers.get("Host", "127.0.0.1:4173")
            status, body = STORE.endpoints(user, bid, host)
            return self._send_json(body, status)
        if path == "/api/requests":
            if user["role"] not in ("station", "contributor"):
                return self._send_json(
                    {"error": "stations and contributors only"}, 403)
            return self._send_json(STORE.list_requests(user))
        if path == "/api/feeds":
            if user["role"] != "partner":
                return self._send_json({"error": "partners only"}, 403)
            return self._send_json(STORE.list_feeds(user))
        if path == "/api/usage":
            if user["role"] != "partner":
                return self._send_json({"error": "partners only"}, 403)
            return self._send_json(STORE.usage_for(user))
        if path == "/api/assets":
            status, body = STORE.list_assets(user)
            return self._send_json(body, status)
        if path == "/api/folders":
            status, body = STORE.list_folders(user)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/captions"):
            aid = path[len("/api/assets/"):-len("/captions")]
            status, body = STORE.caption_status(user, aid)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and \
                path.endswith("/captions/cues"):
            aid = path[len("/api/assets/"):-len("/captions/cues")]
            status, body = STORE.caption_cues(user, aid)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/moderation"):
            aid = path[len("/api/assets/"):-len("/moderation")]
            status, body = STORE.moderation_status(user, aid)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and \
                path.endswith("/intelligence"):
            aid = path[len("/api/assets/"):-len("/intelligence")]
            status, body = STORE.intelligence_status(user, aid)
            return self._send_json(body, status)
        if path == "/api/sales":
            status, body = STORE.sales_for(user)
            return self._send_json(body, status)
        if path == "/api/expenses":
            status, body = STORE.expenses_for(user)
            return self._send_json(body, status)
        if path == "/api/worldnews":
            status, body = STORE.worldnews_status(user)
            return self._send_json(body, status)
        if path == "/api/directory":
            status, body = STORE.directory(user)
            return self._send_json(body, status)
        if path == "/api/pitches":
            status, body = STORE.list_pitches(user)
            return self._send_json(body, status)
        if path == "/api/notifications":
            status, body = STORE.notifications_for(user)
            return self._send_json(body, status)
        if path == "/api/ifb":
            status, body = STORE.get_ifb(user)
            return self._send_json(body, status)
        if path == "/api/saved":
            return self._send_json(STORE.get_saved(user))
        if path == "/api/settings":
            status, body = STORE.get_settings(user)
            return self._send_json(body, status)
        return self._send_json({"error": "not found"}, 404)

    # ---------------- POST / PUT / DELETE ----------------
    def do_POST(self):
        return self._mutate()

    def do_PUT(self):
        return self._mutate()

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        user = self._user()
        if not user:
            return self._send_json({"error": "sign in required"}, 401)
        if parsed.path.startswith("/api/saved/"):
            cid = parsed.path.rsplit("/", 1)[-1]
            status, body = STORE.remove_saved(user, cid)
            return self._send_json(body, status)
        if parsed.path.startswith("/api/assets/"):
            aid = parsed.path.rsplit("/", 1)[-1]
            status, body = STORE.delete_asset(user, aid)
            return self._send_json(body, status)
        if parsed.path.startswith("/api/folders/"):
            fid = parsed.path.rsplit("/", 1)[-1]
            status, body = STORE.delete_folder(user, fid)
            return self._send_json(body, status)
        return self._send_json({"error": "not found"}, 404)

    def _mutate(self):
        path = urllib.parse.urlparse(self.path).path
        payload = self._read_json()
        if payload is None:
            return self._send_json({"error": "invalid json"}, 400)
        if path == "/api/signup":
            status, body = STORE.signup(payload)
            if status == 201:
                self._login_cookie(body.pop("token"))
            return self._send_json(body, status)
        if path == "/api/login":
            status, body = STORE.login(payload)
            if status == 200:
                self._login_cookie(body.pop("token"))
            return self._send_json(body, status)
        if path == "/api/logout":
            STORE.logout(self._token())
            self._logout_cookie()
            return self._send_json({"ok": True})
        user = self._user()
        if not user:
            return self._send_json({"error": "sign in required"}, 401)
        if path == "/api/password":
            status, body = STORE.set_password(user, payload)
            return self._send_json(body, status)
        if path == "/api/content":
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.publish_content(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/content/") and path.count("/") == 3:
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            cid = path[len("/api/content/"):]
            status, body = STORE.update_content(user, cid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/content/") and path.endswith("/unpublish"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            cid = path[len("/api/content/"):-len("/unpublish")]
            status, body = STORE.set_content_visibility(user, cid, False)
            return self._send_json(body, status)
        if path.startswith("/api/content/") and path.endswith("/republish"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            cid = path[len("/api/content/"):-len("/republish")]
            status, body = STORE.set_content_visibility(user, cid, True)
            return self._send_json(body, status)
        if path == "/api/search":
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.search_content(user, payload)
            return self._send_json(body, status)
        if path == "/api/bookings":
            status, body = STORE.create_booking(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/bookings/") and path.endswith("/cancel"):
            bid = path[len("/api/bookings/"):-len("/cancel")]
            if user["role"] != "station":
                return self._send_json({"error": "stations only"}, 403)
            status, body = STORE.cancel_booking(user, bid)
            return self._send_json(body, status)
        if path == "/api/folders":
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.create_folder(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/folders/") and path.count("/") == 3:
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            fid = path[len("/api/folders/"):]
            status, body = STORE.rename_folder(user, fid, payload)
            return self._send_json(body, status)
        if path == "/api/requests":
            status, body = STORE.create_request(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.count("/") == 3:
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            rid = path[len("/api/requests/"):]
            status, body = STORE.update_request(user, rid, payload)
            return self._send_json(body, status)
        if path == "/api/pitches":
            status, body = STORE.create_pitch(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/pitches/") and path.endswith("/accept"):
            pid = path[len("/api/pitches/"):-len("/accept")]
            status, body = STORE.decide_pitch(user, pid, True)
            return self._send_json(body, status)
        if path.startswith("/api/pitches/") and path.endswith("/decline"):
            pid = path[len("/api/pitches/"):-len("/decline")]
            status, body = STORE.decide_pitch(user, pid, False)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/assign"):
            rid = path[len("/api/requests/"):-len("/assign")]
            status, body = STORE.assign_direct(user, rid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/apply"):
            rid = path[len("/api/requests/"):-len("/apply")]
            status, body = STORE.apply(user, rid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/accept"):
            rid = path[len("/api/requests/"):-len("/accept")]
            status, body = STORE.accept_application(user, rid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/submit"):
            rid = path[len("/api/requests/"):-len("/submit")]
            status, body = STORE.submit_delivery(user, rid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/approve"):
            rid = path[len("/api/requests/"):-len("/approve")]
            status, body = STORE.approve_delivery(user, rid)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/changes"):
            rid = path[len("/api/requests/"):-len("/changes")]
            status, body = STORE.request_changes(user, rid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/requests/") and path.endswith("/notes"):
            rid = path[len("/api/requests/"):-len("/notes")]
            status, body = STORE.post_note(user, rid, payload)
            return self._send_json(body, status)
        if "/notes/" in path and path.endswith("/resolve"):
            inner = path[len("/api/requests/"):-len("/resolve")]
            rid, _, nid = inner.partition("/notes/")
            status, body = STORE.resolve_note(user, rid, nid)
            return self._send_json(body, status)
        if path == "/api/me":
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.update_profile(user, payload)
            return self._send_json(body, status)
        if path == "/api/feeds":
            status, body = STORE.create_feed(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/feeds/") and path.endswith("/status"):
            fid = path[len("/api/feeds/"):-len("/status")]
            status, body = STORE.feed_status(user, fid, payload)
            return self._send_json(body, status)
        if path == "/api/saved":
            status, body = STORE.add_saved(user, payload)
            return self._send_json(body, status)
        if path == "/api/notifications/seen":
            status, body = STORE.mark_notifications_seen(user)
            return self._send_json(body, status)
        if path == "/api/ifb":
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.put_ifb(user, payload)
            return self._send_json(body, status)
        if path == "/api/settings":
            if self.command != "PUT":
                return self._send_json({"error": "not found"}, 404)
            status, body = STORE.put_settings(user, payload)
            return self._send_json(body, status)
        if path == "/api/uploads":
            status, body = STORE.upload(user, payload)
            return self._send_json(body, status)
        if path == "/api/assets":
            status, body = STORE.asset_init(user, payload)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/chunk"):
            aid = path[len("/api/assets/"):-len("/chunk")]
            status, body = STORE.asset_chunk(user, aid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/complete"):
            aid = path[len("/api/assets/"):-len("/complete")]
            status, body = STORE.asset_complete(user, aid)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/move"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            aid = path[len("/api/assets/"):-len("/move")]
            status, body = STORE.move_asset(user, aid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/captions"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            aid = path[len("/api/assets/"):-len("/captions")]
            status, body = STORE.request_captions(user, aid, payload)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and path.endswith("/moderation"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            aid = path[len("/api/assets/"):-len("/moderation")]
            status, body = STORE.request_moderation(user, aid)
            return self._send_json(body, status)
        if path.startswith("/api/assets/") and \
                path.endswith("/intelligence"):
            if self.command != "POST":
                return self._send_json({"error": "not found"}, 404)
            aid = path[len("/api/assets/"):-len("/intelligence")]
            status, body = STORE.request_intelligence(user, aid)
            return self._send_json(body, status)
        return self._send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4173"))
    ThreadingHTTPServer.allow_reuse_address = 0  # fail loudly on double-start
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        print("Relay running at http://127.0.0.1:%d/" % port)
        print("Captions: " + ("enabled (META_API_KEY is set)"
                              if META_API_KEY else
                              "disabled (put META_API_KEY in .env to enable)"))
        print("Moderation: " + ("enabled (META_API_KEY is set)"
                                if META_API_KEY else
                                "disabled (put META_API_KEY in .env to "
                                "enable)"))
        httpd.serve_forever()

