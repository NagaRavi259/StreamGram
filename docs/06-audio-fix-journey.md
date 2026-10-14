# The audio-fix feature: full timeline, dead ends, and how we actually got there

> **Superseded.** The server-side ffmpeg pipeline this doc is about has been removed —
> `streamgram/utils/audio_fix.py` is gone, replaced by a client-side player (ArtPlayer +
> Mediabunny, decoding AC3/E-AC3/DTS via WebCodecs in the browser instead of transcoding on the
> server). Kept as historical record: the debugging lessons below (the `-seekable 1` fix, the
> fragmented-MP4-duration trap) are still worth knowing even though the mechanism they're about is
> gone.

Purpose of this doc: a resumable record. Read this before touching
`streamgram/utils/audio_fix.py` again — several of the dead ends below look like
reasonable things to try, and they were already tried and ruled out for
documented reasons. Don't re-walk them blind.

## 0. How this started

A real file was shared via the bot: `Spider-Man.Brand.New.Day.2026.1080p.AMZN.WEB-DL.Tel.Eng.DD 5.mkv`.
Report: **video plays smoothly, there is no audio at all.**

MediaInfo on the real file showed why:

- Video: **AV1**, 1920×800, 10-bit — natively supported by every modern browser.
- Audio (both tracks): **E-AC-3 (Dolby Digital Plus)** — Telugu marked default,
  English not default. No browser, in any form (native `<video>`, MSE,
  WebCodecs), ships a decoder for this codec family (AC3/E-AC3/DTS/TrueHD) —
  it's licensed, and that's a deliberate, permanent gap, not a bug that will
  get fixed upstream.

Important scoping fact established immediately: the client-side MKV/WebM
multi-audio player prototype built just before this (`webplayer/`, see
`docs/05-mkv-multiaudio-streaming.md`, commit `b8b6905`) **does not help
here**. It was never wired into the real watch page, and even if it were, its
whole approach is MSE-based client-side demuxing — which cannot play E-AC-3
either, client-side or not. A genuinely different mechanism was needed:
something has to re-encode the audio. The question was only ever *where*
(server vs. client) and *how much* (just audio, or the whole file).

Decision: a **narrow server-side exception** — copy video
untouched (`-c:v copy`, no video transcode, ever), re-encode only the broken
audio track to AAC. This is the one deliberate crack in this project's
"server is a dumb byte pipe" ethos (see `docs/01-architecture-and-flow.md`),
scoped as tightly as possible.

## 1. Commit `6f3ef90` — first working version

**Built:** `streamgram/utils/audio_fix.py`, wired into `streamgram/server/stream_routes.py`.

- `get_audio_fix_plan()`: probes the file once with `ffprobe` (self-HTTP
  loopback through the server's own existing streaming endpoint — reuses all
  existing Range/auth handling), decides if the chosen audio track's codec is
  in `UNSUPPORTED_AUDIO_CODECS = {ac3, eac3, dts, truehd, mlp}`. Result cached
  per file id for the process lifetime.
- `start_ffmpeg_transcode()`: spawns `ffmpeg -c:v copy -c:a aac ... -f mp4
  pipe:1`, piped straight into the HTTP response.
