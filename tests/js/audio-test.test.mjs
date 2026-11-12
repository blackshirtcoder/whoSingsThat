import assert from "node:assert/strict";
import test from "node:test";

const ELEMENT_IDS = [
  "connection-status",
  "identity-status",
  "game-id",
  "display-name",
  "game-state",
  "songmaster",
  "winning-buzzer",
  "selected-playlists",
  "displayed-song",
  "scoreboard",
  "playlist-list",
  "audio-preference",
  "audio-capability",
  "audio-state",
  "audio-detail",
  "error",
  "event-log",
  "connect",
  "disconnect",
  "create-game",
  "join-game",
  "start-game",
  "play-song",
  "ack-failure",
  "buzz",
  "reveal",
  "end-game",
  "cancel-game",
  "close-game",
  "scoring",
  "scoring-label",
  "enable-audio",
  "disable-audio",
];

class FakeElement {
  constructor() {
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.value = "";
  }

  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) ?? [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }

  append(...children) {
    this.children.push(...children);
  }

  prepend(child) {
    this.children.unshift(child);
  }

  replaceChildren(...children) {
    this.children = children;
  }
}

class FakeAudio extends FakeElement {
  pause() {}
  load() {}
  removeAttribute() {}
}

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

test("diagnostic module completes synchronous startup before rendering controls", async () => {
  const elements = new Map(ELEMENT_IDS.map((id) => [id, new FakeElement()]));
  globalThis.Audio = FakeAudio;
  globalThis.document = {
    getElementById: (id) => elements.get(id),
    createElement: () => new FakeElement(),
  };
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ playlists: [] }),
  });
  globalThis.localStorage = new MemoryStorage();
  globalThis.location = { protocol: "http:", host: "127.0.0.1:8000" };
  globalThis.window = { addEventListener() {} };

  const diagnostic = await import("../../src/who_sings_that/web/static/audio-test.js");

  assert.equal(elements.get("audio-state").textContent, "idle");
  assert.equal(elements.get("connect").disabled, false);
  assert.equal(elements.get("connect").listeners.get("click")?.length, 1);

  const noGame = diagnostic.deriveControlState({
    connected: true,
    state: null,
    discovery: { active_game_exists: false, joinable: false },
    audioUsable: false,
  });
  assert.equal(noGame.createGame, true);
  assert.equal(noGame.joinGame, false);

  const joinable = diagnostic.deriveControlState({
    connected: true,
    state: null,
    discovery: { active_game_exists: true, joinable: true },
    audioUsable: false,
  });
  assert.equal(joinable.createGame, false);
  assert.equal(joinable.joinGame, true);

  const contestant = diagnostic.deriveControlState({
    connected: true,
    state: {
      state: "scoring",
      recipient: {
        eligible_to_buzz: false,
        available_songmaster_operations: [],
      },
    },
    discovery: { active_game_exists: true, joinable: true },
    audioUsable: false,
  });
  assert.equal(contestant.createGame, false);
  assert.equal(contestant.joinGame, false);
  assert.equal(contestant.scoringAllowed, false);

  const songmaster = diagnostic.deriveControlState({
    connected: true,
    state: {
      state: "scoring",
      recipient: {
        eligible_to_buzz: false,
        available_songmaster_operations: ["accept_score"],
      },
      current_round: { round_id: "initial-round" },
    },
    discovery: { active_game_exists: true, joinable: true },
    audioUsable: false,
  });
  assert.equal(songmaster.scoringAllowed, true);
  assert.deepEqual(songmaster.scoringAction, {
    operation: "accept_score",
    roundId: "initial-round",
  });

  const correction = diagnostic.deriveControlState({
    connected: true,
    state: {
      state: "round_complete",
      recipient: {
        eligible_to_buzz: false,
        available_songmaster_operations: ["correct_score"],
      },
      current_round: { scoring_correction_round_id: "completed-round" },
    },
    discovery: { active_game_exists: true, joinable: true },
    audioUsable: false,
  });
  assert.equal(correction.scoringAllowed, true);
  assert.deepEqual(correction.scoringAction, {
    operation: "correct_score",
    roundId: "completed-round",
  });

  const timeoutRound = diagnostic.deriveControlState({
    connected: true,
    state: {
      state: "round_complete",
      recipient: {
        eligible_to_buzz: false,
        available_songmaster_operations: ["play_song"],
      },
      current_round: { scoring_correction_round_id: null },
    },
    discovery: { active_game_exists: true, joinable: true },
    audioUsable: false,
  });
  assert.equal(timeoutRound.scoringAllowed, false);

  const sharedWinner = {
    current_round: { buzzer_public_participant_id: "winner-id" },
    participants: [
      { public_participant_id: "other-id", display_name: "Songmaster" },
      { public_participant_id: "winner-id", display_name: "Contestant" },
    ],
  };
  assert.equal(diagnostic.buzzerDisplayName(sharedWinner), "Contestant");
  assert.equal(
    diagnostic.buzzerDisplayName({
      ...sharedWinner,
      current_round: { buzzer_public_participant_id: null },
    }),
    "—",
  );

  assert.deepEqual(
    diagnostic.scoreboardRows({
      scoreboard_order: ["winner-id", "other-id"],
      participants: [
        {
          public_participant_id: "other-id",
          display_name: "Songmaster",
          committed_score: 10,
          provisional_score: 0,
          effective_score: 10,
        },
        {
          public_participant_id: "winner-id",
          display_name: "Contestant",
          committed_score: 20,
          provisional_score: 40,
          effective_score: 60,
        },
      ],
    }),
    [
      {
        displayName: "Contestant",
        committedScore: 20,
        provisionalScore: 40,
        effectiveScore: 60,
      },
      {
        displayName: "Songmaster",
        committedScore: 10,
        provisionalScore: 0,
        effectiveScore: 10,
      },
    ],
  );
});
