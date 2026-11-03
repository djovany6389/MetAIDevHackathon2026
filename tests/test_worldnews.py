"""Offline tests for the world briefing (no network, stub fetch)."""
import json
import os
import tempfile
import unittest

import server as relay
import worldnews

SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Floods hit the valley</title><link>https://example.com/a</link><description>Valley towns evacuate as waters rise.</description></item>
<item><title>Markets rally</title><link>https://example.com/b</link><description>Stocks climb for a third day.</description></item>
<item><title></title><link>https://example.com/c</link></item>
</channel></rss>"""


class WorldNewsCoreTest(unittest.TestCase):
    def test_parse_rss(self):
        items = worldnews.parse_rss(SAMPLE_RSS, "Example")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Floods hit the valley")
        self.assertEqual(items[0]["link"], "https://example.com/a")
        self.assertEqual(items[0]["source"], "Example")
        self.assertEqual(items[0]["description"],
                         "Valley towns evacuate as waters rise.")
        tagged = worldnews.parse_rss(
            b"<rss><channel><item><title>Hi</title>"
            b"<description>A <b>big</b> flood</description>"
            b"</item></channel></rss>", "X")
        self.assertEqual(tagged[0]["description"], "A big flood")
        self.assertEqual(worldnews.parse_rss(b"not xml", "X"), [])
        self.assertEqual(worldnews.parse_rss(b"", "X"), [])
        self.assertEqual(worldnews.parse_rss(b"<rss></rss>", "X"), [])
        tricky = worldnews.parse_rss(
            b"<rss><channel><item><title>Hi</title>"
            b"<link>javascript:alert(1)</link></item></channel></rss>", "X")
        self.assertEqual(tricky[0]["link"], "")

    def test_briefing_prompt_shape(self):
        payload = worldnews.build_briefing_request(
            [{"title": "Big flood", "source": "BBC", "link": "",
              "description": "Waters rising."}])
        text = payload["messages"][0]["content"][0]["text"]
        self.assertIn("1. [BBC] Big flood", text)
        self.assertIn("Waters rising.", text)

    def test_parse_briefing(self):
        digest, summaries = worldnews.parse_briefing(
            'Note: {"digest": "Big day.", "summaries": ["Floods.", "Stocks."]}',
            3)
        self.assertEqual(digest, "Big day.")
        self.assertEqual(summaries, ["Floods.", "Stocks.", ""])
        with self.assertRaises(worldnews.WorldNewsError):
            worldnews.parse_briefing("no json", 2)
        with self.assertRaises(worldnews.WorldNewsError):
            worldnews.parse_briefing("{}", 2)

    def test_feeds_configured(self):
        self.assertGreaterEqual(len(worldnews.feeds()), 2)
        for source, url in worldnews.feeds():
            self.assertTrue(source)
            self.assertTrue(url.startswith("https://"))
        os.environ["WORLDNEWS_FEEDS"] = "Demo|https://example.com/rss"
        self.addCleanup(os.environ.pop, "WORLDNEWS_FEEDS", None)
        self.assertEqual(worldnews.feeds(),
                         [("Demo", "https://example.com/rss")])


class WorldNewsJobTest(unittest.TestCase):
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
        self.store.signup({"email": "partner@skyline.example",
                           "password": "password-1", "name": "Skyline",
                           "role": "partner", "org": "Skyline"})
        self.station = self.store.find_user("station@metroline.tv")
        self.contrib = self.store.find_user("maya@mayachen.media")
        self.partner = self.store.find_user("partner@skyline.example")
        real_key = relay.META_API_KEY
        self.addCleanup(setattr, relay, "META_API_KEY", real_key)
        relay.META_API_KEY = "test-key"

    def _stub_enqueue(self, calls):
        real_enqueue = relay.enqueue_worldnews
        relay.enqueue_worldnews = lambda s: calls.append(True)
        self.addCleanup(setattr, relay, "enqueue_worldnews", real_enqueue)

    def test_refresh_ready(self):
        relay.process_worldnews_job(
            self.store, fetch_fn=lambda url: SAMPLE_RSS,
            summarize_fn=lambda items, key: {
                "summary": "Digest here.",
                "summaries": ["AI A", "AI B"]})
        st, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["summary"], "Digest here.")
        self.assertEqual(len(body["items"]), 6)
        self.assertEqual(body["items"][0]["summary"], "AI A")
        self.assertEqual(body["items"][3]["summary"],
                         "Stocks climb for a third day.")
        self.assertTrue(body["updated"])
        st, _ = self.store.worldnews_status(self.partner)
        self.assertEqual(st, 200)

    def test_summarize_failure_keeps_headlines(self):
        def boom(items, key):
            raise worldnews.WorldNewsError("no model")

        relay.process_worldnews_job(self.store, fetch_fn=lambda u: SAMPLE_RSS,
                                    summarize_fn=boom)
        _, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["summary"], "")
        self.assertEqual(body["items"][0]["summary"],
                         "Valley towns evacuate as waters rise.")

    def test_refresh_partial_failure(self):
        def flaky(url):
            if "bbc" in url:
                raise worldnews.WorldNewsError("down")
            return SAMPLE_RSS

        relay.process_worldnews_job(
            self.store, fetch_fn=flaky,
            summarize_fn=lambda i, k: {"summary": "Digest.",
                                       "summaries": ["AI A"] * 4})
        _, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(len(body["items"]), 4)
        self.assertEqual(body["items"][0]["summary"], "AI A")

    def test_refresh_total_failure(self):
        def down(url):
            raise worldnews.WorldNewsError("down")

        relay.process_worldnews_job(self.store, fetch_fn=down,
                                    summarize_fn=lambda i, k: "Digest.")
        _, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "error")
        self.assertIn("feeds", body["error"])

    def test_refresh_no_key(self):
        relay.META_API_KEY = ""
        seen = []
        relay.process_worldnews_job(
            self.store, fetch_fn=lambda url: SAMPLE_RSS,
            summarize_fn=lambda items, key: seen.append(True))
        _, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["summary"], "")
        self.assertEqual(len(body["items"]), 6)
        self.assertEqual(body["items"][1]["summary"],
                         "Stocks climb for a third day.")
        self.assertEqual(seen, [])

    def test_status_staleness(self):
        calls = []
        self._stub_enqueue(calls)
        st, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(calls, [True])
        st, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(calls, [True])
        self.store._set_worldnews({"status": "ready", "summary": "S",
                                   "items": [], "error": ""})
        st, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(calls, [True])
        self.store.worldnews["updated"] = "2024-01-01T00:00:00+00:00"
        st, body = self.store.worldnews_status(self.contrib)
        self.assertEqual(body["summary"], "S")
        self.assertEqual(calls, [True, True])

    def test_ui_wiring(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "contributor.html"),
                  encoding="utf-8") as f:
            dash = f.read()
        self.assertIn("worldBox", dash)
        self.assertIn("/api/worldnews", dash)
        self.assertIn("World briefing", dash)
        self.assertIn("Gathering headlines", dash)
        self.assertIn("worldRetry", dash)
        self.assertIn("worldStage", dash)
        self.assertIn("worldNext", dash)


if __name__ == "__main__":
    unittest.main()
