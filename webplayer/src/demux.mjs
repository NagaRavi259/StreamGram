// Client-side Matroska/WebM demuxer: probing, subtitle-cue extraction, and a byte-level
// track filter (video + one chosen audio track) that produces a valid WebM buffer for MSE.
//
// Scope (see docs/05-mkv-multiaudio-streaming.md for the full picture):
//  - Parses the whole file into memory before walking it. Fine for modest file sizes;
//    incremental/streaming parsing of very large files is a documented follow-up, not done here.
//  - Only handles the "no lacing" SimpleBlock/Block case (one frame per block), which is what
//    every modern muxer (ffmpeg, mkvmerge) produces by default. A laced block is detected and
//    reported via `unsupportedLacing` rather than silently mis-parsed.
//  - Subtitle text extraction covers plain UTF-8/WebVTT text cues (S_TEXT/UTF8, D_WEBVTT/SUBTITLES).
//    ASS/SSA styling and PGS bitmap subtitles are out of scope for this pass.

// ---------------------------------------------------------------- EBML primitives

/** Reads an EBML "ID" vint: the marker bit is kept as part of the value (EBML convention). */
function readId(bytes, pos) {
  const b0 = bytes[pos];
  if (b0 === undefined) throw new RangeError(`readId: out of bounds at ${pos}`);
  if (b0 === 0) throw new Error(`readId: invalid vint (leading zero byte) at ${pos}`);
  let length = 1;
  let mask = 0x80;
  while (!(b0 & mask)) {
    length++;
    mask >>= 1;
  }
  let value = b0;
  for (let i = 1; i < length; i++) {
    const b = bytes[pos + i];
    if (b === undefined) throw new RangeError(`readId: out of bounds at ${pos + i}`);
    value = value * 256 + b;
  }
  return { value, nextPos: pos + length };
}

/** Reads an EBML "size" vint: marker bit stripped; all-data-bits-1 means "unknown size". */
function readSize(bytes, pos) {
  const b0 = bytes[pos];
  if (b0 === undefined) throw new RangeError(`readSize: out of bounds at ${pos}`);
  if (b0 === 0) throw new Error(`readSize: invalid vint (leading zero byte) at ${pos}`);
  let length = 1;
  let mask = 0x80;
  while (!(b0 & mask)) {
    length++;
    mask >>= 1;
  }
  let value = b0 & (mask - 1);
  let allOnes = value === mask - 1;
  for (let i = 1; i < length; i++) {
    const b = bytes[pos + i];
    if (b === undefined) throw new RangeError(`readSize: out of bounds at ${pos + i}`);
    value = value * 256 + b;
    if (b !== 0xff) allOnes = false;
  }
  return { value: allOnes ? null : value, nextPos: pos + length, length };
}

function readUint(bytes, start, end) {
  let v = 0;
  for (let i = start; i < end; i++) v = v * 256 + bytes[i];
  return v;
}

function readFloat(bytes, start, end) {
  const len = end - start;
  const view = new DataView(bytes.buffer, bytes.byteOffset + start, len);
  if (len === 4) return view.getFloat32(0, false);
  if (len === 8) return view.getFloat64(0, false);
  throw new Error(`readFloat: unexpected float width ${len}`);
}

function readAscii(bytes, start, end) {
  return String.fromCharCode(...bytes.subarray(start, end));
}

function readUtf8(bytes, start, end) {
  return new TextDecoder("utf-8").decode(bytes.subarray(start, end));
}

// ---------------------------------------------------------------- element IDs used

const ID = Object.freeze({
  EBML: 0x1a45dfa3,
  SEGMENT: 0x18538067,
  INFO: 0x1549a966,
  TIMECODE_SCALE: 0x2ad7b1,
  DURATION: 0x4489,
  TRACKS: 0x1654ae6b,
  TRACK_ENTRY: 0xae,
  TRACK_NUMBER: 0xd7,
  TRACK_TYPE: 0x83,
  CODEC_ID: 0x86,
  LANGUAGE: 0x22b59c,
  NAME: 0x536e,
  FLAG_DEFAULT: 0x88,
  AUDIO: 0xe1,
  CHANNELS: 0x9f,
  SAMPLING_FREQUENCY: 0xb5,
  VIDEO: 0xe0,
  PIXEL_WIDTH: 0xb0,
  PIXEL_HEIGHT: 0xba,
  CLUSTER: 0x1f43b675,
  TIMECODE: 0xe7,
  SIMPLE_BLOCK: 0xa3,
  BLOCK_GROUP: 0xa0,
  BLOCK: 0xa1,
  BLOCK_DURATION: 0x9b,
  CUES: 0x1c53bb6b,
  TAGS: 0x1254c367,
  SEEK_HEAD: 0x114d9b74,
});

