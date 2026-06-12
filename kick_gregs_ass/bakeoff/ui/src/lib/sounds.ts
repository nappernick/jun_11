/**
 * sounds — fire-and-forget audio cues for the FightArena broadcast treatment.
 *
 * Three ring events have a matching sound, served from public/sounds:
 *   * "contender" — the live challenger crosses the promote line mid-pass
 *     (the CONTENDER! ignite).
 *   * "promoted"  — a completed round where the challenger took the belt.
 *   * "defended"  — a completed round where the champion held.
 *
 * A base element per cue is lazily created once and cached so the decoded buffer
 * is reused. Each play CLONES that base and plays the clone, so two islands that
 * promote/defend on the same tick both sound instead of one rewinding the other
 * (simultaneous-run overlap). Playback is best-effort: browsers block audio until
 * the user has interacted with the page, so the returned promise rejection is
 * swallowed rather than thrown.
 */

export type SoundCue = "contender" | "promoted" | "defended";

const SOURCES: Record<SoundCue, string> = {
  contender: "/sounds/contender_audio.mp3",
  promoted: "/sounds/promoted_audio.mp3",
  defended: "/sounds/defended_audio.mp3",
};

const cache = new Map<SoundCue, HTMLAudioElement>();

function baseElement(cue: SoundCue): HTMLAudioElement | null {
  if (typeof Audio === "undefined") return null;
  let audio = cache.get(cue);
  if (audio === undefined) {
    audio = new Audio(SOURCES[cue]);
    audio.preload = "auto";
    cache.set(cue, audio);
  }
  return audio;
}

/** Play a ring cue. Each call plays an independent clone, so concurrent islands'
 * cues overlap instead of cutting one another off. No-op when audio is
 * unavailable/blocked. */
export function playCue(cue: SoundCue): void {
  const base = baseElement(cue);
  if (base === null) return;
  // Clone so simultaneous plays don't share one element's playhead. The clone
  // reuses the cached element's already-fetched source (same URL), so this adds
  // no network cost. Fall back to the base element if cloneNode is unavailable.
  const audio =
    typeof base.cloneNode === "function" ? (base.cloneNode(true) as HTMLAudioElement) : base;
  try {
    audio.currentTime = 0;
    const result = audio.play();
    if (result !== undefined) {
      // Autoplay policy or transient decode errors — best-effort, ignore.
      result.catch(() => {});
    }
  } catch {
    // Some engines throw synchronously if play() is called too early.
  }
}
