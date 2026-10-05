import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  probeTracks,
  listTracks,
  extractSubtitleCues,
  iterateBlocks,
  filterToSingleAudioTrack,
  _internal,
} from "../../src/demux.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = path.join(__dirname, "..", "fixtures", "fixture.webm");
const fixtureBytes = new Uint8Array(readFileSync(FIXTURE_PATH));

function probe() {
  return probeTracks(fixtureBytes);
}

// ---------------------------------------------------------------- vint primitives

describe("EBML vint primitives", () => {
  test("readId keeps the marker bit (matches canonical element-id constants)", () => {
    // 0x1A45DFA3 (EBML header) is a real 4-byte ID present at the very start of the fixture.
    const { value, nextPos } = _internal.readId(fixtureBytes, 0);
    assert.equal(value, 0x1a45dfa3);
    assert.equal(nextPos, 4);
  });

  test("readSize strips the marker bit and returns the numeric size", () => {
    // Byte right after the 4-byte EBML id in the fixture is 0x9F -> 1-byte vint, value 0x1F=31.
    const { value, nextPos, length } = _internal.readSize(fixtureBytes, 4);
    assert.equal(value, 31);
    assert.equal(length, 1);
    assert.equal(nextPos, 5);
  });

  test("readSize detects the all-ones 'unknown size' marker", () => {
    const unknown8 = Uint8Array.of(0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff);
    const { value } = _internal.readSize(unknown8, 0);
    assert.equal(value, null);
  });

  test("readId/readSize reject a leading zero byte (invalid vint)", () => {
    const bad = Uint8Array.of(0x00, 0x01);
    assert.throws(() => _internal.readId(bad, 0));
    assert.throws(() => _internal.readSize(bad, 0));
  });

  test("encodeSize round-trips through readSize for a range of sizes", () => {
    for (const size of [0, 1, 126, 127, 128, 16383, 16384, 20000000, 2 ** 28]) {
      const encoded = _internal.encodeSize(size);
      const padded = new Uint8Array([...encoded, 0]); // readSize needs bytes available after it
      const { value, length } = _internal.readSize(padded, 0);
      assert.equal(value, size, `size ${size} round-tripped as ${value}`);
      assert.equal(length, encoded.length);
    }
  });

  test("cueTextFromPayload strips the identifier/settings header", () => {
    assert.equal(_internal.cueTextFromPayload("\n\nHello"), "Hello");
    assert.equal(_internal.cueTextFromPayload("id\nsettings\n\nHello world"), "Hello world");
    assert.equal(_internal.cueTextFromPayload("just text, no blank line"), "just text, no blank line");
  });
});

// ---------------------------------------------------------------- track probing

describe("probeTracks against the real fixture", () => {
  test("finds exactly 6 tracks in source order", () => {
    const p = probe();
    assert.equal(p.tracks.length, 6);
    assert.deepEqual(p.tracks.map((t) => t.number), [1, 2, 3, 4, 5, 6]);
  });

  test("timecodeScale is 1ms/tick (the ffmpeg webm default)", () => {
    assert.equal(probe().timecodeScale, 1_000_000);
  });

  test("container duration is reported (~6.008s in ticks)", () => {
    const p = probe();
    assert.ok(p.durationTicks !== null);
    assert.ok(Math.abs(p.durationTicks - 6008) < 1, `durationTicks=${p.durationTicks}`);
  });

  test("video track metadata", () => {
    const v = probe().tracks.find((t) => t.number === 1);
    assert.equal(v.type, _internal.TRACK_TYPE_VIDEO);
    assert.equal(v.codecId, "V_VP9");
    assert.equal(v.video.width, 320);
    assert.equal(v.video.height, 240);
  });

  test("three audio tracks with distinct language/title metadata, including non-ASCII", () => {
    const p = probe();
    const byNum = (n) => p.tracks.find((t) => t.number === n);
    const eng = byNum(2), jpn = byNum(3), fre = byNum(4);
    assert.equal(eng.type, _internal.TRACK_TYPE_AUDIO);
    assert.equal(eng.language, "eng");
    assert.equal(eng.name, "English");
    assert.equal(eng.default, true);
    assert.equal(eng.codecId, "A_OPUS");
    assert.equal(eng.audio.channels, 1);
    assert.equal(eng.audio.samplingFrequency, 48000);

    assert.equal(jpn.language, "jpn");
    assert.equal(jpn.name, "日本語"); // non-ASCII UTF-8 title must round-trip exactly
    assert.equal(jpn.default, false);

    assert.equal(fre.language, "fre");
    assert.equal(fre.name, "Français"); // UTF-8 with a combining/accented character
    assert.equal(fre.default, false);
  });

  test("two subtitle tracks with webvtt codec and matching languages", () => {
    const p = probe();
    const engSub = p.tracks.find((t) => t.number === 5);
    const freSub = p.tracks.find((t) => t.number === 6);
    assert.equal(engSub.type, _internal.TRACK_TYPE_SUBTITLE);
    assert.equal(engSub.codecId, "D_WEBVTT/SUBTITLES");
    assert.equal(engSub.language, "eng");
    assert.equal(freSub.language, "fre");
  });

  test("listTracks() gives a clean public summary", () => {
    const summary = listTracks(probe());
    assert.deepEqual(
      summary.map((t) => t.kind),
      ["video", "audio", "audio", "audio", "subtitle", "subtitle"]
    );
    assert.equal(summary[1].name, "English");
  });
});