- A **recursion guard**: ffprobe/ffmpeg's own self-HTTP call carries a custom
  header (`X-Internal-Call: 1`, sent via ffmpeg's `-headers` option); the
  route handler checks for it and skips the audio-fix branch entirely for
  that request. Without this, probing or transcoding a file would try to
  probe/transcode itself recursively — this was caught in design, before
  writing any other code, by tracing through what the self-HTTP call would
  actually hit.
- A **concurrency cap** (`MAX_CONCURRENT_AUDIO_FIX`, default 2): refuses new
  transcodes past the limit, falling back to plain video-only passthrough
  rather than piling up ffmpeg processes.
- Fails open everywhere: missing `ffmpeg`/`ffprobe`, a probe timeout, a bad
  probe result — all result in plain passthrough, never a broken request.

**Verified** with a real end-to-end test (not mocked): built a real
E-AC-3-in-MKV fixture with real `ffmpeg`, ran a real server, hit it over real
HTTP, confirmed with real `ffprobe`/`ffmpeg` that the output kept the original
video codec and carried AAC audio, decoding cleanly.

Tested on the real file: **confirmed the audio fix itself worked**, but
reported three problems after ~20 minutes / 5 attempts:

1. Worked once, then **never again** — every subsequent attempt silently
   fell back to video-only.
2. After working once, it **stopped around 40 seconds** and wouldn't load
   further; refreshing made it fail outright (video length shown, no audio).
3. **No logs at all** to diagnose any of this with.

## 2. Commit `324033a` — the slot-leak bug, live logging, dual links

**Root cause of "worked once, never again":** the concurrency gate released
its reserved slot inside an `async`/lock-based cleanup path
(`await _get_gate().leave()`). When the first transcode stalled and the
client gave up, that cleanup could itself be interrupted by task
cancellation *before* reaching the release call — a classic asyncio pitfall.
With a cap of 2, one or two bad runs permanently wedged the gate, and every
later request silently fell back to video-only forever after (exactly
matching "worked once, then only video" and "fails after refresh").

**Fix:** rewrote the gate as a plain synchronous counter
(`_ConcurrencyGate.try_enter()`/`.leave()`, no `await`, no lock). Python's
asyncio is cooperatively single-threaded, so a bare increment/decrement
between awaits is safe without one — and critically, a synchronous call
placed in an unconditional `finally` cannot be skipped by cancellation the
way an `await`-ing one can. Proved this with a dedicated test that
deliberately cancels a stream mid-read and asserts the slot comes back.

**Also added** (this is the direct answer to "no logs at all"):

- Live `stderr` draining from the ffmpeg process — logged line by line as it
  runs, not just dumped after it exits. Previously a stall or slow failure
  was invisible until (if ever) the process died.
- Periodic progress logging (bytes sent, elapsed time) during streaming.
- A **stall timeout** (45s of zero new output → log a loud warning and end
  the response cleanly, instead of hanging forever with no explanation).
- Raised the ffprobe timeout 20s → 60s (a multi-GB file's index can take a
  while to reach over Telegram; the old timeout may have been silently
  failing open more often than realized).

**Also built** (separate ask: "show both player URLs so we can compare"): an
explicit `?audiofix=1` / `?audiofix=0` query-param override
(`parse_audiofix_override`) that forces the fix path on or off regardless of
auto-detection, and a second "🔊 Watch (Audio Fixed) 🔊" button in every bot
message alongside the existing auto-detecting one.

Tested: **confirmed the slot-leak fix worked, audio played correctly
for ~20 minutes with no issues** — but no seeking was possible (progressive
response, no Range support on the fixed stream), and the question of a
language/track picker came up for the first time (the file has Telugu
default + English — nothing let you pick).

## 3. Commit `6dd1040` — seeking, no disk cache

Two architectural options were on the table for seeking:

- **A (cache to disk once, serve normally after)** — simpler, perfect native
  seeking after the first view, but needs disk space + an eviction policy.
- **B (re-transcode from the seek point every time, no disk cache)** — no
  disk usage, but every seek pays a real reconnect/rebuffer cost, and needs a
  custom player (the stream can't report its own duration or support real
  byte-range seeking when every view is a fresh transcode).

**Went with option B.** Built:

- `probe_duration_seconds()` (later folded into `probe_media_info()`, see
  below) so the page can know the real total duration up front, since the
  stream itself can't say.
- `start_seconds` param on `start_ffmpeg_transcode()` → ffmpeg's `-ss` placed
  *before* `-i` (fast input seek — the only option that makes sense anyway
  since `-c:v copy` can't seek within a re-encode that doesn't exist).
- `streamgram/template/audiofix_player.html` — a hand-built player (not reusing
  Plyr, which has no way to represent "duration the stream itself doesn't
  know"): tracks `baseOffset` + `video.currentTime` as the real position,
  renders its own seek bar against the server-told duration, and on seek
  pauses, reloads `video.src` with `&start=<seconds>`, and resumes once ready.
- `render_template.py` now decides, per request, whether to serve this player
  or the plain one, based on whether the fix actually applies.

Verified end-to-end against a 6s fixture with 1-second keyframes (deliberately
short keyframe interval, to make seeking meaningful within a short test clip):
duration probing accurate, `&start=3` on a 6s file yields the right remaining
span, watch page renders the right player. 25 checks, stable across repeats.

Tested: **seeking worked, but surfaced three new problems:**

1. Audio lags badly after seeking.
2. Fresh page load shows the loading spinner forever (though pressing play
   worked fine regardless).
3. No language-switch UI showed up.

## 4. Commit `e14c431` — first (incomplete) attempt at the lag, spinner fix, track picker

**Spinner:** trivial, genuinely fixed — the spinner only cleared on the
`playing` event, so a freshly-loaded-but-not-yet-played video (autoplay is
off by default) spun forever even though play worked instantly. Fixed by
also clearing it on `canplay`/`loadeddata`.

**Audio lag, attempt 1 (wrong-ish diagnosis, partially right fix):** the
working theory at the time: stream-copied video always snaps to the nearest
keyframe at/before a seek point, but ffmpeg's default "accurate seek" trims
*decoded* streams (our re-encoded audio) to land exactly on the requested
second — so video starts at the keyframe and audio starts later, at the
literal requested time, a gap of up to one keyframe interval. Added
`-af aresample=async=1` (lets the audio resampler stretch/compress to stay
locked to video) and `-avoid_negative_ts make_zero`.

Verified with a seek to a deliberately non-keyframe-aligned point (3.4s,
1-second keyframes) — audio/video gap measured at 0.2s, called it fixed.
**In hindsight this test was barely discriminating** (0.2s against a 0.3s
threshold, on a keyframe interval so tight the real bug had little room to
show up) — a preview of a mistake repeated later at larger scale.

**Track picker:** added `probe_media_info()` (superseding the separate
duration-only probe — one combined `ffprobe -show_format -show_streams` call
instead of two), returning every audio track's language/title/codec/default
flag, not just the chosen one. `get_audio_fix_plan()` gained an
`audio_track_index` override: an explicit pick always routes through the
fixed/seekable pipeline for that track, regardless of whether that specific
track's own codec needed fixing — so switching languages never silently
changes which pipeline is in play. Player gained a `<select>` (only shown
when there's more than one track), wired to `&atrack=N`, carried through
every subsequent seek too.

Retested: **"lag is not there when playing from scratch, when i seek it
starts lag"** — a precise, valuable clarification: this narrowed it to
something specifically about the seek path, not a general drift. Also:
**the video container visibly shrinks and grows on every seek.**

## 5. Commit `49e98d8` — container resize fix, second (still incomplete) lag attempt

**Container resize:** genuinely fixed. `probe_media_info()` extended to also
report video width/height; the watch page now sets a fixed CSS
`aspect-ratio` on the video box from the real probed resolution (falling
back to 16/9 if unknown), with `object-fit: contain` on the `<video>` inside
it. The box's size is now completely decoupled from whatever the currently-
loading segment reports, so it never visibly jumps.

**Audio lag, attempt 2:** re-read the "lag only after seeking" clarification
and reasoned (correctly, as it turned out, but for the specifically-seek-
triggered case) that video (never decoded) always snaps to the nearest
keyframe, while ffmpeg's default accurate-seek trims the *decoded* audio to
land exactly on the requested second — a gap up to one full keyframe
interval. Added `-noaccurate_seek` so audio, like video, just lands wherever
the demuxer's own seek landed, no independent trim.

Strengthened the resample filter to the fuller documented form
(`aresample=async=1:min_hard_comp=0.100000:first_pts=0`) and added
`-frag_duration 2000000` to bound fragment size for tighter audio/video
interleaving.

**The test for this led to a long, important detour — see §6 below** before
it actually proved anything. At the time of this commit, the test used a
12-second fixture with 1-second keyframes and a 0.3s/0.5s tolerance, and
superficially "passed." It shouldn't have been trusted; see below.

Retested: **"lag is not there when playing from scratch, when i seek it
start lag"** — same report, unchanged. This fix hadn't actually landed yet
at the point of that report (the commit before this one didn't have
`-noaccurate_seek`; this is the one that added it) — important to track
which commit a given piece of feedback is actually about.

## 6. Commit `b7bcdf7` — finding the real bug

This is the one that actually fixes seeking. Getting here took a long,
twisty investigation — recorded in full because the dead ends are
instructive and easy to re-walk by accident.

**Step 1 — re-verify `-noaccurate_seek` with a *longer* test.** Bumped the
test fixture from 12s/1s-keyframes to what seemed like a more meaningful
30s/3s-keyframes, specifically because a 1-second keyframe interval gives
the bug almost no room to show up even when present (confirmed after the
fact: the *previous* test's "pass" was nearly meaningless, see
commit 5's note above).

**Step 2 — new test immediately failed, differently and more severely than
expected**: `format.duration` on a seek-to-3-of-12-seconds output came back
as ~12.2s (nearly the *whole* file), not the expected ~9s.

**Step 3 — false lead: blamed `avoid_negative_ts`.** Isolated each added
flag one at a time. Found `avoid_negative_ts` *alone* broke duration
reporting (9.0s → 12.0s), and `movflags frag_keyframe+empty_moov` *alone*
also broke it (→ 12.2s). Formed a theory: `avoid_negative_ts make_zero`
computes its timestamp shift from the *original* (pre-seek) stream start,
"un-clipping" frames that were supposed to stay negative-and-hidden. This
theory felt solid, was wrong in its conclusion, and cost real time.

**Step 4 — checked actual packet timestamps, not just `format.duration`.**
This is where it got clearer *and* more confusing at once: packet-level PTS
showed video spanning the *entire original file* in the "broken" case
(e.g., 0.2 to 12.1 for a 12s file) — not just mislabeled, genuinely all 120
of 120 frames present. But also found, by removing fragmentation entirely,
that the "working" 1-fragment-free case *also* had 120 packets present —
just negatively timestamped (−3.0 to 8.9) so that the *negative-PTS-discard
convention* (which compliant players apply, but which is a convention, not
universal) made it *look* trimmed. Neither case was actually dropping
frames at the demuxer level. This genuinely looked, at this point, like
`-ss` + `-c:v copy` wasn't skipping anything at the read level at all, over
HTTP, regardless of muxer — a much bigger problem than expected.

**Step 5 — suspected the fixture itself, and this time it mattered: tested
a 60-second fixture directly against a local file.** Seeking there worked
perfectly (330 of 600 packets, correctly reduced, no negative timestamps).
This showed the 12-second fixture specifically was too short for ffmpeg's
seek logic to meaningfully engage at all — it was reading the whole tiny
file regardless of `-ss`, an artifact invisible performance-wise (the file
is minuscule either way) but very visible content-wise. **This was real and
worth catching, but it wasn't the whole story.**

**Step 6 — re-tested the 60-second fixture through the self-HTTP loopback
(not a local file) — and it broke again**, identically to the 12-second
case (`format.duration` ~60.2s instead of the correct ~33s; all 600 frames
present in the fragmented output). This is the pivotal finding: **the bug
was real, reproducible, and specific to HTTP access — never present when
ffmpeg reads the same bytes from a local file.**

**Step 7 — instrumented the server to log every incoming `Range` header**,
to see what ffmpeg was actually asking for. For a seek to 30s of a 60s
file, it issued exactly four requests: `bytes=0-`, `bytes=2831557-` (near
the file's end), `bytes=16419-`, `bytes=745-` — none of them anywhere near
the byte offset that 30 seconds would correspond to (~1.4MB). It was
probing the start and end (very likely hunting for Matroska's SeekHead/Cues
index) and then just... giving up and reading from near byte 0, full stop.

**Step 8 — verified the server itself was not the bug**: manually replayed
those exact four `Range` requests against the running server and confirmed
every single one came back byte-identical to a direct read of the source
file at that offset. The server was never the problem; ffmpeg simply never
asked for the right range.

**Step 9 — the actual fix.** Tried ffmpeg's `-seekable 1` input option
(explicitly asserting to ffmpeg's HTTP client that the server supports real
range-based seeking, rather than letting it guess/distrust that and fall
back to the broken strategy above). This fixed it immediately and
completely — the fourth request became `bytes=1269551-` (right where the
30-second target's keyframe actually lives), and the output correctly
contained only the remaining ~33 seconds, non-fragmented-style negative
timestamps no longer even needed. Added `-seekable 1` to both the
probe (`ffprobe`) and transcode (`ffmpeg`) self-HTTP calls.

**Step 10 — one more wrinkle, confirmed harmless.** A *shallow* seek (3
seconds into a 30-second file) still read from near the start even with
`-seekable 1`. Traced this to a sensible, unrelated ffmpeg optimization:
if a seek target is close enough to the start to already be covered by
ffmpeg's own initial format-probe read, it reuses that buffered data
instead of issuing a new seek. Confirmed the threshold empirically
(between 3s and 5s for this test fixture) and confirmed it's a non-issue
for the real use case — nobody meaningfully seeks to second 3 of a 2-hour
movie. Adjusted the test's seek points to land safely past this threshold
rather than "fixing" a non-bug.

**Step 11 — fixed the test's own tolerance.** `-noaccurate_seek` means
video always snaps to the nearest *preceding* keyframe — up to one full
keyframe interval early is correct, promised behavior, not imprecision to
tighten away. The test's original 1-second tolerance was tighter than what
the feature actually guarantees; widened it to `keyframe_interval + 1`.

**End state, verified for real:** a seek to a non-keyframe-aligned point
(7.5s, 3-second keyframes) now shows real audio/video packet timestamps
within 0.2s of each other (confirmed via `ffprobe` packet inspection, not
`format.duration`, which is known-unreliable for fragmented/`empty_moov`
output specifically and was never a safe metric to trust here — see Step 4).
28 checks, 4 runs clean, full existing suite still green.

## 7. Current status (as of commit `b7bcdf7`)

**Working and verified:**
- Audio-fix detection, transcode, concurrency gate (leak-proof), live
  logging, stall detection.
- Dual comparison links (`?audiofix=1`/`0`) in bot messages.
- Seeking via `start=` re-transcode, no disk cache, verified not to silently
  read the wrong content (the actual bug fixed in commit `b7bcdf7`).
- Audio/video sync at a seek point, verified via real packet timestamps.
- Language/track picker (`&atrack=N`), consistent pipeline regardless of
  which track is picked.
- Fixed-size video container (no visible resize on seek/track-switch).

**Not yet re-confirmed against the real file** as of this doc: the `-seekable 1`
fix (commit `b7bcdf7`) has been pushed but not yet tested against the real
Spider-Man file. Everything above "current status" up to this point was
verified only against synthetic fixtures, not yet the real file.

**Known, accepted limitations** (not bugs, don't re-litigate):
- Every seek pays a real reconnect/rebuffer cost (the no-disk-cache
  trade-off explicitly chosen over caching to disk).
- Seeking snaps to the nearest preceding keyframe (up to one keyframe
  interval early) — expected for fast, non-accurate seeking with
  `-c:v copy`.
- A seek within the first few seconds of a file may be served from the very
  start rather than the exact requested point (ffmpeg's own probe-buffer
  reuse optimization) — harmless for realistic seeking in a long file.
- `format.duration` reported by `ffprobe` on the *transcoded/fragmented*
  output is not a trustworthy correctness signal (confirmed unreliable for
  `empty_moov` fragmented MP4) — any future test or debugging session
  should measure real packet timestamps (`ffprobe -show_entries
  packet=pts_time`) instead, not container-level duration metadata.

**If audio lag is still reported after this push**, do not restart from the
`avoid_negative_ts` theory (§6 Step 3) or assume the test fixture is
automatically trustworthy (§6 Steps 1-2, 5) — both looked right and weren't.
Start instead by logging the actual `Range` requests ffmpeg issues against
the real file in production (the stall/stderr logging added in commit
`324033a` is the tool for this) and compare against what a correct seek
should ask for.
