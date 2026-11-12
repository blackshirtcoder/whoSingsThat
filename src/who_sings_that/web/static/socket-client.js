const PROTOCOL_VERSION = 1;
const TOKEN_STORAGE_KEY = "wst.browserToken.v1";
const GAME_STORAGE_KEY = "wst.gameSessionId.v1";
const AUDIO_COMMANDS = new Set(["prepare_audio", "play_audio", "stop_audio"]);

/** WebSocket transport/identity helper. It does not interpret game state. */
export class WstSocketClient {
  constructor({
    url,
    WebSocketImpl = globalThis.WebSocket,
    storage = globalThis.localStorage,
    setTimer = globalThis.setTimeout.bind(globalThis),
    clearTimer = globalThis.clearTimeout.bind(globalThis),
    onStatus = () => {},
    onState = () => {},
    onDiscovery = () => {},
    onGameEvent = () => {},
    onAudioCommand = () => {},
    onProtocolIssue = () => {},
    onConnectionLost = () => {},
    onIdentity = () => {},
  }) {
    this.url = url;
    this.WebSocketImpl = WebSocketImpl;
    this.storage = storage;
    this._setTimer = setTimer;
    this._clearTimer = clearTimer;
    this._callbacks = {
      onStatus,
      onState,
      onDiscovery,
      onGameEvent,
      onAudioCommand,
      onProtocolIssue,
      onConnectionLost,
      onIdentity,
    };
    this.socket = null;
    this.connectionId = null;
    this.state = "disconnected";
    this.latestState = null;
    this._generation = 0;
    this._requestSequence = 0;
    this._pending = new Map();
    this._explicitlyClosed = true;
    this._retryAttempt = 0;
    this._retryTimer = null;
    this._connectPromise = null;
  }

  get browserToken() {
    return this.storage.getItem(TOKEN_STORAGE_KEY);
  }

  get rememberedGameId() {
    return this.storage.getItem(GAME_STORAGE_KEY);
  }

  get connected() {
    return this.state === "connected" && this.socket?.readyState === this.WebSocketImpl.OPEN;
  }

  connect() {
    if (this._connectPromise) return this._connectPromise;
    if (this.connected) return Promise.resolve();
    this._explicitlyClosed = false;
    this._clearRetry();
    const generation = ++this._generation;
    this._setStatus("connecting");
    this._connectPromise = new Promise((resolve, reject) => {
      let socket;
      try {
        socket = new this.WebSocketImpl(this.url);
      } catch (error) {
        this._connectPromise = null;
        this._setStatus("disconnected");
        reject(error);
        return;
      }
      this.socket = socket;
      const failInitial = (error) => {
        if (this._connectPromise) reject(error);
      };
      socket.addEventListener("message", (event) => {
        this._handleMessage(event.data, generation, resolve, failInitial);
      });
      socket.addEventListener("error", () => {
        if (generation === this._generation && this.state === "connecting") {
          failInitial(new Error("WebSocket connection failed."));
        }
      });
      socket.addEventListener("close", (event) => {
        this._handleClose(event, generation, failInitial);
      });
    }).finally(() => {
      this._connectPromise = null;
    });
    return this._connectPromise;
  }

  disconnect() {
    this._explicitlyClosed = true;
    ++this._generation;
    this._clearRetry();
    this._rejectPending(new Error("WebSocket disconnected."));
    const socket = this.socket;
    this.socket = null;
    this.connectionId = null;
    this.latestState = null;
    this._setStatus("disconnected");
    this._callbacks.onConnectionLost({ explicit: true, code: 1000 });
    if (socket && socket.readyState < this.WebSocketImpl.CLOSING) socket.close(1000);
  }

  async sendCommand(type, payload = {}) {
    if (!this.connected) throw new Error("WebSocket is not connected.");
    const requestId = `web-${Date.now().toString(36)}-${++this._requestSequence}`;
    const response = new Promise((resolve, reject) => {
      this._pending.set(requestId, { resolve, reject, type });
    });
    try {
      this.socket.send(
        JSON.stringify({
          version: PROTOCOL_VERSION,
          request_id: requestId,
          type,
          payload,
        }),
      );
    } catch (error) {
      this._pending.delete(requestId);
      throw error;
    }
    return response;
  }

  rememberMembership(gameSessionId) {
    if (typeof gameSessionId !== "string" || gameSessionId.length === 0) return;
    this.storage.setItem(GAME_STORAGE_KEY, gameSessionId);
  }

  forgetMembership() {
    this.storage.removeItem(GAME_STORAGE_KEY);
  }

  async _handleMessage(raw, generation, resolveConnect, rejectConnect) {
    if (generation !== this._generation) return;
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      this._callbacks.onProtocolIssue("Ignored non-JSON WebSocket message.");
      return;
    }
    if (!message || typeof message !== "object" || message.version !== PROTOCOL_VERSION) {
      this._callbacks.onProtocolIssue("Ignored an invalid protocol envelope.");
      return;
    }

