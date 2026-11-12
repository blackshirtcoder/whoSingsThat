import assert from "node:assert/strict";
import test from "node:test";

import {
  AudioCommandRouter,
  preparationReport,
} from "../../src/who_sings_that/web/static/audio-protocol.js";

const identity = {
  gameSessionId: "11111111-1111-4111-8111-111111111111",
  preparationId: "22222222-2222-4222-8222-222222222222",
};

test("READY and FAILED reports contain only the authoritative preparation identity", () => {
  assert.deepEqual(preparationReport({ outcome: "ready", identity }), {
    type: "audio_preparation_ready",
    payload: { preparation_id: identity.preparationId },
  });
  assert.deepEqual(preparationReport({ outcome: "failed", identity, reason: "private detail" }), {
    type: "audio_preparation_failed",
    payload: { preparation_id: identity.preparationId },
  });
  assert.equal(preparationReport({ outcome: "canceled", identity }), null);
});

test("router reports preparation terminal state and routes Play/Stop without private logging", async () => {
  const calls = [];
  const sent = [];
  const player = {
    async prepare(payload) {
      calls.push(["prepare", payload]);
      return { outcome: "ready", identity };
    },
    async play(payload) {
      calls.push(["play", payload]);
      return true;
    },
    stop(payload) {
      calls.push(["stop", payload]);
      return true;
    },
  };
  const router = new AudioCommandRouter({
    player,
    sendCommand: async (type, payload) => sent.push([type, payload]),
  });
  await router.handle("prepare_audio", { media_url: "/media/private-capability" });
  await router.handle("play_audio", { round_id: "round" });
  await router.handle("stop_audio", { reason: "accepted_buzz" });
  assert.deepEqual(sent, [["audio_preparation_ready", { preparation_id: identity.preparationId }]]);
  assert.deepEqual(calls.map(([type]) => type), ["prepare", "play", "stop"]);
  assert.doesNotMatch(JSON.stringify(sent), /private-capability/);
});

test("router suppresses late preparation reports after connection loss", async () => {
  const sent = [];
  const router = new AudioCommandRouter({
    player: {
      prepare: async () => ({ outcome: "ready", identity }),
    },
    isConnected: () => false,
    sendCommand: async (...args) => sent.push(args),
  });
  await router.handle("prepare_audio", {});
  assert.deepEqual(sent, []);
});
