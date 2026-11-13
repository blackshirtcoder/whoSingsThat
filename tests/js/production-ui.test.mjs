import assert from "node:assert/strict";
import test from "node:test";

import {
  LocalActionCoordinator,
  buildLobbyModel,
  entryPresentation,
} from "../../src/who_sings_that/web/static/production-ui.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class FakeSounds {
  events = [];
  initializeFromUserGesture() { this.events.push("initialize"); }
  play(name) { this.events.push(`play:${name}`); return Promise.resolve(true); }
}

test("explicit connection initializes sound in the gesture path and plays only on success", async () => {
  const sounds = new FakeSounds();
  const order = sounds.events;
  const socket = {
    connect: async () => { order.push("connect"); },
    rememberMembership() {},
  };
  const coordinator = new LocalActionCoordinator({ socket, sounds });
  await coordinator.connect();
  assert.deepEqual(order, ["initialize", "connect", "play:connected"]);

  // Status callbacks and automatic reconnects do not pass through the local action.
  order.push("automatic-reconnect");
  assert.equal(order.filter((item) => item === "play:connected").length, 1);
});

test("failed explicit connection is reported without a Connected sound", async () => {
  const sounds = new FakeSounds();
  const errors = [];
  const coordinator = new LocalActionCoordinator({
    socket: {
      connect: async () => { throw new Error("WebSocket connection failed."); },
      rememberMembership() {},
    },
    sounds,
    onError: (message) => errors.push(message),
  });
  assert.equal(await coordinator.connect(), null);
  assert.deepEqual(sounds.events, ["initialize"]);
  assert.deepEqual(errors, ["WebSocket connection failed."]);
});

test("rejected commands make no success sound and sound failures do not reject game work", async () => {
  const sounds = new FakeSounds();
  const errors = [];
  const socket = {
    sendCommand: async () => { throw new Error("Server rejected the command."); },
    rememberMembership() {},
  };
  const coordinator = new LocalActionCoordinator({
    socket,
    sounds,
    onError: (message) => errors.push(message),
  });
  assert.equal(await coordinator.create("Alice"), null);
  assert.deepEqual(sounds.events, ["initialize"]);
  assert.deepEqual(errors, ["Server rejected the command."]);

  sounds.play = () => { throw new Error("device unavailable"); };
  socket.sendCommand = async () => ({ game_session_id: "private-game-id" });
  socket.rememberMembership = () => {};
  assert.deepEqual(await coordinator.join("Bob"), { game_session_id: "private-game-id" });
});

test("pending Create or Join cannot be submitted twice", async () => {
  const pending = deferred();
  let calls = 0;
  const socket = {
    sendCommand: () => { calls += 1; return pending.promise; },
    rememberMembership() {},
  };
  const coordinator = new LocalActionCoordinator({ socket, sounds: new FakeSounds() });
  const first = coordinator.join("Bob");
  const duplicate = coordinator.join("Bob");
  assert.equal(await duplicate, null);
  assert.equal(calls, 1);
  pending.resolve({ game_session_id: "private-game-id" });
  await first;
});

test("authoritative discovery selects Create or Join without exposing an ID", () => {
  const create = entryPresentation({ active_game_exists: false });
  assert.deepEqual(create, {
    eyebrow: "No active game",
    title: "Create a new game",
    detail: "Enter your display name to become the Songmaster.",
    action: "create",
  });
  const join = entryPresentation({
    active_game_exists: true,
    joinable: true,
    songmaster_display_name: "Alice",
    game_session_id: "must-not-be-used",
  });
  assert.equal(join.action, "join");
  assert.match(join.detail, /Alice is the Songmaster/);
  assert.doesNotMatch(JSON.stringify(join), /must-not-be-used/);
  assert.equal(entryPresentation({ active_game_exists: true, joinable: false }).action, null);
});

test("lobby presentation derives role and connection states but omits secret round data", () => {
  const projection = {
    active: true,
    state: "lobby",
    game_session_id: "private-game-id",
    songmaster_public_participant_id: "alice-public-id",
    recipient: { public_participant_id: "bob-public-id", is_songmaster: false },
    participants: [
      { public_participant_id: "alice-public-id", display_name: "Alice", connected: true },
      { public_participant_id: "bob-public-id", display_name: "Bob", connected: false },
    ],
    displayed_song: { artist: "Secret Artist", title: "Secret Title" },
    current_round: { answer: "must never render" },
  };
  const contestant = buildLobbyModel(projection);
  assert.equal(contestant.isSongmaster, false);
  assert.equal(contestant.songmasterName, "Alice");
  assert.deepEqual(contestant.participants, [
    { displayName: "Alice", connected: true, isSongmaster: true, isSelf: false },
    { displayName: "Bob", connected: false, isSongmaster: false, isSelf: true },
  ]);
  assert.doesNotMatch(JSON.stringify(contestant), /Secret|answer|private-game-id/);

  projection.recipient = { public_participant_id: "alice-public-id", is_songmaster: true };
  assert.equal(buildLobbyModel(projection).isSongmaster, true);
});