const TRACK_TYPE_VIDEO = 1;
const TRACK_TYPE_AUDIO = 2;
const TRACK_TYPE_SUBTITLE = 17;

const TEXT_SUBTITLE_CODECS = new Set(["S_TEXT/UTF8", "S_TEXT/ASCII", "D_WEBVTT/SUBTITLES"]);

/** Master (container) elements worth recursing into for this scope. Everything else (Cues,
 * SeekHead, Tags, ...) is skipped structurally but never causes a parse error. */
const MASTER_IDS = new Set([ID.SEGMENT, ID.INFO, ID.TRACKS, ID.TRACK_ENTRY, ID.AUDIO, ID.VIDEO]);

// ---------------------------------------------------------------- top-level element walker

/** Yields {id, size, headerStart, dataStart, dataEnd} for each top-level child element of
 * bytes[start:end]. Does not recurse; callers recurse explicitly where needed. */
function* walkElements(bytes, start, end) {
  let pos = start;
  while (pos < end) {
    const { value: id, nextPos: afterId } = readId(bytes, pos);
    const { value: size, nextPos: afterSize } = readSize(bytes, afterId);
    const dataStart = afterSize;
    const dataEnd = size === null ? end : dataStart + size;
    if (dataEnd > end) {
      throw new Error(`walkElements: element 0x${id.toString(16)} at ${pos} overruns its parent`);
    }
    yield { id, size, headerStart: pos, dataStart, dataEnd };
    pos = dataEnd;
  }
}

function findChild(bytes, start, end, id) {
  for (const el of walkElements(bytes, start, end)) {
    if (el.id === id) return el;
  }
  return null;
}

// ---------------------------------------------------------------- track probing

/**
 * @param {Uint8Array} bytes  the whole file
 * @returns {{
 *   timecodeScale: number,
 *   durationTicks: number|null,
 *   tracks: Array<{number:number, type:number, codecId:string, language:string, name:string,
 *                   default:boolean, audio:{channels:number,samplingFrequency:number}|null,
 *                   video:{width:number,height:number}|null}>,
 *   segment: {dataStart:number, dataEnd:number},
 * }}
 */
export function probeTracks(bytes) {
  const ebml = findChild(bytes, 0, bytes.length, ID.EBML);
  if (!ebml) throw new Error("probeTracks: not an EBML file (no EBML header found)");
  const segment = findChild(bytes, ebml.dataEnd, bytes.length, ID.SEGMENT);
  if (!segment) throw new Error("probeTracks: no Segment element found");

  const info = findChild(bytes, segment.dataStart, segment.dataEnd, ID.INFO);
  let timecodeScale = 1_000_000; // default per spec: 1ms per tick
  let durationTicks = null;
  if (info) {
    const tsEl = findChild(bytes, info.dataStart, info.dataEnd, ID.TIMECODE_SCALE);
    if (tsEl) timecodeScale = readUint(bytes, tsEl.dataStart, tsEl.dataEnd);
    const durEl = findChild(bytes, info.dataStart, info.dataEnd, ID.DURATION);
    if (durEl) durationTicks = readFloat(bytes, durEl.dataStart, durEl.dataEnd);
  }

  const tracksEl = findChild(bytes, segment.dataStart, segment.dataEnd, ID.TRACKS);
  const tracks = [];
  if (tracksEl) {
    for (const entry of walkElements(bytes, tracksEl.dataStart, tracksEl.dataEnd)) {
      if (entry.id !== ID.TRACK_ENTRY) continue;
      const track = {
        number: null,
        type: null,
        codecId: "",
        language: "und",
        name: "",
        default: true, // Matroska spec default for FlagDefault when the element is absent
        audio: null,
        video: null,
        headerStart: entry.headerStart,
        dataStart: entry.dataStart,
        dataEnd: entry.dataEnd,
      };
      for (const f of walkElements(bytes, entry.dataStart, entry.dataEnd)) {
        switch (f.id) {
          case ID.TRACK_NUMBER:
            track.number = readUint(bytes, f.dataStart, f.dataEnd);
            break;
          case ID.TRACK_TYPE:
            track.type = readUint(bytes, f.dataStart, f.dataEnd);
            break;
          case ID.CODEC_ID:
            track.codecId = readAscii(bytes, f.dataStart, f.dataEnd);
            break;
          case ID.LANGUAGE:
            track.language = readAscii(bytes, f.dataStart, f.dataEnd);
            break;
          case ID.NAME:
            track.name = readUtf8(bytes, f.dataStart, f.dataEnd);
            break;
          case ID.FLAG_DEFAULT:
            track.default = readUint(bytes, f.dataStart, f.dataEnd) === 1;
            break;
          case ID.AUDIO: {
            let channels = 1;
            let samplingFrequency = 8000;
            const chEl = findChild(bytes, f.dataStart, f.dataEnd, ID.CHANNELS);
            if (chEl) channels = readUint(bytes, chEl.dataStart, chEl.dataEnd);
            const sfEl = findChild(bytes, f.dataStart, f.dataEnd, ID.SAMPLING_FREQUENCY);
            if (sfEl) samplingFrequency = readFloat(bytes, sfEl.dataStart, sfEl.dataEnd);
            track.audio = { channels, samplingFrequency };
            break;
          }
          case ID.VIDEO: {
            const wEl = findChild(bytes, f.dataStart, f.dataEnd, ID.PIXEL_WIDTH);
            const hEl = findChild(bytes, f.dataStart, f.dataEnd, ID.PIXEL_HEIGHT);
            track.video = {
              width: wEl ? readUint(bytes, wEl.dataStart, wEl.dataEnd) : null,
              height: hEl ? readUint(bytes, hEl.dataStart, hEl.dataEnd) : null,
            };
            break;
          }
          default:
            break; // unknown/uninteresting field: skip, never an error
        }
      }
      tracks.push(track);
    }
  }

  return {
    timecodeScale,
    durationTicks,
    tracks,
    segment: { dataStart: segment.dataStart, dataEnd: segment.dataEnd },
  };
}

