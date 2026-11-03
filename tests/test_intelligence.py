"""Offline tests for media intelligence (no network, stub enricher)."""
import base64
import io
import json
import os
import tempfile
import unittest
import wave

import intelligence
import server as relay


def make_wav_bytes(nchan=1, width=1, rate=8000, seconds=0.5):
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchan)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes([128] * n * nchan))
    return buf.getvalue()


class IntelCoreTest(unittest.TestCase):
    def test_build_enrich_request(self):
        payload = intelligence.build_enrich_request("hello " * 5000)
        self.assertEqual(payload["model"], intelligence.SPARK_MODEL)
        text = payload["messages"][0]["content"][0]["text"]
        self.assertIn("JSON", text)
        self.assertLessEqual(
            len(text), len(intelligence.INTEL_PROMPT) +
            intelligence.MAX_TRANSCRIPT_CHARS)

    def test_parse_enrichment(self):
        body = intelligence.parse_enrichment(
            '{"summary": "Harbor at dawn.", "tags": ["Harbor", "boats"], '
            '"description": "Aerials over the harbor."}')
        self.assertEqual(body["summary"], "Harbor at dawn.")
        self.assertEqual(body["tags"], ["harbor", "boats"])
        self.assertEqual(body["description"], "Aerials over the harbor.")
        wrapped = intelligence.parse_enrichment(
            'Here you go: {"summary": "S", "tags": [], '
            '"description": ""} done')
        self.assertEqual(wrapped["summary"], "S")
        with self.assertRaises(intelligence.IntelligenceError):
            intelligence.parse_enrichment("no json here")
        with self.assertRaises(intelligence.IntelligenceError):
            intelligence.parse_enrichment(
                '{"summary": "", "tags": [], "description": ""}')

    def test_normalize_tags(self):
        self.assertEqual(
            intelligence.normalize_tags(
                [" Harbor ", "HARBOR", "", "x" * 40, 7, None]),
            ["harbor", "x" * 32, "7", "none"])
        self.assertEqual(intelligence.normalize_tags("nope"), [])
        many = ["t%d" % i for i in range(20)]
        self.assertEqual(len(intelligence.normalize_tags(many)),
                         intelligence.MAX_TAGS)

    def test_transcript_from_cues(self):
        cues = [{"text": "hello"}, {"text": "world"}, {"nope": 1}]
        self.assertEqual(intelligence.transcript_from_cues(cues),
                         "hello world")
        self.assertEqual(intelligence.transcript_from_cues([]), "")


class IntelJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.store = relay.Store(self.dir)
        self.store.signup({"email": "station@metroline.tv",
                           "password": "password-1", "name": "Metro",
                           "role": "station", "org": "Metroline TV"})
        self.store.signup({"email": "maya@mayachen.media",
                           "password": "password-1", "name": "Maya",
                           "role": "contributor", "roles": ["Reporter"]})
        self.station = self.store.find_user("station@metroline.tv")
        self.contrib = self.store.find_user("maya@mayachen.media")
        real_key = relay.META_API_KEY
        self.addCleanup(setattr, relay, "META_API_KEY", real_key)
        old_auto = os.environ.get("INTEL_AUTO")
        os.environ.pop("INTEL_AUTO", None)
        self.addCleanup(self._restore_auto, old_auto)

    @staticmethod
    def _restore_auto(old):
        if old is None:
            os.environ.pop("INTEL_AUTO", None)
        else:
            os.environ["INTEL_AUTO"] = old

    def _upload_wav(self):
        raw = make_wav_bytes()
        st, sess = self.store.asset_init(
            self.contrib, {"name": "note.wav", "size": len(raw)})
        self.assertEqual(st, 201, sess)
        st, _ = self.store.asset_chunk(
            self.contrib, sess["id"],
            {"index": 0, "data": base64.b64encode(raw).decode()})
        self.assertEqual(st, 200)
        st, _ = self.store.asset_complete(self.contrib, sess["id"])
        self.assertEqual(st, 200)
        return sess["id"]

    @staticmethod
    def _stub_transcribe(wav_bytes):
        assert wav_bytes[:4] == b"RIFF"
        return [{"start": 0, "end": 0.5, "speaker": "Maya",
                 "text": "hello captions"}]

    @staticmethod
    def _stub_enrich(transcript, api_key):
        assert "hello captions" in transcript
        assert api_key
        return {"summary": "A greeting.", "tags": ["greeting"],
                "description": "Someone says hello."}

    def _stub_enqueue(self, calls):
        real_enqueue = relay.enqueue_intelligence
        relay.enqueue_intelligence = lambda s, a: calls.append(a)
        self.addCleanup(setattr, relay, "enqueue_intelligence", real_enqueue)

    def _ready_transcript(self):
        aid = self._upload_wav()
        self.store._set_caption(aid, "pending")
        relay.process_caption_job(self.store, aid,
                                  transcribe_fn=self._stub_transcribe)
        return aid

    def test_auto_intel_on_caption_ready(self):
        relay.META_API_KEY = "test-key"
        calls = []
        self._stub_enqueue(calls)
        aid = self._ready_transcript()
        self.assertEqual(calls, [aid])
        self.assertEqual(self.store.intelligence[aid]["status"], "pending")
        os.environ["INTEL_AUTO"] = "0"
        aid2 = self._upload_wav()
        self.store._set_caption(aid2, "pending")
        relay.process_caption_job(self.store, aid2,
                                  transcribe_fn=self._stub_transcribe)
        self.assertEqual(calls, [aid])
        self.assertNotIn(aid2, self.store.intelligence)

    def test_process_ready(self):
        os.environ["INTEL_AUTO"] = "0"
        aid = self._ready_transcript()
        relay.process_intelligence_job(self.store, aid,
                                       enrich_fn=self._stub_enrich)
        st, body = self.store.intelligence_status(self.station, aid)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["summary"], "A greeting.")
        self.assertEqual(body["tags"], ["greeting"])
        self.assertEqual(body["description"], "Someone says hello.")
        st, _ = self.store.intelligence_status(self.contrib, aid)
        self.assertEqual(st, 403)

    def test_process_error_paths(self):
        os.environ["INTEL_AUTO"] = "0"
        aid = self._ready_transcript()

        def boom(transcript, api_key):
            raise intelligence.IntelligenceError("no model")

        relay.process_intelligence_job(self.store, aid, enrich_fn=boom)
        st, body = self.store.intelligence_status(self.station, aid)
        self.assertEqual(body["status"], "error")
        self.assertIn("no model", body["error"])
        bare = self._upload_wav()
        relay.process_intelligence_job(self.store, bare,
                                       enrich_fn=self._stub_enrich)
        st, body = self.store.intelligence_status(self.station, bare)
        self.assertEqual(body["status"], "error")
        self.assertIn("transcript", body["error"])

    def test_request_validates(self):
        relay.META_API_KEY = ""
        aid = self._upload_wav()
        st, _ = self.store.request_intelligence(self.station, aid)
        self.assertEqual(st, 409)
        st, _ = self.store.request_intelligence(self.station, "as_missing")
        self.assertEqual(st, 404)
        st, _ = self.store.request_intelligence(self.contrib, aid)
        self.assertEqual(st, 403)
        os.environ["INTEL_AUTO"] = "0"
        ready = self._ready_transcript()
        st, _ = self.store.request_intelligence(self.station, ready)
        self.assertEqual(st, 503)
        st, _ = self.store.signup({
            "email": "intelother@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("intelother@creator.tv")
        st, _ = self.store.request_intelligence(other, ready)
        self.assertEqual(st, 403)
        st, _ = self.store.intelligence_status(other, ready)
        self.assertEqual(st, 403)
        relay.META_API_KEY = "test-key"
        calls = []
        self._stub_enqueue(calls)
        st, body = self.store.request_intelligence(self.station, ready)
        self.assertEqual(st, 202, body)
        self.assertEqual(calls, [ready])
        self.store._set_intelligence(ready, {"status": "ready",
                                             "summary": "S", "tags": [],
                                             "description": "D"})
        st, body = self.store.request_intelligence(self.station, ready)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "ready")

    def test_delete_cleans_intel(self):
        os.environ["INTEL_AUTO"] = "0"
        aid = self._ready_transcript()
        relay.process_intelligence_job(self.store, aid,
                                       enrich_fn=self._stub_enrich)
        st, rows = self.store.list_assets(self.contrib)
        self.assertEqual(st, 200)
        mine = [r for r in rows if r["id"] == aid][0]
        self.assertNotIn("intel", mine)
        self.assertNotIn("intelligence", mine)
        st, _ = self.store.delete_asset(self.contrib, aid)
        self.assertEqual(st, 200)
        self.assertNotIn(aid, self.store.intelligence)

    def test_ui_wiring(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "media.html"),
                  encoding="utf-8") as f:
            media = f.read()
        self.assertNotIn("intelCell", media)
        self.assertNotIn("/intelligence", media)
        with open(os.path.join(public, "listings.html"),
                  encoding="utf-8") as f:
            listings = f.read()
        self.assertNotIn("Suggest from file", listings)
        self.assertNotIn("/intelligence", listings)
        with open(os.path.join(public, "booking.html"),
                  encoding="utf-8") as f:
            booking = f.read()
        self.assertIn("intelBox", booking)
        self.assertIn("AI summary", booking)
        self.assertIn("/intelligence", booking)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.store = relay.Store(self.dir)
        self.store.signup({"email": "station@metroline.tv",
                           "password": "password-1", "name": "Metro",
                           "role": "station", "org": "Metroline TV"})
        self.store.signup({"email": "maya@mayachen.media",
                           "password": "password-1", "name": "Maya",
                           "role": "contributor", "roles": ["Reporter"]})
        self.station = self.store.find_user("station@metroline.tv")
        self.contrib = self.store.find_user("maya@mayachen.media")
        real_key = relay.META_API_KEY
        self.addCleanup(setattr, relay, "META_API_KEY", real_key)
        relay.META_API_KEY = ""
        os.environ.pop("INTEL_AUTO", None)
        self.addCleanup(os.environ.pop, "INTEL_AUTO", None)

    def _upload(self, name="cut.mp4"):
        st, sess = self.store.asset_init(
            self.contrib, {"name": name, "size": 3})
        self.assertEqual(st, 201, sess)
        st, _ = self.store.asset_chunk(
            self.contrib, sess["id"], {"index": 0, "data": "YWJj"})
        self.assertEqual(st, 200)
        st, _ = self.store.asset_complete(self.contrib, sess["id"])
        self.assertEqual(st, 200)
        return sess["id"]

    def _publish(self, **kw):
        payload = {"kind": "package", "title": "Harbor at dawn",
                   "description": "Aerials and interviews.",
                   "market": "Boston, MA", "price": 120}
        payload.update(kw)
        st, item = self.store.publish_content(self.contrib, payload)
        self.assertEqual(st, 201, item)
        return item

    def _seed_transcript(self, aid, text):
        os.makedirs(os.path.join(self.dir, "captions"), exist_ok=True)
        with open(os.path.join(self.dir, "captions", aid + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump({"cues": [{"start": 0.0, "end": 1.0, "speaker": "",
                                 "text": text}]}, f)
        self.store._set_caption(aid, "ready")

    def _seed_intel(self, aid, tags):
        self.store._set_intelligence(aid, {"status": "ready",
                                           "summary": "Seeded summary.",
                                           "tags": tags,
                                           "description": "Seeded."})

    @staticmethod
    def _parse(**kw):
        out = {"keywords": [], "market": None, "kind": None,
               "from": None, "to": None}
        out.update(kw)
        return lambda text, key, today: dict(out)

    def test_recorded_validation(self):
        item = self._publish(recorded="2026-03-15")
        self.assertEqual(item["recorded"], "2026-03-15")
        st, body = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Bad date",
            "description": "Nope.", "market": "Boston, MA",
            "price": 10, "recorded": "March 2026"})
        self.assertEqual(st, 400, body)
        st, body = self.store.update_content(
            self.contrib, item["id"], {"recorded": "2026-04-01"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["recorded"], "2026-04-01")
        st, body = self.store.update_content(
            self.contrib, item["id"], {"recorded": ""})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["recorded"], "")
        st, body = self.store.update_content(
            self.contrib, item["id"], {"recorded": "yesterday"})
        self.assertEqual(st, 400, body)

    def test_search_keywords_ranking(self):
        self._publish(title="Harbor floods at dawn",
                      description="boats in the mist")
        aid = self._upload()
        self._publish(title="Street interview", assetId=aid,
                      description="talking heads")
        self._seed_intel(aid, ["flooding", "miami"])
        self._seed_transcript(aid, "the levee breach flooded homes")
        self._publish(title="Unrelated cooking", description="soup")
        st, body = self.store.search_content(
            self.station, {"q": "harbor"},
            parse_fn=self._parse(keywords=["harbor"]))
        self.assertEqual(st, 200, body)
        self.assertEqual(
            [c["title"] for c in body["results"]], ["Harbor floods at dawn"])
        st, body = self.store.search_content(
            self.station, {"q": "flooding"},
            parse_fn=self._parse(keywords=["flooding"]))
        self.assertEqual(
            [c["title"] for c in body["results"]], ["Street interview"])
        st, body = self.store.search_content(
            self.station, {"q": "levee"},
            parse_fn=self._parse(keywords=["levee"]))
        self.assertEqual(
            [c["title"] for c in body["results"]], ["Street interview"])
        self.assertFalse(body["fallback"])

    def test_search_filters(self):
        self._publish(title="Old Miami floods", market="Miami, FL",
                      kind="package", recorded="2025-01-10")
        self._publish(title="New Miami floods", market="Miami, FL",
                      kind="package", recorded="2026-03-20")
        self._publish(title="Miami podcast", market="Miami, FL",
                      kind="podcast", recorded="2026-03-21")
        self._publish(title="Dateless floods", market="Miami, FL")
        st, body = self.store.search_content(
            self.station, {"q": "", "market": "Miami, FL"})
        self.assertEqual(st, 200, body)
        self.assertEqual(len(body["results"]), 4)
        st, body = self.store.search_content(
            self.station, {"q": "", "from": "2026-03-01",
                           "to": "2026-03-31"})
        self.assertEqual(
            sorted(c["title"] for c in body["results"]),
            ["Miami podcast", "New Miami floods"])
        st, body = self.store.search_content(
            self.station, {"q": "", "kind": "podcast"})
        self.assertEqual(
            [c["title"] for c in body["results"]], ["Miami podcast"])
        st, body = self.store.search_content(
            self.station, {"q": "", "kind": "nope"})
        self.assertEqual(len(body["results"]), 4)

    def test_search_roles_and_fallback(self):
        self._publish(title="Harbor Dawn Patrol")
        st, _ = self.store.search_content(self.contrib, {"q": "harbor"})
        self.assertEqual(st, 403)

        def boom(text, key, today):
            raise intelligence.IntelligenceError("no model")

        st, body = self.store.search_content(
            self.station, {"q": "Harbor Dawn!"}, parse_fn=boom)
        self.assertEqual(st, 200, body)
        self.assertTrue(body["fallback"])
        self.assertEqual(
            [c["title"] for c in body["results"]], ["Harbor Dawn Patrol"])

    def test_search_route(self):
        self._publish(title="Miami floods", market="Miami, FL")
        real_store = relay.STORE
        relay.STORE = self.store
        self.addCleanup(setattr, relay, "STORE", real_store)
        h = relay.Handler.__new__(relay.Handler)
        h.path = "/api/search"
        h.command = "POST"
        h._user = lambda: self.station
        h._read_json = lambda: {"q": "", "market": "Miami, FL"}
        out = {}
        h._send_json = lambda obj, status=200: out.update(
            status=status, obj=obj)
        h._mutate()
        self.assertEqual(out["status"], 200)
        self.assertEqual(
            [c["title"] for c in out["obj"]["results"]], ["Miami floods"])

    def test_discovery_ui_wiring(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "discover.html"),
                  encoding="utf-8") as f:
            discover = f.read()
        self.assertIn("aiq", discover)
        self.assertIn("aiGo", discover)
        self.assertIn("/api/search", discover)
        self.assertIn("Recorded", discover)
        with open(os.path.join(public, "publish.html"),
                  encoding="utf-8") as f:
            self.assertIn("pRecorded", f.read())
        with open(os.path.join(public, "listings.html"),
                  encoding="utf-8") as f:
            self.assertIn("eRecorded", f.read())
        with open(os.path.join(public, "booking.html"),
                  encoding="utf-8") as f:
            self.assertIn("bRecorded", f.read())


