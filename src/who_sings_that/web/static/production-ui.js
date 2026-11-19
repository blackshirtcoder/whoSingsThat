import { AudioCommandRouter } from "./audio-protocol.js";
import { BrowserAudioPlayer } from "./audio-player.js";
import { MusicOutputWorkflow } from "./music-output.js";
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

export function buildLobbyModel(state, playlistCatalog = []) {
  if (!state?.active || !state.recipient || !Array.isArray(state.participants)) return null;
  const recipientId = state.recipient.public_participant_id;
  const operations = new Set(state.recipient.available_songmaster_operations ?? []);
  const participants = state.participants.map((participant) => ({
    publicParticipantId: participant.public_participant_id,
    displayName: safeName(participant.display_name, "Player"),
    connected: participant.connected === true,
    isSongmaster: participant.is_songmaster === true || participant.public_participant_id === state.songmaster_public_participant_id,
    isSelf: participant.public_participant_id === recipientId,
  }));
  const songmaster = participants.find((participant) => participant.isSongmaster);
  const catalogById = new Map(
    playlistCatalog.map((playlist) => [playlist.playlist_id, playlist]),
  );
  const selectedIds = Array.isArray(state.selected_playlist_ids)
    ? state.selected_playlist_ids.filter((value) => typeof value === "string")
    : [];
  const selectedPlaylists = state.started && Array.isArray(state.selected_playlists)
    ? state.selected_playlists.map((playlist) => ({
      playlistId: playlist.playlist_id,
      name: safeName(playlist.playlist_name, "Selected playlist"),
      songCount: safeCount(playlist.playlist_song_count),
    }))
    : selectedIds.map((playlistId) => {
      const playlist = catalogById.get(playlistId);
      return {
        playlistId,
        name: safeName(playlist?.name, "Selected playlist"),
        songCount: safeCount(playlist?.song_count),
      };
    });
  const settings = state.settings ?? {};
  const musicEnabled = state.recipient.audio_enabled === true;
  const musicReady = musicEnabled && state.recipient.browser_audio_usable === true;
  const isLobby = state.state === "lobby";
  const isSongmaster = state.recipient.is_songmaster === true;
  let startReadiness = "Ready to start.";
  if (selectedIds.length === 0) startReadiness = "Select at least one playlist.";
  else if (!musicReady) startReadiness = "Confirm music output on this device.";

  return {
    isSongmaster,
    isLobby,
    phase: safePhase(state.state),
    songmasterName: songmaster?.displayName ?? "Songmaster",
    participants,
    selectedIds,
    selectedPlaylists,
    playlistCatalog: playlistCatalog.map((playlist) => ({
      playlistId: playlist.playlist_id,
      name: safeName(playlist.name, "Playlist"),
      songCount: safeCount(playlist.song_count),
      selected: selectedIds.includes(playlist.playlist_id),
    })),
    settings: {
      playbackOffsetSeconds: settings.playback_offset_seconds,
      playbackDurationSeconds: settings.playback_duration_seconds,
      speedBonusPlateauPercent: settings.speed_bonus_plateau_percent,
    },
    musicEnabled,
    musicReady,
    canChangeMusic: ["lobby", "round_complete"].includes(state.state),
    canEditSettings: isSongmaster && operations.has("set_game_settings"),
    canSelectPlaylists: isSongmaster && isLobby && operations.has("select_playlist"),
    canStart: isSongmaster && operations.has("start_game") && selectedIds.length > 0 && musicReady,
    startReadiness,
    canCancel: isSongmaster && operations.has("cancel_game"),
    canTransfer: isSongmaster && operations.has("transfer_songmaster"),
  };
}

export function validateSettingsProposal(values) {
  const offsetText = String(values?.playbackOffsetSeconds ?? "").trim();
  const durationText = String(values?.playbackDurationSeconds ?? "").trim();
  const plateauText = String(values?.speedBonusPlateauPercent ?? "").trim();
  if (!/^(0|[1-9]\d*)$/.test(offsetText)) {
    return { error: "Playback offset must be a whole number of zero seconds or more." };
  }
  if (!/^[1-9]\d*$/.test(durationText)) {
    return { error: "Playback duration must be a positive whole number of seconds." };
  }
  const plateau = Number(plateauText);
  if (plateauText === "" || !Number.isFinite(plateau) || plateau < 0 || plateau > 100) {
    return { error: "Speed-bonus plateau must be from 0 through 100 percent." };
  }
  return {
    proposal: {
      playback_offset_seconds: Number(offsetText),
      playback_duration_seconds: Number(durationText),
      speed_bonus_plateau_percent: plateau,
    },
  };
}