// ---------------------------------------------------------------- block iteration

/** Decodes a SimpleBlock/Block's leading header (track number vint + 2-byte signed relative
 * timecode + flags byte). Only the "no lacing" case is supported; lacing is flagged, not parsed. */
function decodeBlockHeader(bytes, start) {
  const { value: trackNumber, nextPos } = readSize(bytes, start); // same vint shape as a size
  const relTimecode = new DataView(bytes.buffer, bytes.byteOffset + nextPos, 2).getInt16(0, false);
  const flags = bytes[nextPos + 2];
  const lacing = (flags >> 1) & 0x3; // bits 1-2 of the flags byte
  const payloadStart = nextPos + 3;
  return { trackNumber, relTimecode, flags, lacing, payloadStart };
}

/**
 * Iterates every Cluster in the Segment, yielding one entry per SimpleBlock/Block:
 * {trackNumber, timestampMs, durationMs, data (Uint8Array view, no copy), unsupportedLacing}.
 */
export function* iterateBlocks(bytes, probe) {
  const { timecodeScale, segment } = probe;
  const msPerTick = timecodeScale / 1_000_000;
  for (const cl of walkElements(bytes, segment.dataStart, segment.dataEnd)) {
    if (cl.id !== ID.CLUSTER) continue;
    const tcEl = findChild(bytes, cl.dataStart, cl.dataEnd, ID.TIMECODE);
    const clusterTicks = tcEl ? readUint(bytes, tcEl.dataStart, tcEl.dataEnd) : 0;
    for (const el of walkElements(bytes, cl.dataStart, cl.dataEnd)) {
      if (el.id === ID.SIMPLE_BLOCK) {
        const h = decodeBlockHeader(bytes, el.dataStart);
        yield {
          trackNumber: h.trackNumber,
          timestampMs: (clusterTicks + h.relTimecode) * msPerTick,
          durationMs: null,
          data: bytes.subarray(h.payloadStart, el.dataEnd),
          unsupportedLacing: h.lacing !== 0,
          raw: { elementBytes: bytes.subarray(el.headerStart, el.dataEnd) },
        };
      } else if (el.id === ID.BLOCK_GROUP) {
        const blockEl = findChild(bytes, el.dataStart, el.dataEnd, ID.BLOCK);
        if (!blockEl) continue;
        const h = decodeBlockHeader(bytes, blockEl.dataStart);
        const durEl = findChild(bytes, el.dataStart, el.dataEnd, ID.BLOCK_DURATION);
        const durationTicks = durEl ? readUint(bytes, durEl.dataStart, durEl.dataEnd) : null;
        yield {
          trackNumber: h.trackNumber,
          timestampMs: (clusterTicks + h.relTimecode) * msPerTick,
          durationMs: durationTicks === null ? null : durationTicks * msPerTick,
          data: bytes.subarray(h.payloadStart, blockEl.dataEnd),
          unsupportedLacing: h.lacing !== 0,
          raw: { elementBytes: bytes.subarray(el.headerStart, el.dataEnd) },
        };
      }
    }
  }
}

