import { NotificationSoundManager } from "./notification-sounds.js";
import { CommandError, WstSocketClient } from "./socket-client.js";

const ACTION_SOUNDS = Object.freeze({
  connect: "connected",
  create: "gameCreated",
  join: "welcome",
});

export class LocalActionCoordinator {
  constructor({ socket, sounds, onPending = () => {}, onNotice = () => {}, onError = () => {} }) {
    this.socket = socket;
    this.sounds = sounds;
    this.onPending = onPending;
    this.onNotice = onNotice;
    this.onError = onError;
    this.pending = null;
  }

  connect() {
    return this._run("connect", () => this.socket.connect(), "Connected to the game server.");
  }

  create(displayName) {
    return this._run(
      "create",
      () => this.socket.sendCommand("create_game", { display_name: displayName }),
      "Game created. You’re the Songmaster.",
    );
  }

  join(displayName) {
    return this._run(
      "join",
      () => this.socket.sendCommand("join_game", { display_name: displayName }),
      "Welcome. You joined the game.",
    );
  }

  async _run(action, operation, notice) {
    if (this.pending !== null) return null;
    // This remains before operation(), whose WebSocket work crosses an async boundary.
    try { this.sounds.initializeFromUserGesture(); } catch { /* sound is optional */ }
    this.pending = action;
    this.onPending(action);
    try {
      const result = await operation();
      if (result?.game_session_id) this.socket.rememberMembership(result.game_session_id);
      this.onNotice(notice);
      try { void Promise.resolve(this.sounds.play(ACTION_SOUNDS[action])).catch(() => {}); }
      catch { /* sound is optional */ }
      return result;
    } catch (error) {
      this.onError(publicMessage(error));
      return null;
    } finally {
      this.pending = null;
      this.onPending(null);
    }
  }
}

export function entryPresentation(discovery) {
  if (!discovery) {
    return { eyebrow: "Server connected", title: "Checking for an active game…", detail: "This will only take a moment.", action: null };
  }
  if (!discovery.active_game_exists) {
    return { eyebrow: "No active game", title: "Create a new game", detail: "Enter your display name to become the Songmaster.", action: "create" };
  }
  if (discovery.joinable) {
    const songmaster = safeName(discovery.songmaster_display_name, "The current Songmaster");
    return { eyebrow: "Active game", title: "Join the game", detail: `${songmaster} is the Songmaster. Enter your display name to join.`, action: "join" };
  }
  return { eyebrow: "Game complete", title: "This game has ended", detail: "Wait for the Songmaster to close it before creating another game.", action: null };
}

export function buildLobbyModel(state) {
  if (!state?.active || !state.recipient || !Array.isArray(state.participants)) return null;
  const recipientId = state.recipient.public_participant_id;
  const participants = state.participants.map((participant) => ({
    displayName: safeName(participant.display_name, "Player"),
    connected: participant.connected === true,
    isSongmaster: participant.public_participant_id === state.songmaster_public_participant_id,
    isSelf: participant.public_participant_id === recipientId,
  }));
  const songmaster = participants.find((participant) => participant.isSongmaster);
  return {
    isSongmaster: state.recipient.is_songmaster === true,
    phase: safePhase(state.state),
    songmasterName: songmaster?.displayName ?? "Songmaster",
    participants,
  };
}

export class ProductionApp {
  constructor({ documentImpl = globalThis.document, locationImpl = globalThis.location } = {}) {
    this.document = documentImpl;
    this.elements = collectElements(documentImpl);
    this.connectionStatus = "disconnected";
    this.discovery = null;
    this.projection = null;
    this.membershipAction = null;

    this.sounds = new NotificationSoundManager({
      onIssue: (message) => this.showSoundIssue(message),
    });
    this.socket = new WstSocketClient({
      url: `${locationImpl.protocol === "https:" ? "wss" : "ws"}://${locationImpl.host}/ws`,
      onStatus: (status) => { this.connectionStatus = status; this.render(); },
      onDiscovery: (discovery) => { this.discovery = discovery; this.render(); },
      onState: (state) => this.receiveState(state),
      onGameEvent: () => {},
      onAudioCommand: () => {},
      onProtocolIssue: (message) => this.showError(message),
      onConnectionLost: () => { this.projection = null; this.render(); },
    });
    this.actions = new LocalActionCoordinator({
      socket: this.socket,
      sounds: this.sounds,
      onPending: () => this.render(),
      onNotice: (message) => this.showNotice(message),
      onError: (message) => this.showError(message),
    });
  }

