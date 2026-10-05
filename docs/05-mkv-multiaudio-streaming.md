# MKV multi-audio + subtitles in the browser — planning + prototype build

> **2026-10-05 update:** hard constraint — no server-side encode/decode. All demuxing/decoding
> must happen client-side. Options A/B/C below (server-side ffmpeg) are kept for reference but
> are **superseded by Option D** (§4.4) given this constraint. Read §4.4 first.

> **2026-10-05 update 2:** a working prototype of Option D's client-side demux/player path has
> been built and verified against a real multi-track WebM file in real headless Chromium (not
> theorized — see §8 for what exists, what's verified, and what's explicitly out of scope).

Status: **prototype built and passing a real-browser regression suite** (see §8). The options
discussion below (§1-6) is the original planning pass; read §8 for what actually got built.

## 1. What exists today (verified against the code, 2026-10-05)

- No demuxing, probing or transcoding anywhere in the codebase (`ffmpeg`/`ffprobe`/`subprocess`
  grep across `streamgram/` returns nothing).
- The "player" (`streamgram/template/req.html`) is a plain `<video>`/`<audio>` tag wrapped in Plyr.js
  for UI chrome, `src` pointed directly at the raw streaming endpoint.
- The stream itself (`streamgram/utils/custom_dl.py` `ByteStreamer.yield_file`, `streamgram/server/stream_routes.py`
  `media_streamer`) is a pure byte-range passthrough from Telegram's DC to the HTTP response —
  no container awareness, no track awareness.
- `Content-Type` is whatever Telegram tagged the upload with, or guessed from the filename
  extension (`mimetypes.guess_type`).
- Range/seek support is real and solid (byte-offset reads against Telegram work for arbitrary
  offsets) — this turns out to matter for the plan below.

## 2. The actual problem

Browsers don't reliably support the **Matroska container** via `<video src>`, independent of the
codecs inside it. Chrome won't play `.mkv` at all even with fully-supported H.264/AAC inside;
Firefox has partial/inconsistent support; Safari none. So today, multi-audio MKV is broken twice
over: the container itself likely won't play, and there's no mechanism to pick a track out of a
raw file even if it did.

So this isn't "add a dropdown to the existing player" — it's "add a remuxing layer that gives the
browser a container it understands, containing only the track(s) picked."

## 3. Key simplification

Our server already serves arbitrary-byte-range HTTP at `/{id}?hash=...` — exactly what `ffmpeg`'s
built-in `http` protocol handler wants for seeking. So:

```
ffmpeg -i http://127.0.0.1:<PORT>/<id>?hash=<hash> -map 0:v:0 -map 0:a:<n> -c copy -f mp4 pipe:1
```

lets ffmpeg demux/seek the Telegram-backed file without any custom seekable-file plumbing — it
reuses the exact range logic already in production. ffmpeg sits *in front of* the existing
endpoint; the existing endpoint doesn't change.

(Internal-only: this self-HTTP call should go over loopback, and still carries the same `?hash=`
so no new auth surface is introduced — same hash check as every other request.)

## 4. Architecture options

### Option A — Probe + on-demand remux (stream-copy), pick-before-play
1. `ffprobe` the file once (via the self-HTTP trick above), cache the result (track list: video
   codec/res, each audio track's codec/language/title, each subtitle track's codec/language).
2. Watch page shows an audio/subtitle picker populated from the probe.
3. Chosen combo → `ffmpeg -map 0:v:0 -map 0:a:<chosen> -c copy -f <mp4|webm> pipe:1`, streamed to
   the browser as the video `src`.
4. Subtitles extracted separately and cheaply (`-map 0:s:<n> -f webvtt pipe:1`, text-only, no
   video re-encode) and attached as a `<track kind="subtitles">`.
5. No re-encode in the common case (codecs already web-safe) — just container repackaging:
   cheap-ish I/O + light CPU, not full transcoding.