export function settingsWarningPresentation(warning, proposal) {
  if (
    !warning ||
    typeof warning.confirmation_token !== "string" ||
    !Array.isArray(warning.conditions)
  ) {
    throw new TypeError("The server returned an invalid settings warning.");
  }
  const messages = warning.conditions.map((condition) => {
    if (condition.code === "large_playback_offset") {
      return `The proposed ${condition.playback_offset_seconds}-second offset is unusually large (over ${condition.threshold_seconds} seconds).`;
    }
    if (condition.code === "duration_ineligible_majority") {
      return `${condition.ineligible_song_count} of ${condition.remaining_song_count} remaining songs would be too short and skipped when encountered.`;
    }
    return "The server requires confirmation for this proposal.";
  });
  return {
    token: warning.confirmation_token,
    proposal: { ...proposal },
    proposalText: `${proposal.playback_offset_seconds}s offset · ${proposal.playback_duration_seconds}s duration · ${proposal.speed_bonus_plateau_percent}% speed plateau`,
    messages,
  };
}

export class ProductionApp {
  constructor({
    documentImpl = globalThis.document,
    locationImpl = globalThis.location,
    fetchImpl = globalThis.fetch?.bind(globalThis),
  } = {}) {
    this.document = documentImpl;
    this.fetch = fetchImpl;
    this.elements = collectElements(documentImpl);
    this.connectionStatus = "disconnected";
    this.discovery = null;
    this.projection = null;
    this.membershipAction = null;
    this.playlistCatalog = [];
    this.playlistsLoaded = false;
    this.playlistsLoading = false;
    this.pendingCommand = null;
    this.settingsConfirmation = null;
    this.pendingConfirmation = null;
    this.outputState = { state: "idle", pending: false, playbackStarted: false };

    this.sounds = new NotificationSoundManager({
      onIssue: (message) => this.showSoundIssue(message),
    });
    this.player = new BrowserAudioPlayer({
      mediaElement: this.elements.mysteryAudio,
      onStatus: (status) => { this.audioStatus = status; },
      onRuntimeIssue: (message) => this.showError(message),
    });
    this.socket = new WstSocketClient({
      url: `${locationImpl.protocol === "https:" ? "wss" : "ws"}://${locationImpl.host}/ws`,
      onStatus: (status) => { this.connectionStatus = status; this.render(); },
      onDiscovery: (discovery) => { this.discovery = discovery; this.render(); },
      onState: (state) => this.receiveState(state),
      onGameEvent: () => {},
      onAudioCommand: (type, payload) => { void this.audioRouter.handle(type, payload); },
      onProtocolIssue: (message) => this.showError(message),
      onConnectionLost: () => {
        this.output.reset();
        this.projection = null;
        this.render();
      },
    });
    this.audioRouter = new AudioCommandRouter({
      player: this.player,
      sendCommand: (type, payload) => this.socket.sendCommand(type, payload),
      isConnected: () => this.socket.connected,
      onIssue: (message) => this.showError(message),
    });
    this.output = new MusicOutputWorkflow({
      mediaElement: this.elements.outputCheckAudio,
      mysteryPlayer: this.player,
      sendCommand: (type, payload) => this.socket.sendCommand(type, payload),
      onChange: (state) => {
        this.outputState = state;
        this.renderOutputSheet();
        if (this.projection) this.renderLobby();
      },
      onError: (message) => this.showError(message),
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
    this.elements.musicAction.addEventListener("click", () => this.changeMusicOutput());
    this.elements.heardOutput.addEventListener("click", () => { void this.output.confirmHeard(); });
    this.elements.replayOutput.addEventListener("click", () => this.output.replay());
    this.elements.cancelOutput.addEventListener("click", () => { void this.output.cancel(); });
    this.elements.editSettings.addEventListener("click", () => this.openSettings());
    this.elements.closeSettings.addEventListener("click", () => this.closeSettings());
    this.elements.settingsForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.submitSettings();
    });
    this.elements.cancelSettingsWarning.addEventListener("click", () => this.cancelSettingsWarning());
    this.elements.confirmSettingsWarning.addEventListener("click", () => { void this.confirmSettingsWarning(); });
    this.elements.startGame.addEventListener("click", () => { void this.startGame(); });
    this.elements.cancelGame.addEventListener("click", () => this.confirmCancelGame());
    this.elements.transferSongmaster.addEventListener("click", () => this.confirmTransfer());
    this.elements.confirmCancel.addEventListener("click", () => this.closeConfirmation());
    this.elements.confirmAction.addEventListener("click", () => { void this.runConfirmation(); });
    globalThis.window?.addEventListener("pagehide", () => this.socket.disconnect(), { once: true });
    this.renderSoundPreference();
    this.render();
  }

