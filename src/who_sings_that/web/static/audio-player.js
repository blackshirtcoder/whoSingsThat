const HAVE_FUTURE_DATA = 3;
const DURATION_FAILSAFE_MARGIN_SECONDS = 2;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const STOP_REASONS = new Set([
  "accepted_buzz",
  "playback_timeout",
  "round_void",
  "playback_client_loss",
  "songmaster_loss",
  "preparation_failed",
  "game_termination",
  "connection_replaced",
]);

export const AudioState = Object.freeze({
  IDLE: "idle",
  PREPARING: "preparing",
  READY: "ready",
  PLAYING: "playing",
  STOPPED: "stopped",
  FAILED: "failed",
});

/**
 * Owns one HTMLAudioElement and only its local media lifecycle.
 * Authoritative game state, scoring, and WebSocket messages remain elsewhere.
 */
export class BrowserAudioPlayer {
  constructor({
    mediaElement = new Audio(),
    setTimer = globalThis.setTimeout.bind(globalThis),
    clearTimer = globalThis.clearTimeout.bind(globalThis),
    createUnlockSource = createAudibleUnlockTone,
    onStatus = () => {},
    onRuntimeIssue = () => {},
  } = {}) {
    this.media = mediaElement;
    this.media.preload = "auto";
    this._setTimer = setTimer;
    this._clearTimer = clearTimer;
    this._createUnlockSource = createUnlockSource;
    this._onStatus = onStatus;
    this._onRuntimeIssue = onRuntimeIssue;
    this._state = AudioState.IDLE;
    this._detail = "Audio is not enabled for this connection.";
    this._capable = false;
    this._generation = 0;
    this._identity = null;
    this._preparationCleanup = null;
    this._durationTimer = null;
    this._unlockCleanup = null;
    this._emitStatus();
  }

  get status() {
    return {
      state: this._state,
      detail: this._detail,
      browserAudioUsable: this._capable,
      gameSessionId: this._identity?.gameSessionId ?? null,
      preparationId: this._identity?.preparationId ?? null,
      roundId: this._identity?.roundId ?? null,
    };
  }

  /** Must be called directly from a click/tap handler. Plays a brief audible tone. */
  async initializeFromUserGesture() {
    const generation = ++this._generation;
    this._cancelAsyncWork();
    this.media.pause();
    this._releaseMedia();
    this._identity = null;
    this._capable = false;
    this._setState(AudioState.IDLE, "Checking this browser's audio output…");

    const unlock = this._createUnlockSource();
    this._unlockCleanup = unlock.cleanup;
    this.media.volume = 0.12;
    this.media.src = unlock.src;
    this.media.load();

    try {
      const evidence = waitForAnyEvent(
        this.media,
        ["playing", "timeupdate", "ended"],
        ["error", "abort"],
        2000,
        this._setTimer,
        this._clearTimer,
      );
      const playPromise = this.media.play();
      if (playPromise && typeof playPromise.then === "function") {
        await playPromise;
      }
      await evidence;
      if (generation !== this._generation) {
        return false;
      }
      this._capable = true;
      this._setState(
        AudioState.IDLE,
        "Audio output was confirmed. Each track must still prepare successfully.",
      );
      return true;
    } catch (error) {
      if (generation !== this._generation) {
        return false;
      }
      this._capable = false;
      this._setState(AudioState.FAILED, safeMediaError(error, "Audio could not be enabled."));
      return false;
    } finally {
      if (generation === this._generation) {
        this.media.pause();
        this.media.volume = 1;
        this._releaseMedia();
        this._cleanupUnlockSource();
      }
    }
  }