### Option B — Same as A, but keep *all* audio tracks in the remux
Chrome's `HTMLMediaElement.audioTracks` API supports switching audio tracks **client-side, zero
extra requests**, for a multi-audio MP4 — ships today in Chromium. So: remux with every audio
track kept, let JS toggle `.enabled` per track where supported, fall back to "reload with `&a=N`"
for Firefox/Safari where that API is weaker/absent. Nicer UX, marginally bigger output, same CPU
cost as A.

### Option C — Full HLS/DASH packaging (separate audio renditions, segmented, hls.js)
How Jellyfin/Plex/Netflix actually do it — real segmenting, adaptive behavior, clean in-player
track switching. But needs real segment infrastructure, and segments would need re-fetching from
Telegram per segment per viewer unless a caching layer is added. Bigger lift; stretch goal, not v1.

**Superseded.** See §4.4 — server-side encode/decode was ruled out entirely, which rules
out A/B/C as written (all three run ffmpeg on the server). Kept above for comparison.

### 4.4 Option D — fully client-side demux, native decode, server untouched (current lead)

Constraint driving this: **no server-side encode/decode; the client does the heavy lifting.**
This turns out to fit the project's existing ethos well (server is a dumb byte pipe to Telegram;
it stays exactly that).

**Prior art / building blocks** (so this isn't invented from scratch):
- **WebTorrent's player stack** (`render-media`, `videostream`, `mediasource` npm packages) —
  solves the same shape of problem: demux a container client-side from a byte-range-addressable
  source (their torrent pieces; our case, the existing `/{id}?hash=...` endpoint) and feed MSE.
- **`matroska-subtitles`** (npm, same author as WebTorrent) — extracts SRT/ASS/PGS subtitle
  tracks from an MKV stream client-side.
- **JASSUB** — WASM `libass` port; renders *styled* ASS/SSA subtitles on a canvas overlay synced
  to `video.currentTime`. This is what Jellyfin's web client uses, entirely client-side.
- **WebCodecs API** — native in Chromium, shipping in recent Safari, partial in Firefox. JS hands
  the browser's own decoder encoded packets, gets raw frames back — more flexible than MSE's
  strict container-format rules, and a natural pairing with a demux-only (no decode) JS/WASM
  parser.
- **`mediabunny`** — newer library aimed squarely at "read Matroska/MP4/WebM in-browser, decode
  via WebCodecs, no server transcoding." Worth evaluating as a foundation before hand-rolling a
  demuxer.

**What changes server-side: plausibly nothing.** The existing byte-range endpoint already serves
exactly what a client-side demuxer needs. Even track *probing* can move client-side — a well-muxed
MKV's EBML header + Tracks element sit near the front of the file, so the client can read them
with one `Range` request and parse EBML itself; no server probe endpoint required.

**What changes client-side (the real work):**
1. JS/WASM Matroska demuxer reads the file via `fetch()` + `Range` headers (same URL as today),
   parses EBML/Tracks/Clusters/SimpleBlocks/Cues.
2. Extracted packets feed either `MediaSource`/`SourceBuffer` (repackaged into fMP4 boxes) or
   `WebCodecs` decoders directly (more modern, better fit for multi-track switching).
3. Audio track switching = choosing which packets reach the decoder — instant, no new network
   request, since the data is already flowing.
4. Subtitles: SRT/ASS parsed client-side (SRT/ASS → WebVTT trivially for plain rendering; ASS →
   JASSUB for styled fidelity). PGS (bitmap) still the hard case — same deferred recommendation
   as before, just client-side now instead of server-side.
5. **Seeking is easier than any server-remux option**: the client already controls its own `Range`
   requests, so seeking = compute byte offset from Cues/cluster timecodes, issue a new `Range`
   GET, resume parsing. No process restart, no "logical timeline" hack.

**Trade-off to be explicit about:** Matroska interleaves all tracks' data within each Cluster, so
a client-side demuxer **downloads every audio/subtitle track's bytes regardless of which one is
selected** — it only *decodes* the chosen one(s). Server CPU cost → ~0, but bandwidth is "whole
file" either way (true of today's passthrough too). Usually a minor tax (audio is a small slice
of total bitrate vs. video) but worth stating plainly.

