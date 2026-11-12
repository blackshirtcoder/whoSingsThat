import { BrowserAudioPlayer } from "./audio-player.js";
import { AudioCommandRouter } from "./audio-protocol.js";
import { CommandError, WstSocketClient } from "./socket-client.js";

const byId = (id) => document.getElementById(id);
const elements = Object.fromEntries(
  [
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
  ].map((id) => [id, byId(id)]),
);

let playlists = [];
let latestState = null;
let latestDiscovery = null;
// BrowserAudioPlayer emits its initial status synchronously. Cross-component
// rendering starts only after the audio, socket, and router collaborators exist.
let startupComplete = false;
const audio = new BrowserAudioPlayer({
  onStatus: (status) => {
    elements["audio-state"].textContent = status.state;
    elements["audio-detail"].textContent = status.detail;
    if (startupComplete) renderControls();
  },
  onRuntimeIssue: (message) => showError(message),
});

const socket = new WstSocketClient({
  url: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
  onStatus: (status) => {
    elements["connection-status"].textContent = status;
    renderControls();
  },
  onIdentity: () => {
    elements["identity-status"].textContent = "established (token hidden)";
    appendLog("Browser identity established or restored.");
  },
  onDiscovery: (discovery) => {
    latestDiscovery = discovery;
    if (!latestState) renderDiscovery(discovery);
    renderControls();
    appendLog(
      `game_discovery: ${safeLabel(discovery.state)}; joinable=${Boolean(discovery.joinable)}.`,
    );
  },
  onState: (state, messageType) => {
    const previousGameId = latestState?.game_session_id;
    if (
      state.active === false ||
      (previousGameId && previousGameId !== state.game_session_id)
    ) {
      audio.reset("Game session changed; enable audio again if needed.");
    }
    latestState = state.active === false ? null : state;
    if (state.active === false) {
      socket.forgetMembership();
      latestDiscovery = {
        active_game_exists: false,
        joinable: false,
        state: "no_game",
        songmaster_display_name: null,
      };
    }
    renderState(state);
    appendLog(`${messageType}: revision ${state.revision}, state ${state.state}.`);
  },
  onGameEvent: (event) => appendLog(`game_event: ${safeLabel(event.event)}.`),
  onAudioCommand: (type, payload) => {
    appendLog(`${type} received (private media details hidden).`);
    void audioRouter.handle(type, payload);
  },
  onProtocolIssue: (message) => showError(message),
  onConnectionLost: ({ explicit, code }) => {
    latestState = null;
    audio.reset("Connection lost; audio capability must be enabled again.");
    elements["identity-status"].textContent = "retained locally; connection required";
    elements["audio-preference"].textContent = "unknown";
    elements["audio-capability"].textContent = "false";
    appendLog(`${explicit ? "Disconnected" : "Connection lost"} (code ${code}).`);
    renderControls();
  },
});

const audioRouter = new AudioCommandRouter({
  player: audio,
  sendCommand: async (type, payload) => {
    await socket.sendCommand(type, payload);
    appendLog(`${type} accepted for queueing.`);
  },
  isConnected: () => socket.connected,
  onIssue: (message) => showError(message),
});
startupComplete = true;

function renderState(state) {
  if (!state.active) {
    latestState = null;
    elements["game-id"].textContent = "none";
    elements["game-state"].textContent = "no game";
    elements.songmaster.textContent = "—";
    elements["winning-buzzer"].textContent = "—";
    elements["selected-playlists"].textContent = "none";
    renderScoreboard(null);
    renderControls();
    return;
  }
  socket.rememberMembership(state.game_session_id);
  elements["game-id"].textContent = state.game_session_id;
  elements["game-state"].textContent = state.state;
  const songmaster = state.participants?.find(
    (participant) => participant.public_participant_id === state.songmaster_public_participant_id,
  );
  elements.songmaster.textContent = songmaster?.display_name ?? "unknown";
  elements["winning-buzzer"].textContent = buzzerDisplayName(state);
  elements["audio-preference"].textContent = String(state.recipient?.audio_enabled ?? false);
  elements["audio-capability"].textContent = String(
    state.recipient?.browser_audio_usable ?? false,
  );
  const names = playlists
    .filter((playlist) => state.selected_playlist_ids?.includes(playlist.playlist_id))
    .map((playlist) => playlist.name);
  elements["selected-playlists"].textContent = names.length ? names.join(", ") : "none";
  const displayed = state.displayed_song;
  elements["displayed-song"].textContent =
    displayed?.artist && displayed?.title
      ? `${displayed.artist} — ${displayed.title}`
      : displayed
        ? `position ${displayed.deck_position}; answer hidden`
        : "hidden / none";
  renderScoreboard(state);
  renderPlaylists();
  renderControls();
}

function renderDiscovery(discovery) {
  const active = Boolean(discovery?.active_game_exists);
  elements["game-id"].textContent = active ? "active (ID managed by server)" : "none";
  elements["game-state"].textContent = discovery?.state ?? "no game";
  elements.songmaster.textContent = discovery?.songmaster_display_name ?? "—";
}

