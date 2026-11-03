"""Relay Network API tests (standard library only, no sockets).

Exercises the same Store methods the HTTP layer calls.

Run:  python -m unittest discover -s tests -v
"""
import base64
import io
import os
import re
import tempfile
from html.parser import HTMLParser
import unittest
from datetime import datetime, timedelta, timezone

import server as relay


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = relay.Store(self.tmp.name)
        n = getattr(type(self), "_counter", 0) + 1
        type(self)._counter = n
        self.users = {}
        for role, email, extra in [
                ("station", "s%d@station.tv" % n, {"org": "Station %d" % n}),
                ("contributor", "c%d@creator.media" % n, {"roles": ["Reporter"]}),
                ("partner", "p%d@hotel.example" % n, {"org": "Hotel %d" % n})]:
            st, body = self.store.signup(dict(
                email=email, password="password-1", name="User %d" % n,
                role=role, **extra))
            self.assertEqual(st, 201, (st, body))
            self.users[role] = self.store.find_user(email)

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def station(self):
        return self.users["station"]

    @property
    def contrib(self):
        return self.users["contrib"] if "contrib" in self.users else self.users["contributor"]

    @property
    def partner(self):
        return self.users["partner"]

    def _live_feed(self, rate=50):
        st, item = self.store.create_feed(self.partner, {
            "name": "Rooftop", "location": "Roof", "rate": rate})
        self.assertEqual(st, 201, item)
        st, _ = self.store.feed_status(
            self.partner, item["id"], {"status": "live"})
        self.assertEqual(st, 200)
        return item

    def test_signup_requires_org_for_station_and_partner(self):
        st, _ = self.store.signup({
            "email": "noname@station.tv", "password": "password-1",
            "name": "No Name", "role": "station"})
        self.assertEqual(st, 400)
        st, body = self.store.signup({
            "email": "named@station.tv", "password": "password-1",
            "name": "Jane", "role": "station", "org": "Named TV"})
        self.assertEqual(st, 201, body)
        self.assertEqual(
            self.store.find_user("named@station.tv")["org"], "Named TV")
        st, _ = self.store.signup({
            "email": "noprop@biz.example", "password": "password-1",
            "name": "No Prop", "role": "partner"})
        self.assertEqual(st, 400)

    def test_auth_roundtrip_and_roles(self):
        st, body = self.store.login(
            {"email": self.station["email"], "password": "password-1"})
        self.assertEqual(st, 200)
        user = self.store.user_for_token(body["token"])
        self.assertEqual(user["role"], "station")
        st, _ = self.store.create_booking(self.contrib, {})
        self.assertEqual(st, 403)
        st, _ = self.store.create_feed(self.station, {})
        self.assertEqual(st, 403)
        # sessions persist across restarts
        again = relay.Store(self.tmp.name)
        self.assertEqual(
            again.user_for_token(body["token"])["email"], self.station["email"])

    def test_booking_pricing_is_server_side(self):
        feed = self._live_feed(50)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, bk = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": today, "from": "10:00",
            "until": "12:00", "total": "$0.01"})
        self.assertEqual(st, 201, bk)
        self.assertEqual(bk["unitPrice"], 50)
        self.assertEqual(bk["hours"], 2)
        self.assertEqual(bk["total"], 100)
        self.assertTrue(bk["invoice"].startswith("INV-"))
        mine = self.store.list_bookings(self.station)
        self.assertTrue(any(b["id"] == bk["id"] for b in mine))

    def test_feed_uplink_stored_and_validated(self):
        st, item = self.store.create_feed(self.partner, {
            "name": "Uplinked", "location": "Roof", "rate": 40,
            "uplink": "srt://uplink.example.com:9000?streamid=cam-1"})
        self.assertEqual(st, 201, item)
        self.assertEqual(item["uplink"],
                         "srt://uplink.example.com:9000?streamid=cam-1")
        mine = self.store.list_feeds(self.partner)
        self.assertEqual(
            [f for f in mine if f["id"] == item["id"]][0]["uplink"],
            "srt://uplink.example.com:9000?streamid=cam-1")
        st, body = self.store.create_feed(self.partner, {
            "name": "Bad uplink", "location": "Roof", "rate": 40,
            "uplink": "not a url"})
        self.assertEqual(st, 400, body)

    def test_offline_feed_not_bookable(self):
        st, item = self.store.create_feed(self.partner, {
            "name": "Dark", "location": "Basement", "rate": 10})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, body = self.store.create_booking(self.station, {
            "contentId": item["id"], "date": today, "from": "10:00",
            "until": "11:00"})
        self.assertEqual(st, 409, body)

    def test_partner_feed_visible_in_catalog(self):
        feed = self._live_feed(42)
        found = [c for c in self.store.catalog() if c["id"] == feed["id"]]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["price"], 42)
        self.assertEqual(found[0]["kind"], "tower")

    def test_usage_derived_from_real_bookings(self):
        n0 = len(self.store.usage_for(self.partner))
        feed = self._live_feed(60)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": today, "from": "08:00",
            "until": "10:00"})
        after = self.store.usage_for(self.partner)
        self.assertEqual(len(after), n0 + 1)
        self.assertEqual(after[0]["earnings"], 72.0)  # 60*2*0.6
        self.assertEqual(after[0]["hours"], 2)

    def test_endpoints_gated_to_active_window(self):
        feed = self._live_feed(20)
        now = datetime.now(timezone.utc)
        fmt = "%H:%M"
        st, live = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": now.strftime("%Y-%m-%d"),
            "from": (now - timedelta(minutes=5)).strftime(fmt),
            "until": (now + timedelta(minutes=55)).strftime(fmt)})
        self.assertEqual(st, 201, live)
        st, eps = self.store.endpoints(self.station, live["id"])
        self.assertEqual(st, 200, eps)
        self.assertIn("token", eps["srt"])
        st, future = self.store.create_booking(self.station, {
            "contentId": feed["id"],
            "date": (now + timedelta(days=2)).strftime("%Y-%m-%d"),
            "from": "10:00", "until": "11:00"})
        st, _ = self.store.endpoints(self.station, future["id"])
        self.assertEqual(st, 403)

    def _assigned_request(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Harbor story", "brief": "Two minutes on the harbor."})
        self.assertEqual(st, 201)
        self.store.apply(self.contrib, rq["id"], {"note": "Pick me"})
        st, _ = self.store.accept_application(
            self.station, rq["id"], {"email": self.contrib["email"]})
        self.assertEqual(st, 200)
        return rq["id"]

    def test_assignment_lifecycle(self):
        rid = self._assigned_request()
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual(req["status"], "assigned")
        self.assertEqual(req["assignee"]["email"], self.contrib["email"])
        st, _ = self.store.accept_application(
            self.station, rid, {"email": self.contrib["email"]})
        self.assertEqual(st, 409)
        st, sess = self.store.asset_init(
            self.contrib, {"name": "cut.mp4", "size": 3})
        self.store.asset_chunk(self.contrib, sess["id"],
                               {"index": 0, "data": "YWJj"})
        self.store.asset_complete(self.contrib, sess["id"])
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": sess["id"], "note": "First cut"})
        self.assertEqual(st, 200)
        st, _ = self.store.request_changes(
            self.station, rid, {"feedback": "Trim the intro"})
        self.assertEqual(st, 200)
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual(req["status"], "assigned")
        self.assertEqual(req["feedback"], "Trim the intro")
        st, _ = self.store.submit_delivery(self.contrib, rid, {"note": "v2"})
        self.assertEqual(st, 200)
        st, _ = self.store.approve_delivery(self.station, rid)
        self.assertEqual(st, 200)
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual(req["status"], "completed")

    def test_delivery_versions_accumulate(self):
        rid = self._assigned_request()
        self.store.submit_delivery(self.contrib, rid, {"note": "v1"})
        self.store.request_changes(self.station, rid, {"feedback": "again"})
        self.store.submit_delivery(self.contrib, rid, {"note": "v2"})
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual([d["v"] for d in req["deliveries"]], [1, 2])
        self.assertEqual(req["delivery"]["note"], "v2")

    def test_delivery_files_preview_and_download(self):
        rid = self._assigned_request()
        st, sess = self.store.asset_init(
            self.contrib, {"name": "cut.mp4", "size": 3})
        self.assertEqual(st, 201, sess)
        self.store.asset_chunk(self.contrib, sess["id"],
                               {"index": 0, "data": "YWJj"})
        self.store.asset_complete(self.contrib, sess["id"])
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": sess["id"], "note": "First cut"})
        self.assertEqual(st, 200)
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual(req["delivery"]["assetName"], "cut.mp4")
        self.assertEqual(req["delivery"]["assetMime"], "video/mp4")
        st, _ = self.store.request_changes(
            self.station, rid, {"feedback": "Trim the intro"})
        self.assertEqual(st, 200)
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": sess["id"], "note": "v2"})
        self.assertEqual(st, 200)
        st, _ = self.store.approve_delivery(self.station, rid)
        self.assertEqual(st, 200)
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rid][0]
        self.assertEqual(req["status"], "completed")
        self.assertEqual(req["delivery"]["v"], 2)
        self.assertEqual(req["delivery"]["assetMime"], "video/mp4")
        # download rights: owner + commissioning station only
        self.assertTrue(
            self.store.asset_downloadable(self.contrib, sess["id"]))
        self.assertTrue(
            self.store.asset_downloadable(self.station, sess["id"]))
        st, _ = self.store.signup({
            "email": "other@station.tv", "password": "password-1",
            "name": "Other", "role": "station", "org": "Other TV"})
        self.assertEqual(st, 201)
        other_station = self.store.find_user("other@station.tv")
        self.assertFalse(
            self.store.asset_downloadable(other_station, sess["id"]))
        self.assertFalse(
            self.store.asset_downloadable(self.partner, sess["id"]))
        self.assertFalse(
            self.store.asset_downloadable(self.station, "as_missing"))
        # ?download=1 serves an attachment, gated the same way
        real_store = relay.STORE
        relay.STORE = self.store
        self.addCleanup(setattr, relay, "STORE", real_store)

        def serve(user, aid, download):
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
            h._serve_asset(aid, download)
            return out

        out = serve(self.station, sess["id"], False)
        self.assertEqual(out["status"], 200)
        self.assertNotIn("Content-Disposition", out["headers"])
        out = serve(self.station, sess["id"], True)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["headers"]["Content-Disposition"],
                         'attachment; filename="cut.mp4"')
        out = serve(self.contrib, sess["id"], True)
        self.assertEqual(out["status"], 200)
        out = serve(other_station, sess["id"], True)
        self.assertEqual(out["status"], 403)
        out = serve(None, sess["id"], True)
        self.assertEqual(out["status"], 401)

    def _upload_file(self, name="cut.mp4", complete=True):
        st, sess = self.store.asset_init(
            self.contrib, {"name": name, "size": 3})
        self.assertEqual(st, 201, sess)
        if complete:
            self.store.asset_chunk(self.contrib, sess["id"],
                                   {"index": 0, "data": "YWJj"})
            self.store.asset_complete(self.contrib, sess["id"])
        return sess["id"]

    def test_contributor_can_delete_video(self):
        aid = self._upload_file()
        path = os.path.join(self.tmp.name, "assets",
                             self.store.asset_for(aid)["stored"])
        self.assertTrue(os.path.isfile(path))
        self.store._set_caption(aid, "ready")
        self.store._set_moderation(aid, {"status": "clean", "flags": []})
        st, body = self.store.delete_asset(self.contrib, aid)
        self.assertEqual(st, 200, body)
        self.assertIsNone(self.store.asset_for(aid))
        self.assertFalse(os.path.isfile(path))
        self.assertNotIn(aid, self.store.captions)
        self.assertNotIn(aid, self.store.moderation)
        rows = self.store.list_assets(self.contrib)[1]
        self.assertEqual([r for r in rows if r["id"] == aid], [])
        h = relay.Handler.__new__(relay.Handler)
        h.path = "/api/assets/" + aid
        h._user = lambda: self.contrib
        out = {}
        h._send_json = lambda obj, status=200: out.update(
            status=status, obj=obj)
        h.do_DELETE()
        self.assertEqual(out["status"], 404)

    def test_delete_asset_validates(self):
        st, _ = self.store.delete_asset(self.contrib, "as_missing")
        self.assertEqual(st, 404)
        aid = self._upload_file(complete=False)
        st, _ = self.store.signup({
            "email": "other-" + self.contrib["email"],
            "password": "password-1", "name": "Other",
            "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other-" + self.contrib["email"])
        st, _ = self.store.delete_asset(other, aid)
        self.assertEqual(st, 403)
        st, _ = self.store.delete_asset(self.station, aid)
        self.assertEqual(st, 403)
        st, body = self.store.delete_asset(self.contrib, aid)
        self.assertEqual(st, 200, body)

    def test_delete_blocked_when_in_use(self):
        aid = self._upload_file()
        st, item = self.store.publish_content(self.contrib, {
            "kind": "raw", "title": "Harbor B-roll",
            "description": "Two minutes of harbor.", "market": "New York",
            "price": 99, "assetId": aid})
        self.assertEqual(st, 201, item)
        st, body = self.store.delete_asset(self.contrib, aid)
        self.assertEqual(st, 409, body)
        self.assertIn("marketplace", body["error"])
        rid = self._assigned_request()
        aid2 = self._upload_file("delivery.mp4")
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid2, "note": "First cut"})
        self.assertEqual(st, 200)
        st, body = self.store.delete_asset(self.contrib, aid2)
        self.assertEqual(st, 409, body)
        self.assertIn("station", body["error"])
        aid3 = self._upload_file("sample.mp4")
        st, _ = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Rooftop live",
            "brief": "Sunset set.", "assetId": aid3})
        self.assertEqual(st, 201)
        st, body = self.store.delete_asset(self.contrib, aid3)
        self.assertEqual(st, 409, body)
        self.assertIn("pitch", body["error"])

    def test_station_download_only_after_approval(self):
        rid = self._assigned_request()
        aid = self._upload_file()
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid, "note": "First cut"})
        self.assertEqual(st, 200)
        self.assertTrue(self.store.asset_downloadable(self.contrib, aid))
        self.assertFalse(self.store.asset_downloadable(self.station, aid))
        st, _ = self.store.approve_delivery(self.station, rid)
        self.assertEqual(st, 200)
        self.assertTrue(self.store.asset_downloadable(self.station, aid))

    def test_requests_download_gated_on_approval(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "requests.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("allowDownload", body)
        self.assertIn('deliveryFile(del, r.status === "completed")', body)
        self.assertIn('r.status === "completed" ? " · <a href=\'/assets/"',
                      body)

    def test_contributor_can_edit_listing(self):
        aid = self._upload_file()
        st, item = self.store.publish_content(self.contrib, {
            "kind": "raw", "title": "Harbor B-roll",
            "description": "Two minutes of harbor.", "market": "New York",
            "price": 99, "assetId": aid})
        self.assertEqual(st, 201, item)
        cid = item["id"]
        st, body = self.store.update_content(self.contrib, cid, {
            "title": "Harbor B-roll, extended", "price": 149})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["title"], "Harbor B-roll, extended")
        self.assertEqual(body["price"], 149)
        self.assertEqual(body["market"], "New York")
        self.assertEqual(body["asset"], aid)
        st, _ = self.store.update_content(
            self.contrib, cid, {"title": "  "})
        self.assertEqual(st, 400)
        st, _ = self.store.update_content(
            self.contrib, cid, {"price": 0})
        self.assertEqual(st, 400)
        st, _ = self.store.update_content(
            self.contrib, cid, {"price": "free"})
        self.assertEqual(st, 400)
        st, _ = self.store.update_content(
            self.contrib, "ct_missing", {"title": "x"})
        self.assertEqual(st, 404)
        st, _ = self.store.update_content(
            self.station, cid, {"title": "x"})
        self.assertEqual(st, 403)
        st, _ = self.store.signup({
            "email": "other-" + self.contrib["email"],
            "password": "password-1", "name": "Other",
            "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other-" + self.contrib["email"])
        st, _ = self.store.update_content(other, cid, {"title": "x"})
        self.assertEqual(st, 404)

    def test_contributor_can_swap_listing_file(self):
        aid = self._upload_file()
        aid2 = self._upload_file("replacement.mp4")
        st, item = self.store.publish_content(self.contrib, {
            "kind": "raw", "title": "Harbor B-roll",
            "description": "Two minutes of harbor.", "market": "New York",
            "price": 99, "assetId": aid})
        self.assertEqual(st, 201, item)
        cid = item["id"]
        st, body = self.store.update_content(
            self.contrib, cid, {"assetId": aid2})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["asset"], aid2)
        shown = [c for c in self.store.catalog() if c["id"] == cid][0]
        self.assertEqual(shown["stream"], "/assets/" + aid2)
        self.assertEqual(shown["assetName"], "replacement.mp4")
        st, body = self.store.update_content(
            self.contrib, cid, {"assetId": ""})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["asset"], "")
        st, _ = self.store.update_content(
            self.contrib, cid, {"assetId": "as_missing"})
        self.assertEqual(st, 400)
        aid3 = self._upload_file("partial.mp4", complete=False)
        st, _ = self.store.update_content(
            self.contrib, cid, {"assetId": aid3})
        self.assertEqual(st, 400)

    def test_publish_page_has_edit_listing(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "publish.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("Edit listing", body)
        self.assertIn('method: "PUT"', body)
        self.assertIn("/api/content/", body)
        self.assertIn("Attached file", body)
        self.assertIn("eAsset", body)

    def test_folder_crud_and_defaults(self):
        st, rows = self.store.list_folders(self.contrib)
        self.assertEqual(st, 200)
        self.assertEqual([f["name"] for f in rows],
                         ["Uploads", "Raw footage", "Finals"])
        self.assertTrue(rows[0]["system"])
        st, body = self.store.create_folder(
            self.contrib, {"name": "B-roll"})
        self.assertEqual(st, 201, body)
        fid = body["id"]
        st, _ = self.store.create_folder(self.contrib, {"name": "b-roll"})
        self.assertEqual(st, 409)
        st, _ = self.store.create_folder(self.contrib, {"name": "  "})
        self.assertEqual(st, 400)
        st, body = self.store.rename_folder(
            self.contrib, fid, {"name": "Selects"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["name"], "Selects")
        st, _ = self.store.rename_folder(
            self.contrib, "uploads", {"name": "Nope"})
        self.assertEqual(st, 400)
        st, _ = self.store.rename_folder(
            self.contrib, "fld_missing", {"name": "Nope"})
        self.assertEqual(st, 404)
        st, _ = self.store.list_folders(self.station)
        self.assertEqual(st, 403)
        st, _ = self.store.create_folder(self.station, {"name": "x"})
        self.assertEqual(st, 403)

    def test_folder_delete_moves_files_home(self):
        aid = self._upload_file()
        st, folder = self.store.create_folder(
            self.contrib, {"name": "Keepers"})
        self.assertEqual(st, 201, folder)
        st, _ = self.store.move_asset(
            self.contrib, aid, {"folder": folder["id"]})
        self.assertEqual(st, 200)
        rows = self.store.list_assets(self.contrib)[1]
        self.assertEqual(
            [r for r in rows if r["id"] == aid][0]["folder"],
            folder["id"])
        st, body = self.store.delete_folder(self.contrib, folder["id"])
        self.assertEqual(st, 200, body)
        self.assertEqual(body["moved"], 1)
        rows = self.store.list_assets(self.contrib)[1]
        self.assertEqual(
            [r for r in rows if r["id"] == aid][0]["folder"], "uploads")
        st, _ = self.store.delete_folder(self.contrib, "uploads")
        self.assertEqual(st, 400)
        st, _ = self.store.delete_folder(self.contrib, "fld_missing")
        self.assertEqual(st, 404)

    def test_move_asset_validates(self):
        aid = self._upload_file()
        st, body = self.store.move_asset(
            self.contrib, aid, {"folder": "finals"})
        self.assertEqual(st, 200, body)
        st, _ = self.store.move_asset(
            self.contrib, aid, {"folder": "fld_missing"})
        self.assertEqual(st, 404)
        st, _ = self.store.move_asset(
            self.contrib, "as_missing", {"folder": "finals"})
        self.assertEqual(st, 404)
        st, _ = self.store.signup({
            "email": "other-" + self.contrib["email"],
            "password": "password-1", "name": "Other",
            "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("other-" + self.contrib["email"])
        st, _ = self.store.move_asset(other, aid, {"folder": "finals"})
        self.assertEqual(st, 403)
        st, _ = self.store.move_asset(
            self.station, aid, {"folder": "finals"})
        self.assertEqual(st, 403)
        rows = self.store.list_assets(self.contrib)[1]
        self.assertEqual(
            [r for r in rows if r["id"] == aid][0]["folder"], "finals")

    def test_media_library_has_folders(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "media.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("folderBar", body)
        self.assertIn("New folder", body)
        self.assertIn("Delete folder", body)
        self.assertIn("mvsel", body)
        self.assertIn("/api/folders", body)
        self.assertIn("curFolder", body)

    def test_media_library_has_delete_button(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "media.html"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("delbtn", body)
        self.assertIn('method: "DELETE"', body)

    def test_station_can_edit_request(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Harbor story", "brief": "Two minutes on the harbor.",
            "budget": "$350", "location": "New York, NY"})
        self.assertEqual(st, 201, rq)
        st, body = self.store.update_request(self.station, rq["id"], {
            "title": "Harbor story, extended", "brief": "Four minutes now.",
            "budget": "$700"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["title"], "Harbor story, extended")
        self.assertEqual(body["brief"], "Four minutes now.")
        self.assertEqual(body["budget"], "$700")
        self.assertEqual(body["location"], "New York, NY")
        st, _ = self.store.update_request(
            self.station, rq["id"], {"title": "  "})
        self.assertEqual(st, 400)
        st, _ = self.store.update_request(
            self.station, rq["id"], {"brief": ""})
        self.assertEqual(st, 400)
        st, _ = self.store.update_request(
            self.station, rq["id"], {"roles": "Reporter"})
        self.assertEqual(st, 400)
        st, _ = self.store.update_request(
            self.contrib, rq["id"], {"title": "Nope"})
        self.assertEqual(st, 403)
        st, _ = self.store.update_request(
            self.station, "rq_missing", {"title": "Nope"})
        self.assertEqual(st, 404)
        st, _ = self.store.update_request(
            self.partner, rq["id"], {"title": "Nope"})
        self.assertEqual(st, 403)
        st, _ = self.store.signup({
            "email": "other@station.tv", "password": "password-1",
            "name": "Other", "role": "station", "org": "Other TV"})
        self.assertEqual(st, 201)
        st, _ = self.store.update_request(
            self.store.find_user("other@station.tv"), rq["id"],
            {"title": "Nope"})
        self.assertEqual(st, 404)
        self.store.apply(self.contrib, rq["id"], {"note": "Pick me"})
        st, _ = self.store.accept_application(
            self.station, rq["id"], {"email": self.contrib["email"]})
        self.assertEqual(st, 200)
        st, body = self.store.update_request(
            self.station, rq["id"], {"title": "Fixed after assign",
                                     "roles": ["Reporter", "Editor"]})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["roles"], ["Reporter", "Editor"])
        st, _ = self.store.submit_delivery(
            self.contrib, rq["id"], {"note": "v1"})
        self.assertEqual(st, 200)
        st, _ = self.store.approve_delivery(self.station, rq["id"])
        self.assertEqual(st, 200)
        st, body = self.store.update_request(
            self.station, rq["id"], {"budget": "$800"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["budget"], "$800")
        with open(os.path.join(os.path.dirname(relay.__file__), "public",
                               "requests.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Edit brief", html)
        self.assertIn("comma-separated", html)

    def test_station_notifications(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Harbor story", "brief": "Two minutes on the harbor."})
        self.assertEqual(st, 201, rq)
        rid = rq["id"]
        self.store.apply(self.contrib, rid, {"note": "Pick me"})
        self.store.accept_application(
            self.station, rid, {"email": self.contrib["email"]})
        self.store.submit_delivery(self.contrib, rid, {"note": "v1"})
        st, _ = self.store.post_note(
            self.contrib, rid, {"text": "Which pier works best?"})
        self.assertEqual(st, 201)
        st, _ = self.store.post_note(
            self.station, rid, {"text": "Pier 9 is fine."})
        self.assertEqual(st, 201)
        st, _ = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Night market",
            "brief": "After dark at the fish market."})
        self.assertEqual(st, 201)
        st, body = self.store.notifications_for(self.station)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["unread"], 4)
        kinds = sorted(i["kind"] for i in body["items"])
        self.assertEqual(kinds, ["application", "delivery", "note", "pitch"])
        self.assertEqual(body["items"][0]["kind"], "pitch")
        self.assertTrue(all(i["link"] == "/requests" for i in body["items"]))
        st, _ = self.store.mark_notifications_seen(self.station)
        self.assertEqual(st, 200)
        st, body = self.store.notifications_for(self.station)
        self.assertEqual(body["unread"], 0)
        self.assertEqual(len(body["items"]), 4)
        self.store.post_note(self.contrib, rid, {"text": "One more thing"})
        st, body = self.store.notifications_for(self.station)
        self.assertEqual(body["unread"], 1)
        st, mine = self.store.notifications_for(self.contrib)
        self.assertEqual(st, 200, mine)
        st, _ = self.store.mark_notifications_seen(self.partner)
        self.assertEqual(st, 200)

    def test_contributor_notifications(self):
        mine = self.contrib.get("roles") or ["Reporter"]
        st, rq = self.store.create_request(self.station, {
            "title": "Harbor story", "brief": "Two minutes on the harbor.",
            "roles": mine[:1]})
        self.assertEqual(st, 201, rq)
        st, _ = self.store.create_request(self.station, {
            "title": "Submarine story", "brief": "Not for our roles.",
            "roles": ["Submarine commander"]})
        self.assertEqual(st, 201)
        rid = rq["id"]
        st, body = self.store.notifications_for(self.contrib)
        self.assertEqual(st, 200, body)
        briefs = [i for i in body["items"] if i["kind"] == "brief"]
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["link"], "/contributor")
        self.assertEqual(body["unread"], 1)
        self.store.apply(self.contrib, rid, {"note": "Pick me"})
        st, body = self.store.notifications_for(self.contrib)
        self.assertFalse([i for i in body["items"]
                            if i["kind"] == "brief"])
        self.store.accept_application(
            self.station, rid, {"email": self.contrib["email"]})
        st, body = self.store.notifications_for(self.contrib)
        kinds = [i["kind"] for i in body["items"]]
        self.assertIn("assigned", kinds)
        self.assertNotIn("delivery", kinds)
        self.store.submit_delivery(self.contrib, rid, {"note": "v1"})
        self.store.post_note(self.station, rid, {"text": "Looking good"})
        st, body = self.store.notifications_for(self.contrib)
        notes = [i for i in body["items"] if i["kind"] == "note"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["link"], "/assignments")
        self.store.request_changes(
            self.station, rid, {"feedback": "Trim the intro"})
        st, body = self.store.notifications_for(self.contrib)
        changes = [i for i in body["items"] if i["kind"] == "changes"]
        self.assertEqual(len(changes), 1)
        self.assertIn("Trim the intro", changes[0]["detail"])
        self.assertTrue(changes[0]["at"])
        self.store.submit_delivery(self.contrib, rid, {"note": "v2"})
        self.store.approve_delivery(self.station, rid)
        st, body = self.store.notifications_for(self.contrib)
        self.assertIn("approved",
                      [i["kind"] for i in body["items"]])
        st, p1 = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Night market",
            "brief": "After dark at the fish market."})
        self.assertEqual(st, 201)
        st, p2 = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Dawn patrol",
            "brief": "Sunrise over the bay."})
        self.assertEqual(st, 201)
        self.store.decide_pitch(self.station, p1["id"], True)
        self.store.decide_pitch(self.station, p2["id"], False)
        st, body = self.store.notifications_for(self.contrib)
        pitches = sorted(i["title"] for i in body["items"]
                         if i["kind"] == "pitch")
        self.assertEqual(pitches, ["Your pitch “Dawn patrol” was declined",
                                   "Your pitch “Night market” was accepted"])
        self.store.mark_notifications_seen(self.contrib)
        st, body = self.store.notifications_for(self.contrib)
        self.assertEqual(body["unread"], 0)
        self.assertTrue(body["items"])
        st, _ = self.store.signup({
            "email": "other@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        st, body = self.store.notifications_for(
            self.store.find_user("other@creator.tv"))
        self.assertEqual(body["unread"], 0)

    def test_partner_notifications(self):
        st, feed = self.store.create_feed(self.partner, {
            "name": "Harbor cam", "location": "Pier 9", "rate": 120})
        self.assertEqual(st, 201, feed)
        st, _ = self.store.feed_status(
            self.partner, feed["id"], {"status": "live"})
        self.assertEqual(st, 200)
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Quick clip",
            "description": "A finished package for licensing.",
            "market": "Miami, FL", "price": 50})
        self.assertEqual(st, 201, item)
        future = (datetime.now(timezone.utc) + timedelta(days=2)
                  ).strftime("%Y-%m-%d")
        st, bk = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": future, "from": "10:00",
            "until": "12:00"})
        self.assertEqual(st, 201, bk)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, _ = self.store.create_booking(self.station, {
            "contentId": item["id"], "date": today})
        self.assertEqual(st, 201)
        st, body = self.store.notifications_for(self.partner)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["unread"], 1)
        self.assertEqual(len(body["items"]), 1)
        note = body["items"][0]
        self.assertEqual(note["kind"], "booking")
        self.assertIn("Metroline", note["title"])
        self.assertIn("EST", note["detail"])
        self.assertEqual(note["link"], "/partner#usage")
        st, _ = self.store.cancel_booking(self.station, bk["id"])
        self.assertEqual(st, 200)
        st, body = self.store.notifications_for(self.partner)
        self.assertEqual(body["items"], [])

    def test_delivery_files_private_to_project(self):
        rid = self._assigned_request()
        st, sess = self.store.asset_init(
            self.contrib, {"name": "private.mp4", "size": 3})
        self.assertEqual(st, 201, sess)
        self.store.asset_chunk(self.contrib, sess["id"],
                               {"index": 0, "data": "YWJj"})
        self.store.asset_complete(self.contrib, sess["id"])
        aid = sess["id"]
        st, _ = self.store.signup({
            "email": "other@station.tv", "password": "password-1",
            "name": "Other", "role": "station", "org": "Other TV"})
        self.assertEqual(st, 201)
        other_station = self.store.find_user("other@station.tv")
        st, _ = self.store.submit_delivery(
            self.contrib, rid, {"assetId": aid, "note": "First cut"})
        self.assertEqual(st, 200)
        _, denied = self.store.asset_access(self.contrib, aid)
        self.assertIsNone(denied)
        _, denied = self.store.asset_access(self.station, aid)
        self.assertIsNone(denied)
        _, denied = self.store.asset_access(other_station, aid)
        self.assertEqual(denied, 403)
        _, denied = self.store.asset_access(self.partner, aid)
        self.assertEqual(denied, 403)
        # publishing the same file to the marketplace opens the preview
        st, _ = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Public cut",
            "description": "The same file, now listed for everyone.",
            "market": "Miami, FL", "price": 50, "assetId": aid})
        self.assertEqual(st, 201)
        _, denied = self.store.asset_access(other_station, aid)
        self.assertIsNone(denied)

    def test_assignment_notes(self):
        rid = self._assigned_request()
        st, note = self.store.post_note(
            self.station, rid, {"text": "Hold the shot", "tc": "01:12"})
        self.assertEqual(st, 201, note)
        st, _ = self.store.post_note(self.station, rid, {"text": "Bad tc",
                                                         "tc": "nope"})
        self.assertEqual(st, 400)
        st, body = self.store.resolve_note(self.contrib, rid, note["id"])
        self.assertEqual(st, 200, body)
        self.assertTrue(body["resolved"])
        st, body = self.store.resolve_note(self.contrib, rid, note["id"])
        self.assertFalse(body["resolved"])
        outsider = dict(self.contrib, email="stranger@x.tv")
        st, _ = self.store.post_note(outsider, rid, {"text": "hi"})
        self.assertEqual(st, 404)

    def test_direct_assign(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Direct job", "brief": "No applications needed."})
        st, _ = self.store.assign_direct(
            self.station, rq["id"], {"email": self.contrib["email"]})
        self.assertEqual(st, 200)
        req = [r for r in self.store.list_requests(self.station)
               if r["id"] == rq["id"]][0]
        self.assertEqual(req["status"], "assigned")
        st, _ = self.store.assign_direct(
            self.station, rq["id"], {"email": "ghost@x.tv"})
        self.assertEqual(st, 409)
        st, rq2 = self.store.create_request(self.station, {
            "title": "Direct job 2", "brief": "Another one."})
        st, _ = self.store.assign_direct(
            self.station, rq2["id"], {"email": self.station["email"]})
        self.assertEqual(st, 404)

    def test_directory_roles(self):
        st, rows = self.store.directory(self.station)
        self.assertEqual(st, 200)
        self.assertTrue(any(r["email"] == self.contrib["email"] for r in rows))
        st, rows = self.store.directory(self.contrib)
        self.assertTrue(any(r["email"] == self.station["email"] for r in rows))
        st, _ = self.store.directory(self.partner)
        self.assertEqual(st, 403)

    def test_pitch_flow(self):
        st, pitch = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Harbor series",
            "brief": "Three parts on the harbor.", "budget": "$900"})
        self.assertEqual(st, 201, pitch)
        st, incoming = self.store.list_pitches(self.station)
        self.assertTrue(any(p["id"] == pitch["id"] for p in incoming))
        st, body = self.store.decide_pitch(self.station, pitch["id"], True)
        self.assertEqual(st, 200, body)
        mine = [b for b in self.store.list_requests(self.contrib)
                if b.get("mine")]
        self.assertTrue(any(b["type"] == "Commissioned pitch" for b in mine))
        st, pitch2 = self.store.create_pitch(self.contrib, {
            "station": self.station["email"], "title": "Nope",
            "brief": "Declined pitch."})
        st, body = self.store.decide_pitch(self.station, pitch2["id"], False)
        self.assertEqual(body["status"], "declined")
        st, _ = self.store.decide_pitch(self.station, pitch2["id"], True)
        self.assertEqual(st, 409)

    def test_contributor_sees_own_assignments(self):
        rid = self._assigned_request()
        mine = [b for b in self.store.list_requests(self.contrib)
                if b.get("mine")]
        self.assertTrue(any(b["id"] == rid for b in mine))

    def test_station_ifb_maps_to_irl_sales(self):
        st, body = self.store.put_ifb(
            self.station, {"url": "https://ifb.example.com/studio.mp3"})
        self.assertEqual(st, 200, body)
        st, body = self.store.get_ifb(self.station)
        self.assertEqual(body["url"], "https://ifb.example.com/studio.mp3")
        st, _ = self.store.put_ifb(self.station, {"url": "srt://nope:9000"})
        self.assertEqual(st, 400)
        st, _ = self.store.put_ifb(self.contrib, {"url": "https://x/y.mp3"})
        self.assertEqual(st, 403)
        st, _ = self.store.get_ifb(self.contrib)
        self.assertEqual(st, 403)
        st, _ = self.store.put_ifb(self.partner, {"url": "https://x/y.mp3"})
        self.assertEqual(st, 403)
        st, irl = self.store.publish_content(self.contrib, {
            "kind": "irl", "title": "Street live",
            "description": "Live from downtown with two-way talkback.",
            "market": "Miami, FL", "price": 180})
        self.assertEqual(st, 201, irl)
        st, pkg = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Quick clip",
            "description": "A finished package for licensing.",
            "market": "Miami, FL", "price": 50})
        self.assertEqual(st, 201, pkg)
        now = datetime.now(timezone.utc)
        fmt = "%H:%M"
        today = now.strftime("%Y-%m-%d")
        st, _ = self.store.create_booking(self.station, {
            "contentId": irl["id"], "date": today,
            "from": (now - timedelta(minutes=5)).strftime(fmt),
            "until": (now + timedelta(minutes=55)).strftime(fmt)})
        self.assertEqual(st, 201)
        st, _ = self.store.create_booking(self.station, {
            "contentId": pkg["id"], "date": today})
        self.assertEqual(st, 201)
        st, sales = self.store.sales_for(self.contrib)
        self.assertEqual(st, 200, sales)
        live = [r for r in sales["rows"] if r["kind"] == "irl"][0]
        self.assertEqual(live["stationIfb"],
                         "https://ifb.example.com/studio.mp3")
        self.assertTrue(live["liveNow"])
        self.assertTrue(live["from"] and live["until"])
        fixed = [r for r in sales["rows"] if r["kind"] == "package"][0]
        self.assertEqual(fixed["stationIfb"], "")
        st, _ = self.store.signup({
            "email": "other@station.tv", "password": "password-1",
            "name": "Other", "role": "station", "org": "Other TV"})
        other = self.store.find_user("other@station.tv")
        st, _ = self.store.create_booking(other, {
            "contentId": irl["id"], "date": today,
            "from": (now - timedelta(minutes=5)).strftime(fmt),
            "until": (now + timedelta(minutes=55)).strftime(fmt)})
        self.assertEqual(st, 201)
        st, sales = self.store.sales_for(self.contrib)
        quiet = [r for r in sales["rows"]
                 if r["station"] == "Other TV"][0]
        self.assertEqual(quiet["stationIfb"], "")
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "station.html"),
                  encoding="utf-8") as f:
            self.assertIn('id="ifbUrl"', f.read())
        with open(os.path.join(public, "sales.html"),
                  encoding="utf-8") as f:
            self.assertIn("stationIfb", f.read())

    def test_sales_from_own_listings(self):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Seller clip",
            "description": "A package that actually sells.",
            "market": "Miami, FL", "price": 200})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.store.create_booking(self.station, {
            "contentId": item["id"], "date": today})
        st, sales = self.store.sales_for(self.contrib)
        self.assertEqual(st, 200)
        self.assertEqual(sales["count"], 1)
        self.assertEqual(sales["gross"], 200)
        st, _ = self.store.sales_for(self.station)
        self.assertEqual(st, 403)

    def test_profile_update(self):
        st, view = self.store.update_profile(self.contrib, {
            "based": "Austin, TX", "specialties": "Docs",
            "roles": ["Producer"]})
        self.assertEqual(st, 200, view)
        self.assertEqual(view["based"], "Austin, TX")
        self.assertEqual(view["roles"], ["Producer"])
        me = self.store.find_user(self.contrib["email"])
        self.assertEqual(me["specialties"], "Docs")

    def test_asset_list_with_listings(self):
        st, sess = self.store.asset_init(
            self.contrib, {"name": "lib.mp4", "size": 3})
        self.store.asset_chunk(self.contrib, sess["id"],
                               {"index": 0, "data": "YWJj"})
        self.store.asset_complete(self.contrib, sess["id"])
        self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Linked clip",
            "description": "Uses the uploaded library file.",
            "market": "Miami, FL", "price": 50, "assetId": sess["id"]})
        st, rows = self.store.list_assets(self.contrib)
        self.assertEqual(st, 200)
        row = [r for r in rows if r["id"] == sess["id"]][0]
        self.assertTrue(row["complete"])
        self.assertIn("Linked clip", row["listings"])
        st, _ = self.store.list_assets(self.station)
        self.assertEqual(st, 403)

    def test_request_apply_once(self):
        st, rq = self.store.create_request(self.station, {
            "title": "Need vox pops",
            "brief": "Thirty seconds of street interviews."})
        self.assertEqual(st, 201, rq)
        briefs = self.store.list_requests(self.contrib)
        self.assertTrue(any(b["id"] == rq["id"] for b in briefs))
        st, _ = self.store.apply(
            self.contrib, rq["id"], {"note": "I can do it"})
        self.assertEqual(st, 200)
        st, _ = self.store.apply(self.contrib, rq["id"], {})
        self.assertEqual(st, 409)

    def test_contributor_can_publish_raw_footage(self):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "raw", "title": "Downtown b-roll",
            "description": "Twenty minutes of unedited downtown b-roll.",
            "market": "Chicago, IL", "price": 75})
        self.assertEqual(st, 201, item)
        self.assertEqual(item["unit"], "license")
        self.assertEqual(item["kindLabel"], "Raw footage")
        view = [c for c in self.store.catalog() if c["id"] == item["id"]][0]
        self.assertFalse(view.get("stream"))

    def test_contributor_can_publish_package(self):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Harbor at dawn",
            "description": "Two minute scenic package with nat sound.",
            "market": "New York, NY", "price": 120})
        self.assertEqual(st, 201, item)
        self.assertTrue(any(c["id"] == item["id"]
                            for c in self.store.catalog()))

    def test_preview_streams_by_kind(self):
        by_kind = {}
        for kind, title in [("irl", "Street live"),
                            ("package", "Finished piece"),
                            ("show", "Weekly show"),
                            ("podcast", "Talk episode")]:
            st, item = self.store.publish_content(self.contrib, {
                "kind": kind, "title": title,
                "description": "A watchable demo listing for previews.",
                "market": "Miami, FL", "price": 150})
            self.assertEqual(st, 201, item)
            found = [c for c in self.store.catalog() if c["id"] == item["id"]]
            by_kind[kind] = found[0]
        for kind, view in by_kind.items():
            self.assertEqual(view["stream"], relay.DEFAULT_STREAMS[kind], kind)
        self.assertFalse(by_kind["irl"].get("streamFallback"))
        self.assertTrue(by_kind["package"].get("streamFallback"))
        st, feed = self.store.create_feed(self.partner, {
            "name": "Quay", "location": "Pier", "rate": 25})
        tower = [c for c in self.store.catalog() if c["id"] == feed["id"]]
        self.assertEqual(tower[0]["stream"],
                         relay.DEFAULT_STREAMS["tower"])
        self.assertEqual(tower[0]["streamFallback"],
                         relay.FALLBACK_STREAMS["tower"])
        st, custom = self.store.publish_content(self.contrib, {
            "kind": "show", "title": "Custom show",
            "description": "A show with its own preview file.",
            "market": "Miami, FL", "price": 150,
            "stream": "https://example.com/preview.mp4",
            "streamFallback": "https://example.com/backup.mp4"})
        self.assertEqual(st, 201, custom)
        self.assertEqual(custom["stream"], "https://example.com/preview.mp4")
        self.assertEqual(custom["streamFallback"],
                         "https://example.com/backup.mp4")
        st, body = self.store.publish_content(self.contrib, {
            "kind": "irl", "title": "Bad live",
            "description": "Live with a bad stream URL.",
            "market": "Miami, FL", "price": 150,
            "stream": "ftp://example.com/x.m3u8"})
        self.assertEqual(st, 400, body)

    def test_event_has_preview_stream(self):
        self.store.content.append(
            {"id": "evt-test", "kind": "event", "title": "Live forum"})
        evt = [c for c in self.store.catalog() if c["id"] == "evt-test"][0]
        self.assertEqual(evt["stream"], relay.DEFAULT_STREAMS["event"])
        self.assertEqual(evt["streamFallback"],
                         relay.FALLBACK_STREAMS["event"])

    def test_saved_roundtrip(self):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Save me",
            "description": "A package worth saving for later use.",
            "market": "Austin, TX", "price": 50})
        st, saved = self.store.add_saved(
            self.station, {"contentId": item["id"]})
        self.assertEqual(st, 200, saved)
        self.assertIn(item["id"], saved["saved"])
        self.assertIn(item["id"], self.store.get_saved(self.station)["saved"])
        st, saved = self.store.remove_saved(self.station, item["id"])
        self.assertNotIn(item["id"], saved["saved"])

    def test_partner_settings_persist_server_side(self):
        st, s = self.store.put_settings(self.partner, {
            "bank": "First Bank x1234", "notif": "Muted"})
        self.assertEqual(st, 200, s)
        st, s2 = self.store.get_settings(self.partner)
        self.assertEqual(s2["bank"], "First Bank x1234")
        st, _ = self.store.get_settings(self.station)
        self.assertEqual(st, 403)

    def test_asset_upload_roundtrip(self):
        st, sess = self.store.asset_init(
            self.contrib, {"name": "clip.mp4", "size": 9})
        self.assertEqual(st, 201, sess)
        aid = sess["id"]
        st, _ = self.store.asset_chunk(
            self.contrib, aid, {"index": 1, "data": "eA=="})
        self.assertEqual(st, 409)
        st, _ = self.store.asset_chunk(
            self.contrib, aid, {"index": 0, "data": "MDEzNDU2"})
        self.assertEqual(st, 200)
        st, _ = self.store.asset_chunk(
            self.contrib, aid, {"index": 1, "data": "Nzg5"})
        self.assertEqual(st, 200)
        st, done = self.store.asset_complete(self.contrib, aid)
        self.assertEqual(st, 200, done)
        asset = self.store.asset_for(aid)
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp.name, "assets", asset["stored"])))
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Real clip",
            "description": "A package with its own uploaded file.",
            "market": "Miami, FL", "price": 99, "assetId": aid})
        self.assertEqual(st, 201, item)
        view = [c for c in self.store.catalog() if c["id"] == item["id"]][0]
        self.assertEqual(view["stream"], "/assets/" + aid)
        self.assertEqual(view["assetName"], "clip.mp4")
        _, denied = self.store.asset_access(self.contrib, aid)
        self.assertIsNone(denied)
        _, denied = self.store.asset_access(self.station, aid)
        self.assertIsNone(denied)
        _, denied = self.store.asset_access(self.partner, aid)
        self.assertEqual(denied, 403)
        _, denied = self.store.asset_access(self.station, "as_missing")
        self.assertEqual(denied, 404)

    def test_asset_upload_guards(self):
        st, _ = self.store.asset_init(
            self.station, {"name": "clip.mp4", "size": 10})
        self.assertEqual(st, 403)
        st, _ = self.store.asset_init(
            self.contrib, {"name": "clip.txt", "size": 10})
        self.assertEqual(st, 400)
        st, sess = self.store.asset_init(
            self.contrib, {"name": "half.mp4", "size": 10})
        st, body = self.store.asset_complete(self.contrib, sess["id"])
        self.assertEqual(st, 400, body)
        st, body = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Unfinished",
            "description": "References an incomplete upload.",
            "market": "Miami, FL", "price": 10, "assetId": sess["id"]})
        self.assertEqual(st, 400, body)

    def test_windows_use_est(self):
        start, end = relay.window_bounds("2026-06-01", "18:00", "20:00")
        self.assertEqual(start.utcoffset(), timedelta(hours=-5))
        self.assertEqual((start.hour, end.hour), (18, 20))
        booking = {"status": "upcoming", "date": "2026-06-01",
                   "from": "18:00", "until": "20:00"}
        self.assertTrue(relay.Store.booking_live(
            booking, now=datetime(2026, 6, 1, 23, 30,
                                  tzinfo=timezone.utc)))
        self.assertFalse(relay.Store.booking_live(
            booking, now=datetime(2026, 6, 1, 19, 0,
                                  tzinfo=timezone.utc)))
        feed = self._live_feed(50)
        st, bk = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": "2026-06-01", "from": "18:00",
            "until": "20:00"})
        self.assertEqual(st, 201, bk)
        text = self.store.invoice_text(self.station, bk["id"])
        self.assertIn("18:00-20:00 EST", text)

    def test_range_parsing(self):
        self.assertEqual(relay.parse_range(None, 100), (0, 99, False))
        self.assertEqual(relay.parse_range("bytes=0-9", 100), (0, 9, True))
        self.assertEqual(relay.parse_range("bytes=50-", 100), (50, 99, True))
        self.assertEqual(relay.parse_range("bytes=0-9999", 100), (0, 99, True))
        self.assertEqual(relay.parse_range("bytes=99-50", 100), (99, 50, False))
        self.assertEqual(relay.parse_range("items=0-10", 100), (0, 99, False))

    def test_upload_validation(self):
        st, up = self.store.upload(self.station, {
            "name": "brief.pdf",
            "data": base64.b64encode(b"%PDF-1.4 fake").decode()})
        self.assertEqual(st, 201, up)
        path, _ = self.store.upload_path(up["id"])
        self.assertTrue(path and os.path.isfile(path))
        st, _ = self.store.upload(self.station, {
            "name": "evil.exe", "data": base64.b64encode(b"MZ").decode()})
        self.assertEqual(st, 400)

    def test_invoice_and_cancel(self):
        feed = self._live_feed(30)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, bk = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": today, "from": "09:00",
            "until": "10:00", "cardLast4": "4242"})
        text = self.store.invoice_text(self.station, bk["id"])
        self.assertIn(bk["invoice"], text)
        self.assertIn("4242", text)
        st, _ = self.store.cancel_booking(self.station, bk["id"])
        self.assertEqual(st, 200)
        st, _ = self.store.cancel_booking(self.station, bk["id"])
        self.assertEqual(st, 409)

    def test_expenses_for_station(self):
        feed = self._live_feed(50)
        future = (datetime.now(timezone.utc) + timedelta(days=2)
                  ).strftime("%Y-%m-%d")
        st, hourly = self.store.create_booking(self.station, {
            "contentId": feed["id"], "date": future, "from": "10:00",
            "until": "12:00"})
        self.assertEqual(st, 201, hourly)
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Expensed clip",
            "description": "A package the station pays for.",
            "market": "Miami, FL", "price": 200})
        self.assertEqual(st, 201, item)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, fixed = self.store.create_booking(self.station, {
            "contentId": item["id"], "date": today})
        self.assertEqual(st, 201, fixed)
        st, exp = self.store.expenses_for(self.station)
        self.assertEqual(st, 200, exp)
        self.assertEqual(exp["count"], 2)
        self.assertEqual(exp["total"], 300)
        row = [r for r in exp["rows"]
               if r["bookingId"] == fixed["id"]][0]
        self.assertEqual(row["title"], "Expensed clip")
        self.assertEqual(row["total"], 200)
        self.assertTrue(row["invoice"].startswith("INV-"))
        st, _ = self.store.cancel_booking(self.station, hourly["id"])
        self.assertEqual(st, 200)
        st, exp = self.store.expenses_for(self.station)
        self.assertEqual(exp["count"], 1)
        self.assertEqual(exp["total"], 200)
        st, _ = self.store.expenses_for(self.contrib)
        self.assertEqual(st, 403)
        st, _ = self.store.expenses_for(self.partner)
        self.assertEqual(st, 403)

    def test_expenses_page_routed_to_stations(self):
        self.assertEqual(relay.ROUTES["/expenses"], "/expenses.html")
        self.assertEqual(relay.PAGE_ROLES["/expenses"], "station")
        page = os.path.join(os.path.dirname(relay.__file__), "public",
                             "expenses.html")
        with open(page, encoding="utf-8") as f:
            html = f.read()
        self.assertIn('/api/expenses', html)
        self.assertIn('guard("station")', html)

    def test_requests_page_split_from_form(self):
        self.assertEqual(relay.ROUTES["/requests"], "/requests.html")
        self.assertEqual(relay.PAGE_ROLES["/requests"], "station")
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "requests.html"),
                  encoding="utf-8") as f:
            listing = f.read()
        with open(os.path.join(public, "request.html"),
                  encoding="utf-8") as f:
            form = f.read()
        self.assertIn('guard("station")', listing)
        self.assertIn("sentList", listing)
        self.assertIn("pitchList", listing)
        self.assertNotIn("sentList", form)
        self.assertNotIn("pitchList", form)
        self.assertIn("submitBtn", form)
        self.assertNotIn("submitBtn", listing)

    def test_pitches_page_split_from_assignments(self):
        self.assertEqual(relay.ROUTES["/pitches"], "/pitches.html")
        self.assertEqual(relay.PAGE_ROLES["/pitches"], "contributor")
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "pitches.html"),
                  encoding="utf-8") as f:
            pitches = f.read()
        with open(os.path.join(public, "assignments.html"),
                  encoding="utf-8") as f:
            assignments = f.read()
        self.assertIn('guard("contributor")', pitches)
        self.assertIn("ptSend", pitches)
        self.assertIn("ptStation", pitches)
        self.assertIn("pitchList", pitches)
        self.assertIn("/api/pitches", pitches)
        self.assertIn('href="/pitches"', assignments)
        self.assertNotIn("pitchPanel", assignments)
        self.assertNotIn("ptSend", assignments)
        self.assertNotIn("ptStation", assignments)
        self.assertNotIn("pitchList", assignments)
        self.assertIn("rooms", assignments)
        self.assertIn("modbox", assignments)

    def test_listings_page_split_from_publish(self):
        self.assertEqual(relay.ROUTES["/listings"], "/listings.html")
        self.assertEqual(relay.PAGE_ROLES["/listings"], "contributor")
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "listings.html"),
                  encoding="utf-8") as f:
            listings = f.read()
        with open(os.path.join(public, "publish.html"),
                  encoding="utf-8") as f:
            publish = f.read()
        self.assertIn('guard("contributor")', listings)
        self.assertIn("myList", listings)
        self.assertIn("/api/content", listings)
        self.assertIn("/api/listings", listings)
        self.assertIn("modPillFor", listings)
        self.assertIn("Unpublish", listings)
        self.assertIn("/unpublish", listings)
        self.assertIn('href="/listings"', publish)
        self.assertNotIn("myList", publish)
        self.assertNotIn("loadMine", publish)
        self.assertNotIn("Your marketplace listings", publish)
        self.assertIn("pubBtn", publish)
        self.assertIn("pTitle", publish)

    def _publish(self, asset_id=""):
        st, item = self.store.publish_content(self.contrib, {
            "kind": "package", "title": "Harbor at dawn",
            "description": "Aerials and interviews.", "market": "Boston, MA",
            "price": 120, "assetId": asset_id})
        self.assertEqual(st, 201, item)
        return item

    def test_unpublish_listing(self):
        item = self._publish()
        cid = item["id"]
        self.assertTrue(any(c["id"] == cid for c in self.store.catalog()))
        self.assertIsNotNone(self.store.content_item(cid))
        st, _ = self.store.set_content_visibility(self.station, cid, False)
        self.assertEqual(st, 403)
        st, _ = self.store.signup({
            "email": "unpub-other@creator.tv", "password": "password-1",
            "name": "Other", "role": "contributor"})
        self.assertEqual(st, 201)
        other = self.store.find_user("unpub-other@creator.tv")
        st, _ = self.store.set_content_visibility(other, cid, False)
        self.assertEqual(st, 404)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st, bk = self.store.create_booking(self.station, {
            "contentId": cid, "date": today, "qty": 1})
        self.assertEqual(st, 201, bk)
        st, body = self.store.set_content_visibility(
            self.contrib, cid, False)
        self.assertEqual(st, 200, body)
        self.assertTrue(body["unpublished"])
        self.assertFalse(any(c["id"] == cid for c in self.store.catalog()))
        self.assertIsNone(self.store.content_item(cid))
        st, body = self.store.create_booking(self.station, {
            "contentId": cid, "date": today, "qty": 1})
        self.assertEqual(st, 404, body)
        self.assertIn(item["title"],
                      self.store.invoice_text(self.station, bk["id"]))
        st, exp = self.store.expenses_for(self.station)
        self.assertEqual(
            [r for r in exp["rows"] if r["bookingId"] == bk["id"]][0]["title"],
            item["title"])
        st, rows = self.store.own_listings(self.contrib)
        self.assertEqual(st, 200)
        mine = [c for c in rows if c["id"] == cid]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["unpublished"])
        st, _ = self.store.own_listings(self.station)
        self.assertEqual(st, 403)
        st, body = self.store.set_content_visibility(
            self.contrib, cid, True)
        self.assertEqual(st, 200, body)
        self.assertFalse(body["unpublished"])
        self.assertTrue(any(c["id"] == cid for c in self.store.catalog()))

    def test_unpublished_asset_hidden_from_stations(self):
        aid = self._upload_file()
        item = self._publish(aid)
        asset, denied = self.store.asset_access(self.station, aid)
        self.assertIsNotNone(asset)
        self.assertIsNone(denied)
        st, _ = self.store.set_content_visibility(
            self.contrib, item["id"], False)
        self.assertEqual(st, 200)
        asset, denied = self.store.asset_access(self.station, aid)
        self.assertIsNone(asset)
        self.assertEqual(denied, 403)
        asset, denied = self.store.asset_access(self.contrib, aid)
        self.assertIsNotNone(asset)
        self.assertIsNone(denied)

    def test_nav_icons_cover_sidebar_links(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        with open(os.path.join(public, "js", "auth.js"),
                  encoding="utf-8") as f:
            auth = f.read()
        block = re.search(r"NAV_ICONS = \{(.*?)\};", auth, re.S)
        self.assertIsNotNone(block)
        keys = set(re.findall(r'"([^"]+)":', block.group(1)))
        self.assertTrue(keys)

        class NavLinks(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.hrefs = []

            def handle_starttag(self, tag, attrs):
                if tag == "nav":
                    self.depth += 1
                if tag == "a" and self.depth:
                    href = dict(attrs).get("href")
                    if href:
                        self.hrefs.append(href)

            def handle_endtag(self, tag):
                if tag == "nav":
                    self.depth -= 1

        checked = 0
        for name in sorted(os.listdir(public)):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(public, name), encoding="utf-8") as f:
                parser = NavLinks()
                parser.feed(f.read())
            for href in parser.hrefs:
                self.assertTrue(
                    href in keys or href.split("?")[0] in keys,
                    "%s: %s has no nav icon" % (name, href))
                checked += 1
        self.assertTrue(checked)

    def test_platform_uses_real_icons(self):
        public = os.path.join(os.path.dirname(relay.__file__), "public")
        glyphs = ("◉◍◷▭⦿⌖⌕▶✕✓⚠⚑★⌄⬆＋🖇⚡⤴↗⭳⇪∿◫▦▤↓○●↑")
        targets = [os.path.join(public, n) for n in os.listdir(public)
                   if n.endswith(".html")]
        targets += [os.path.join(public, "js", "auth.js"),
                    os.path.join(public, "css", "styles.css")]
        offenders = []
        for path in sorted(targets):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if path.endswith(".html"):
                text = re.sub(r"<nav.*?</nav>", "", text, flags=re.S)
            found = sorted(set(text) & set(glyphs))
            if found:
                offenders.append("%s: %s" % (
                    os.path.basename(path), "".join(found)))
        self.assertEqual(offenders, [])

    def test_password_change(self):
        st, body = self.store.set_password(
            self.station, {"current": "password-1", "new": "password-2"})
        self.assertEqual(st, 200, body)
        st, _ = self.store.login(
            {"email": self.station["email"], "password": "password-2"})
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
