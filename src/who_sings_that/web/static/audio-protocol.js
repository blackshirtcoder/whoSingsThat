/** Routes validated unsolicited audio controls without owning game state. */
export class AudioCommandRouter {
  constructor({ player, sendCommand, isConnected = () => true, onIssue = () => {} }) {
    this.player = player;
    this.sendCommand = sendCommand;
    this.isConnected = isConnected;
    this.onIssue = onIssue;
  }

  async handle(type, payload) {
    if (type === "prepare_audio") {
      const outcome = await this.player.prepare(payload);
      const report = preparationReport(outcome);
      if (!report || !this.isConnected()) return outcome;
      try {
        await this.sendCommand(report.type, report.payload);
      } catch (error) {
        this.onIssue(publicMessage(error));
      }
      return outcome;
    }
    if (type === "play_audio") return this.player.play(payload);
    if (type === "stop_audio") return this.player.stop(payload);
    this.onIssue("Ignored an unknown audio command.");
    return false;
  }
}

export function preparationReport(outcome) {
  if (
    !outcome ||
    !outcome.identity ||
    typeof outcome.identity.preparationId !== "string"
  ) {
    return null;
  }
  const types = {
    ready: "audio_preparation_ready",
    failed: "audio_preparation_failed",
  };
  const type = types[outcome.outcome];
  if (!type) return null;
  return {
    type,
    payload: { preparation_id: outcome.identity.preparationId },
  };
}

function publicMessage(error) {
  return error instanceof Error ? error.message : "Audio preparation report failed.";
}