class IntelQueryTest(unittest.TestCase):
    def test_parse_search_query(self):
        real_urlopen = intelligence.urllib.request.urlopen

        class Resp:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.raw

        def serve(raw):
            intelligence.urllib.request.urlopen = (
                lambda *a, **k: Resp(raw))

        self.addCleanup(setattr, intelligence.urllib.request, "urlopen",
                        real_urlopen)
        serve(b'{"choices": [{"message": {"content": "Find: '
              b'{"keywords": ["flooding"], "market": "Miami, FL", '
              b'"kind": "package", "from": "2026-03-01", '
              b'"to": "2026-03-31"}."}}]}')
        out = intelligence.parse_search_query("flooding in Miami", "key",
                                              "2026-04-01")
        self.assertEqual(out["keywords"], ["flooding"])
        self.assertEqual(out["market"], "Miami, FL")
        self.assertEqual(out["kind"], "package")
        self.assertEqual(out["from"], "2026-03-01")
        serve(b'{"choices": [{"message": {"content": "no json"}}]}')
        with self.assertRaises(intelligence.IntelligenceError):
            intelligence.parse_search_query("hi", "key", "2026-04-01")
        serve(b'{"choices": [{"message": {"content": '
              b'"{}"}}]}')
        with self.assertRaises(intelligence.IntelligenceError):
            intelligence.parse_search_query("hi", "key", "2026-04-01")


if __name__ == "__main__":
    unittest.main()
