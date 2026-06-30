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
export function createRecognizer(handlers: {
  onFinal: (text: string) => void;
  onError?: (error: string) => void;
  onEnd?: () => void;
}): Recognizer | null {
  const Ctor = recognitionCtor();
  if (!Ctor) return null;
  const rec = new Ctor();
  rec.lang = navigator.language || "en-US";
  rec.continuous = false;
  rec.interimResults = false;
  rec.onresult = (e) => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) text += r[0].transcript;
    }
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
