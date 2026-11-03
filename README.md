# Relay Network — stations-first marketplace

Cloud production network: stations discover and book live feeds, tower cams,
packages and live event production, or commission original coverage from
verified contributors. Partners (business owners) provide tower-cam feeds.

## Run it

Requires Python 3 (standard library only, no installs).

```powershell
cd C:\Users\ddeus\Project
python server.py
```

Then open http://127.0.0.1:4173/ in your browser.

To use another port: `set PORT=4200 && python server.py`

## Pages

- `/` — role-based signup (Station / Contributor / Partner)
- `/login` — sign in
- `/discover` — station content marketplace (stations only)
- `/booking?id=<id>` — listing detail + booking (stations only)
- `/request` — commission original content with the coverage form (stations only)
- `/requests` — track sent requests and incoming pitches (stations only)
- `/bookings` — station bookings & licensed library
- `/expenses` — station expense report (paid bookings, CSV export)
- `/station` — station profile: studio IFB feed + password
- `/contributor` — contributor workspace: matched briefs and applications
- `/assignments` — contributor assignments with delivery submit
- `/pitches` — pitch stories to stations directly
- `/publish` — publish finished work with file upload (contributors only)
- `/listings` — contributor's marketplace listings with edit and unpublish
- `/media` — contributor media library (folders: Uploads, Raw footage, Finals, plus custom folders; every file moved between them)
- `/sales` — contributor sales from marketplace listings
- `/profile` — contributor profile and password
- `/partner` — partner workspace: tower-cam feeds, usage & earnings

## Login & permissions

Passwords are PBKDF2-hashed in `data/users.json`; sessions are HttpOnly
cookies backed by `data/sessions.json` (30-day expiry, survives restarts).

| Who | Pages | API |
| --- | --- | --- |
| Station | discover, request, requests, booking, bookings, expenses, station | read catalog, saved, CRUD own bookings/requests, expense report, IFB feed |
| Contributor | contributor, assignments, pitches, publish, listings, media, sales, profile | briefs, apply, assignments, pitches, publish, own files, sales, profile |
| Partner | partner dashboard | manage own feeds, usage/earnings, settings |

Wrong-role page visits redirect to the role's home with a notice;
wrong-role API calls return 403.

## API

- `POST /api/signup`, `POST /api/login`, `POST /api/logout`, `GET /api/me`,
  `POST /api/password` (change password)
- `GET /api/content`, `GET /api/content/<id>`, `POST /api/content`,
  `POST /api/content/<id>/unpublish|republish`, `GET /api/listings`
  (contributors publish; partner tower feeds appear automatically;
  unpublished listings leave the catalog but stay editable)
- `POST /api/search` (station natural-language + filtered
  marketplace search; see below)
- `GET /api/expenses` (station expense report, paid bookings only)
- `GET/POST /api/bookings`, `GET /api/bookings/<id>/invoice`,
  `GET /api/bookings/<id>/endpoints` (live window only),
  `POST /api/bookings/<id>/cancel`
- `GET/POST /api/requests`, `PUT /api/requests/<id>` (station edits
  own brief at any status: title, brief, budget, …),
  `POST /api/requests/<id>/apply`,
  `POST /api/requests/<id>/accept|submit|approve|changes`,
  `POST /api/requests/<id>/notes`,
  `POST /api/requests/<id>/notes/<nid>/resolve`
- `GET /api/assets` (own files), `GET /api/sales`, `PUT /api/me`
- `GET /api/directory`, `GET/POST /api/pitches`,
  `POST /api/pitches/<id>/accept|decline`, `POST /api/requests/<id>/assign`
- `GET /api/worldnews` (cached world-headline briefing from public
  RSS feeds with an AI digest; dashboard "Trending now" panel)
- `GET /api/notifications`, `POST /api/notifications/seen`
  (per-role activity feed for the bell icon: stations get
  applications/deliveries/notes/pitches, contributors get
  assignments/changes/approvals/notes/decisions/new briefs,
  partners get feed bookings)
- `GET/PUT /api/ifb` (station studio IFB audio URL, auto-mapped to
  IRL bookings on the contributor sales page)
- `GET/POST /api/feeds`, `POST /api/feeds/<id>/status` (partners)
- `GET /api/usage` (derived from real bookings, partners)
- `GET/POST /api/saved`, `DELETE /api/saved/<id>`
- `GET/PUT /api/settings` (partners)
- `POST /api/uploads`, `GET /uploads/<id>` (brief attachments, PDF/JPG/PNG ≤ 5 MB)
- `POST /api/assets`, `POST /api/assets/<id>/chunk`,
  `POST /api/assets/<id>/complete`, `GET /assets/<id>` (contributor
  video/audio, chunked, up to 2 GB; range requests supported),
  `GET /assets/<id>?download=1` (download for the commissioning
  station and the file owner only)
- `POST /api/assets/<id>/captions`, `GET /api/assets/<id>/captions`,
  `GET /api/assets/<id>/captions/cues`,
  `GET /assets/<id>/captions.vtt` (closed captions via Muse Voice
  Transcribe; see below)
- `POST /api/assets/<id>/intelligence`,
  `GET /api/assets/<id>/intelligence` (AI summary, tags and catalog
  description; see below)

Tower-cam, IRL, package, show, podcast and live-event listings carry a
`stream` URL (plus a `streamFallback` from a second host) and play an inline
preview on Discover (HLS via hls.js with native Safari fallback, MP4
natively; needs internet access). Demo previews are Blender Foundation open
films served from links — nothing is hosted in this repo.