// ---------------------------------------------------------------- block iteration

describe("iterateBlocks against the real fixture", () => {
  test("every block in this fixture is unlaced (documented scope boundary holds)", () => {
    const p = probe();
    for (const block of iterateBlocks(fixtureBytes, p)) {
      assert.equal(block.unsupportedLacing, false, `track ${block.trackNumber} at ${block.timestampMs}ms is laced`);
    }
  });

  test("video track has exactly 60 frames (10fps * 6s)", () => {
    const p = probe();
    const videoFrames = [...iterateBlocks(fixtureBytes, p)].filter((b) => b.trackNumber === 1);
    assert.equal(videoFrames.length, 60);
    assert.equal(videoFrames[0].timestampMs, 0);
  });

  test("all three audio tracks carry the same number of frames (same source duration)", () => {
    const p = probe();
    const blocks = [...iterateBlocks(fixtureBytes, p)];
    const count = (n) => blocks.filter((b) => b.trackNumber === n).length;
    const engCount = count(2);
    assert.ok(engCount > 0);
    assert.equal(count(3), engCount);
    assert.equal(count(4), engCount);
  });
});

// ---------------------------------------------------------------- subtitle cue extraction

describe("extractSubtitleCues against the real fixture", () => {
  test("English subtitle track matches the source .srt exactly", () => {
    const cues = extractSubtitleCues(fixtureBytes, probe(), 5);
    assert.deepEqual(
      cues.map((c) => [c.startMs, c.endMs, c.text]),
      [
        [500, 2000, "Hello"],
        [2500, 4000, "World"],
        [4500, 5500, "Goodbye"],
      ]
    );
  });

  test("French subtitle track matches the source .srt exactly, including accents", () => {
    const cues = extractSubtitleCues(fixtureBytes, probe(), 6);
    assert.deepEqual(
      cues.map((c) => [c.startMs, c.endMs, c.text]),
      [
        [500, 2000, "Bonjour"],
        [2500, 4000, "Monde"],
        [4500, 5500, "Au revoir"],
      ]
    );
  });

  test("rejects a non-subtitle track", () => {
    assert.throws(() => extractSubtitleCues(fixtureBytes, probe(), 1), /not a subtitle track/);
  });

  test("rejects a track number that does not exist", () => {
    assert.throws(() => extractSubtitleCues(fixtureBytes, probe(), 99), /not a subtitle track/);
  });
});

// ---------------------------------------------------------------- track-filtering remux