  async prepare(command) {
    let prepared;
    try {
      prepared = validatePrepareCommand(command);
    } catch (error) {
      this._setState(AudioState.FAILED, safeMediaError(error, "Invalid Prepare command."));
      return { outcome: "failed", reason: "invalid_prepare", identity: null };
    }

    if (!this._capable) {
      this._setState(
        AudioState.FAILED,
        "Audio is not enabled for this authoritative connection.",
      );
      return { outcome: "failed", reason: "audio_not_initialized", identity: prepared };
    }

    const generation = ++this._generation;
    this._cancelAsyncWork();
    this.media.pause();
    this._releaseMedia();
    this._identity = { ...prepared, roundId: null };
    this._setState(AudioState.PREPARING, "Loading and seeking authorized audio…");
    this.media.preload = "auto";
    this.media.src = prepared.mediaUrl;

    try {
      await this._prepareMedia(prepared, generation);
      if (!this._isCurrent(generation, prepared)) {
        return { outcome: "canceled", identity: prepared };
      }
      this._setState(AudioState.READY, "Audio is prepared and waiting for Play.");
      return { outcome: "ready", identity: prepared };
    } catch (error) {
      if (!this._isCurrent(generation, prepared)) {
        return { outcome: "canceled", identity: prepared };
      }
      this._releaseMedia();
      this._setState(AudioState.FAILED, safeMediaError(error, "Audio preparation failed."));
      return { outcome: "failed", reason: "media_preparation_failed", identity: prepared };
    } finally {
      if (generation === this._generation) {
        this._clearPreparationCleanup();
      }
    }
  }

  async play(command) {
    let play;
    try {
      play = validatePlayCommand(command);
    } catch (error) {
      this._onRuntimeIssue(safeMediaError(error, "Invalid Play command."));
      return false;
    }
    if (
      !this._capable ||
      this._state !== AudioState.READY ||
      !samePreparation(this._identity, play) ||
      this._identity.offsetSeconds !== play.offsetSeconds ||
      this._identity.durationSeconds !== play.durationSeconds
    ) {
      this._onRuntimeIssue("Ignored Play because matching prepared audio is unavailable.");
      return false;
    }

    const generation = ++this._generation;
    this._clearPreparationCleanup();
    this._identity = { ...this._identity, roundId: play.roundId };
    try {
      // Reassert the authoritative seek in case the browser adjusted currentTime.
      if (Math.abs(this.media.currentTime - play.offsetSeconds) > 0.05) {
        this.media.currentTime = play.offsetSeconds;
      }
      const playPromise = this.media.play();
      if (playPromise && typeof playPromise.then === "function") {
        await playPromise;
      }
      if (generation !== this._generation || !sameRound(this._identity, play)) {
        this.media.pause();
        return false;
      }
      this._setState(AudioState.PLAYING, "Playing the server-authorized interval.");
      this._armDurationFailsafe(play.durationSeconds, generation, play);
      return true;
    } catch (error) {
      if (generation !== this._generation) {
        this.media.pause();
        return false;
      }
      this._releaseMedia();
      this._setState(AudioState.FAILED, safeMediaError(error, "Browser playback failed."));
      this._onRuntimeIssue(
        "Playback failed after READY; protocol v1 has no post-READY failure report.",
      );
      return false;
    }
  }

  stop(command) {
    let stop;
    try {
      stop = validateStopCommand(command);
    } catch (error) {
      this._onRuntimeIssue(safeMediaError(error, "Invalid Stop command."));
      return false;
    }
    if (!this._identity || this._identity.gameSessionId !== stop.gameSessionId) {
      return false;
    }
    if (
      stop.preparationId !== null &&
      stop.preparationId !== this._identity.preparationId
    ) {
      return false;
    }
    if (stop.roundId !== null && stop.roundId !== this._identity.roundId) {
      return false;
    }

    this._halt(AudioState.STOPPED, `Stopped by server (${stop.reason}).`);
    return true;
  }

  cancelPreparation() {
    if (this._state !== AudioState.PREPARING && this._state !== AudioState.READY) {
      return false;
    }
    this._halt(AudioState.STOPPED, "Audio preparation was canceled.");
    return true;
  }

  /** Connection/session loss always revokes local capability and media. */
  reset(reason = "Connection reset; enable audio again after reconnecting.") {
    ++this._generation;
    this._cancelAsyncWork();
    this.media.pause();
    this._releaseMedia();
    this._cleanupUnlockSource();
    this._identity = null;
    this._capable = false;
    this._setState(AudioState.IDLE, reason);
  }

  _halt(state, detail) {
    ++this._generation;
    this._cancelAsyncWork();
    this.media.pause();
    this._releaseMedia();
    this._identity = null;
    this._setState(state, detail);
  }