// ---------------------------------------------------------------- subtitle cue extraction

/** cue text payloads are "identifier\nsettings\n\ntext" with the first two lines usually empty;
 * split on the first blank-line boundary and keep everything after it as the cue text. */
function cueTextFromPayload(utf8Text) {
  const idx = utf8Text.indexOf("\n\n");
  return idx === -1 ? utf8Text : utf8Text.slice(idx + 2);
}

/**
 * @returns {Array<{startMs:number, endMs:number, text:string}>} ordered by start time.
 */
export function extractSubtitleCues(bytes, probe, trackNumber) {
  const track = probe.tracks.find((t) => t.number === trackNumber);
  if (!track || track.type !== TRACK_TYPE_SUBTITLE) {
    throw new Error(`extractSubtitleCues: track ${trackNumber} is not a subtitle track`);
  }
  if (!TEXT_SUBTITLE_CODECS.has(track.codecId)) {
    throw new Error(`extractSubtitleCues: unsupported subtitle codec ${track.codecId} (text-based codecs only)`);
  }
  const cues = [];
  for (const block of iterateBlocks(bytes, probe)) {
    if (block.trackNumber !== trackNumber) continue;
    const text = cueTextFromPayload(readUtf8(block.data, 0, block.data.length));
    const startMs = block.timestampMs;
    const endMs = block.durationMs === null ? startMs : startMs + block.durationMs;
    cues.push({ startMs, endMs, text });
  }
  cues.sort((a, b) => a.startMs - b.startMs);
  return cues;
}

// ---------------------------------------------------------------- track listing (public summary)

export function listTracks(probe) {
  return probe.tracks.map((t) => ({
    number: t.number,
    kind: t.type === TRACK_TYPE_VIDEO ? "video" : t.type === TRACK_TYPE_AUDIO ? "audio" : t.type === TRACK_TYPE_SUBTITLE ? "subtitle" : "other",
    codecId: t.codecId,
    language: t.language,
    name: t.name,
    default: t.default,
  }));
}

// ---------------------------------------------------------------- EBML writer (for the filter/remux)

function encodeSize(value) {
  for (let length = 1; length <= 8; length++) {
    const max = 2 ** (7 * length) - 2;
    if (value <= max) {
      const out = new Uint8Array(length);
      let v = value;
      for (let i = length - 1; i >= 1; i--) {
        out[i] = v & 0xff;
        v = Math.floor(v / 256);
      }
      out[0] = v | (0x80 >> (length - 1));
      return out;
    }
  }
  throw new Error(`encodeSize: value ${value} too large`);
}