**Browser compatibility caveat:** WebCodecs support isn't uniform (strong in Chromium/recent
Safari, still catching up in Firefox). Needs a fallback path for unsupported browsers — most
likely: fall back to today's plain `<video src>` behavior (whatever single default track the
browser's limited native support gives), not a hard failure.

**Open build-approach question:** hand-roll a minimal MKV parser + raw WebCodecs (smaller,
fully understood, more work) vs. build on an existing library like `mediabunny` (faster to ship,
more maintained, less control/understanding of internals, needs vetting for maturity/bundle size).

## 5. Things that get harder than they look

1. **Seeking breaks once ffmpeg is in the pipe.** Byte offsets in remuxed output don't correspond
   to source byte offsets — today's Range-seeking is free (raw passthrough); post-remux, scrubbing
   needs: re-run `ffmpeg -ss <seconds>` against the same self-HTTP input on each seek, JS tracks a
   "logical timeline" since each new stream starts its own byte-0 at the seek point. Solvable
   (this is literally what Jellyfin does for "direct stream + remux") but it's separate work from
   the remux itself, not a side effect.
2. **Codec compatibility isn't universal.** H.264/AAC/VP9/Opus → cheap stream-copy. HEVC video or
   AC3/DTS/TrueHD audio (common in MKV rips) → real re-encoding, CPU-expensive. Proposal: detect
   via the probe; for v1, either refuse (fall back to download-only) or only transcode audio
   (cheap) while requiring video to already be web-compatible. Full video transcoding = defer.
3. **Subtitles are three different problems:**
   - SRT: plain text, trivial → WebVTT.
   - ASS/SSA: styled, converts to WebVTT but loses styling/positioning unless rendered properly
     (e.g. libass via WASM — real extra work, a v2+ item).
   - PGS: bitmap subs from disc rips, can't become WebVTT at all — would need image-overlay
     rendering. Proposal: explicitly unsupported in v1.
4. **Cost model changes.** Today streaming is ~free on our server (I/O passthrough only); ffmpeg
   processes are CPU-bound. Needs: a concurrency cap (`MAX_CONCURRENT_REMUX` or similar), cleanup
   of orphaned ffmpeg processes on client disconnect (Starlette disconnect handling), maybe a
   "server busy" response above the cap.
5. **No dedup across viewers.** Two people on the same shared link each spin up their own ffmpeg
   process re-pulling bytes from Telegram — no shared-cache layer today. Fine at low traffic;
   worth revisiting if a link gets heavy concurrent viewership.
6. **Ops:** needs the `ffmpeg` binary installed (apt package, not pip) — update `Dockerfile` and
   `docs/04-deployment.md` once this moves forward.

## 6. Open decisions (need a call before scoping into stages)