  async _prepareMedia(prepared, generation) {
    const cleanup = [];
    let timeoutId;
    let cancelPreparation = () => {};
    const preparation = new Promise((resolve, reject) => {
      let metadataLoaded = false;
      let seekComplete = false;
      let playable = false;
      let settled = false;

      const finish = (error = null) => {
        if (settled) return;
        settled = true;
        if (error) reject(error);
        else resolve();
      };
      cancelPreparation = () => finish(new Error("Audio preparation was canceled."));
      const check = () => {
        if (!this._isCurrent(generation, prepared)) {
          finish(new Error("Audio preparation was canceled."));
          return;
        }
        if (metadataLoaded && seekComplete && playable) finish();
      };
      const listen = (type, handler) => {
        this.media.addEventListener(type, handler);
        cleanup.push(() => this.media.removeEventListener(type, handler));
      };
      listen("loadedmetadata", () => {
        metadataLoaded = true;
        try {
          this.media.currentTime = prepared.offsetSeconds;
          seekComplete = prepared.offsetSeconds === 0;
        } catch (error) {
          finish(error);
          return;
        }
        check();
      });
      listen("seeked", () => {
        seekComplete = true;
        check();
      });
      for (const eventName of ["loadeddata", "canplay"]) {
        listen(eventName, () => {
          playable = this.media.readyState >= HAVE_FUTURE_DATA || eventName === "canplay";
          check();
        });
      }
      listen("error", () => finish(mediaElementError(this.media)));
      listen("abort", () => finish(new Error("The browser aborted media loading.")));
      timeoutId = this._setTimer(
        () => finish(new Error("Audio was not ready before the preparation deadline.")),
        Math.max(1, prepared.timeoutSeconds * 1000),
      );
      cleanup.push(() => this._clearTimer(timeoutId));
      this.media.load();
    });
    this._preparationCleanup = () => {
      cleanup.splice(0).forEach((remove) => remove());
      cancelPreparation();
    };
    return preparation;
  }

  _armDurationFailsafe(durationSeconds, generation, identity) {
    this._clearDurationTimer();
    const onEnded = () => {
      if (generation !== this._generation || !sameRound(this._identity, identity)) return;
      this._onRuntimeIssue(
        "Media ended before the local duration failsafe; full-interval playback is not assumed.",
      );
      this._halt(AudioState.STOPPED, "The media ended naturally.");
    };
    const onError = () => {
      if (generation !== this._generation || !sameRound(this._identity, identity)) return;
      this._onRuntimeIssue(
        "Media failed after READY; protocol v1 has no post-READY failure report.",
      );
      this._halt(AudioState.FAILED, "The browser reported a playback error.");
    };
    this.media.addEventListener("ended", onEnded, { once: true });
    this.media.addEventListener("error", onError, { once: true });
    const remove = () => {
      this.media.removeEventListener("ended", onEnded);
      this.media.removeEventListener("error", onError);
    };
    this._durationTimer = {
      id: this._setTimer(() => {
        remove();
        if (generation !== this._generation || !sameRound(this._identity, identity)) return;
        this._halt(AudioState.STOPPED, "Stopped by the local duration failsafe.");
      }, (durationSeconds + DURATION_FAILSAFE_MARGIN_SECONDS) * 1000),
      remove,
    };
  }

  _isCurrent(generation, identity) {
    return generation === this._generation && samePreparation(this._identity, identity);
  }

  _cancelAsyncWork() {
    this._clearPreparationCleanup();
    this._clearDurationTimer();
  }

  _clearPreparationCleanup() {
    if (this._preparationCleanup) this._preparationCleanup();
    this._preparationCleanup = null;
  }

  _clearDurationTimer() {
    if (!this._durationTimer) return;
    this._clearTimer(this._durationTimer.id);
    this._durationTimer.remove();
    this._durationTimer = null;
  }

  _cleanupUnlockSource() {
    if (this._unlockCleanup) this._unlockCleanup();
    this._unlockCleanup = null;
  }

  _releaseMedia() {
    this.media.removeAttribute("src");
    this.media.load();
  }

  _setState(state, detail) {
    this._state = state;
    this._detail = detail;
    this._emitStatus();
  }

  _emitStatus() {
    this._onStatus(this.status);
  }
}