  receiveState(state) {
    if (state.active === false) {
      this.output.reset();
      this.projection = null;
      this.socket.forgetMembership();
      this.discovery = { active_game_exists: false, joinable: false, state: "no_game", songmaster_display_name: null };
      this.closeSettings();
      this.cancelSettingsWarning();
      this.closeConfirmation();
    } else {
      this.projection = state;
      this.socket.rememberMembership(state.game_session_id);
      if (state.state === "lobby" && !this.playlistsLoaded) void this.loadPlaylists();
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
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    if (!model) return;
    this.elements.gamePhase.textContent = model.phase;
    this.elements.lobbyTitle.textContent = model.isLobby ? "Game lobby" : "Game room";
    this.elements.roleBadge.textContent = model.isSongmaster ? "Songmaster" : "Contestant";
    this.elements.roleBadge.className = `role-badge${model.isSongmaster ? " songmaster" : ""}`;
    this.elements.waitingTitle.textContent = model.isLobby
      ? (model.isSongmaster ? "Set up the game" : `Waiting for ${model.songmasterName}`)
      : "Game setup is locked";
    this.elements.waitingDetail.textContent = model.isLobby
      ? (model.isSongmaster ? "Choose music, check output, then start when ready." : "The Songmaster is preparing the game.")
      : "The game has started. Full gameplay controls are coming in D4.";
    this.elements.startedCard.hidden = model.isLobby;

    this.renderMusic(model);
    this.renderPlaylists(model);
    this.renderSettings(model);
    this.renderParticipants(model);
    this.renderSongmasterActions(model);
  }

  renderMusic(model) {
    let label = "Off";
    let detail = "Mystery songs will play somewhere else.";
    let statusClass = "";
    if (model.musicEnabled && !model.musicReady) {
      label = "Check needed";
      detail = "The preference is selected, but audible output is not confirmed for this connection.";
      statusClass = "warning";
    }
    if (model.musicReady) {
      label = "Ready";
      detail = "This device can be selected for mystery-song playback.";
      statusClass = "ok";
    }
    if (this.outputState.pending) detail = "Saving this device’s music-output choice…";
    this.elements.musicStatus.textContent = label;
    this.elements.musicStatus.className = `status-pill ${statusClass}`.trim();
    this.elements.musicDetail.textContent = detail;
    this.elements.musicAction.hidden = !model.canChangeMusic;
    this.elements.musicAction.disabled = this.outputState.pending;
    this.elements.musicAction.textContent = model.musicReady ? "Disable music on this device" : "Check music output";
  }

  renderPlaylists(model) {
    const count = model.selectedPlaylists.length;
    this.elements.playlistCount.textContent = count === 0 ? "None selected" : `${count} selected`;
    if (count === 0) {
      this.elements.selectedPlaylists.replaceChildren(textElement(this.document, "div", "empty-selection", "No playlist selected yet."));
    } else {
      this.elements.selectedPlaylists.replaceChildren(...model.selectedPlaylists.map((playlist) => selectedPlaylistElement(this.document, playlist)));
    }
    this.elements.playlistControls.hidden = !model.canSelectPlaylists;
    if (!model.canSelectPlaylists) return;
    if (this.playlistsLoading) {
      this.elements.playlistOptions.replaceChildren(textElement(this.document, "div", "empty-selection", "Loading playlists…"));
      return;
    }
    if (model.playlistCatalog.length === 0) {
      this.elements.playlistOptions.replaceChildren(textElement(this.document, "div", "empty-selection", "No playlists are available."));
      return;
    }
    this.elements.playlistOptions.replaceChildren(...model.playlistCatalog.map((playlist) => {
      const button = playlistChoiceElement(this.document, playlist);
      button.disabled = this.pendingCommand !== null;
      button.addEventListener("click", () => { void this.togglePlaylist(playlist); });
      return button;
    }));
  }

  renderSettings(model) {
    this.elements.settingOffset.textContent = formatUnit(model.settings.playbackOffsetSeconds, "s");
    this.elements.settingDuration.textContent = formatUnit(model.settings.playbackDurationSeconds, "s");
    this.elements.settingPlateau.textContent = formatUnit(model.settings.speedBonusPlateauPercent, "%");
    this.elements.editSettings.hidden = !model.canEditSettings;
  }

  renderParticipants(model) {
    const connectedCount = model.participants.filter((participant) => participant.connected).length;
    this.elements.participantCount.textContent = `${connectedCount} of ${model.participants.length} here`;
    this.elements.participantList.replaceChildren(...model.participants.map((participant) => participantElement(this.document, participant)));
  }

  renderSongmasterActions(model) {
    this.elements.songmasterActions.hidden = !(model.isSongmaster && model.isLobby);
    if (this.elements.songmasterActions.hidden) return;
    this.elements.readinessText.textContent = model.startReadiness;
    this.elements.readinessDot.className = `status-dot ${model.canStart ? "status-ok" : "status-warning"}`;
    this.elements.startGame.disabled = !model.canStart || this.pendingCommand !== null;
    this.elements.startGame.textContent = this.pendingCommand === "start_game" ? "Starting…" : "Start game";
    this.elements.cancelGame.hidden = !model.canCancel;
    this.elements.cancelGame.disabled = this.pendingCommand !== null;
    this.elements.transferControls.hidden = !model.canTransfer;
    const currentValue = this.elements.transferRecipient.value;
    const targets = model.participants.filter((participant) => participant.connected && !participant.isSelf);
    const options = [optionElement(this.document, "", targets.length ? "Choose a connected player" : "No connected player available")];
    options.push(...targets.map((participant) => optionElement(this.document, participant.publicParticipantId, participant.displayName)));
    this.elements.transferRecipient.replaceChildren(...options);
    if (targets.some((participant) => participant.publicParticipantId === currentValue)) this.elements.transferRecipient.value = currentValue;
    this.elements.transferRecipient.disabled = this.pendingCommand !== null || targets.length === 0;
    this.elements.transferSongmaster.disabled = this.pendingCommand !== null || targets.length === 0 || !this.elements.transferRecipient.value;
    this.elements.transferRecipient.onchange = () => {
      this.elements.transferSongmaster.disabled = !this.elements.transferRecipient.value || this.pendingCommand !== null;
    };
  }

  renderOutputSheet() {
    const open = ["checking", "error", "enabling"].includes(this.outputState.state);
    this.elements.outputCheckSheet.hidden = !open;
    if (!open) return;
    const failed = this.outputState.state === "error";
    const enabling = this.outputState.state === "enabling";
    this.elements.outputCheckDetail.textContent = failed
      ? "The chime did not play. Retry it, or cancel and leave music off."
      : (enabling ? "Saving your confirmed music output…" : "Confirm only if the full chime is audible on this device.");
    this.elements.heardOutput.disabled = enabling || !this.outputState.playbackStarted;
    this.elements.replayOutput.disabled = enabling;
    this.elements.cancelOutput.disabled = enabling;
  }

  async loadPlaylists() {
    if (this.playlistsLoading || this.playlistsLoaded || !this.fetch) return;
    this.playlistsLoading = true;
    this.render();
    try {
      const response = await this.fetch("/api/playlists", { cache: "no-store" });
      if (!response.ok) throw new Error("Playlists could not be loaded.");
      const payload = await response.json();
      if (!Array.isArray(payload.playlists)) throw new Error("Playlist data was invalid.");
      this.playlistCatalog = payload.playlists.filter((playlist) => (
        typeof playlist?.playlist_id === "string" && typeof playlist?.name === "string"
      ));
      this.playlistsLoaded = true;
    } catch (error) {
      this.showError(publicMessage(error));
    } finally {
      this.playlistsLoading = false;
      this.render();
    }
  }

  changeMusicOutput() {
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    if (!model?.canChangeMusic || this.outputState.pending) return;
    this.clearMessages();
    if (model.musicReady) void this.output.disable();
    else this.output.begin();
  }

  async togglePlaylist(playlist) {
    const operation = playlist.selected ? "deselect_playlist" : "select_playlist";
    await this.runCommand(operation, operation, { playlist_id: playlist.playlistId });
  }

  openSettings(proposal = null) {
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    if (!model?.canEditSettings) return;
    const values = proposal ?? {
      playback_offset_seconds: model.settings.playbackOffsetSeconds,
      playback_duration_seconds: model.settings.playbackDurationSeconds,
      speed_bonus_plateau_percent: model.settings.speedBonusPlateauPercent,
    };
    this.elements.settingsOffset.value = String(values.playback_offset_seconds ?? "");
    this.elements.settingsDuration.value = String(values.playback_duration_seconds ?? "");
    this.elements.settingsPlateau.value = String(values.speed_bonus_plateau_percent ?? "");
    this.elements.settingsError.hidden = true;
    this.elements.settingsSheet.hidden = false;
  }

  closeSettings() {
    this.elements.settingsSheet.hidden = true;
    this.elements.settingsError.hidden = true;
  }

  async submitSettings() {
    if (this.pendingCommand !== null) return;
    const validated = validateSettingsProposal({
      playbackOffsetSeconds: this.elements.settingsOffset.value,
      playbackDurationSeconds: this.elements.settingsDuration.value,
      speedBonusPlateauPercent: this.elements.settingsPlateau.value,
    });
    if (validated.error) {
      this.elements.settingsError.textContent = validated.error;
      this.elements.settingsError.hidden = false;
      return;
    }
    this.elements.settingsError.hidden = true;
    const result = await this.runCommand("settings", "set_game_settings", validated.proposal, { quiet: true });
    if (!result) return;
    if (result.applied === false) {
      if (!result.warning) {
        this.showError("The server did not apply those settings.");
        return;
      }
      try {
        this.settingsConfirmation = settingsWarningPresentation(result.warning, validated.proposal);
      } catch (error) {
        this.showError(publicMessage(error));
        return;
      }
      this.closeSettings();
      this.renderSettingsWarning();
      return;
    }
    this.closeSettings();
    this.showNotice("Game settings updated.");
  }

  renderSettingsWarning() {
    const confirmation = this.settingsConfirmation;
    this.elements.settingsWarningSheet.hidden = confirmation === null;
    if (!confirmation) return;
    this.elements.warningProposal.textContent = confirmation.proposalText;
    this.elements.warningConditions.replaceChildren(...confirmation.messages.map((message) => textElement(this.document, "li", "", message)));
    this.elements.confirmSettingsWarning.disabled = this.pendingCommand !== null;
  }

  cancelSettingsWarning() {
    const proposal = this.settingsConfirmation?.proposal ?? null;
    this.settingsConfirmation = null;
    this.elements.settingsWarningSheet.hidden = true;
    if (proposal && this.projection) this.openSettings(proposal);
  }

  async confirmSettingsWarning() {
    if (!this.settingsConfirmation || this.pendingCommand !== null) return;
    const confirmation = this.settingsConfirmation;
    const payload = {
      ...confirmation.proposal,
      duration_warning_confirmation_token: confirmation.token,
    };
    const result = await this.runCommand("settings_confirmation", "set_game_settings", payload, { quiet: true });
    if (!result) return;
    if (result.applied === false) {
      if (!result.warning) {
        this.showError("The server did not apply those settings.");
        return;
      }
      try {
        this.settingsConfirmation = settingsWarningPresentation(
          result.warning,
          confirmation.proposal,
        );
        this.renderSettingsWarning();
      } catch (error) {
        this.showError(publicMessage(error));
      }
      return;
    }
    this.settingsConfirmation = null;
    this.elements.settingsWarningSheet.hidden = true;
    this.showNotice("Game settings updated.");
  }

  async startGame() {
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    if (!model?.canStart) return;
    const result = await this.runCommand("start_game", "start_game", {}, { quiet: true });
    if (result) this.showNotice("Game started.");
  }

  confirmCancelGame() {
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    if (!model?.canCancel) return;
    this.openConfirmation({
      title: "Cancel this game?",
      detail: "The lobby and its memberships will be discarded. Playlists in the library are not affected.",
      label: "Cancel game",
      run: () => this.runCommand("cancel_game", "cancel_game", {}, { quiet: true }),
    });
  }

  confirmTransfer() {
    const model = buildLobbyModel(this.projection, this.playlistCatalog);
    const participantId = this.elements.transferRecipient.value;
    const recipient = model?.participants.find((participant) => participant.publicParticipantId === participantId && participant.connected && !participant.isSelf);
    if (!model?.canTransfer || !recipient) return;
    this.openConfirmation({
      title: `Make ${recipient.displayName} the Songmaster?`,
      detail: "They will immediately receive Songmaster controls. Scores and game setup stay unchanged.",
      label: "Transfer role",
      run: () => this.runCommand("transfer_songmaster", "transfer_songmaster", { recipient_public_participant_id: participantId }, { quiet: true }),
    });
  }

  openConfirmation(confirmation) {
    this.pendingConfirmation = confirmation;
    this.elements.confirmTitle.textContent = confirmation.title;
    this.elements.confirmDetail.textContent = confirmation.detail;
    this.elements.confirmAction.textContent = confirmation.label;
    this.elements.confirmSheet.hidden = false;
  }

  closeConfirmation() {
    if (this.pendingCommand !== null) return;
    this.pendingConfirmation = null;
    this.elements.confirmSheet.hidden = true;
  }

  async runConfirmation() {
    if (!this.pendingConfirmation || this.pendingCommand !== null) return;
    const run = this.pendingConfirmation.run;
    const result = await run();
    if (result) {
      this.pendingConfirmation = null;
      this.elements.confirmSheet.hidden = true;
    }
  }

  async runCommand(key, operation, payload = {}, { quiet = false } = {}) {
    if (this.pendingCommand !== null) return null;
    this.clearMessages();
    this.pendingCommand = key;
    this.render();
    this.renderSettingsWarning();
    try {
      const result = await this.socket.sendCommand(operation, payload);
      if (!quiet) this.showNotice("Game setup updated.");
      return result;
    } catch (error) {
      if (error instanceof CommandError && error.code === "stale_settings_confirmation" && this.settingsConfirmation) {
        const proposal = this.settingsConfirmation.proposal;
        this.settingsConfirmation = null;
        this.elements.settingsWarningSheet.hidden = true;
        this.openSettings(proposal);
        this.elements.settingsError.textContent = "That confirmation expired because the game changed. Review the proposal and save it again.";
        this.elements.settingsError.hidden = false;
      } else {
        this.showError(publicMessage(error));
      }
      return null;
    } finally {
      this.pendingCommand = null;
      this.render();
      this.renderSettingsWarning();
    }
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
  const avatar = textElement(documentImpl, "span", "avatar", participant.displayName.slice(0, 1).toLocaleUpperCase());
  const copy = documentImpl.createElement("div");
  copy.className = "participant-copy";
  const name = documentImpl.createElement("div");
  name.className = "participant-name";
  name.append(textElement(documentImpl, "span", "", participant.displayName));
  if (participant.isSelf) name.append(textElement(documentImpl, "span", "you-tag", "YOU"));
  if (participant.isSongmaster) {
    const crown = textElement(documentImpl, "span", "crown", "◆");
    crown.setAttribute("aria-label", "Songmaster");
    name.append(crown);
  }
  const role = participant.isSongmaster ? "Songmaster" : "Contestant";
  const state = textElement(documentImpl, "div", `participant-state${participant.connected ? " online" : ""}`, `${role} · ${participant.connected ? "Connected" : "Disconnected"}`);
  copy.append(name, state);
  item.append(avatar, copy);
  return item;
}

function selectedPlaylistElement(documentImpl, playlist) {
  const item = documentImpl.createElement("div");
  item.className = "selected-item";
  item.append(
    textElement(documentImpl, "span", "", playlist.name),
    textElement(documentImpl, "span", "", `${playlist.songCount} ${playlist.songCount === 1 ? "song" : "songs"}`),
  );
  return item;
}

function playlistChoiceElement(documentImpl, playlist) {
  const button = documentImpl.createElement("button");
  button.type = "button";
  button.className = `playlist-choice${playlist.selected ? " selected" : ""}`;
  button.setAttribute("aria-pressed", String(playlist.selected));
  button.append(
    textElement(documentImpl, "span", "", playlist.name),
    textElement(documentImpl, "small", "", playlist.selected ? "Selected" : `${playlist.songCount} songs`),
  );
  return button;
}

function optionElement(documentImpl, value, label) {
  const option = documentImpl.createElement("option");
  option.value = value;
  option.textContent = label;
  return option;
}

function textElement(documentImpl, tag, className, text) {
  const element = documentImpl.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function collectElements(documentImpl) {
  const get = (id) => documentImpl.getElementById(id);
  return {
    soundToggle: get("sound-toggle"), connectionDot: get("connection-dot"), connectionLabel: get("connection-label"),
    disconnect: get("disconnect"), connectionView: get("connection-view"), connect: get("connect"), entryView: get("entry-view"),
    entryEyebrow: get("entry-eyebrow"), entryTitle: get("entry-title"), entryDetail: get("entry-detail"), membershipForm: get("membership-form"),
    displayName: get("display-name"), membershipAction: get("membership-action"), lobbyView: get("lobby-view"), gamePhase: get("game-phase"),
    lobbyTitle: get("lobby-title"), roleBadge: get("role-badge"), waitingTitle: get("waiting-title"), waitingDetail: get("waiting-detail"),
    musicStatus: get("music-status"), musicDetail: get("music-detail"), musicAction: get("music-action"),
    playlistCount: get("playlist-count"), selectedPlaylists: get("selected-playlists"), playlistControls: get("playlist-controls"), playlistOptions: get("playlist-options"),
    editSettings: get("edit-settings"), settingOffset: get("setting-offset"), settingDuration: get("setting-duration"), settingPlateau: get("setting-plateau"),
    startedCard: get("started-card"), participantCount: get("participant-count"), participantList: get("participant-list"),
    songmasterActions: get("songmaster-actions"), readinessDot: get("readiness-dot"), readinessText: get("readiness-text"), startGame: get("start-game"),
    transferControls: get("transfer-controls"), transferRecipient: get("transfer-recipient"), transferSongmaster: get("transfer-songmaster"), cancelGame: get("cancel-game"),
    error: get("error"), notice: get("notice"), soundIssue: get("sound-issue"), soundIssueText: get("sound-issue-text"), retrySound: get("retry-sound"),
    settingsSheet: get("settings-sheet"), settingsForm: get("settings-form"), settingsOffset: get("settings-offset"), settingsDuration: get("settings-duration"), settingsPlateau: get("settings-plateau"),
    settingsError: get("settings-error"), closeSettings: get("close-settings"), saveSettings: get("save-settings"),
    settingsWarningSheet: get("settings-warning-sheet"), warningProposal: get("warning-proposal"), warningConditions: get("warning-conditions"),
    cancelSettingsWarning: get("cancel-settings-warning"), confirmSettingsWarning: get("confirm-settings-warning"),
    outputCheckSheet: get("output-check-sheet"), outputCheckDetail: get("output-check-detail"), heardOutput: get("heard-output"), replayOutput: get("replay-output"), cancelOutput: get("cancel-output"),
    confirmSheet: get("confirm-sheet"), confirmTitle: get("confirm-title"), confirmDetail: get("confirm-detail"), confirmCancel: get("confirm-cancel"), confirmAction: get("confirm-action"),
    mysteryAudio: get("mystery-audio"), outputCheckAudio: get("output-check-audio"),
  };
}

function safeName(value, fallback) {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function safeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function safePhase(value) {
  const labels = { lobby: "Lobby", finished: "Game complete", round_complete: "Between songs", audio_preparing: "Preparing audio", playing: "Song playing", awaiting_reveal: "Answer pending", scoring: "Scoring" };
  return labels[value] ?? "Game room";
}

function formatUnit(value, unit) {
  return value === null || value === undefined || value === "" ? "—" : `${value}${unit}`;
}

function publicMessage(error) {
  if (error instanceof CommandError) return error.message;
  return error instanceof Error ? error.message : "The operation could not be completed.";
}