export function deriveControlState({ connected, state, discovery, audioUsable }) {
  const recipient = state?.recipient;
  const operations = new Set(recipient?.available_songmaster_operations ?? []);
  const participant = Boolean(state);
  const scoringAction = deriveScoringAction(state);
  const scoringAllowed = scoringAction !== null;
  const stableAudioState = ["lobby", "round_complete"].includes(state?.state);
  return {
    createGame: connected && !participant && !discovery?.active_game_exists,
    joinGame: connected && !participant && Boolean(discovery?.joinable),
    scoringAllowed,
    scoringAction,
    stableAudioState,
    operations,
    buzz: Boolean(recipient?.eligible_to_buzz),
    enableAudio: connected && participant && stableAudioState,
    disableAudio: connected && participant && stableAudioState && audioUsable,
  };
}

function renderControls() {
  const connected = socket.connected;
  const controls = deriveControlState({
    connected,
    state: latestState,
    discovery: latestDiscovery,
    audioUsable: audio.status.browserAudioUsable,
  });
  const operations = controls.operations;
  elements.connect.disabled = connected || socket.state === "connecting";
  elements.disconnect.disabled = !connected;
  elements["create-game"].disabled = !controls.createGame;
  elements["join-game"].disabled = !controls.joinGame;
  elements["start-game"].disabled = !operations.has("start_game");
  elements["play-song"].disabled = !operations.has("play_song");
  elements["ack-failure"].disabled = !operations.has(
    "acknowledge_audio_preparation_failure",
  );
  elements.buzz.disabled = !controls.buzz;
  elements.reveal.disabled = !operations.has("reveal");
  elements["end-game"].disabled = !operations.has("end_game");
  elements["cancel-game"].disabled = !operations.has("cancel_game");
  elements["close-game"].disabled = !operations.has("close_game");
  elements.scoring.hidden = !controls.scoringAllowed;
  elements["scoring-label"].textContent =
    controls.scoringAction?.operation === "correct_score"
      ? "Correct score:"
      : "Accept score:";
  for (const button of elements.scoring.children) {
    if (button?.dataset?.score) button.disabled = !controls.scoringAllowed;
  }
  elements["enable-audio"].disabled = !controls.enableAudio;
  elements["disable-audio"].disabled = !controls.disableAudio;
  elements["playlist-list"].disabled = !operations.has("select_playlist");
}

function renderPlaylists() {
  elements["playlist-list"].replaceChildren();
  const legend = document.createElement("legend");
  legend.textContent = "Development playlists";
  elements["playlist-list"].append(legend);
  if (!playlists.length) {
    const empty = document.createElement("p");
    empty.textContent = "No playlists are present in the configured catalog.";
    elements["playlist-list"].append(empty);
    return;
  }
  const selected = new Set(latestState?.selected_playlist_ids ?? []);
  for (const playlist of playlists) {
    const label = document.createElement("label");
    label.className = "playlist-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selected.has(playlist.playlist_id);
    input.dataset.playlistId = playlist.playlist_id;
    input.addEventListener("change", () => void changePlaylist(input));
    label.append(input, ` ${playlist.name} (${playlist.song_count} tracks)`);
    elements["playlist-list"].append(label);
  }
  renderControls();
}

function renderScoreboard(state) {
  const rows = scoreboardRows(state);
  elements.scoreboard.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("li");
    empty.textContent = "No players";
    elements.scoreboard.append(empty);
    return;
  }
  for (const row of rows) {
    const item = document.createElement("li");
    const provisional = row.provisionalScore
      ? ` (${row.committedScore} committed ${signedPoints(row.provisionalScore)} provisional)`
      : "";
    item.textContent = `${row.displayName} — ${pointsLabel(row.effectiveScore)}${provisional}`;
    elements.scoreboard.append(item);
  }
}

export function scoreboardRows(state) {
  const participants = new Map(
    (state?.participants ?? []).map((participant) => [
      participant.public_participant_id,
      participant,
    ]),
  );
  const orderedIds = state?.scoreboard_order ?? [...participants.keys()];
  return orderedIds.flatMap((publicId) => {
    const participant = participants.get(publicId);
    if (!participant) return [];
    return [
      {
        displayName: participant.display_name,
        committedScore: participant.committed_score,
        provisionalScore: participant.provisional_score,
        effectiveScore: participant.effective_score,
      },
    ];
  });
}

function pointsLabel(points) {
  return `${points} ${points === 1 ? "point" : "points"}`;
}

function signedPoints(points) {
  return `${points > 0 ? "+" : ""}${points}`;
}

async function changePlaylist(input) {
  input.disabled = true;
  const operation = input.checked ? "select_playlist" : "deselect_playlist";
  try {
    await command(operation, { playlist_id: input.dataset.playlistId });
  } catch {
    input.checked = !input.checked;
  } finally {
    input.disabled = false;
  }
}

