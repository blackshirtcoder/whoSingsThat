import assert from "node:assert/strict";
import test from "node:test";

import {
  MusicOutputWorkflow,
  musicOutputCheckUrlForTests,
} from "../../src/who_sings_that/web/static/music-output.js";

class FakeMedia {
  constructor(src = "") {
    this.listeners = new Map();
    this.currentTime = 4;
    this.src = src;
    this.playCalls = 0;
    this.pauseCalls = 0;
    this.playBehavior = () => Promise.resolve();
  }

  getAttribute(name) { return name === "src" && this.src ? this.src : null; }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) ?? [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }
  emit(name) { for (const callback of this.listeners.get(name) ?? []) callback(); }
  play() { this.playCalls += 1; return this.playBehavior(); }
  pause() { this.pauseCalls += 1; }
}

class FakeMysteryPlayer {
  confirmations = 0;
  resets = 0;
  confirmOutputFromDedicatedCheck() { this.confirmations += 1; }
  reset() { this.resets += 1; }
}

function makeWorkflow(mediaSrc = "") {
  const media = new FakeMedia(mediaSrc);
  const player = new FakeMysteryPlayer();
  const commands = [];
  const issues = [];
  const workflow = new MusicOutputWorkflow({
    mediaElement: media,
    mysteryPlayer: player,
    sendCommand: async (type, payload) => { commands.push({ type, payload }); return {}; },
    onError: (message) => issues.push(message),
  });
  return { workflow, media, player, commands, issues };
}

test("music output uses the packaged full WAV and claims no capability before audible confirmation", async () => {
  const { workflow, media, player, commands } = makeWorkflow(
    "/static/sounds/audio-ready.wav?v=stale",
  );
  assert.equal(media.src, musicOutputCheckUrlForTests);
  assert.equal(workflow.begin(), true);
  assert.equal(media.playCalls, 1);
  assert.equal(player.confirmations, 0);
  assert.deepEqual(commands, []);
  assert.equal(await workflow.confirmHeard(), false);

  // A successful play promise and even browser playback evidence are not the
  // human's confirmation.
  await Promise.resolve();
  media.emit("playing");
  assert.equal(player.confirmations, 0);
  assert.deepEqual(commands, []);
});

test("I heard it enables preference then capability without cutting off the chime", async () => {
  const { workflow, media, player, commands } = makeWorkflow();
  workflow.begin();
  media.emit("playing");
  assert.equal(await workflow.confirmHeard(), true);
  assert.equal(player.confirmations, 1);
  assert.equal(media.pauseCalls, 0, "confirmation lets audio-ready.wav finish");
  assert.deepEqual(commands, [
    { type: "set_audio_preference", payload: { audio_enabled: true } },
    { type: "set_audio_capability", payload: { browser_audio_usable: true } },
  ]);
});

test("output check can replay and cancellation leaves the device unselected", async () => {
  const { workflow, media, player, commands } = makeWorkflow();
  workflow.begin();
  media.emit("playing");
  assert.equal(workflow.replay(), true);
  assert.equal(media.playCalls, 2);
  assert.equal(await workflow.cancel(), true);
  assert.equal(player.confirmations, 0);
  assert.equal(player.resets, 1);
  assert.ok(media.pauseCalls > 0);
  assert.deepEqual(commands, [
    { type: "set_audio_preference", payload: { audio_enabled: false } },
    { type: "set_audio_capability", payload: { browser_audio_usable: false } },
  ]);
});

test("disable is independent of interface notification sounds", async () => {
  const { workflow, player, commands } = makeWorkflow();
  const interfaceSounds = { enabled: false, events: [] };
  assert.equal(await workflow.disable(), true);
  assert.equal(player.resets, 1);
  assert.deepEqual(interfaceSounds, { enabled: false, events: [] });
  assert.deepEqual(commands.map((command) => command.type), [
    "set_audio_preference",
    "set_audio_capability",
  ]);
});

test("blocked check offers retry or cancel without claiming capability", async () => {
  const { workflow, media, player, commands, issues } = makeWorkflow();
  media.playBehavior = () => Promise.reject(new Error("blocked"));
  workflow.begin();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(workflow.state, "error");
  assert.equal(player.confirmations, 0);
  assert.deepEqual(commands, []);
  assert.match(issues.at(-1), /blocked/i);

  media.playBehavior = () => Promise.resolve();
  assert.equal(workflow.replay(), true);
  assert.equal(media.playCalls, 2);
});