function validatePrepareCommand(command) {
  const gameSessionId = requiredUuid(command?.game_session_id, "game_session_id");
  const preparationId = requiredUuid(command?.preparation_id, "preparation_id");
  const mediaUrl = command?.media_url;
  if (typeof mediaUrl !== "string" || !/^\/media\/[A-Za-z0-9_-]+$/.test(mediaUrl)) {
    throw new TypeError("Prepare media_url is not an opaque local media capability.");
  }
  return {
    gameSessionId,
    preparationId,
    mediaUrl,
    offsetSeconds: nonnegativeNumber(
      command.effective_playback_offset_seconds,
      "effective_playback_offset_seconds",
    ),
    durationSeconds: positiveNumber(command.playback_duration_seconds, "playback_duration_seconds"),
    timeoutSeconds: positiveNumber(command.preparation_timeout_seconds, "preparation_timeout_seconds"),
  };
}

function validatePlayCommand(command) {
  const authoritativeStartedAt = requiredString(
    command?.authoritative_playback_started_at,
    "authoritative_playback_started_at",
  );
  if (Number.isNaN(Date.parse(authoritativeStartedAt))) {
    throw new TypeError("authoritative_playback_started_at must be an ISO timestamp.");
  }
  return {
    gameSessionId: requiredUuid(command?.game_session_id, "game_session_id"),
    preparationId: requiredUuid(command?.preparation_id, "preparation_id"),
    roundId: requiredUuid(command?.round_id, "round_id"),
    offsetSeconds: nonnegativeNumber(
      command.effective_playback_offset_seconds,
      "effective_playback_offset_seconds",
    ),
    durationSeconds: positiveNumber(command.playback_duration_seconds, "playback_duration_seconds"),
    authoritativeStartedAt,
  };
}

function validateStopCommand(command) {
  const reason = requiredString(command?.reason, "reason");
  if (!STOP_REASONS.has(reason)) throw new TypeError("Stop reason is not supported.");
  return {
    gameSessionId: requiredUuid(command?.game_session_id, "game_session_id"),
    preparationId: optionalUuid(command?.preparation_id, "preparation_id"),
    roundId: optionalUuid(command?.round_id, "round_id"),
    reason,
  };
}

function requiredUuid(value, field) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw new TypeError(`${field} must be a UUID.`);
  }
  return value;
}

function optionalUuid(value, field) {
  return value === null || value === undefined ? null : requiredUuid(value, field);
}

function requiredString(value, field) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${field} must be a nonempty string.`);
  }
  return value;
}

function positiveNumber(value, field) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) {
    throw new TypeError(`${field} must be positive.`);
  }
  return number;
}

function nonnegativeNumber(value, field) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    throw new TypeError(`${field} must be nonnegative.`);
  }
  return number;
}

function samePreparation(left, right) {
  return Boolean(
    left &&
      right &&
      left.gameSessionId === right.gameSessionId &&
      left.preparationId === right.preparationId,
  );
}

function sameRound(left, right) {
  return samePreparation(left, right) && left.roundId === right.roundId;
}

function mediaElementError(media) {
  const code = media.error?.code;
  return new Error(code ? `The browser reported media error ${code}.` : "Media loading failed.");
}

function safeMediaError(error, fallback) {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function waitForAnyEvent(media, successNames, failureNames, timeoutMs, setTimer, clearTimer) {
  return new Promise((resolve, reject) => {
    const listeners = [];
    let settled = false;
    let timer = null;
    const cleanup = () => {
      listeners.forEach(([name, listener]) => media.removeEventListener(name, listener));
      clearTimer(timer);
    };
    const finish = (error = null) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    for (const name of successNames) {
      const listener = () => finish();
      listeners.push([name, listener]);
      media.addEventListener(name, listener);
    }
    for (const name of failureNames) {
      const listener = () => finish(mediaElementError(media));
      listeners.push([name, listener]);
      media.addEventListener(name, listener);
    }
    timer = setTimer(() => finish(new Error("Audio output confirmation timed out.")), timeoutMs);
  });
}

/** Create a short audible 880 Hz WAV; this is an honest output check, not silent autoplay. */
function createAudibleUnlockTone() {
  const sampleRate = 8000;
  const samples = 640;
  const buffer = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(buffer);
  const write = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index));
    }
  };
  write(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  write(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, samples * 2, true);
  for (let index = 0; index < samples; index += 1) {
    const envelope = 1 - index / samples;
    const value = Math.sin((2 * Math.PI * 880 * index) / sampleRate) * envelope;
    view.setInt16(44 + index * 2, Math.round(value * 12000), true);
  }
  const url = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  return { src: url, cleanup: () => URL.revokeObjectURL(url) };
}
