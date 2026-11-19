import assert from "node:assert/strict";
import test from "node:test";

import {
  AUDIO_READY_SOUND_URL,
  NOTIFICATION_SOUND_FILES,
  SOUND_ASSET_VERSION,
  soundAssetUrl,
} from "../../src/who_sings_that/web/static/sound-assets.js";

test("all notification WAV URLs share one stable cache version", () => {
  assert.equal(SOUND_ASSET_VERSION, "1");
  assert.deepEqual(NOTIFICATION_SOUND_FILES, {
    connected: "/static/sounds/connected.wav?v=1",
    gameCreated: "/static/sounds/game-created.wav?v=1",
    welcome: "/static/sounds/welcome.wav?v=1",
    audioReady: "/static/sounds/audio-ready.wav?v=1",
  });
  assert.ok(
    Object.values(NOTIFICATION_SOUND_FILES).every(
      (url) => new URL(url, "https://example.test").searchParams.get("v") === SOUND_ASSET_VERSION,
    ),
  );
});

test("the dedicated output check reuses the versioned audio-ready asset", () => {
  assert.equal(AUDIO_READY_SOUND_URL, NOTIFICATION_SOUND_FILES.audioReady);
  assert.equal(AUDIO_READY_SOUND_URL, soundAssetUrl("audio-ready.wav"));
});
