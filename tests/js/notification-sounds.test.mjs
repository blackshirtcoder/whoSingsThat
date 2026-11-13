import assert from "node:assert/strict";
import test from "node:test";

import {
  NOTIFICATION_SOUND_FILES,
  NotificationSoundManager,
} from "../../src/who_sings_that/web/static/notification-sounds.js";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
}

class FakeSource {
  starts = 0;
  stops = 0;
  connect() {}
  addEventListener() {}
  start() { this.starts += 1; }
  stop() { this.stops += 1; }
}

class FakeAudioContext {
  static instances = [];
  state = "suspended";
  destination = {};
  sources = [];
  resumeCalls = 0;

  constructor() { FakeAudioContext.instances.push(this); }
  resume() { this.resumeCalls += 1; this.state = "running"; return Promise.resolve(); }
  decodeAudioData(data) { return Promise.resolve({ decoded: data.byteLength }); }
  createBufferSource() {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }
}

function successfulFetch(urls) {
  return async (url) => {
    urls.push(url);
    return { ok: true, arrayBuffer: async () => new ArrayBuffer(12) };
  };
}

test("logical event mappings use four replaceable packaged files", () => {
  assert.deepEqual(NOTIFICATION_SOUND_FILES, {
    connected: "/static/sounds/connected.wav",
    gameCreated: "/static/sounds/game-created.wav",
    welcome: "/static/sounds/welcome.wav",
    audioReady: "/static/sounds/audio-ready.wav",
  });
});

test("gesture initialization resumes audio before later successful playback", async () => {
  FakeAudioContext.instances = [];
  const urls = [];
  const manager = new NotificationSoundManager({
    AudioContextImpl: FakeAudioContext,
    fetchImpl: successfulFetch(urls),
    storage: new MemoryStorage(),
  });

  const activation = manager.initializeFromUserGesture();
  const context = FakeAudioContext.instances[0];
  assert.equal(context.resumeCalls, 1);
  assert.equal(urls.length, 0, "success asset is not played before acknowledgment");
  await activation;
  assert.equal(await manager.play("connected"), true);
  assert.deepEqual(urls, ["/static/sounds/connected.wav"]);
  assert.equal(context.sources[0].starts, 1);
});

test("blocked and missing sound assets report failure without throwing", async () => {
  const issues = [];
  class BlockedContext extends FakeAudioContext {
    resume() { this.resumeCalls += 1; return Promise.reject(new Error("blocked")); }
  }
  const blocked = new NotificationSoundManager({
    AudioContextImpl: BlockedContext,
    fetchImpl: successfulFetch([]),
    storage: new MemoryStorage(),
    onIssue: (message) => issues.push(message),
  });
  blocked.initializeFromUserGesture();
  assert.equal(await blocked.play("welcome"), false);

  const missing = new NotificationSoundManager({
    AudioContextImpl: FakeAudioContext,
    fetchImpl: async () => ({ ok: false }),
    storage: new MemoryStorage(),
    onIssue: (message) => issues.push(message),
  });
  await missing.initializeFromUserGesture();
  assert.equal(await missing.play("gameCreated"), false);
  assert.equal(issues.length, 2);
});

test("interface preference is independent and disabling stops only its own sources", async () => {
  const storage = new MemoryStorage();
  const mysterySong = { pauseCalls: 0, pause() { this.pauseCalls += 1; } };
  const manager = new NotificationSoundManager({
    AudioContextImpl: FakeAudioContext,
    fetchImpl: successfulFetch([]),
    storage,
  });
  await manager.initializeFromUserGesture();
  await manager.play("welcome");
  manager.setEnabled(false);
  assert.equal(manager.enabled, false);
  assert.equal(mysterySong.pauseCalls, 0);
  assert.equal(await manager.play("connected"), false);
});
