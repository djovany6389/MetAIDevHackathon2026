"""Offline tests for the moderation pipeline (no network, stub judge)."""
import io
import os
import tempfile
import unittest

import moderation
import server as relay


class ModerationCoreTest(unittest.TestCase):
    def test_judge_text_json(self):
        unsafe, cat, ai = moderation.judge_text(
            '{"verdict": "unsafe", "category": "nudity", "ai": "real"}')
        self.assertTrue(unsafe)
        self.assertEqual(cat, "nudity")
        self.assertEqual(ai, "real")
        unsafe, cat, ai = moderation.judge_text(
            'Sure: {"verdict": "safe", "category": "none", "ai": "ai"} '
            'done')
        self.assertFalse(unsafe)
        self.assertEqual(cat, "none")
        self.assertEqual(ai, "ai")
        unsafe, cat, ai = moderation.judge_text('{"verdict": "safe"}')
        self.assertEqual(ai, "unsure")

    def test_judge_text_variants(self):
        unsafe, _, ai = moderation.judge_text('{"label": "explicit"}')
        self.assertTrue(unsafe)
        self.assertEqual(ai, "unsure")
        unsafe, _, ai = moderation.judge_text(
            '{"classification": "clean"}')
        self.assertFalse(unsafe)
        self.assertEqual(ai, "unsure")
        unsafe, _, ai = moderation.judge_text(
            "this frame is unsafe to air")
        self.assertTrue(unsafe)
        self.assertEqual(ai, "unsure")
        unsafe, _, ai = moderation.judge_text("the frame looks safe")
        self.assertFalse(unsafe)
        self.assertEqual(ai, "unsure")
        with self.assertRaises(moderation.UnreadableVerdict):
            moderation.judge_text("cannot tell, ask again later")

    def test_aggregate_ai(self):
        self.assertEqual(
            moderation.aggregate_ai(["ai", "ai", "real"]),
            {"verdict": "ai", "score": 0.67, "judged": 3,
             "label": "LIKELY AI-MADE", "tone": "red"})
        self.assertEqual(
            moderation.aggregate_ai(["real", "real", "real", "unsure"]),
            {"verdict": "real", "score": 0.0, "judged": 3,
             "label": None, "tone": None})
        self.assertEqual(
            moderation.aggregate_ai(["ai", "unsure"])["verdict"],
            "uncertain")
        self.assertEqual(
            moderation.aggregate_ai([])["verdict"], "uncertain")
        full = moderation.aggregate_ai(["ai"] * 5)
        self.assertEqual(full["label"], "AI GENERATED VIDEO")
        self.assertEqual(full["tone"], "red")
        weak = moderation.aggregate_ai(["ai", "real", "real", "real"])
        self.assertEqual(weak["verdict"], "real")
        self.assertEqual(weak["label"], "POSSIBLY AI-MADE")
        self.assertEqual(weak["tone"], "amber")

    def test_extract_text(self):
        self.assertEqual(moderation.extract_text(
            {"choices": [{"message": {"content": '{"verdict":"safe"}'}}]}),
            '{"verdict":"safe"}')
        self.assertEqual(moderation.extract_text(
            {"choices": [{"message": {"content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"}]}}]}), "a b")
        self.assertEqual(
            moderation.extract_text({"output_text": "hi"}), "hi")
        self.assertEqual(moderation.extract_text({}), "")
        self.assertEqual(moderation.extract_text("nope"), "")

    def _stub_urlopen(self, raw):
        real_urlopen = moderation.urllib.request.urlopen

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return raw

        moderation.urllib.request.urlopen = lambda *a, **k: Resp()
        self.addCleanup(setattr, moderation.urllib.request, "urlopen",
                        real_urlopen)

    def test_spark_verdict_empty_text_reports_shape(self):
        self._stub_urlopen(b'{"data": [{"x": 1}]}')
        with self.assertRaises(moderation.UnreadableVerdict) as ctx:
            moderation.spark_verdict(b"fakejpeg", "key")
        self.assertIn("data", str(ctx.exception))

    def test_spark_verdict_cutoff_is_fatal(self):
        self._stub_urlopen(b'{"choices": [{"finish_reason": "length", '
                           b'"message": {"content": null}}]}')
        with self.assertRaises(moderation.ModerationError) as ctx:
            moderation.spark_verdict(b"fakejpeg", "key")
        self.assertIs(type(ctx.exception), moderation.ModerationError)
        self.assertIn("cut off", str(ctx.exception))

    def test_build_verdict_request(self):
        payload = moderation.build_verdict_request(b"\xff\xd8fake")
        self.assertEqual(payload["model"], moderation.SPARK_MODEL)
        parts = payload["messages"][0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("JSON", parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"))

    def test_merge_ranges(self):
        self.assertEqual(moderation.merge_ranges([]), [])
        flags = [(0.0, True, "nudity"), (2.0, True, "nudity"),
                 (4.0, False, "none"), (6.0, True, "nudity"),
                 (30.0, True, "other")]
        merged = moderation.merge_ranges(flags)
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (0.0, 8.0))
        self.assertEqual(merged[0]["category"], "nudity")
        self.assertEqual((merged[1]["start"], merged[1]["end"]),
                         (30.0, 32.0))
        short = moderation.merge_ranges([(0.0, True, "nudity")],
                                        min_seconds=2.0)
        self.assertEqual(short, [])
        fixed = moderation.merge_ranges([(0.0, True, "unverified"),
                                         (2.0, True, "nudity")])
        self.assertEqual(fixed[0]["category"], "nudity")

    def test_blur_filter(self):
        self.assertEqual(
            moderation.blur_filter([(2.0, 4.0), (30.0, 32.5)]),
            "boxblur=luma_radius=20:luma_power=1:enable='"
            "between(t,2.00,4.00)+between(t,30.00,32.50)'")

    def test_evaluate_frames(self):
        def judge(img):
            return (img == b"bad",
                    "nudity" if img == b"bad" else "none", "real")

        out = moderation.evaluate_frames(
            [(0.0, b"ok"), (2.0, b"bad")], judge)
        self.assertEqual(out, [(0.0, False, "none", "real"),
                               (2.0, True, "nudity", "real")])

        def unreadable(img):
            raise moderation.UnreadableVerdict("unreadable verdict: '...'")

        out = moderation.evaluate_frames(
            [(0.0, b"ok"), (2.0, b"bad")],
            lambda img: judge(img) if img == b"ok" else unreadable(img))
        self.assertEqual(out[1], (2.0, True, "unverified", "unsure"))
        with self.assertRaises(moderation.ModerationError) as ctx:
            moderation.evaluate_frames([(0.0, b"x")], unreadable)
        self.assertIn("no moderation verdict was readable",
                      str(ctx.exception))
        self.assertIn("last failure", str(ctx.exception))

        def fatal(img):
            raise moderation.ModerationError(
                "moderation request failed: HTTP Error 401")

        with self.assertRaises(moderation.ModerationError) as ctx:
            moderation.evaluate_frames([(0.0, b"x")], fatal)
        self.assertIn("HTTP Error 401", str(ctx.exception))


class ModerationJobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="relay-mod-")
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
        old_auto = os.environ.get("MOD_AUTO")
        os.environ.pop("MOD_AUTO", None)
        self.addCleanup(self._restore_auto, old_auto)

    @staticmethod
    def _restore_auto(old):
        if old is None:
            os.environ.pop("MOD_AUTO", None)
        else:
            os.environ["MOD_AUTO"] = old

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

    def _assigned_request(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Harbor story", "brief": "Two minutes on the harbor."})
        self.assertEqual(st, 201)
        self.store.apply(self.contrib, rq["id"], {"note": "Pick me"})
        st, _ = self.store.accept_application(
            self.station, rq["id"], {"email": self.contrib["email"]})
        self.assertEqual(st, 200)
        return rq["id"]

    def test_request_validates(self):
        relay.META_API_KEY = ""
        aid = self._upload()
        st, _ = self.store.request_moderation(self.station, aid)
        self.assertEqual(st, 503)
        st, _ = self.store.request_moderation(self.station, "as_missing")
        self.assertEqual(st, 404)
        st, body = self.store.request_moderation(self.contrib, aid)
        self.assertEqual(st, 403, body)
        relay.META_API_KEY = "test-key"
        wid = self._upload("note.wav")
        st, body = self.store.request_moderation(self.station, wid)
        self.assertEqual(st, 400, body)
        st, body = self.store.request_moderation(self.contrib, aid)
        self.assertEqual(st, 403, body)
        st, _ = self.store.moderation_status(self.contrib, aid)
        self.assertEqual(st, 200)
        st, _ = self.store.signup({
            "email": "other@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other@creator.tv")
        st, _ = self.store.request_moderation(other, aid)
        self.assertEqual(st, 403)
        st, _ = self.store.moderation_status(other, aid)
        self.assertEqual(st, 403)

    def test_request_enqueues(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload()
        st, body = self.store.request_moderation(self.station, aid)
        self.assertEqual(st, 202, body)
        self.assertEqual(body["status"], "pending")

    def _stub_scan(self, flags, ai=None):
        real_scan = moderation.scan_video
        moderation.scan_video = lambda *a, **k: {
            "flags": flags, "frames": 3,
            "ai": ai or {"verdict": "real", "score": 0.0, "judged": 3}}
        self.addCleanup(setattr, moderation, "scan_video", real_scan)
        real_blur = moderation.run_blur

        def fake_blur(asset_path, spans, out_path):
            with open(out_path, "wb") as f:
                f.write(b"blurred-bytes")

        moderation.run_blur = fake_blur
        self.addCleanup(setattr, moderation, "run_blur", real_blur)

    def test_process_flagged(self):
        aid = self._upload()
        self._stub_scan([{"start": 2.0, "end": 4.0,
                           "category": "nudity"}])
        relay.process_moderation_job(self.store, aid)
        st, body = self.store.moderation_status(self.contrib, aid)
        self.assertEqual(body["status"], "flagged")
        self.assertEqual(len(body["flags"]), 1)
        self.assertEqual(body["ai"]["verdict"], "real")
        path = os.path.join(self.dir, "moderation", aid + ".blur.mp4")
        self.assertTrue(os.path.isfile(path))
        rows = [r for r in self.store.list_assets(self.contrib)
                if r["id"] == aid]
        self.assertEqual(rows[0]["moderation"], "flagged")
        self.assertEqual(rows[0]["moderationAi"]["verdict"], "real")

    def test_process_clean(self):
        aid = self._upload()
        self._stub_scan([])
        relay.process_moderation_job(self.store, aid)
        st, body = self.store.moderation_status(self.contrib, aid)
        self.assertEqual(body["status"], "clean")
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "moderation", aid + ".blur.mp4")))

    def test_job_error_path(self):
        aid = self._upload()
        real_scan = moderation.scan_video

        def boom(*a, **k):
            raise moderation.ModerationError("no frames")

        moderation.scan_video = boom
        self.addCleanup(setattr, moderation, "scan_video", real_scan)
        relay.process_moderation_job(self.store, aid)
        st, body = self.store.moderation_status(self.contrib, aid)
        self.assertEqual(body["status"], "error")
        self.assertIn("no frames", body["error"])

    def test_auto_scan_on_submit(self):
        relay.META_API_KEY = "test-key"
        rid = self._assigned_request()
        aid = self._upload()
        calls = []
        real_enqueue = relay.enqueue_moderation
        relay.enqueue_moderation = lambda s, a: calls.append(a)
        self.addCleanup(setattr, relay, "enqueue_moderation", real_enqueue)
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid, "note": "First cut"})
        self.assertEqual(st, 200)
        self.assertEqual(calls, [aid])
        self.assertEqual(self.store.moderation[aid]["status"], "pending")
        os.environ["MOD_AUTO"] = "0"
        aid2 = self._upload("second.mp4")
        st, _ = self.store.request_changes(
            self.station, rid, {"feedback": "Trim it"})
        self.assertEqual(st, 200)
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid2, "note": "v2"})
        self.assertEqual(st, 200)
        self.assertEqual(calls, [aid])
        self.assertNotIn(aid2, self.store.moderation)

    def test_pitch_sample_auto_scan(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload()
        calls = []
        real_enqueue = relay.enqueue_moderation
        relay.enqueue_moderation = lambda s, a: calls.append(a)
        self.addCleanup(setattr, relay, "enqueue_moderation", real_enqueue)
        st, pitch = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Rooftop live",
            "brief": "Sunset set from a roof.", "assetId": aid})
        self.assertEqual(st, 201, pitch)
        self.assertEqual(pitch["assetMime"], "video/mp4")
        self.assertEqual(calls, [aid])
        self.assertEqual(self.store.moderation[aid]["status"], "pending")

    def test_pitch_flag_notifications(self):
        os.environ["MOD_AUTO"] = "0"
        relay.META_API_KEY = "test-key"
        aid = self._upload()
        st, pitch = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Rooftop live",
            "brief": "Sunset set from a roof.", "assetId": aid})
        self.assertEqual(st, 201, pitch)
        self.store._set_moderation(aid, {
            "status": "flagged",
            "flags": [{"start": 4.0, "end": 8.0, "category": "nudity"}],
            "ai": {"verdict": "ai", "score": 0.6, "judged": 5}})
        st, body = self.store.notifications_for(self.station)
        mods = [i for i in body["items"]
                if i["id"] == "pitchmod:%s" % pitch["id"]]
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["link"], "/requests")
        ais = [i for i in body["items"]
               if i["id"] == "pitchai:%s" % pitch["id"]]
        self.assertEqual(len(ais), 1)
        self.assertIn("60%", ais[0]["detail"])

    def test_review_serving_gate(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload()
        self._stub_scan([{"start": 0.0, "end": 2.0, "category": "nudity"}])
        relay.process_moderation_job(self.store, aid)
        rid = self._assigned_request()
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid, "note": "First cut"})
        self.assertEqual(st, 200)
        st, _ = self.store.approve_delivery(self.station, rid)
        self.assertEqual(st, 200)
        bare = self._upload("bare.mp4")
        st, _ = self.store.signup({
            "email": "other@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other@creator.tv")
        real_store = relay.STORE
        relay.STORE = self.store
        self.addCleanup(setattr, relay, "STORE", real_store)

        def serve(user, aid, **kw):
            h = relay.Handler.__new__(relay.Handler)
            h.headers = {}
            h._user = lambda: user
            out = {"headers": {}}
            h.send_response = lambda code: out.update(status=code)
            h.send_header = lambda k, v: out["headers"].update({k: v})
            h.end_headers = lambda: None
            h.wfile = io.BytesIO()
            h._send_json = lambda obj, status=200: out.update(
                status=status, obj=obj)
            h._serve_asset(aid, **kw)
            out["body"] = h.wfile.getvalue()
            return out

        out = serve(self.station, aid, review=True)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["headers"]["Content-Type"], "video/mp4")
        self.assertIn(b"blurred-bytes", out["body"])
        out = serve(None, aid, review=True)
        self.assertEqual(out["status"], 401)
        out = serve(other, aid, review=True)
        self.assertEqual(out["status"], 403)
        out = serve(self.station, bare, review=True)
        self.assertEqual(out["status"], 404)
        out = serve(self.station, aid, download=True)
        self.assertEqual(out["status"], 200)
        self.assertIn(b"blurred-bytes", out["body"])
        out = serve(self.contrib, aid, download=True)
        self.assertEqual(out["status"], 200)
        self.assertNotIn(b"blurred-bytes", out["body"])

    def test_notifications_flagged(self):
        os.environ["MOD_AUTO"] = "0"
        relay.META_API_KEY = "test-key"
        rid = self._assigned_request()
        aid = self._upload()
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid, "note": "First cut"})
        self.assertEqual(st, 200)
        self.store._set_moderation(aid, {
            "status": "flagged",
            "flags": [{"start": 62.0, "end": 66.0, "category": "nudity"}],
            "ai": {"verdict": "ai", "score": 0.75, "judged": 4}})
        st, body = self.store.notifications_for(self.station)
        mods = [i for i in body["items"] if i["kind"] == "flagged"]
        self.assertEqual(len(mods), 1)
        self.assertIn("1:02", mods[0]["detail"])
        self.assertEqual(mods[0]["link"], "/requests")
        ais = [i for i in body["items"] if i["kind"] == "ai"]
        self.assertEqual(len(ais), 1)
        self.assertIn("75%", ais[0]["detail"])
        self.assertEqual(ais[0]["link"], "/requests")
        st, body = self.store.notifications_for(self.contrib)
        mine = [i for i in body["items"] if i["kind"] == "flagged"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["link"], "/assignments")
        self.assertEqual(
            [i for i in body["items"] if i["kind"] == "ai"], [])

    def _publish(self, asset_id=""):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Harbor at dawn",
            "description": "Aerials and interviews.", "market": "Boston, MA",
            "price": 120, "assetId": asset_id})
        self.assertEqual(st, 201, item)
        return item

    def _stub_enqueue(self, calls):
        real_enqueue = relay.enqueue_moderation
        relay.enqueue_moderation = lambda s, a: calls.append(a)
        self.addCleanup(setattr, relay, "enqueue_moderation", real_enqueue)

    def _catalog_view(self, cid):
        found = [c for c in self.store.catalog() if c["id"] == cid]
        self.assertEqual(len(found), 1)
        return found[0]

    def _serve(self, user, aid, **kw):
        h = relay.Handler.__new__(relay.Handler)
        h.headers = {}
        h._user = lambda: user
        out = {"headers": {}}
        h.send_response = lambda code: out.update(status=code)
        h.send_header = lambda k, v: out["headers"].update({k: v})
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        h._send_json = lambda obj, status=200: out.update(
            status=status, obj=obj)
        h._serve_asset(aid, **kw)
        out["body"] = h.wfile.getvalue()
        return out

    def test_publish_auto_scans_video(self):
        relay.META_API_KEY = "test-key"
        calls = []
        self._stub_enqueue(calls)
        aid = self._upload()
        self._publish(aid)
        self.assertEqual(calls, [aid])
        self.assertEqual(self.store.moderation[aid]["status"], "pending")
        wid = self._upload("note.wav")
        self._publish(wid)
        self.assertEqual(calls, [aid])
        self.assertNotIn(wid, self.store.moderation)
        self._publish("")
        self.assertEqual(calls, [aid])
        os.environ["MOD_AUTO"] = "0"
        aid2 = self._upload("second.mp4")
        self._publish(aid2)
        self.assertEqual(calls, [aid])
        self.assertNotIn(aid2, self.store.moderation)

    def test_update_listing_asset_rescans(self):
        relay.META_API_KEY = "test-key"
        calls = []
        self._stub_enqueue(calls)
        item = self._publish("")
        aid = self._upload()
        st, body = self.store.update_content(
            self.contrib, item["id"], {"assetId": aid})
        self.assertEqual(st, 200, body)
        self.assertEqual(calls, [aid])
        self.assertEqual(self.store.moderation[aid]["status"], "pending")
        st, body = self.store.update_content(
            self.contrib, item["id"], {"assetId": aid})
        self.assertEqual(st, 200, body)
        self.assertEqual(calls, [aid])

    def test_catalog_carries_marketplace_labels(self):
        relay.META_API_KEY = "test-key"
        calls = []
        self._stub_enqueue(calls)
        aid = self._upload()
        item = self._publish(aid)
        view = self._catalog_view(item["id"])
        self.assertEqual(view["moderation"], "pending")
        self.assertEqual(view["assetMime"], "video/mp4")
        self.assertEqual(view["reviewStream"], "")
        self.store._set_moderation(aid, {
            "status": "clean", "flags": [], "frames": 10,
            "ai": {"verdict": "ai", "score": 0.75, "judged": 4,
                   "label": "LIKELY AI-MADE", "tone": "red"}})
        view = self._catalog_view(item["id"])
        self.assertEqual(view["moderation"], "clean")
        self.assertEqual(view["moderationFlags"], 0)
        self.assertEqual(view["moderationAi"]["label"], "LIKELY AI-MADE")
        self.assertEqual(view["reviewStream"], "")
        self.store._set_moderation(aid, {
            "status": "flagged",
            "flags": [{"start": 2.0, "end": 4.0, "category": "nudity"},
                      {"start": 10.0, "end": 12.0, "category": "other"}],
            "frames": 10,
            "ai": {"verdict": "real", "score": 0.0, "judged": 10},
            "review": aid + ".blur.mp4"})
        view = self._catalog_view(item["id"])
        self.assertEqual(view["moderation"], "flagged")
        self.assertEqual(view["moderationFlags"], 2)
        self.assertEqual(view["reviewStream"], "")
        os.makedirs(os.path.join(self.dir, "moderation"), exist_ok=True)
        with open(os.path.join(self.dir, "moderation", aid + ".blur.mp4"),
                  "wb") as f:
            f.write(b"blurred-bytes")
        view = self._catalog_view(item["id"])
        self.assertEqual(view["reviewStream"],
                         "/assets/%s?review=1" % aid)
        bare = self._publish("")
        view = self._catalog_view(bare["id"])
        self.assertEqual(view["moderation"], "none")
        self.assertEqual(view["moderationAi"], {})
        self.assertEqual(view["moderationFlags"], 0)
        self.assertEqual(view["reviewStream"], "")
        self.assertEqual(view["assetMime"], "")

    def test_marketplace_review_serves_any_station(self):
        relay.META_API_KEY = "test-key"
        os.environ["MOD_AUTO"] = "0"
        aid = self._upload()
        self._publish(aid)
        self._stub_scan([{"start": 0.0, "end": 2.0,
                           "category": "nudity"}])
        relay.process_moderation_job(self.store, aid)
        st, _ = self.store.signup({
            "email": "other@station.tv", "password": "password-1",
            "name": "Other", "role": "station", "org": "Other TV"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other@station.tv")
        real_store = relay.STORE
        relay.STORE = self.store
        self.addCleanup(setattr, relay, "STORE", real_store)
        out = self._serve(other, aid, review=True)
        self.assertEqual(out["status"], 200)
        self.assertIn(b"blurred-bytes", out["body"])
        out = self._serve(other, aid)
        self.assertEqual(out["status"], 200)
        self.assertNotIn(b"blurred-bytes", out["body"])
        st, body = self.store.moderation_status(other, aid)
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "flagged")

    def test_marketplace_ui_wiring(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "discover.html"),
                  encoding="utf-8") as f:
            discover = f.read()
        self.assertIn("modPillFor", discover)
        self.assertIn("aiPillFor", discover)
        self.assertIn("reviewStream", discover)
        self.assertIn("data-modpill", discover)
        with open(os.path.join(public, "booking.html"),
                  encoding="utf-8") as f:
            booking = f.read()
        self.assertIn("modbox", booking)
        self.assertIn("enhanceMod", booking)
        self.assertIn("pvVideo", booking)
        self.assertNotIn("modPanel", booking)
        self.assertNotIn("Content check", booking)
        with open(os.path.join(public, "publish.html"),
                  encoding="utf-8") as f:
            publish = f.read()
        self.assertIn("modPillFor", publish)
        with open(os.path.join(public, "js", "auth.js"),
                  encoding="utf-8") as f:
            shared = f.read()
        self.assertIn("function modPillFor", shared)

    def test_review_ui_wiring(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "js", "auth.js"),
                  encoding="utf-8") as f:
            shared = f.read()
        self.assertIn("function enhanceMod", shared)
        self.assertIn("/moderation", shared)
        self.assertIn("review=1", shared)
        self.assertIn("POSSIBLY AI-MADE", shared)
        self.assertIn("aiPillFor", shared)
        self.assertIn("ai.label", shared)
        self.assertIn("Scan for sensitive content", shared)
        self.assertIn("!forStation", shared)
        self.assertIn("flag-tag", shared)
        self.assertIn("CHECKED · CLEAN", shared)
        self.assertIn('icon("checkcircle", 11)', shared)
        self.assertIn('icon("info", 11)', shared)
        self.assertIn('icon("alert", 11)', shared)
        for name, token in [("requests.html", "enhanceMod(b, true)"),
                            ("requests.html", "modbox"),
                            ("requests.html",
                             "modbox' data-aid='\" + p.assetId"),
                            ("assignments.html", "enhanceMod(x, false)"),
                            ("assignments.html", "modbox")]:
            with open(os.path.join(public, name), encoding="utf-8") as f:
                self.assertIn(token, f.read())
        with open(os.path.join(public, "media.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("Check content", body)
        self.assertNotIn("modbtn", body)
        self.assertIn("/moderation", body)
        self.assertIn("POSSIBLY AI-MADE", body)
        self.assertIn('icon("checkcircle", 11)', body)
        self.assertIn('icon("info", 11)', body)
        self.assertIn('icon("alert", 11)', body)
        self.assertIn("aiBadgeFor", body)


if __name__ == "__main__":
    unittest.main()