describe("filterToSingleAudioTrack", () => {
  for (const [audioNum, lang] of [[2, "eng"], [3, "jpn"], [4, "fre"]]) {
    test(`filtering to video + audio track ${audioNum} (${lang}) produces a valid 2-track WebM`, () => {
      const p = probe();
      const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, audioNum);
      const p2 = probeTracks(filtered); // must itself be parseable from scratch
      assert.equal(p2.tracks.length, 2);
      assert.deepEqual(p2.tracks.map((t) => t.number).sort(), [1, audioNum]);
      assert.equal(p2.tracks.find((t) => t.number === 1).codecId, "V_VP9");
      assert.equal(p2.tracks.find((t) => t.number === audioNum).language, lang);
    });

    test(`filtered output for audio track ${audioNum} contains exactly the video+chosen-audio blocks, byte-identical to source`, () => {
      const p = probe();
      const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, audioNum);
      const p2 = probeTracks(filtered);

      // filterToSingleAudioTrack stable-sorts blocks within each cluster by timecode across
      // tracks (Chromium's MSE WebM parser requires non-decreasing timecodes across all tracks
      // within a cluster, stricter than Matroska itself) — so compare as same-timestamp-ordered
      // sequences rather than assuming the source's original per-track interleave is preserved.
      const byTimestampThenTrack = (a, b) => a.timestampMs - b.timestampMs || a.trackNumber - b.trackNumber;
      const sourceBlocks = [...iterateBlocks(fixtureBytes, p)]
        .filter((b) => b.trackNumber === 1 || b.trackNumber === audioNum)
        .sort(byTimestampThenTrack);
      const filteredBlocks = [...iterateBlocks(filtered, p2)].sort(byTimestampThenTrack);
      assert.equal(filteredBlocks.length, sourceBlocks.length);
      for (let i = 0; i < sourceBlocks.length; i++) {
        assert.equal(filteredBlocks[i].trackNumber, sourceBlocks[i].trackNumber);
        assert.equal(filteredBlocks[i].timestampMs, sourceBlocks[i].timestampMs);
        assert.deepEqual([...filteredBlocks[i].data], [...sourceBlocks[i].data]);
      }
    });

    test(`filtered output for audio track ${audioNum} drops every other audio track`, () => {
      const p = probe();
      const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, audioNum);
      const p2 = probeTracks(filtered);
      const otherTracks = [2, 3, 4].filter((n) => n !== audioNum);
      const blocks = [...iterateBlocks(filtered, p2)];
      for (const other of otherTracks) {
        assert.equal(blocks.filter((b) => b.trackNumber === other).length, 0);
      }
    });

    test(`filtered output for audio track ${audioNum} has non-decreasing block timestamps across tracks (required by Chromium's MSE WebM parser)`, () => {
      const p = probe();
      const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, audioNum);
      const p2 = probeTracks(filtered);
      const blocks = [...iterateBlocks(filtered, p2)];
      assert.ok(blocks.length > 0);
      for (let i = 1; i < blocks.length; i++) {
        assert.ok(
          blocks[i].timestampMs >= blocks[i - 1].timestampMs,
          `block ${i} (track ${blocks[i].trackNumber}, ${blocks[i].timestampMs}ms) is earlier than block ${i - 1} (track ${blocks[i - 1].trackNumber}, ${blocks[i - 1].timestampMs}ms)`
        );
      }
    });
  }

  test("drops each TrackEntry's Name element (Chromium's MSE parser rejects non-ASCII Name text, even though it's valid EBML/UTF-8)", () => {
    const p = probe();
    for (const audioNum of [2, 3, 4]) {
      const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, audioNum);
      const p2 = probeTracks(filtered);
      for (const t of p2.tracks) {
        assert.equal(t.name, "", `track ${t.number} in the audio-${audioNum} filtered output should have no Name`);
      }
    }
  });

  test("rejects an audio track number that doesn't exist", () => {
    const p = probe();
    assert.throws(() => filterToSingleAudioTrack(fixtureBytes, p, 1, 999), /expected both track/);
  });

  test("the filtered buffer is meaningfully smaller than the source (two audio tracks dropped)", () => {
    const p = probe();
    const filtered = filterToSingleAudioTrack(fixtureBytes, p, 1, 2);
    assert.ok(filtered.length < fixtureBytes.length);
  });
});