async function command(type, payload = {}) {
  clearError();
  try {
    const result = await socket.sendCommand(type, payload);
    appendLog(`${type}: accepted.`);
    if (result.game_session_id) socket.rememberMembership(result.game_session_id);
    return result;
  } catch (error) {
    showCommandError(type, error);
    throw error;
  }
}

elements.connect.addEventListener("click", () => {
  clearError();
  socket.connect().catch((error) => showError(publicMessage(error)));
});
elements.disconnect.addEventListener("click", () => socket.disconnect());
elements["create-game"].addEventListener("click", () => {
  void command("create_game", { display_name: elements["display-name"].value });
});
elements["join-game"].addEventListener("click", () => {
  void command("join_game", {
    display_name: elements["display-name"].value,
  });
});
elements["start-game"].addEventListener("click", () => void command("start_game"));
elements["play-song"].addEventListener("click", () => void command("play_song"));
elements["ack-failure"].addEventListener("click", () => {
  const preparationId = latestState?.current_round?.preparation_id;
  if (preparationId) {
    void command("acknowledge_audio_preparation_failure", { preparation_id: preparationId });
  }
});
elements.buzz.addEventListener("click", () => {
  const roundId = latestState?.current_round?.round_id;
  if (roundId) void command("buzz", { round_id: roundId });
});
elements.reveal.addEventListener("click", () => {
  const roundId = latestState?.current_round?.round_id;
  if (roundId) void command("reveal", { round_id: roundId });
});
elements["end-game"].addEventListener("click", () => void command("end_game"));
elements["cancel-game"].addEventListener("click", () => void command("cancel_game"));
elements["close-game"].addEventListener("click", () => void command("close_game"));
elements.scoring.addEventListener("click", (event) => {
  const scoringChoice = event.target.dataset?.score;
  const action = deriveScoringAction(latestState);
  if (scoringChoice && action) {
    void command(action.operation, {
      round_id: action.roundId,
      scoring_choice: scoringChoice,
    });
  }
});

export function deriveScoringAction(state) {
  const operations = new Set(
    state?.recipient?.available_songmaster_operations ?? [],
  );
  if (
    state?.state === "scoring" &&
    operations.has("accept_score") &&
    state.current_round?.round_id
  ) {
    return {
      operation: "accept_score",
      roundId: state.current_round.round_id,
    };
  }
  if (
    state?.state === "round_complete" &&
    operations.has("correct_score") &&
    state.current_round?.scoring_correction_round_id
  ) {
    return {
      operation: "correct_score",
      roundId: state.current_round.scoring_correction_round_id,
    };
  }
  return null;
}

export function buzzerDisplayName(state) {
  const buzzerId = state?.current_round?.buzzer_public_participant_id;
  if (!buzzerId) return "—";
  return (
    state.participants?.find(
      (participant) => participant.public_participant_id === buzzerId,
    )?.display_name ?? "unknown"
  );
}

// initializeFromUserGesture is intentionally the first action in this click callback.
elements["enable-audio"].addEventListener("click", async () => {
  const usable = await audio.initializeFromUserGesture();
  if (!usable) {
    showError(audio.status.detail);
    return;
  }
  try {
    await command("set_audio_preference", { audio_enabled: true });
    await command("set_audio_capability", { browser_audio_usable: true });
  } catch {
    audio.reset("The server did not accept audio capability for this connection.");
  }
});

elements["disable-audio"].addEventListener("click", async () => {
  audio.reset("Audio was disabled locally.");
  try {
    await command("set_audio_capability", { browser_audio_usable: false });
    await command("set_audio_preference", { audio_enabled: false });
  } catch {
    // command() already displayed a safe server error.
  }
});

function appendLog(message) {
  const item = document.createElement("li");
  item.textContent = message;
  elements["event-log"].prepend(item);
  while (elements["event-log"].children.length > 80) {
    elements["event-log"].lastElementChild.remove();
  }
}

function showCommandError(operation, error) {
  const code = error instanceof CommandError ? ` (${error.code})` : "";
  showError(`${operation} failed${code}: ${publicMessage(error)}`);
}

function showError(message) {
  elements.error.textContent = message;
  appendLog(`Error: ${message}`);
}

function clearError() {
  elements.error.textContent = "";
}

function publicMessage(error) {
  return error instanceof Error ? error.message : "Operation failed.";
}

function safeLabel(value) {
  return typeof value === "string" && /^[a-z0-9_]+$/.test(value) ? value : "unknown";
}

async function loadPlaylists() {
  try {
    const response = await fetch("/api/playlists", { cache: "no-store" });
    if (!response.ok) throw new Error("Playlist metadata could not be loaded.");
    const payload = await response.json();
    playlists = Array.isArray(payload.playlists) ? payload.playlists : [];
    if (latestState) renderState(latestState);
    else renderPlaylists();
  } catch (error) {
    showError(publicMessage(error));
  }
}

window.addEventListener("pagehide", () => socket.disconnect(), { once: true });
renderControls();
void loadPlaylists();
