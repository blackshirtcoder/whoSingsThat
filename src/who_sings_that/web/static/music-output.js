import { AUDIO_READY_SOUND_URL } from "./sound-assets.js";

/**
 * Owns the human-audible music-output check. The check element is deliberately
 * separate from the BrowserAudioPlayer's server-controlled mystery-song media.
 */
export class MusicOutputWorkflow {
  constructor({
    mediaElement,
    mysteryPlayer,
    sendCommand,
    onChange = () => {},
    onError = () => {},
  }) {
    this.media = mediaElement;
    this.player = mysteryPlayer;
    this.sendCommand = sendCommand;
    this.onChange = onChange;
    this.onError = onError;
    this.state = "idle";
    this.playbackStarted = false;
    this.pending = false;
    this.media.preload = "auto";
    this.media.src = AUDIO_READY_SOUND_URL;
    this.media.addEventListener("playing", () => {
      if (this.state !== "checking") return;
      this.playbackStarted = true;
      this._emit();
    });
    this.media.addEventListener("ended", () => this._emit());
    this.media.addEventListener("error", () => {
      if (this.state !== "checking") return;
      this.state = "error";
      this.onError("The output-check chime could not play. Try again or cancel.");
      this._emit();
    });
  }

  begin() {
    if (this.pending) return false;
    this.state = "checking";
    this.playbackStarted = false;
    this._emit();
    this._playCheck();
    return true;
  }

  replay() {
    if (this.pending || !["checking", "error"].includes(this.state)) return false;
    this.state = "checking";
    this.playbackStarted = false;
    this._emit();
    this._playCheck();
    return true;
  }

  async confirmHeard() {
    if (this.pending || this.state !== "checking" || !this.playbackStarted) return false;
    this.pending = true;
    this.state = "enabling";
    // Do not pause the check. Firefox for Android must be allowed to play the
    // complete WAV instead of treating the first playback event as completion.
    this.player.confirmOutputFromDedicatedCheck();
    this._emit();
    try {
      await this.sendCommand("set_audio_preference", { audio_enabled: true });
      await this.sendCommand("set_audio_capability", { browser_audio_usable: true });
      this.state = "enabled";
      return true;
    } catch (error) {
      this.player.reset("Music output was not enabled.");
      this.state = "error";
      this.onError(publicMessage(error));
      return false;
    } finally {
      this.pending = false;
      this._emit();
    }
  }

  async cancel() {
    if (this.pending) return false;
    this._stopCheck();
    this.player.reset("Music playback is off on this device.");
    this.state = "idle";
    this._emit();
    return this._disableOnServer();
  }

  async disable() {
    if (this.pending) return false;
    this._stopCheck();
    this.player.reset("Music playback is off on this device.");
    this.state = "idle";
    this._emit();
    return this._disableOnServer();
  }

  reset() {
    this._stopCheck();
    this.player.reset();
    this.pending = false;
    this.state = "idle";
    this.playbackStarted = false;
    this._emit();
  }

  async _disableOnServer() {
    this.pending = true;
    this._emit();
    try {
      // Preference is the target-selection gate. Clear it first so this device
      // cannot remain selectable if the later capability event is delayed.
      await this.sendCommand("set_audio_preference", { audio_enabled: false });
      await this.sendCommand("set_audio_capability", { browser_audio_usable: false });
      return true;
    } catch (error) {
      this.onError(publicMessage(error));
      return false;
    } finally {
      this.pending = false;
      this._emit();
    }
  }

  _playCheck() {
    try {
      this.media.currentTime = 0;
      const promise = this.media.play();
      if (promise && typeof promise.catch === "function") {
        void promise.catch(() => this._playFailed());
      }
    } catch {
      this._playFailed();
    }
  }

  _playFailed() {
    if (this.state !== "checking") return;
    this.state = "error";
    this.onError("The output-check chime was blocked. Tap Play again to retry.");
    this._emit();
  }

  _stopCheck() {
    this.media.pause();
    try { this.media.currentTime = 0; } catch { /* media may not be loaded */ }
  }

  _emit() {
    this.onChange({
      state: this.state,
      pending: this.pending,
      playbackStarted: this.playbackStarted,
    });
  }
}

function publicMessage(error) {
  return error instanceof Error ? error.message : "Music output could not be changed.";
}

export const musicOutputCheckUrlForTests = AUDIO_READY_SOUND_URL;
