// Browser-native voice I/O (Web Speech API) behind a thin, swappable surface.
// This is the zero-dependency first cut: SpeechRecognition for STT, speechSynthesis
// for TTS — no API keys, no server. A cloud STT/TTS provider can replace these
// wrappers later without touching App/ChatPanel, which only import the functions
// below. The backend already speaks the voice contract: stream:true (low latency),
// speakable:true (TTS-clean text), and clean mid-stream cancellation (barge-in).

type SpeechRecognitionAlt = { transcript: string };
type SpeechRecognitionResultLike = { isFinal: boolean; 0: SpeechRecognitionAlt };
type SpeechRecognitionEventLike = {
  results: ArrayLike<SpeechRecognitionResultLike>;
};
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}
type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function sttSupported(): boolean {
  return recognitionCtor() !== null;
}

export function ttsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export interface Recognizer {
  start(): void;
  stop(): void;
}

// Single-utterance recognition: onFinal fires once with the best final transcript,
// then onEnd fires when the mic closes (final result, manual stop, or error).
// Pass onInterim to receive the live in-progress transcript while the user talks.
export function createRecognizer(handlers: {
  onFinal: (text: string) => void;
  onInterim?: (text: string) => void;
  onError?: (error: string) => void;
  onEnd?: () => void;
}): Recognizer | null {
  const Ctor = recognitionCtor();
  if (!Ctor) return null;
  const rec = new Ctor();
  rec.lang = navigator.language || "en-US";
  rec.continuous = false;
  rec.interimResults = !!handlers.onInterim;
  rec.onresult = (e) => {
    let text = "";
    let interim = "";
    for (let i = 0; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) text += r[0].transcript;
      else interim += r[0].transcript;
    }
    handlers.onInterim?.((text + interim).trim());
    text = text.trim();
    if (text) handlers.onFinal(text);
  };
  rec.onerror = (e) => handlers.onError?.(e.error);
  rec.onend = () => handlers.onEnd?.();
  return {
    start: () => {
      try {
        rec.start();
      } catch {
        // start() throws if already running — ignore.
      }
    },
    stop: () => {
      try {
        rec.stop();
      } catch {
        // ignore
      }
    },
  };
}

// Speak text; cancels any current utterance first so a new reply (or a barge-in)
// replaces the old. onEnd fires when speech finishes OR is cancelled.
export function speak(text: string, opts?: { onEnd?: () => void }): void {
  if (!ttsSupported() || !text.trim()) {
    opts?.onEnd?.();
    return;
  }
  const synth = window.speechSynthesis;
  synth.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = navigator.language || "en-US";
  u.onend = () => opts?.onEnd?.();
  u.onerror = () => opts?.onEnd?.();
  synth.speak(u);
}

export function cancelSpeech(): void {
  if (ttsSupported()) window.speechSynthesis.cancel();
}

export interface SpeechStream {
  // Feed a streamed text delta; complete sentences are spoken as they form.
  push(delta: string): void;
  // No more deltas. finalText is the authoritative full reply: if it extends
  // the streamed text, the unstreamed suffix is spoken too; if nothing was
  // streamed, the whole reply is spoken. Fires onEnd once the queue drains.
  finish(finalText?: string): void;
  // Barge-in: drop the queue and stay silent (onEnd will NOT fire).
  cancel(): void;
}

// A sentence boundary the synthesizer can speak up to: terminal punctuation
// (plus closing quotes/brackets) followed by whitespace, or a newline. Requiring
// the trailing whitespace keeps decimals like "3.14" intact.
const SENTENCE_BOUNDARY = /[.!?…]+[)"'’”\]]*\s+|\n+/g;

// Incremental TTS for a streamed reply: sentences are enqueued on the native
// speechSynthesis queue as they complete, so Cora starts talking with the first
// sentence instead of after the last token.
export function createSpeechStream(opts?: { onEnd?: () => void }): SpeechStream {
  let buf = ""; // streamed text not yet handed to the synthesizer
  let consumed = ""; // everything pushed so far, for the finish() suffix check
  let pending = 0;
  let finished = false;
  let cancelled = false;

  const maybeEnd = () => {
    if (finished && pending === 0 && !cancelled) opts?.onEnd?.();
  };
  const enqueue = (text: string) => {
    if (!text.trim() || !ttsSupported()) return;
    pending++;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = navigator.language || "en-US";
    u.onend = () => {
      pending--;
      maybeEnd();
    };
    u.onerror = () => {
      pending--;
      maybeEnd();
    };
    window.speechSynthesis.speak(u);
  };
  const speakCompleteSentences = () => {
    let last = 0;
    SENTENCE_BOUNDARY.lastIndex = 0;
    for (let m; (m = SENTENCE_BOUNDARY.exec(buf)); ) {
      last = m.index + m[0].length;
    }
    if (last > 0) {
      enqueue(buf.slice(0, last));
      buf = buf.slice(last);
    }
  };

  return {
    push: (delta) => {
      if (cancelled || finished) return;
      buf += delta;
      consumed += delta;
      speakCompleteSentences();
    },
    finish: (finalText) => {
      if (cancelled || finished) return;
      finished = true;
      if (finalText) {
        if (finalText.startsWith(consumed)) {
          buf += finalText.slice(consumed.length);
        } else if (!consumed.trim()) {
          buf = finalText;
        }
        // Streamed text diverging from the final reply (rare) → speak only
        // what streamed rather than double-speaking.
      }
      enqueue(buf);
      buf = "";
      maybeEnd(); // nothing to say at all → release the speaking state now
    },
    cancel: () => {
      cancelled = true;
      buf = "";
      cancelSpeech();
    },
  };
}