**Superseded by the no-server-encode constraint (2026-10-05) — items 2 and 4 below no longer
apply (there's no server-side transcode or process pool to size). Current open items:**

1. Build approach: hand-roll a minimal demuxer + raw WebCodecs, or build on `mediabunny`
   (or similar) — smaller/custom vs. faster-to-ship/more maintained?
2. Still want a visible audio/subtitle picker UI, or lean on native `audioTracks`/`textTracks`
   where the browser exposes them?
3. PGS subtitles: skip entirely (recommended) or worth the extra client-side lift?
4. Confirm this is additive: the existing plain byte-passthrough path and template stay untouched
   for files that don't need multi-track handling (non-MKV, or MKV with a single track).
5. Fallback UX for browsers without adequate WebCodecs/MSE support — degrade to today's plain
   `<video src>` behavior, surfaced how (silent fallback vs. a visible notice)?

### Superseded (kept for history — assumed server-side ffmpeg, ruled out)
1. ~~Option A, B, or aim straight for something closer to C?~~
2. ~~Is video transcoding (HEVC etc.) in scope at all?~~
3. ~~Concurrency/cost ceiling for simultaneous remux processes?~~

## 8. What was actually built (prototype, `webplayer/`)

A standalone prototype lives in `webplayer/` (not wired into the bot/server yet — see "Not done"
below). It follows Option D exactly: the server is untouched, everything happens client-side.

### 8.1 What exists

- **`src/demux.mjs`** — a hand-rolled, dependency-free EBML/Matroska parser (not `mediabunny` or
  another library — the open "build approach" question from §6 was resolved in favor of hand-
  rolling, since the scope turned out to be small and fully understood/tested beats an unvetted
  dependency for a v1). Exposes:
  - `probeTracks(bytes)` — reads EBML header + Segment/Info/Tracks, returns track list (number,
    type, codec, language, name, default flag, audio/video params) plus timing info.
  - `iterateBlocks(bytes, probe)` — yields every SimpleBlock/Block across all Clusters with
    track number, absolute timestamp, and a zero-copy view of the payload.
  - `extractSubtitleCues(bytes, probe, trackNumber)` — plain-text cue extraction for
    `S_TEXT/UTF8`/`D_WEBVTT/SUBTITLES` tracks (SRT/WebVTT-style text; **not** ASS/SSA styling,
    **not** PGS bitmap subs — see "Not done" below).
  - `filterToSingleAudioTrack(bytes, probe, videoTrackNumber, audioTrackNumber)` — the core
    "client does the heavy lifting" primitive: builds a new, valid WebM buffer containing only
    the video track and one chosen audio track, by copying bytes verbatim (EBML header, Info,
    the two kept TrackEntry elements, cluster Timecodes, kept Blocks) and only recomputing the
    wrapping element sizes. **This is a byte-level filter, not a re-encode** — it never touches
    codec-level data, satisfying the no-server-and-no-client-transcode-either spirit of the
    constraint (the client isn't decoding/re-encoding anything either; it's just picking bytes).
- **`src/webplayer.mjs`** — wires the demuxer to a `<video>` element: fetches the whole file
  once, probes tracks, builds a `MediaSource`/`SourceBuffer` fed by `filterToSingleAudioTrack`'s
  output, and exposes `selectAudio(number)` (rebuilds the MSE pipeline, preserving playback
  position/play-state) and `selectSubtitle(number|null)` (pure client-side cue lookup, entirely
  separate from the MSE pipeline — subtitles are never muxed into the video stream).
- **Test suite**: `test/unit/demux.test.mjs` (35 `node:test` cases against a real synthetic
  fixture) + `test/e2e/run.mjs` (8 Playwright-driven checks against real headless Chromium:
  track listing, default playback, subtitle text at specific timestamps and across track
  switches, audio track switching with position/play-state preservation, no uncaught page
  errors). Fixture: `test/fixtures/fixture.webm`, a 6s synthetic WebM (ffmpeg, VP9 + 3×Opus +
  2×WebVTT-in-WebM subtitle tracks, including non-ASCII metadata) built specifically to exercise
  multi-audio + multi-subtitle + non-default-track selection. Both suites pass repeatably (run
  5× back-to-back with no flakes as of this writing).

### 8.2 Two real bugs found and fixed by testing against a real browser

Both of these were invisible to `ffprobe`/`ffmpeg` (which accepted the "broken" output as fully
valid) and only surfaced because the regression suite drives **real Chromium via Playwright**,
not just the hand-rolled parser against itself. Worth recording since they're exactly the kind of
thing a server-side (ffmpeg-based) remux would never have hit, and a reason real-browser testing
was worth the setup cost:

1. **Cluster block order**: Matroska only requires non-decreasing timecodes *per track* within a
   Cluster. Chromium's MSE "WebM Byte Stream Format" parser requires non-decreasing timecodes
   **across all tracks** in a Cluster — stricter than the container spec itself. The source
   file's natural interleave (audio blocks slightly ahead of a video frame by a few ms) is
   perfectly legal Matroska but got silently rejected by `appendBuffer()` (`SourceBuffer` `error`
   event, `MediaSource.readyState` → `"ended"`). Fix: `filterToSingleAudioTrack` now stable-sorts
   kept blocks within each Cluster by timecode before re-emitting — still a byte-level copy, just
   reordered, which Matroska permits.
2. **Non-ASCII `TrackEntry` `Name`**: a `TrackEntry` whose `Name` field contains non-ASCII text
   (e.g. "日本語") caused Chromium's MSE init-segment parser to reject the *entire* `appendBuffer()`
   call outright (`MediaSource.readyState` → `"closed"`, a harder failure than the per-block
   case above) — confirmed with bytes that are valid EBML and valid UTF-8 (round-tripped
   correctly by this project's own parser and accepted without complaint by `ffprobe`/`ffmpeg`).
   The `Name` field is purely cosmetic and never read back out of the MSE-destined buffer (the
   player already gets track names for its UI from the original, untouched file buffer), so the
   fix drops the `Name` element entirely when building the filtered output.

Both are covered by regression tests (`demux.test.mjs`: the `drops each TrackEntry's Name
element` test and the `has non-decreasing block timestamps across tracks` test for every audio
track pairing) so a regression would be caught immediately rather than needing to be
rediscovered via browser-level trial and error again.

### 8.3 Explicitly out of scope / not done (do not assume these work)

- **Not wired into the bot/server or `req.html` template.** This is a standalone prototype under
  `webplayer/`, proven against a synthetic fixture — integrating it into the actual watch page,
  routing real file URLs through it, and UI for the audio/subtitle pickers (§6 open question 2)
  is unstarted follow-up work.
- **Codec scope**: only WebM-native codecs (VP8/VP9/AV1 video, Opus/Vorbis audio) — `webplayer.mjs`
  throws a clear error for anything else. H.264/AAC-in-MKV (very common in real-world rips) needs
  an actual ISOBMFF/fMP4 remux step (still client-side, just a different container target), not
  implemented.
- **No incremental/streaming fetch.** `createPlayer` does one whole-file `fetch()`. Real seeking
  on a large remote file (computing byte offsets from Cues, issuing new `Range` requests, feeding
  MSE incrementally) is the §4.4/§5 bandwidth-and-seeking discussion, still unimplemented.
- **Subtitle styling**: plain-text cues only (SRT/WebVTT-shaped). No ASS/SSA styling (would need
  JASSUB per §4.4), no PGS bitmap subtitles (still the recommended-unsupported case from §5.3).
- **Lacing**: only the "no lacing" SimpleBlock/Block case is parsed (what every modern muxer
  produces by default); a laced block is detected and reported (`unsupportedLacing`) rather than
  decoded.
- **WebCodecs path**: not attempted. MSE turned out sufficient for the WebM-native-codec case;
  WebCodecs remains the documented option for a future fMP4/H.264 path or finer-grained control.

## 9. Log

| Date | What happened |
|---|---|
| 2026-10-05 | Ruled out server-side encode/decode entirely ("use the client for the heavy lifting"). Added Option D: fully client-side demux (WebTorrent-stack-style, matroska-subtitles, JASSUB, WebCodecs, mediabunny as prior art/building blocks); server side likely needs zero changes, even probing can move client-side. Noted the bandwidth-vs-CPU trade-off (can't cherry-pick track bytes from interleaved Clusters) and that seeking is actually easier in this model. Superseded the A/B/C-specific open questions. Nothing implemented. |
| 2026-10-05 | Initial brainstorm: read the full existing streaming path, confirmed zero existing transcoding, identified the self-HTTP-as-ffmpeg-input trick, drafted options A/B/C and the open decisions above. Nothing implemented. |
