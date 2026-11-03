"""Offline tests for the caption pipeline (no network, stub transcriber)."""
import base64
import io
import json
import os
import tempfile
import unittest
import wave

import captions
import server as relay


def make_wav_bytes(nchan=2, width=2, rate=44100, seconds=1.0):
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchan)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 1:
            w.writeframes(bytes([128] * n * nchan))
        else:
            w.writeframes(b"\x00" * n * nchan * width)
    return buf.getvalue()


class CaptionCoreTest(unittest.TestCase):
    def test_vtt_timestamp(self):
        self.assertEqual(captions.vtt_timestamp(0), "00:00:00.000")
        self.assertEqual(captions.vtt_timestamp(61.5), "00:01:01.500")
        self.assertEqual(captions.vtt_timestamp(3661.25), "01:01:01.250")

    def test_segments_to_vtt(self):
        vtt = captions.segments_to_vtt([
            {"start": 1, "end": 2.5, "speaker": "A", "text": "hi"},
            {"start": 3, "end": 4, "speaker": "", "text": "yo\n-->"},
            {"start": 5, "end": 6, "speaker": "", "text": "   "}])
        self.assertEqual(
            vtt, "WEBVTT\n"
                 "\n"
                 "00:00:01.000 --> 00:00:02.500\n"
                 "hi\n"
                 "\n"
                 "00:00:03.000 --> 00:00:04.000\n"
                 "yo ->\n"
                 "\n")

    def test_vtt_to_cues(self):
        vtt = captions.segments_to_vtt([
            {"start": 0.5, "end": 2.0, "speaker": "Maya",
             "text": "hello captions"},
            {"start": 3.0, "end": 4.25, "speaker": "",
             "text": "no speaker here"}])
        cues = captions.vtt_to_cues(vtt)
        self.assertEqual(cues, [
            {"start": 0.5, "end": 2.0, "speaker": "",
             "text": "hello captions"},
            {"start": 3.0, "end": 4.25, "speaker": "",
             "text": "no speaker here"}])
        legacy = captions.vtt_to_cues(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nMaya: hi\n\n")
        self.assertEqual(legacy[0]["speaker"], "Maya")
        self.assertEqual(legacy[0]["text"], "hi")
        self.assertEqual(captions.vtt_to_cues("WEBVTT\n\n"), [])
        self.assertEqual(captions.vtt_to_cues("not captions at all"), [])
        self.assertEqual(captions.vtt_to_cues(""), [])

    def test_wav_to_pcm16_mono_16k(self):
        pcm = captions.wav_to_pcm16_mono_16k(
            make_wav_bytes(nchan=2, width=2, rate=44100, seconds=1.0))
        self.assertAlmostEqual(len(pcm), 16000 * 2, delta=100)
        self.assertEqual(pcm, b"\x00" * len(pcm))
        pcm8 = captions.wav_to_pcm16_mono_16k(
            make_wav_bytes(nchan=1, width=1, rate=8000, seconds=0.5))
        self.assertAlmostEqual(len(pcm8), 8000 * 2, delta=100)
        self.assertEqual(pcm8, b"\x00" * len(pcm8))
        with self.assertRaises(captions.CaptionError):
            captions.wav_to_pcm16_mono_16k(b"not a wav file")

    def test_chunk_offsets(self):
        stride = captions.SAMPLE_RATE * 2 * captions.CHUNK_SECONDS
        chunks = captions.chunk_pcm(b"\x00" * (stride + 100))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], 0.0)
        self.assertEqual(chunks[1][0], float(captions.CHUNK_SECONDS))
        self.assertEqual(len(chunks[1][1]), 100)

    def test_chunk_too_long(self):
        real_max = captions.MAX_MINUTES
        captions.MAX_MINUTES = 0
        self.addCleanup(setattr, captions, "MAX_MINUTES", real_max)
        with self.assertRaises(captions.CaptionError):
            captions.chunk_pcm(b"\x00" * 32000)

    def test_pcm_to_wav_roundtrip(self):
        pcm = b"\x01\x02" * 1600
        raw = captions.pcm_to_wav_bytes(pcm)
        with wave.open(io.BytesIO(raw), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(),
                              w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.readframes(w.getnframes()), pcm)

    def test_build_multipart(self):
        body, content_type = captions.build_multipart(b"RIFFdata")
        self.assertIn("multipart/form-data; boundary=", content_type)
        boundary = content_type.split("boundary=")[1].encode()
        self.assertIn(b'name="request"', body)
        self.assertIn(b'name="audio"', body)
        self.assertIn(b"RIFFdata", body)
        self.assertTrue(body.startswith(b"--" + boundary))
        self.assertIn(b'"mode": "DIARIZATION"', body)
        self.assertIn(b'"audioEncoding": "WAV"', body)
        self.assertIn(b'"model": "muse-voice-transcribe-1.0"', body)
        self.assertNotIn(b"diarize", body)

    def test_parse_segments(self):
        segs = captions.parse_segments(
            {"segments": [{"start": 1, "end": 2, "speaker": "A",
                           "text": "x"}]})
        self.assertEqual(segs, [{"start": 1, "end": 2, "speaker": "A",
                                 "text": "x"}])
        segs = captions.parse_segments(
            {"turns": [{"start_time": 1, "end_time": 2,
                        "speaker_label": "B", "transcript": "y"}]})
        self.assertEqual(segs, [{"start": 1, "end": 2, "speaker": "B",
                                 "text": "y"}])
        segs = captions.parse_segments(
            {"turns": [{"startMs": 1000, "endMs": 2500, "speaker": "A",
                        "transcript": "x"}],
             "audioDurationMs": 3000})
        self.assertEqual(segs, [{"start": 1.0, "end": 2.5, "speaker": "A",
                                 "text": "x"}])
        segs = captions.parse_segments(
            {"turns": [{"startMs": 0, "transcript": "x"},
                        {"startMs": 2000, "transcript": "y"}],
             "audioDurationMs": 5000})
        self.assertEqual([(s["start"], s["end"]) for s in segs],
                         [(0.0, 2.0), (2.0, 5.0)])
        self.assertEqual(captions.parse_segments("nope"), [])
        self.assertEqual(captions.parse_segments({}), [])

    def test_transcribe_error_includes_body(self):
        real_urlopen = captions.urllib.request.urlopen
        err = captions.urllib.error.HTTPError(
            captions.TRANSCRIBE_URL, 400, "Bad Request", {},
            io.BytesIO(b'{"message": "malformed request"}'))

        def boom(*a, **k):
            raise err

        captions.urllib.request.urlopen = boom
        self.addCleanup(setattr, captions.urllib.request, "urlopen",
                        real_urlopen)
        with self.assertRaises(captions.CaptionError) as ctx:
            captions.muse_transcribe(b"RIFF....", "key")
        self.assertIn("HTTP Error 400", str(ctx.exception))
        self.assertIn("malformed request", str(ctx.exception))


class CaptionJobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="relay-cap-")
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

    def _upload_wav(self):
        raw = make_wav_bytes(nchan=1, width=1, rate=8000, seconds=0.5)
        st, sess = self.store.asset_init(
            self.contrib, {"name": "note.wav", "size": len(raw)})
        self.assertEqual(st, 201, sess)
        st, _ = self.store.asset_chunk(
            self.contrib, sess["id"],
            {"index": 0, "data": base64.b64encode(raw).decode()})
        self.assertEqual(st, 200)
        st, done = self.store.asset_complete(self.contrib, sess["id"])
        self.assertEqual(st, 200, done)
        return sess["id"]

    @staticmethod
    def _stub(wav_bytes):
        assert wav_bytes[:4] == b"RIFF"
        return [{"start": 0, "end": 0.5, "speaker": "Maya",
                 "text": "hello captions"}]

    def test_request_validates(self):
        relay.META_API_KEY = ""  # explicit: must not depend on ambient env
        aid = self._upload_wav()
        st, _ = self.store.request_captions(self.contrib, aid)
        self.assertEqual(st, 503)
        st, _ = self.store.request_captions(self.station, aid)
        self.assertEqual(st, 503)
        st, _ = self.store.request_captions(self.contrib, "as_missing")
        self.assertEqual(st, 404)
        st, _ = self.store.signup({
            "email": "other@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other@creator.tv")
        st, _ = self.store.request_captions(other, aid)
        self.assertEqual(st, 403)
        st, _ = self.store.caption_status(other, aid)
        self.assertEqual(st, 403)

    def test_auto_caption_flag(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload_wav()
        st, body = self.store.request_captions(self.station, aid,
                                               {"auto": True})
        self.assertEqual(st, 202, body)
        old = os.environ.get("CAPTIONS_AUTO")
        os.environ["CAPTIONS_AUTO"] = "0"

        def restore():
            if old is None:
                os.environ.pop("CAPTIONS_AUTO", None)
            else:
                os.environ["CAPTIONS_AUTO"] = old

        self.addCleanup(restore)
        aid2 = self._upload_wav()
        st, body = self.store.request_captions(self.station, aid2,
                                               {"auto": True})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "disabled")
        self.assertNotIn(aid2, self.store.captions)
        st, body = self.store.request_captions(self.station, aid2, {})
        self.assertEqual(st, 202, body)

    def test_players_auto_caption_on_play(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "js", "auth.js"),
                  encoding="utf-8") as f:
            shared = f.read()
        self.assertIn("function attachRollup", shared)
        self.assertIn("/captions/cues", shared)
        with open(os.path.join(public, "css", "styles.css"),
                  encoding="utf-8") as f:
            self.assertIn(".cc608", f.read())
        for name, token in [("assignments.html", "attachRollup(this, '"),
                            ("requests.html", "attachRollup(this, '"),
                            ("media.html", "attachRollup(this, '"),
                            ("discover.html", "attachRollup(video"),
                            ("booking.html", "attachRollup(")]:
            with open(os.path.join(public, name), encoding="utf-8") as f:
                body = f.read()
            self.assertIn(token, body)
            self.assertNotIn("ensureCc(", body)
            self.assertNotIn("ccTrack(", body)
            self.assertNotIn("ccTrackEl(", body)

    def test_station_can_generate_captions(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload_wav()
        st, body = self.store.request_captions(self.station, aid)
        self.assertEqual(st, 202, body)
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "js", "auth.js"),
                  encoding="utf-8") as f:
            shared = f.read()
        self.assertIn("function enhanceCc", shared)
        self.assertIn("Generate captions", shared)
        self.assertIn("modCcPill", shared)
        with open(os.path.join(public, "requests.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("ccbox", body)
        self.assertIn("enhanceCc(b)", body)

    def test_request_enqueues(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload_wav()
        st, body = self.store.request_captions(self.contrib, aid)
        self.assertEqual(st, 202, body)
        self.assertEqual(body["status"], "pending")

    def test_request_and_process(self):
        aid = self._upload_wav()
        self.store._set_caption(aid, "pending")
        real_enqueue = relay.enqueue_intelligence
        relay.enqueue_intelligence = lambda s, a: None
        self.addCleanup(setattr, relay, "enqueue_intelligence", real_enqueue)
        relay.process_caption_job(self.store, aid,
                                  transcribe_fn=self._stub)
        st, body = self.store.caption_status(self.contrib, aid)
        self.assertEqual(body["status"], "ready")
        path = os.path.join(self.dir, "captions", aid + ".vtt")
        with open(path, encoding="utf-8") as f:
            vtt = f.read()
        self.assertIn("WEBVTT", vtt)
        self.assertIn("hello captions", vtt)
        self.assertNotIn("Maya:", vtt)
        rows = [r for r in self.store.list_assets(self.contrib)
                if r["id"] == aid]
        self.assertEqual(rows[0]["captions"], "ready")

    def test_cues_persisted_and_served(self):
        aid = self._upload_wav()
        self.store._set_caption(aid, "pending")
        real_enqueue = relay.enqueue_intelligence
        relay.enqueue_intelligence = lambda s, a: None
        self.addCleanup(setattr, relay, "enqueue_intelligence", real_enqueue)
        relay.process_caption_job(self.store, aid,
                                  transcribe_fn=self._stub)
        path = os.path.join(self.dir, "captions", aid + ".json")
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["cues"],
                         [{"start": 0.0, "end": 0.5, "speaker": "Maya",
                           "text": "hello captions"}])
        st, body = self.store.caption_cues(self.contrib, aid)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["cues"][0]["text"], "hello captions")
        st, body = self.store.caption_cues(self.station, aid)
        self.assertEqual(st, 200, body)
        st, _ = self.store.signup({
            "email": "cueother@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("cueother@creator.tv")
        st, _ = self.store.caption_cues(other, aid)
        self.assertEqual(st, 403)
        bare = self._upload_wav()
        st, body = self.store.caption_cues(self.contrib, bare)
        self.assertEqual(st, 404, body)
        st, _ = self.store.caption_cues(self.contrib, "as_missing")
        self.assertEqual(st, 404)

    def test_legacy_vtt_backfills_cues(self):
        aid = self._upload_wav()
        vtt = captions.segments_to_vtt(
            [{"start": 0, "end": 0.5, "speaker": "Maya",
              "text": "hello captions"}])
        os.makedirs(os.path.join(self.dir, "captions"), exist_ok=True)
        with open(os.path.join(self.dir, "captions", aid + ".vtt"),
                  "w", encoding="utf-8") as f:
            f.write(vtt)
        self.store._set_caption(aid, "ready")
        again = relay.Store(self.dir)
        path = os.path.join(self.dir, "captions", aid + ".json")
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["cues"][0]["text"], "hello captions")
        st, body = again.caption_cues(
            again.find_user("maya@mayachen.media"), aid)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["cues"][0]["speaker"], "Maya")

    def test_ready_without_files_regenerates(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload_wav()
        self.store._set_caption(aid, "ready")
        st, body = self.store.request_captions(self.contrib, aid)
        self.assertEqual(st, 202, body)
        self.assertEqual(body["status"], "pending")

    def test_job_error_path(self):
        aid = self._upload_wav()

        def boom(wav_bytes):
            raise captions.CaptionError("no speech detected")

        relay.process_caption_job(self.store, aid, transcribe_fn=boom)
        st, body = self.store.caption_status(self.contrib, aid)
        self.assertEqual(body["status"], "error")
        self.assertIn("no speech", body["error"])

    def test_vtt_serving_gate(self):
        relay.META_API_KEY = "test-key"
        aid = self._upload_wav()
        self.store._set_caption(aid, "pending")
        real_enqueue = relay.enqueue_intelligence
        relay.enqueue_intelligence = lambda s, a: None
        self.addCleanup(setattr, relay, "enqueue_intelligence", real_enqueue)
        relay.process_caption_job(self.store, aid,
                                  transcribe_fn=self._stub)
        bare = self._upload_wav()
        real_store = relay.STORE
        relay.STORE = self.store
        self.addCleanup(setattr, relay, "STORE", real_store)

        def serve(user, aid):
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
            h._serve_captions(aid)
            out["body"] = h.wfile.getvalue()
            return out

        out = serve(self.contrib, aid)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["headers"]["Content-Type"],
                         "text/vtt; charset=utf-8")
        self.assertIn(b"WEBVTT", out["body"])
        out = serve(None, aid)
        self.assertEqual(out["status"], 401)
        out = serve(self.contrib, bare)
        self.assertEqual(out["status"], 404)


if __name__ == "__main__":
    unittest.main()
