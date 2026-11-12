import assert from "node:assert/strict";
import test from "node:test";

import {
  WstSocketClient,
  storageKeysForTests,
} from "../../src/who_sings_that/web/static/socket-client.js";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class FakeWebSocket {
  static OPEN = 1;
  static CLOSING = 2;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    this.listeners = new Map();
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }

  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }

  emit(name, value) {
    for (const callback of this.listeners.get(name) ?? []) callback(value);
  }

  serverMessage(message) {
    this.emit("message", { data: JSON.stringify(message) });
  }

  send(raw) { this.sent.push(JSON.parse(raw)); }
  close(code = 1000) {
    this.readyState = FakeWebSocket.CLOSING;
    this.emit("close", { code });
  }
}

function transportConnected(socket) {
  socket.serverMessage({
    version: 1,
    request_id: null,
    type: "transport_connected",
    ok: true,
    result: { connection_id: "server-generated", max_message_bytes: 4096 },
  });
}

function respond(socket, request, result = {}) {
  socket.serverMessage({
    version: 1,
    request_id: request.request_id,
    type: "result",
    ok: true,
    result,
  });
}

async function connectedClient(callbacks = {}, storage = new MemoryStorage()) {
  FakeWebSocket.instances = [];
  const client = new WstSocketClient({
    url: "ws://example/ws",
    WebSocketImpl: FakeWebSocket,
    storage,
    ...callbacks,
  });
  const connecting = client.connect();
  const socket = FakeWebSocket.instances[0];
  transportConnected(socket);
  await Promise.resolve();
  const identity = socket.sent.at(-1);
  assert.equal(identity.type, "establish_identity");
  respond(socket, identity, { browser_token: "opaque-private-browser-token" });
  await connecting;
  return { client, socket, storage };
}

test("identity token persists but is never manufactured as a connection ID", async () => {
  const { client, storage } = await connectedClient();
  assert.equal(storage.getItem(storageKeysForTests.token), "opaque-private-browser-token");
  assert.equal(client.connectionId, "server-generated");
  assert.equal(FakeWebSocket.instances[0].sent[0].payload.browser_token, undefined);
  client.disconnect();
});

test("command results correlate correctly even when replies are reversed", async () => {
  const { client, socket } = await connectedClient();
  const firstPromise = client.sendCommand("first", {});
  const secondPromise = client.sendCommand("second", {});
  const [first, second] = socket.sent.slice(-2);
  respond(socket, second, { order: 2 });
  respond(socket, first, { order: 1 });
  assert.deepEqual(await firstPromise, { order: 1 });
  assert.deepEqual(await secondPromise, { order: 2 });
  client.disconnect();
});

test("unsolicited state and audio commands route separately and malformed messages fail safely", async () => {
  const states = [];
  const discoveries = [];
  const audio = [];
  const issues = [];
  const { client, socket } = await connectedClient({
    onState: (payload) => states.push(payload),
    onDiscovery: (payload) => discoveries.push(payload),
    onAudioCommand: (type, payload) => audio.push([type, payload]),
    onProtocolIssue: (message) => issues.push(message),
  });
  socket.serverMessage({
    version: 1,
    request_id: null,
    type: "game_discovery",
    payload: { active_game_exists: true, joinable: true, state: "lobby" },
  });
  socket.serverMessage({
    version: 1,
    request_id: null,
    type: "state_update",
    payload: { revision: 2, state: "LOBBY" },
  });
  socket.serverMessage({
    version: 1,
    request_id: null,
    type: "prepare_audio",
    payload: { preparation_id: "opaque" },
  });
  socket.emit("message", { data: "not-json" });
  assert.equal(states.length, 1);
  assert.equal(discoveries.length, 1);
  assert.equal(audio[0][0], "prepare_audio");
  assert.match(issues.at(-1), /non-JSON/);
  client.disconnect();
});

test("remembered membership reconnects after identity and does not expose the token", async () => {
  const storage = new MemoryStorage();
  storage.setItem(storageKeysForTests.token, "remembered-browser-token");
  storage.setItem(storageKeysForTests.game, "11111111-1111-4111-8111-111111111111");
  FakeWebSocket.instances = [];
  const client = new WstSocketClient({
    url: "ws://example/ws",
    WebSocketImpl: FakeWebSocket,
    storage,
  });
  const connecting = client.connect();
  const socket = FakeWebSocket.instances[0];
  transportConnected(socket);
  await Promise.resolve();
  const identity = socket.sent.at(-1);
  assert.equal(identity.payload.browser_token, "remembered-browser-token");
  respond(socket, identity, { browser_token: "remembered-browser-token" });
  await Promise.resolve();
  await Promise.resolve();
  const reconnect = socket.sent.at(-1);
  assert.equal(reconnect.type, "reconnect");
  assert.deepEqual(reconnect.payload, {
    game_session_id: "11111111-1111-4111-8111-111111111111",
  });
  respond(socket, reconnect, { game_session_id: reconnect.payload.game_session_id });
  await connecting;
  client.disconnect();
});

test("replacement closes locally without an automatic reconnect loop", async () => {
  const timers = [];
  const losses = [];
  const issues = [];
  const { client, socket } = await connectedClient({
    setTimer: (callback, delay) => {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimer: () => {},
    onConnectionLost: (loss) => losses.push(loss),
    onProtocolIssue: (message) => issues.push(message),
  });
  socket.emit("close", { code: 4001 });
  assert.equal(losses.length, 1);
  assert.equal(timers.length, 0);
  assert.match(issues.at(-1), /replaced/);
  client.disconnect();
});

test("unexpected closure stops the session and schedules bounded backoff reconnect", async () => {
  const timers = [];
  const losses = [];
  const { client, socket } = await connectedClient({
    setTimer: (callback, delay) => {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimer: () => {},
    onConnectionLost: (loss) => losses.push(loss),
  });
  socket.emit("close", { code: 1006 });
  assert.deepEqual(losses, [{ explicit: false, code: 1006 }]);
  assert.equal(timers[0].delay, 500);
  timers[0].callback();
  assert.equal(FakeWebSocket.instances.length, 2);
  client.disconnect();
});
