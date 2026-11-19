import assert from "node:assert/strict";
import test from "node:test";

import {
  LocalActionCoordinator,
  buildLobbyModel,
  entryPresentation,
  settingsWarningPresentation,
  validateSettingsProposal,
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
    recipient: {
      public_participant_id: "bob-public-id",
      is_songmaster: false,
      audio_enabled: false,
      browser_audio_usable: false,
      available_songmaster_operations: [],
    },
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
    { publicParticipantId: "alice-public-id", displayName: "Alice", connected: true, isSongmaster: true, isSelf: false },
    { publicParticipantId: "bob-public-id", displayName: "Bob", connected: false, isSongmaster: false, isSelf: true },
  ]);
  assert.doesNotMatch(JSON.stringify(contestant), /Secret|answer|private-game-id/);
  assert.equal(contestant.canSelectPlaylists, false);
  assert.equal(contestant.canEditSettings, false);
  assert.equal(contestant.canStart, false);
  assert.equal(contestant.canCancel, false);
  assert.equal(contestant.canTransfer, false);

  projection.recipient = {
    public_participant_id: "alice-public-id",
    is_songmaster: true,
    audio_enabled: true,
    browser_audio_usable: true,
    available_songmaster_operations: [
      "select_playlist",
      "start_game",
      "set_game_settings",
      "cancel_game",
      "transfer_songmaster",
    ],
  };
  projection.selected_playlist_ids = ["playlist-one"];
  const songmaster = buildLobbyModel(projection, [
    { playlist_id: "playlist-one", name: "Favorites", song_count: 12 },
  ]);
  assert.equal(songmaster.isSongmaster, true);
  assert.equal(songmaster.canSelectPlaylists, true);
  assert.equal(songmaster.canEditSettings, true);
  assert.equal(songmaster.canStart, true);
  assert.equal(songmaster.canCancel, true);
  assert.equal(songmaster.canTransfer, true);
});

test("authoritative playlist and music facts determine lobby start readiness", () => {
  const base = {
    active: true,
    state: "lobby",
    started: false,
    songmaster_public_participant_id: "songmaster",
    participants: [
      { public_participant_id: "songmaster", display_name: "Alice", connected: true, is_songmaster: true },
    ],
    selected_playlist_ids: [],
    selected_playlists: [],
    settings: {
      playback_offset_seconds: 30,
      playback_duration_seconds: 30,
      speed_bonus_plateau_percent: "10",
    },
    recipient: {
      public_participant_id: "songmaster",
      is_songmaster: true,
      audio_enabled: true,
      browser_audio_usable: false,
      available_songmaster_operations: ["select_playlist", "start_game"],
    },
  };
  const catalog = [{ playlist_id: "rock", name: "Rock", song_count: 8 }];
  const none = buildLobbyModel(base, catalog);
  assert.equal(none.canStart, false);
  assert.match(none.startReadiness, /playlist/i);

  const selected = buildLobbyModel({ ...base, selected_playlist_ids: ["rock"] }, catalog);
  assert.equal(selected.canStart, false);
  assert.match(selected.startReadiness, /music output/i);
  assert.deepEqual(selected.selectedPlaylists, [
    { playlistId: "rock", name: "Rock", songCount: 8 },
  ]);

  const ready = buildLobbyModel({
    ...base,
    selected_playlist_ids: ["rock"],
    recipient: { ...base.recipient, browser_audio_usable: true },
  }, catalog);
  assert.equal(ready.canStart, true);

  const locked = buildLobbyModel({
    ...base,
    state: "round_complete",
    started: true,
    selected_playlist_ids: ["rock"],
    selected_playlists: [{ playlist_id: "rock", playlist_name: "Frozen Rock", playlist_song_count: 7 }],
  }, [{ playlist_id: "rock", name: "Changed later", song_count: 99 }]);
  assert.deepEqual(locked.selectedPlaylists, [
    { playlistId: "rock", name: "Frozen Rock", songCount: 7 },
  ]);
  assert.equal(locked.canSelectPlaylists, false);
});

test("settings validation follows server positivity rules without arbitrary upper limits", () => {
  assert.deepEqual(validateSettingsProposal({
    playbackOffsetSeconds: "120",
    playbackDurationSeconds: "30",
    speedBonusPlateauPercent: "10.5",
  }).proposal, {
    playback_offset_seconds: 120,
    playback_duration_seconds: 30,
    speed_bonus_plateau_percent: 10.5,
  });
  assert.equal(validateSettingsProposal({
    playbackOffsetSeconds: "999999",
    playbackDurationSeconds: "999999",
    speedBonusPlateauPercent: "100",
  }).error, undefined);
  assert.match(validateSettingsProposal({
    playbackOffsetSeconds: "-1",
    playbackDurationSeconds: "30",
    speedBonusPlateauPercent: "10",
  }).error, /offset/i);
  assert.match(validateSettingsProposal({
    playbackOffsetSeconds: "0",
    playbackDurationSeconds: "0",
    speedBonusPlateauPercent: "10",
  }).error, /duration/i);
});

test("structured settings warnings show one or both server conditions and retain exact proposal", () => {
  const proposal = {
    playback_offset_seconds: 121,
    playback_duration_seconds: 90,
    speed_bonus_plateau_percent: 12.5,
  };
  const offsetOnly = settingsWarningPresentation({
    confirmation_token: "opaque-offset-token",
    conditions: [{ code: "large_playback_offset", playback_offset_seconds: 121, threshold_seconds: 120 }],
  }, proposal);
  assert.equal(offsetOnly.messages.length, 1);
  assert.match(offsetOnly.messages[0], /121-second/);

  const durationOnly = settingsWarningPresentation({
    confirmation_token: "opaque-duration-token",
    conditions: [{ code: "duration_ineligible_majority", remaining_song_count: 7, ineligible_song_count: 4 }],
  }, proposal);
  assert.match(durationOnly.messages[0], /4 of 7/);

  const both = settingsWarningPresentation({
    confirmation_token: "one-token-for-both",
    conditions: [
      { code: "duration_ineligible_majority", remaining_song_count: 7, ineligible_song_count: 4 },
      { code: "large_playback_offset", playback_offset_seconds: 121, threshold_seconds: 120 },
    ],
  }, proposal);
  assert.equal(both.token, "one-token-for-both");
  assert.equal(both.messages.length, 2);
  assert.deepEqual(both.proposal, proposal);
});
