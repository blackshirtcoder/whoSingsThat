import assert from "node:assert/strict";
import test from "node:test";

import {
  AudioState,
  BrowserAudioPlayer,
} from "../../src/who_sings_that/web/static/audio-player.js";

const GAME = "11111111-1111-4111-8111-111111111111";
const PREP1 = "22222222-2222-4222-8222-222222222222";
const PREP2 = "33333333-3333-4333-8333-333333333333";
const ROUND1 = "44444444-4444-4444-8444-444444444444";
const ROUND2 = "55555555-5555-4555-8555-555555555555";

class FakeMedia {
  constructor() {
    this.listeners = new Map();
    this.src = "";
    this.currentTime = 0;
    this.readyState = 0;
    this.paused = true;
    this.volume = 1;
    this.loadCalls = 0;
    this.playCalls = 0;
    this.pauseCalls = 0;
    this.playBehavior = async () => {
      this.paused = false;
      queueMicrotask(() => this.emit("playing"));
    };
  }

  addEventListener(name, listener, options = {}) {
    const list = this.listeners.get(name) ?? [];
    list.push({ listener, once: Boolean(options.once) });
    this.listeners.set(name, list);
  }

  removeEventListener(name, listener) {
    this.listeners.set(
      name,
      (this.listeners.get(name) ?? []).filter((item) => item.listener !== listener),
    );
  }

  emit(name) {
    for (const item of [...(this.listeners.get(name) ?? [])]) {
      item.listener({ type: name });
      if (item.once) this.removeEventListener(name, item.listener);
    }
  }

  load() {
    this.loadCalls += 1;
  }

  play() {
    this.playCalls += 1;
    return this.playBehavior();
  }

  pause() {
    this.pauseCalls += 1;
    this.paused = true;
  }

  removeAttribute(name) {
    if (name === "src") this.src = "";
  }
}

class FakeTimers {
  constructor() {
    this.nextId = 0;
    this.timers = new Map();
  }

  set = (callback, milliseconds) => {
    const id = ++this.nextId;
    this.timers.set(id, { callback, milliseconds });
    return id;
  };

  clear = (id) => this.timers.delete(id);

  fireByDelay(milliseconds) {
    const match = [...this.timers.entries()].find(([, timer]) => timer.milliseconds === milliseconds);
    assert.ok(match, `timer with ${milliseconds}ms delay exists`);
    const [id, timer] = match;
    this.timers.delete(id);
    timer.callback();
  }

  callbackByDelay(milliseconds) {
    const match = [...this.timers.values()].find(
      (timer) => timer.milliseconds === milliseconds,
    );
    assert.ok(match, `timer with ${milliseconds}ms delay exists`);
    return match.callback;
  }
}

function preparePayload(preparationId = PREP1) {
  return {
    game_session_id: GAME,
    preparation_id: preparationId,
    media_url: `/media/opaque-${preparationId.slice(0, 4)}`,
    effective_playback_offset_seconds: "30.5",
    playback_duration_seconds: 12,
    preparation_timeout_seconds: 5,
  };
}

function playPayload(preparationId = PREP1, roundId = ROUND1) {
  return {
    game_session_id: GAME,
    preparation_id: preparationId,
    round_id: roundId,
    effective_playback_offset_seconds: "30.5",
    playback_duration_seconds: 12,
    authoritative_playback_started_at: "2026-01-01T00:00:00Z",
  };
}

function stopPayload(preparationId = PREP1, roundId = ROUND1) {
  return {
    game_session_id: GAME,
    preparation_id: preparationId,
    round_id: roundId,
    reason: "accepted_buzz",
  };
}

async function makeEnabledPlayer(overrides = {}) {
  const media = overrides.media ?? new FakeMedia();
  const timers = overrides.timers ?? new FakeTimers();
  const issues = [];
  const player = new BrowserAudioPlayer({
    mediaElement: media,
    setTimer: timers.set,
    clearTimer: timers.clear,
    createUnlockSource: () => ({ src: "blob:audible-test-tone", cleanup: () => {} }),
    onRuntimeIssue: (message) => issues.push(message),
  });
  assert.equal(await player.initializeFromUserGesture(), true);
  return { player, media, timers, issues };
}

