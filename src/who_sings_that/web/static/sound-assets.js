/**
 * Increment this version whenever any packaged notification WAV is replaced.
 * A stable version keeps normal browser caching effective between replacements.
 */
export const SOUND_ASSET_VERSION = "1";

export function soundAssetUrl(filename) {
  return `/static/sounds/${filename}?v=${encodeURIComponent(SOUND_ASSET_VERSION)}`;
}

export const NOTIFICATION_SOUND_FILES = Object.freeze({
  connected: soundAssetUrl("connected.wav"),
  gameCreated: soundAssetUrl("game-created.wav"),
  welcome: soundAssetUrl("welcome.wav"),
  audioReady: soundAssetUrl("audio-ready.wav"),
});

export const AUDIO_READY_SOUND_URL = NOTIFICATION_SOUND_FILES.audioReady;
