// Browser-side player: wires the demux.mjs track filter to a <video> element via MediaSource,
// with a separate subtitle-cue overlay (not part of the MSE stream — see docs/05 for why).
//
// Scope boundary (documented): only the "WebM-native" codec set is supported here (VP8/VP9/AV1
// video, Opus/Vorbis audio) — the set MSE can play without any container remux at all beyond the
// video+one-audio-track byte filter in demux.mjs. H.264/AAC-in-MKV and other legacy codec
// combinations need an actual ISOBMFF (fMP4) remux step, which is a documented follow-up, not
// implemented here.
import { probeTracks, listTracks, extractSubtitleCues, filterToSingleAudioTrack } from "./demux.mjs";

const MSE_VIDEO_CODEC = { V_VP8: "vp8", V_VP9: "vp9", V_AV1: "av01.0.01M.08" };
const MSE_AUDIO_CODEC = { A_OPUS: "opus", A_VORBIS: "vorbis" };

function mimeTypeFor(videoCodecId, audioCodecId) {
  const v = MSE_VIDEO_CODEC[videoCodecId];
  const a = MSE_AUDIO_CODEC[audioCodecId];
  if (!v || !a) {
    throw new Error(
      `Unsupported codec combination for this player: video=${videoCodecId} audio=${audioCodecId} ` +
        `(only WebM-native VP8/VP9/AV1 + Opus/Vorbis are supported in this build)`
    );
  }
  return `video/webm; codecs="${v},${a}"`;
}

/**
 * @param {{videoEl: HTMLVideoElement, url: string}} opts
 */
export async function createPlayer({ videoEl, url }) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`createPlayer: fetch ${url} failed with ${resp.status}`);
  const buf = new Uint8Array(await resp.arrayBuffer());
  const probe = probeTracks(buf);
  const summary = listTracks(probe);

  const videoTrack = summary.find((t) => t.kind === "video");
  const audioTracks = summary.filter((t) => t.kind === "audio");
  const subtitleTracks = summary.filter((t) => t.kind === "subtitle");
  if (!videoTrack) throw new Error("createPlayer: no video track found");
  if (audioTracks.length === 0) throw new Error("createPlayer: no audio track found");

  let currentAudioNumber = null;
  let currentSubtitleNumber = null;
  let currentCues = [];
  let mediaSource = null;

  function cueTextAt(timeMs) {
    const hit = currentCues.find((c) => timeMs >= c.startMs && timeMs < c.endMs);
    return hit ? hit.text : "";
  }

  /** (Re)builds the MSE source for the given audio track, preserving playback position. */
  async function selectAudio(audioNumber) {
    const audioMeta = probe.tracks.find((t) => t.number === audioNumber);
    if (!audioMeta) throw new Error(`selectAudio: no such audio track ${audioNumber}`);
    const videoMeta = probe.tracks.find((t) => t.number === videoTrack.number);
    const mimeType = mimeTypeFor(videoMeta.codecId, audioMeta.codecId);
    const filtered = filterToSingleAudioTrack(buf, probe, videoTrack.number, audioNumber);

    const resumeAt = mediaSource ? videoEl.currentTime : 0;
    const wasPlaying = mediaSource ? !videoEl.paused : false;

    if (mediaSource && videoEl.src) URL.revokeObjectURL(videoEl.src);
    mediaSource = new MediaSource();
    videoEl.src = URL.createObjectURL(mediaSource);

    await new Promise((resolve, reject) => {
      mediaSource.addEventListener(
        "sourceopen",
        () => {
          let sourceBuffer;
          try {
            sourceBuffer = mediaSource.addSourceBuffer(mimeType);
          } catch (e) {
            reject(e);
            return;
          }
          sourceBuffer.addEventListener("error", (e) => reject(e), { once: true });
          sourceBuffer.addEventListener(
            "updateend",
            () => {
              try {
                mediaSource.endOfStream();
              } catch {
                /* harmless if already ended */
              }
              resolve();
            },
            { once: true }
          );
          sourceBuffer.appendBuffer(filtered);
        },
        { once: true }
      );
    });

    currentAudioNumber = audioNumber;
    if (resumeAt > 0) {
      await new Promise((resolve) => {
        const onSeeked = () => {
          videoEl.removeEventListener("seeked", onSeeked);
          resolve();
        };
        videoEl.addEventListener("seeked", onSeeked);
        videoEl.currentTime = resumeAt;
      });
    }
    if (wasPlaying) await videoEl.play();
  }

  function selectSubtitle(subtitleNumber) {
    if (subtitleNumber === null) {
      currentSubtitleNumber = null;
      currentCues = [];
      return;
    }
    const meta = probe.tracks.find((t) => t.number === subtitleNumber);
    if (!meta) throw new Error(`selectSubtitle: no such subtitle track ${subtitleNumber}`);
    currentCues = extractSubtitleCues(buf, probe, subtitleNumber);
    currentSubtitleNumber = subtitleNumber;
  }

  const defaultAudio = audioTracks.find((t) => t.default) || audioTracks[0];
  await selectAudio(defaultAudio.number);
  const defaultSubtitle = subtitleTracks.find((t) => t.default);
  if (defaultSubtitle) selectSubtitle(defaultSubtitle.number);

  return {
    videoTrack,
    audioTracks,
    subtitleTracks,
    selectAudio,
    selectSubtitle,
    get currentAudioNumber() {
      return currentAudioNumber;
    },
    get currentSubtitleNumber() {
      return currentSubtitleNumber;
    },
    get subtitleText() {
      return cueTextAt(videoEl.currentTime * 1000);
    },
    dispose() {},
  };
}
