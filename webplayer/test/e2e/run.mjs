// Playwright-driven regression tests for the browser player, against a real headless Chromium
// and the real synthetic fixture, served over real HTTP Range requests by range_server.mjs.
//
// Not a framework-based test file (no local Playwright Test runner installed) — a small
// self-contained runner so `node test/e2e/run.mjs` works standalone. Each check throws on
// failure; failures are collected and reported at the end so one bad check doesn't hide others.
import { chromium } from "playwright";
import { startRangeServer } from "./range_server.mjs";

const results = [];

async function check(name, fn) {
  const start = Date.now();
  try {
    await fn();
    results.push({ name, ok: true, ms: Date.now() - start });
    console.log(`ok   ${name} (${Date.now() - start}ms)`);
  } catch (e) {
    results.push({ name, ok: false, ms: Date.now() - start, error: e });
    console.log(`FAIL ${name}`);
    console.log("     " + (e && e.stack ? e.stack.split("\n").join("\n     ") : String(e)));
  }
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timed out after ${ms}ms: ${label}`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

async function runChecks(page, pageErrors) {
  // -------------------------------------------------- track listing
  await check("probed tracks match the known fixture layout", async () => {
    const tracks = await page.evaluate(() => ({
      video: window.__player.videoTrack,
      audio: window.__player.audioTracks,
      subtitle: window.__player.subtitleTracks,
    }));
    assertEqual(tracks.video.codecId, "V_VP9", "video codec");
    assertEqual(tracks.audio.length, 3, "audio track count");
    assertEqual(tracks.audio.map((t) => t.language).join(","), "eng,jpn,fre", "audio languages");
    assertEqual(tracks.audio.find((t) => t.language === "jpn").name, "日本語", "jpn title (non-ASCII)");
    assertEqual(tracks.subtitle.length, 2, "subtitle track count");
  });

  // -------------------------------------------------- default playback actually plays
  await check("default audio track is English and playback advances", async () => {
    const defaultAudioNumber = await page.evaluate(() => window.__player.currentAudioNumber);
    const audioTracks = await page.evaluate(() => window.__player.audioTracks);
    const defaultTrack = audioTracks.find((t) => t.number === defaultAudioNumber);
    assertEqual(defaultTrack.language, "eng", "default audio language");

    await page.evaluate(() => document.querySelector("video").play());
    const t0 = await page.evaluate(() => document.querySelector("video").currentTime);
    await page.waitForTimeout(700);
    const t1 = await page.evaluate(() => document.querySelector("video").currentTime);
    if (!(t1 > t0)) throw new Error(`expected currentTime to advance, got ${t0} -> ${t1}`);
  });

  // -------------------------------------------------- subtitle overlay matches expected cues
  await check("English subtitles show the expected text at known timestamps", async () => {
    await page.evaluate(() => {
      const v = document.querySelector("video");
      v.pause();
      window.__player.selectSubtitle(window.__player.subtitleTracks.find((t) => t.language === "eng").number);
      v.currentTime = 1.0; // inside the "Hello" cue (0.5s-2.0s)
    });
    await page.waitForFunction(() => document.querySelector("video").currentTime >= 0.9);
    const textAt1s = await page.evaluate(() => window.__player.subtitleText);
    assertEqual(textAt1s, "Hello", "subtitle text at t=1.0s (eng)");

    await page.evaluate(() => {
      document.querySelector("video").currentTime = 3.0; // inside "World" (2.5-4.0s)
    });
    await page.waitForFunction(() => document.querySelector("video").currentTime >= 2.9);
    const textAt3s = await page.evaluate(() => window.__player.subtitleText);
    assertEqual(textAt3s, "World", "subtitle text at t=3.0s (eng)");

    await page.evaluate(() => {
      document.querySelector("video").currentTime = 2.2; // gap between cues
    });
    await page.waitForFunction(() => document.querySelector("video").currentTime >= 2.1);
    const textInGap = await page.evaluate(() => window.__player.subtitleText);
    assertEqual(textInGap, "", "subtitle text in the gap between cues");
  });

  await check("switching to the French subtitle track changes the overlay text", async () => {
    await page.evaluate(() => {
      const v = document.querySelector("video");
      window.__player.selectSubtitle(window.__player.subtitleTracks.find((t) => t.language === "fre").number);
      v.currentTime = 1.0;
    });
    await page.waitForFunction(() => document.querySelector("video").currentTime >= 0.9);
    const text = await page.evaluate(() => window.__player.subtitleText);
    assertEqual(text, "Bonjour", "subtitle text at t=1.0s (fre)");
  });

  await check("selecting 'no subtitles' clears the overlay", async () => {
    await page.evaluate(() => {
      window.__player.selectSubtitle(null);
    });
    const text = await page.evaluate(() => window.__player.subtitleText);
    assertEqual(text, "", "subtitle text after selecting none");
    assertEqual(await page.evaluate(() => window.__player.currentSubtitleNumber), null, "currentSubtitleNumber after none");
  });

  // -------------------------------------------------- audio track switching keeps playback alive
  await check("switching audio track to Japanese keeps the video element alive and advancing", async () => {
    await page.evaluate(async () => {
      const v = document.querySelector("video");
      v.pause();
      v.currentTime = 2.0;
    });
    await page.waitForTimeout(100);
    await page.evaluate(async () => {
      await window.__player.selectAudio(window.__player.audioTracks.find((t) => t.language === "jpn").number);
    });
    const currentAudio = await page.evaluate(() => window.__player.currentAudioNumber);
    const jpnNumber = await page.evaluate(() => window.__player.audioTracks.find((t) => t.language === "jpn").number);
    assertEqual(currentAudio, jpnNumber, "currentAudioNumber after switching to jpn");

    const resumedAt = await page.evaluate(() => document.querySelector("video").currentTime);
    if (resumedAt < 1.5) throw new Error(`expected playback position to be preserved near 2.0s, got ${resumedAt}`);

    await page.evaluate(() => document.querySelector("video").play());
    const t0 = await page.evaluate(() => document.querySelector("video").currentTime);
    await page.waitForTimeout(700);
    const t1 = await page.evaluate(() => document.querySelector("video").currentTime);
    if (!(t1 > t0)) throw new Error(`expected currentTime to advance after track switch, got ${t0} -> ${t1}`);
  });

  await check("switching audio track to French also works", async () => {
    await page.evaluate(async () => {
      await window.__player.selectAudio(window.__player.audioTracks.find((t) => t.language === "fre").number);
    });
    const currentAudio = await page.evaluate(() => window.__player.currentAudioNumber);
    const freNumber = await page.evaluate(() => window.__player.audioTracks.find((t) => t.language === "fre").number);
    assertEqual(currentAudio, freNumber, "currentAudioNumber after switching to fre");
  });

  await check("no uncaught page errors occurred during the whole run", async () => {
    if (pageErrors.length > 0) throw new Error(`page errors: ${pageErrors.join(" | ")}`);
  });
}

async function main() {
  const watchdog = setTimeout(() => {
    console.error("WATCHDOG: runner did not finish in time, forcing exit");
    process.exit(2);
  }, 60000);

  let server, browser;
  try {
    server = await startRangeServer(0);
    const port = server.address().port;
    const base = `http://127.0.0.1:${port}`;

    browser = await chromium.launch({ args: ["--autoplay-policy=no-user-gesture-required"] });
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(String(e)));
    page.on("console", (msg) => {
      console.log(`  [page:${msg.type()}] ${msg.text()}`);
      if (msg.type() === "error") pageErrors.push(msg.text());
    });

    await page.goto(`${base}/test/e2e/harness.html`);
    await page.waitForFunction("window.__ready !== undefined", { timeout: 10000 });
    const initError = await page.evaluate(() => window.__error || null);
    if (initError) throw new Error(`player failed to initialize: ${initError}`);
    await withTimeout(page.evaluate(() => window.__ready), 15000, "player init (createPlayer)");
    console.log("player initialized");

    await runChecks(page, pageErrors);
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (server) server.close();
    clearTimeout(watchdog);
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exitCode = failed.length > 0 ? 1 : 0;
}

main().catch((e) => {
  console.error("e2e runner crashed:", e);
  process.exitCode = 1;
});