    if (message.type === "transport_connected") {
      const connectionId = message.result?.connection_id;
      if (typeof connectionId !== "string") {
        rejectConnect(new Error("Server did not provide a transport connection ID."));
        return;
      }
      this.connectionId = connectionId;
      // Transport is now writable; public status remains "connecting" until
      // identity establishment and optional membership restoration complete.
      this.state = "connected";
      try {
        await this._establishAndRestore();
        if (generation !== this._generation) return;
        this._retryAttempt = 0;
        this._setStatus("connected");
        resolveConnect();
      } catch (error) {
        rejectConnect(error);
        this._callbacks.onProtocolIssue(publicError(error));
      }
      return;
    }

    if (message.type === "result") {
      const pending = this._pending.get(message.request_id);
      if (!pending) {
        this._callbacks.onProtocolIssue("Received an uncorrelated command result.");
        return;
      }
      this._pending.delete(message.request_id);
      if (message.ok === true) pending.resolve(message.result ?? {});
      else {
        const error = new CommandError(
          message.error?.code ?? "command_failed",
          message.error?.message ?? "The server rejected the command.",
        );
        pending.reject(error);
      }
      return;
    }

    if (message.request_id !== null) {
      this._callbacks.onProtocolIssue("Ignored an unsolicited message with a request ID.");
      return;
    }
    if (message.type === "state_snapshot" || message.type === "state_update") {
      if (!message.payload || typeof message.payload !== "object") {
        this._callbacks.onProtocolIssue("Ignored an invalid state message.");
        return;
      }
      this.latestState = message.payload;
      this._callbacks.onState(message.payload, message.type);
      return;
    }
    if (message.type === "game_discovery") {
      if (!message.payload || typeof message.payload !== "object") {
        this._callbacks.onProtocolIssue("Ignored an invalid game discovery message.");
        return;
      }
      this._callbacks.onDiscovery(message.payload);
      return;
    }
    if (message.type === "game_event") {
      this._callbacks.onGameEvent(message.payload ?? {});
      return;
    }
    if (AUDIO_COMMANDS.has(message.type)) {
      if (!message.payload || typeof message.payload !== "object") {
        this._callbacks.onProtocolIssue(`Ignored invalid ${message.type}.`);
        return;
      }
      this._callbacks.onAudioCommand(message.type, message.payload);
      return;
    }
    this._callbacks.onProtocolIssue("Ignored an unknown unsolicited message type.");
  }

  async _establishAndRestore() {
    const token = this.browserToken;
    const identity = await this.sendCommand(
      "establish_identity",
      token ? { browser_token: token } : {},
    );
    if (typeof identity.browser_token !== "string") {
      throw new Error("Server identity response was invalid.");
    }
    this.storage.setItem(TOKEN_STORAGE_KEY, identity.browser_token);
    this._callbacks.onIdentity();

    const gameId = this.rememberedGameId;
    if (!gameId) return;
    try {
      const result = await this.sendCommand("reconnect", { game_session_id: gameId });
      this.rememberMembership(result.game_session_id ?? gameId);
    } catch (error) {
      if (error instanceof CommandError && ["wrong_game", "invalid_membership"].includes(error.code)) {
        this.forgetMembership();
        return;
      }
      throw error;
    }
  }

  _handleClose(event, generation, rejectConnect) {
    if (generation !== this._generation) return;
    const wasConnecting = this.state === "connecting" || this._connectPromise !== null;
    this.socket = null;
    this.connectionId = null;
    this.latestState = null;
    this._rejectPending(new Error("WebSocket connection closed."));
    this._setStatus("disconnected");
    this._callbacks.onConnectionLost({
      explicit: this._explicitlyClosed,
      code: event.code,
    });
    if (wasConnecting) rejectConnect(new Error("WebSocket closed during connection."));
    if (event.code === 4001) {
      this._callbacks.onProtocolIssue("This browser connection was replaced by another connection.");
      return;
    }
    if (!this._explicitlyClosed) this._scheduleReconnect();
  }

  _scheduleReconnect() {
    if (this._retryAttempt >= 4 || this._retryTimer !== null) {
      if (this._retryAttempt >= 4) {
        this._callbacks.onProtocolIssue("Automatic reconnect stopped after four attempts.");
      }
      return;
    }
    const delay = 500 * 2 ** this._retryAttempt;
    this._retryAttempt += 1;
    this._setStatus("reconnecting");
    this._retryTimer = this._setTimer(() => {
      this._retryTimer = null;
      this.connect().catch(() => {
        if (!this._explicitlyClosed) this._scheduleReconnect();
      });
    }, delay);
  }

  _clearRetry() {
    if (this._retryTimer !== null) this._clearTimer(this._retryTimer);
    this._retryTimer = null;
  }

  _rejectPending(error) {
    for (const pending of this._pending.values()) pending.reject(error);
    this._pending.clear();
  }

  _setStatus(status) {
    this.state = status;
    this._callbacks.onStatus(status);
  }
}

export class CommandError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "CommandError";
    this.code = code;
  }
}

function publicError(error) {
  return error instanceof Error ? error.message : "WebSocket operation failed.";
}

export const storageKeysForTests = Object.freeze({
  token: TOKEN_STORAGE_KEY,
  game: GAME_STORAGE_KEY,
});