  start() {
    this.elements.connect.addEventListener("click", () => { this.clearMessages(); void this.actions.connect(); });
    this.elements.disconnect.addEventListener("click", () => { this.clearMessages(); this.socket.disconnect(); });
    this.elements.membershipForm.addEventListener("submit", (event) => {
      event.preventDefault();
      this.clearMessages();
      const name = this.elements.displayName.value;
      if (this.membershipAction === "create") void this.actions.create(name);
      if (this.membershipAction === "join") void this.actions.join(name);
    });
    this.elements.soundToggle.addEventListener("click", () => this.toggleSounds());
    this.elements.retrySound.addEventListener("click", () => {
      this.elements.soundIssue.hidden = true;
      void this.sounds.retryFromUserGesture();
    });
    globalThis.window?.addEventListener("pagehide", () => this.socket.disconnect(), { once: true });
    this.renderSoundPreference();
    this.render();
  }

  receiveState(state) {
    if (state.active === false) {
      this.projection = null;
      this.socket.forgetMembership();
      this.discovery = { active_game_exists: false, joinable: false, state: "no_game", songmaster_display_name: null };
    } else {
      this.projection = state;
      this.socket.rememberMembership(state.game_session_id);
    }
    this.render();
  }

  render() {
    const connected = this.connectionStatus === "connected";
    this.renderConnectionStatus();
    this.elements.connectionView.hidden = connected;
    this.elements.entryView.hidden = !connected || Boolean(this.projection);
    this.elements.lobbyView.hidden = !connected || !this.projection;
    if (!connected) this.renderConnectView();
    else if (this.projection) this.renderLobby();
    else this.renderEntry();
  }

  renderConnectionStatus() {
    const labels = {
      disconnected: "Not connected",
      connecting: "Connecting securely…",
      reconnecting: "Reconnecting…",
      connected: "Connected to game server",
    };
    this.elements.connectionLabel.textContent = labels[this.connectionStatus] ?? "Not connected";
    const statusClass = {
      connected: "status-ok",
      connecting: "status-pending",
      reconnecting: "status-warning",
    }[this.connectionStatus] ?? "";
    this.elements.connectionDot.className = `status-dot ${statusClass}`.trim();
    this.elements.disconnect.hidden = this.connectionStatus !== "connected";
  }

  renderConnectView() {
    const busy = this.actions.pending === "connect" || ["connecting", "reconnecting"].includes(this.connectionStatus);
    this.elements.connect.disabled = busy;
    this.elements.connect.textContent = busy ? "Connecting…" : "Connect to game";
  }

  renderEntry() {
    const presentation = entryPresentation(this.discovery);
    this.membershipAction = presentation.action;
    this.elements.entryEyebrow.textContent = presentation.eyebrow;
    this.elements.entryTitle.textContent = presentation.title;
    this.elements.entryDetail.textContent = presentation.detail;
    this.elements.membershipForm.hidden = presentation.action === null;
    this.elements.membershipAction.textContent = presentation.action === "create" ? "Create game" : "Join game";
    const pending = this.actions.pending !== null;
    this.elements.membershipAction.disabled = pending;
    this.elements.displayName.disabled = pending;
    if (pending) this.elements.membershipAction.textContent = presentation.action === "create" ? "Creating…" : "Joining…";
  }

  renderLobby() {
    const model = buildLobbyModel(this.projection);
    if (!model) return;
    this.elements.gamePhase.textContent = model.phase;
    this.elements.lobbyTitle.textContent = "Game lobby";
    this.elements.roleBadge.textContent = model.isSongmaster ? "Songmaster" : "Contestant";
    this.elements.roleBadge.className = `role-badge${model.isSongmaster ? " songmaster" : ""}`;
    this.elements.waitingTitle.textContent = model.isSongmaster ? "Waiting for players" : `Waiting for ${model.songmasterName}`;
    this.elements.waitingDetail.textContent = model.isSongmaster
      ? "Players can join while this lobby is open."
      : "The Songmaster will start the game.";
    const connectedCount = model.participants.filter((participant) => participant.connected).length;
    this.elements.participantCount.textContent = `${connectedCount} of ${model.participants.length} here`;
    this.elements.participantList.replaceChildren(...model.participants.map((participant) => participantElement(this.document, participant)));
  }