function concatBytes(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

/** Wraps arbitrary already-encoded `idBytes` + payload into a complete EBML element. */
function wrapElement(idBytes, payload) {
  return concatBytes([idBytes, encodeSize(payload.length), payload]);
}

const ID_BYTES = Object.freeze({
  EBML: Uint8Array.of(0x1a, 0x45, 0xdf, 0xa3),
  SEGMENT: Uint8Array.of(0x18, 0x53, 0x80, 0x67),
  TRACKS: Uint8Array.of(0x16, 0x54, 0xae, 0x6b),
  CLUSTER: Uint8Array.of(0x1f, 0x43, 0xb6, 0x75),
  TRACK_ENTRY: Uint8Array.of(0xae),
});

/**
 * Copies a TrackEntry verbatim except for its Name child, which is dropped. Chromium's MSE
 * WebM init-segment parser has been observed to reject the whole SourceBuffer append when a
 * TrackEntry's Name contains non-ASCII (multi-byte UTF-8) text — confirmed with bytes that are
 * valid EBML/UTF-8 and parse fine elsewhere (our own parser, ffmpeg). The Name is cosmetic only;
 * the player already reads track names from the untouched original buffer for its UI, so it is
 * never needed in the MSE-destined buffer.
 */
function trackEntryWithoutName(bytes, entry) {
  const kept = [];
  for (const child of walkElements(bytes, entry.dataStart, entry.dataEnd)) {
    if (child.id === ID.NAME) continue;
    kept.push(bytes.slice(child.headerStart, child.dataEnd));
  }
  return wrapElement(ID_BYTES.TRACK_ENTRY, concatBytes(kept));
}

/**
 * Builds a new, valid WebM buffer containing only the video track and one chosen audio track
 * from the source file. Every byte that ends up in the output (EBML header, Info, the kept
 * TrackEntry elements, cluster Timecodes, kept Blocks) is copied verbatim from the source —
 * this is a byte-level filter, not a re-encode, so it cannot introduce decode artifacts.
 *
 * @param {Uint8Array} bytes source file
 * @param {ReturnType<typeof probeTracks>} probe
 * @param {number} videoTrackNumber
 * @param {number} audioTrackNumber
 * @returns {Uint8Array}
 */
export function filterToSingleAudioTrack(bytes, probe, videoTrackNumber, audioTrackNumber) {
  const ebml = findChild(bytes, 0, bytes.length, ID.EBML);
  const segment = findChild(bytes, ebml.dataEnd, bytes.length, ID.SEGMENT);
  const info = findChild(bytes, segment.dataStart, segment.dataEnd, ID.INFO);
  const tracksEl = findChild(bytes, segment.dataStart, segment.dataEnd, ID.TRACKS);

  const keep = new Set([videoTrackNumber, audioTrackNumber]);
  const keptTrackEntries = probe.tracks
    .filter((t) => keep.has(t.number))
    .map((t) => trackEntryWithoutName(bytes, t));
  if (keptTrackEntries.length !== 2) {
    throw new Error(
      `filterToSingleAudioTrack: expected both track ${videoTrackNumber} and ${audioTrackNumber} to exist`
    );
  }
  const newTracks = wrapElement(ID_BYTES.TRACKS, concatBytes(keptTrackEntries));

  const newClusters = [];
  for (const cl of walkElements(bytes, segment.dataStart, segment.dataEnd)) {
    if (cl.id !== ID.CLUSTER) continue;
    const tcEl = findChild(bytes, cl.dataStart, cl.dataEnd, ID.TIMECODE);
    const keptBlocks = [];
    for (const el of walkElements(bytes, cl.dataStart, cl.dataEnd)) {
      if (el.id === ID.TIMECODE) continue;
      if (el.id !== ID.SIMPLE_BLOCK && el.id !== ID.BLOCK_GROUP) continue;
      const header =
        el.id === ID.SIMPLE_BLOCK
          ? decodeBlockHeader(bytes, el.dataStart)
          : decodeBlockHeader(bytes, findChild(bytes, el.dataStart, el.dataEnd, ID.BLOCK).dataStart);
      if (keep.has(header.trackNumber)) {
        keptBlocks.push({ relTimecode: header.relTimecode, bytes: bytes.slice(el.headerStart, el.dataEnd) });
      }
    }
    if (keptBlocks.length > 0) {
      // Matroska only requires non-decreasing timecodes per track, but Chromium's MSE WebM
      // byte-stream parser rejects a Cluster whose blocks aren't in non-decreasing timecode
      // order *across all tracks* — a stable sort here restores that without touching any
      // block's own bytes (still a byte-level filter, not a re-encode).
      keptBlocks.sort((a, b) => a.relTimecode - b.relTimecode);
      const keptChildren = [];
      if (tcEl) keptChildren.push(bytes.slice(tcEl.headerStart, tcEl.dataEnd));
      for (const b of keptBlocks) keptChildren.push(b.bytes);
      newClusters.push(wrapElement(ID_BYTES.CLUSTER, concatBytes(keptChildren)));
    }
  }

  const infoBytes = info ? bytes.slice(info.headerStart, info.dataEnd) : new Uint8Array(0);
  const segmentPayload = concatBytes([infoBytes, newTracks, ...newClusters]);
  const newSegment = wrapElement(ID_BYTES.SEGMENT, segmentPayload);
  const ebmlHeaderBytes = bytes.slice(ebml.headerStart, ebml.dataEnd);
  return concatBytes([ebmlHeaderBytes, newSegment]);
}

export const _internal = { readId, readSize, encodeSize, cueTextFromPayload, ID, TRACK_TYPE_VIDEO, TRACK_TYPE_AUDIO, TRACK_TYPE_SUBTITLE };
