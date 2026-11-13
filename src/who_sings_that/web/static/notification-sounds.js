const SOUND_PREFERENCE_KEY = "wst.uiSoundsEnabled.v1";

export const NOTIFICATION_SOUND_FILES = Object.freeze({
  connected: "/static/sounds/connected.wav",
  gameCreated: "/static/sounds/game-created.wav",
  welcome: "/static/sounds/welcome.wav",
  audioReady: "/static/sounds/audio-ready.wav",
});

/** Interface chimes only. It never receives or controls the mystery-song element. */
export class NotificationSoundManager {
  constructor({
    AudioContextImpl = globalThis.AudioContext ?? globalThis.webkitAudioContext,
    fetchImpl = globalThis.fetch?.bind(globalThis),
    storage = globalThis.localStorage,
    onIssue = () => {},
  } = {}) {
    this._AudioContextImpl = AudioContextImpl;
    this._fetch = fetchImpl;
    this._storage = storage;
    this._onIssue = onIssue;
    this._context = null;
    this._activation = null;
    this._buffers = new Map();
    this._activeSources = new Set();
    this._lastEvent = null;
    this._enabled = readPreference(storage);
  }

  get enabled() { return this._enabled; }
  get canRetry() { return this._lastEvent !== null && this._enabled; }

  setEnabled(enabled) {
    this._enabled = Boolean(enabled);
    try {
      this._storage?.setItem(SOUND_PREFERENCE_KEY, String(this._enabled));
    } catch {
      // Storage privacy settings must not affect the game workflow.
    }
    if (!this._enabled) this._stopActiveSources();
  }

  /** Call directly in a click/tap handler, before any asynchronous game work. */
  initializeFromUserGesture() {
    if (!this._enabled) return Promise.resolve(false);
    try {
      if (!this._context) {
        if (!this._AudioContextImpl) throw new Error("Interface audio is unsupported.");
        this._context = new this._AudioContextImpl();
      }
      // resume() is intentionally invoked synchronously in the gesture stack.
      const resumed = this._context.resume();
      this._activation = Promise.resolve(resumed)
        .then(() => this._context?.state === "running")
        .catch(() => false);
      return this._activation;
    } catch {
      this._activation = Promise.resolve(false);
      this._reportIssue("Interface sound is blocked. You can try it again.");
      return this._activation;
    }
  }

  async play(eventName) {
    if (!this._enabled) return false;
    const file = NOTIFICATION_SOUND_FILES[eventName];
    if (!file) return false;
    this._lastEvent = eventName;
    try {
      const active = await (this._activation ?? Promise.resolve(false));
      if (!active || this._context?.state !== "running") {
        throw new Error("audio context is not active");
      }
      const buffer = await this._load(eventName, file);
      const source = this._context.createBufferSource();
      source.buffer = buffer;
      source.connect(this._context.destination);
      source.addEventListener?.("ended", () => this._activeSources.delete(source), {
        once: true,
      });
      this._activeSources.add(source);
      source.start(0);
      return true;
    } catch {
      this._reportIssue("Interface sound could not play. The game is still connected.");
      return false;
    }
  }

  retryFromUserGesture() {
    const eventName = this._lastEvent;
    this.initializeFromUserGesture();
    return eventName ? this.play(eventName) : Promise.resolve(false);
  }

  async _load(eventName, file) {
    if (this._buffers.has(eventName)) return this._buffers.get(eventName);
    if (!this._fetch) throw new Error("sound loading is unavailable");
    const pending = (async () => {
      const response = await this._fetch(file, { cache: "force-cache" });
      if (!response.ok) throw new Error("sound asset is unavailable");
      const data = await response.arrayBuffer();
      return this._context.decodeAudioData(data);
    })();
    this._buffers.set(eventName, pending);
    try {
      return await pending;
    } catch (error) {
      this._buffers.delete(eventName);
      throw error;
    }
  }

  _stopActiveSources() {
    for (const source of this._activeSources) {
      try { source.stop(); } catch { /* already stopped */ }
    }
    this._activeSources.clear();
  }

  _reportIssue(message) {
    try { this._onIssue(message); } catch { /* UI reporting is best effort */ }
  }
}

function readPreference(storage) {
  try { return storage?.getItem(SOUND_PREFERENCE_KEY) !== "false"; }
  catch { return true; }
}

export const soundPreferenceKeyForTests = SOUND_PREFERENCE_KEY;