async function completePreparation(player, media, payload = preparePayload()) {
  const resultPromise = player.prepare(payload);
  assert.equal(media.paused, true);
  media.emit("loadedmetadata");
  assert.equal(media.currentTime, 30.5);
  media.emit("seeked");
  media.readyState = 3;
  media.emit("canplay");
  return resultPromise;
}

test("Prepare loads, seeks, remains silent, and reports READY only after media evidence", async () => {
  const { player, media } = await makeEnabledPlayer();
  const unlockPlayCalls = media.playCalls;
  const resultPromise = player.prepare(preparePayload());
  assert.equal(player.status.state, AudioState.PREPARING);
  assert.equal(media.playCalls, unlockPlayCalls);
  media.emit("loadedmetadata");
  media.emit("seeked");
  assert.equal(player.status.state, AudioState.PREPARING);
  media.readyState = 3;
  media.emit("canplay");
  const result = await resultPromise;
  assert.equal(result.outcome, "ready");
  assert.equal(result.identity.preparationId, PREP1);
  assert.equal(player.status.state, AudioState.READY);
});

test("Prepare reports FAILED on load error and on preparation deadline", async () => {
  const first = await makeEnabledPlayer();
  const failed = first.player.prepare(preparePayload());
  first.media.error = { code: 4 };
  first.media.emit("error");
  assert.equal((await failed).outcome, "failed");
  assert.equal(first.player.status.state, AudioState.FAILED);

  const second = await makeEnabledPlayer();
  const timedOut = second.player.prepare(preparePayload());
  second.timers.fireByDelay(5000);
  assert.equal((await timedOut).outcome, "failed");
});

test("Canceled preparation cannot report late READY", async () => {
  const { player, media } = await makeEnabledPlayer();
  const result = player.prepare(preparePayload());
  assert.equal(player.cancelPreparation(), true);
  media.emit("loadedmetadata");
  media.emit("seeked");
  media.readyState = 3;
  media.emit("canplay");
  assert.equal((await result).outcome, "canceled");
  assert.equal(player.status.state, AudioState.STOPPED);
});

test("Stale events from an old preparation cannot affect a newer preparation", async () => {
  const { player, media } = await makeEnabledPlayer();
  const oldResult = player.prepare(preparePayload(PREP1));
  const newResult = player.prepare(preparePayload(PREP2));
  assert.equal((await oldResult).outcome, "canceled");
  media.emit("loadedmetadata");
  media.emit("seeked");
  media.readyState = 3;
  media.emit("canplay");
  const result = await newResult;
  assert.equal(result.outcome, "ready");
  assert.equal(player.status.preparationId, PREP2);
});

test("Play requires matching prepared identity and invokes media playback", async () => {
  const { player, media, issues } = await makeEnabledPlayer();
  await completePreparation(player, media);
  assert.equal(await player.play(playPayload(PREP2, ROUND2)), false);
  assert.match(issues.at(-1), /matching prepared audio/);
  assert.equal(
    await player.play({ ...playPayload(), playback_duration_seconds: 99 }),
    false,
  );
  const priorCalls = media.playCalls;
  assert.equal(await player.play(playPayload()), true);
  assert.equal(media.playCalls, priorCalls + 1);
  assert.equal(player.status.state, AudioState.PLAYING);
});

test("Rejected play Promise becomes FAILED without inventing PLAYING", async () => {
  const { player, media, issues } = await makeEnabledPlayer();
  await completePreparation(player, media);
  media.playBehavior = async () => {
    throw new Error("NotAllowedError");
  };
  assert.equal(await player.play(playPayload()), false);
  assert.equal(player.status.state, AudioState.FAILED);
  assert.match(issues.at(-1), /no post-READY failure report/);
});