  toggleSounds() {
    const enabled = !this.sounds.enabled;
    this.sounds.setEnabled(enabled);
    if (enabled) this.sounds.initializeFromUserGesture();
    this.renderSoundPreference();
    if (!enabled) this.elements.soundIssue.hidden = true;
    this.showNotice(`Interface sounds ${enabled ? "on" : "off"}.`);
  }

  renderSoundPreference() {
    const enabled = this.sounds.enabled;
    this.elements.soundToggle.setAttribute("aria-pressed", String(enabled));
    this.elements.soundToggle.setAttribute("aria-label", `Turn interface sounds ${enabled ? "off" : "on"}`);
  }

  showError(message) {
    this.elements.error.textContent = message;
    this.elements.error.hidden = false;
  }

  showNotice(message) {
    this.elements.notice.textContent = message;
    this.elements.notice.hidden = false;
  }

  showSoundIssue(message) {
    this.elements.soundIssueText.textContent = message;
    this.elements.soundIssue.hidden = false;
  }

  clearMessages() {
    this.elements.error.hidden = true;
    this.elements.notice.hidden = true;
  }
}

function participantElement(documentImpl, participant) {
  const item = documentImpl.createElement("li");
  item.className = "participant";
  const avatar = documentImpl.createElement("span");
  avatar.className = "avatar";
  avatar.textContent = participant.displayName.slice(0, 1).toLocaleUpperCase();
  const copy = documentImpl.createElement("div");
  copy.className = "participant-copy";
  const name = documentImpl.createElement("div");
  name.className = "participant-name";
  const label = documentImpl.createElement("span");
  label.textContent = participant.displayName;
  name.append(label);
  if (participant.isSelf) {
    const you = documentImpl.createElement("span");
    you.className = "you-tag";
    you.textContent = "YOU";
    name.append(you);
  }
  if (participant.isSongmaster) {
    const crown = documentImpl.createElement("span");
    crown.className = "crown";
    crown.textContent = "◆";
    crown.setAttribute("aria-label", "Songmaster");
    name.append(crown);
  }
  const state = documentImpl.createElement("div");
  state.className = `participant-state${participant.connected ? " online" : ""}`;
  state.textContent = participant.connected ? "Connected" : "Disconnected";
  copy.append(name, state);
  item.append(avatar, copy);
  return item;
}

function collectElements(documentImpl) {
  const get = (id) => documentImpl.getElementById(id);
  return {
    soundToggle: get("sound-toggle"), connectionDot: get("connection-dot"), connectionLabel: get("connection-label"),
    disconnect: get("disconnect"), connectionView: get("connection-view"), connect: get("connect"), entryView: get("entry-view"),
    entryEyebrow: get("entry-eyebrow"), entryTitle: get("entry-title"), entryDetail: get("entry-detail"), membershipForm: get("membership-form"),
    displayName: get("display-name"), membershipAction: get("membership-action"), lobbyView: get("lobby-view"), gamePhase: get("game-phase"),
    lobbyTitle: get("lobby-title"), roleBadge: get("role-badge"), waitingTitle: get("waiting-title"), waitingDetail: get("waiting-detail"),
    participantCount: get("participant-count"), participantList: get("participant-list"), error: get("error"), notice: get("notice"),
    soundIssue: get("sound-issue"), soundIssueText: get("sound-issue-text"), retrySound: get("retry-sound"),
  };
}

function safeName(value, fallback) {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function safePhase(value) {
  const labels = { lobby: "Lobby", finished: "Game complete", round_complete: "Between songs", audio_preparing: "Preparing audio", playing: "Song playing", awaiting_reveal: "Answer pending", scoring: "Scoring" };
  return labels[value] ?? "Game room";
}

function publicMessage(error) {
  if (error instanceof CommandError) return error.message;
  return error instanceof Error ? error.message : "The operation could not be completed.";
}