Users, content, feeds, bookings and requests persist in `data/*.json`.
Billing is a local record (card last-4 only, validated in-browser) —
no external payment processor is connected.

## Closed captions

Contributors generate captions per file from the media library,
stations get the same button on the coverage-request review, and
any video also captions itself on first play: the player requests
transcription, shows a "generating" note over the video, and rolls
the lines in live-TV style the moment the transcript is ready
(later plays show captions instantly). Each request runs as a
background job: audio is extracted (WAV converts with the standard
library; mp4/webm/mov/mkv/mp3/m4a need `ffmpeg` on the server),
split into ≤10 minute chunks, transcribed with Muse Voice
Transcribe (`muse-voice-transcribe-1.0`, language auto-detected,
speaker names hidden from display), and saved as timed cues (plus
a WebVTT copy).
Every video player on the platform (marketplace previews,
assignment and delivery previews, media library) fetches the cues
on first play and renders up to three CEA-608-style roll-up rows —
monospace white-on-black, newest line at the bottom, revealed as
the video plays. Caption files inherit the same access rules as the
video itself, so private delivery files stay private. Set
`CAPTIONS_AUTO=0` to disable on-play generation (the media-library
button keeps working).

Setup: uncomment and fill in `META_API_KEY` in the `.env` file in
the project root (key from https://dev.meta.ai), then restart the
server — it prints whether captions are enabled on startup. Without
it, caption requests return 503 with an explanation. Transcription
bills to your Meta account ($0.18 per audio hour); audio longer than
3 hours per file is refused. A real `META_API_KEY` environment
variable wins over the `.env` file when both exist.

## Media intelligence

When a transcript finishes, the file is automatically enriched: the
transcript goes to Muse Spark (`muse-spark-1.3`), which returns a
short summary, topical tags and a marketplace-ready catalog
description. Results are station-only — they show as an AI summary
block on the listing page, and contributors (including the
uploader) get 403 from the intelligence endpoints. Enrichment reuses
the same `META_API_KEY` in `.env`, needs a finished transcript
first, and bills one small chat call per file. Set `INTEL_AUTO=0`
to disable auto-enrichment (the listing-page button keeps working).

## AI discovery

Stations can search the marketplace in plain language from the
Discover page ("flooding in the Caribbean from last month") with
optional recorded-date bounds. The request is parsed into keywords
plus market, kind and date filters by Muse Spark; if parsing fails
the search falls back to plain keyword matching, flagged in the
results note. Keywords match titles first, then AI tags, then
descriptions, markets and transcripts, and date filters only match
listings with a recorded date — contributors set it on the publish
form (optional) or later in My listings.

## World briefing

The dashboard's Trending now panel carries a world briefing: the
server fetches public RSS feeds (BBC World, NPR News and Al
Jazeera by default), takes the latest headlines, and has Muse Spark
write a short assignment-style digest plus a 1-2 sentence summary
under each headline. The briefing is cached for 30 minutes and
refreshed in the background, so dashboard loads stay instant and
cost one small chat call per refresh; without a `META_API_KEY` (or
if summarising fails) the feed's own descriptions show instead.
Set `WORLDNEWS_FEEDS` (`Name|https://...,…`) to use different feeds.

## Content moderation

Every video delivery is auto-scanned for obscene scenes when it is
submitted, pitch samples attached to coverage requests get the same
scan, and marketplace videos get it when they are published (or when
a listing's file is replaced): `ffmpeg` extracts one frame every 2
seconds, each frame gets a safe/unsafe verdict from Muse Spark
(`muse-spark-1.3`) on the Meta Model API, consecutive unsafe frames
merge into flagged ranges, and `ffmpeg` burns a blur over those
ranges into a review copy. The station's delivery preview and
download show the blurred copy; the contributor's original stays
untouched and private to the project. Flagged scenes appear as
click-to-seek timestamps in the review, and both sides get a bell
notification. Clean videos get a checked badge. The pipeline never
auto-rejects — the station always decides.

Marketplace listings carry their scan result as labels: each
Discover card shows content-check pills (checked-clean, blurred
scenes, AI-made, or a scanning note), the listing page plays the
preview in its main frame with the check labels and flagged
timestamps inline, and flagged previews play the blurred copy.
The listing itself always stays live and bookable.

Setup: moderation reuses the same `META_API_KEY` in `.env` (no second
key), and it needs `ffmpeg` on the server. Only stations can start
scans — from the "Scan for sensitive content" button on the coverage
request review or the listing page; contributors see the results on
their files but cannot run checks. Set `MOD_AUTO=0` to disable
auto-scanning on delivery submit and marketplace publish (the
station scan buttons keep working).
Each scan bills per analyzed frame to your Meta account — check
current Muse Spark vision pricing; videos longer than 3 hours are
refused. Note that sampled frames leave your server for the verdict
call. Scan cadence (1 frame / 2 s) and the minimum flagged range
(2 s) live at the top of `moderation.py`.

The same scan also judges whether the footage looks AI-generated:
each frame verdict includes a real/ai/unsure call, and the file gets
a graduated badge — AI GENERATED VIDEO when nearly every frame
looks synthetic, LIKELY AI-MADE and POSSIBLY AI-MADE for weaker
signals, nothing below a quarter of frames. Station notifications
fire only at majority-or-better. This is a heuristic signal from a
general vision model, not a dedicated synthetic-media detector —
treat it as a prompt to look closer, not proof.

## Tests

```powershell
python -m unittest discover -s tests -v
```