test("Stop pauses playback, is idempotent, and stale Stop cannot interrupt a new round", async () => {
  const { player, media } = await makeEnabledPlayer();
  await completePreparation(player, media);
  await player.play(playPayload());
  assert.equal(player.stop(stopPayload()), true);
  const pauses = media.pauseCalls;
  assert.equal(player.stop(stopPayload()), false);
  assert.equal(media.pauseCalls, pauses);

  await completePreparation(player, media, preparePayload(PREP2));
  await player.play(playPayload(PREP2, ROUND2));
  assert.equal(player.stop(stopPayload(PREP1, ROUND1)), false);
  assert.equal(player.status.state, AudioState.PLAYING);
});

test("Stop invalidates a pending play Promise so it cannot restart", async () => {
  const { player, media } = await makeEnabledPlayer();
  await completePreparation(player, media);
  let resolvePlay;
  media.playBehavior = () => new Promise((resolve) => {
    resolvePlay = resolve;
  });
  const playing = player.play(playPayload());
  assert.equal(player.stop(stopPayload()), true);
  resolvePlay();
  assert.equal(await playing, false);
  assert.equal(media.paused, true);
  assert.equal(player.status.state, AudioState.STOPPED);
});

test("Local duration failsafe uses N + 2 seconds and stops media without server Stop", async () => {
  const first = await makeEnabledPlayer();
  await completePreparation(first.player, first.media);
  await first.player.play(playPayload());
  assert.equal(
    [...first.timers.timers.values()].some((timer) => timer.milliseconds === 12000),
    false,
  );
  first.timers.fireByDelay(14000);
  assert.equal(first.player.status.state, AudioState.STOPPED);
  assert.equal(first.media.paused, true);
});

test("Server Stop cancels the local duration failsafe", async () => {
  const { player, media, timers } = await makeEnabledPlayer();
  await completePreparation(player, media);
  await player.play(playPayload());
  assert.equal(timers.timers.size, 1);

  assert.equal(player.stop(stopPayload()), true);

  assert.equal(timers.timers.size, 0);
  assert.equal(player.status.state, AudioState.STOPPED);
});

test("A stale failsafe callback cannot stop a subsequent round", async () => {
  const { player, media, timers } = await makeEnabledPlayer();
  await completePreparation(player, media);
  await player.play(playPayload());
  const staleFailsafe = timers.callbackByDelay(14000);
  assert.equal(player.stop(stopPayload()), true);

  await completePreparation(player, media, preparePayload(PREP2));
  await player.play(playPayload(PREP2, ROUND2));
  staleFailsafe();

  assert.equal(player.status.state, AudioState.PLAYING);
  assert.equal(player.status.roundId, ROUND2);
  assert.equal(media.paused, false);
});

test("Natural media completion is reported conservatively", async () => {
  const second = await makeEnabledPlayer();
  await completePreparation(second.player, second.media);
  await second.player.play(playPayload());

  second.media.emit("ended");

  assert.equal(second.player.status.state, AudioState.STOPPED);
  assert.match(second.issues.at(-1), /full-interval playback is not assumed/);
});

test("Connection reset stops audio, clears capability, and permits a fresh preparation only after re-enable", async () => {
  const { player, media } = await makeEnabledPlayer();
  await completePreparation(player, media);
  await player.play(playPayload());
  player.reset();
  assert.equal(player.status.browserAudioUsable, false);
  assert.equal(player.status.state, AudioState.IDLE);
  assert.equal(media.paused, true);
  assert.equal(await player.play(playPayload()), false);
  const rejected = await player.prepare(preparePayload(PREP2));
  assert.equal(rejected.outcome, "failed");
  assert.equal(rejected.reason, "audio_not_initialized");
  assert.equal(await player.initializeFromUserGesture(), true);
  const prepared = await completePreparation(player, media, preparePayload(PREP2));
  assert.equal(prepared.outcome, "ready");
});
