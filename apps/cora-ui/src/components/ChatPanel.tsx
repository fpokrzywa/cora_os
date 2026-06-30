import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../types";
import { captureScreenFrame } from "../screenCapture";
import {
  createRecognizer,
  sttSupported,
  ttsSupported,
  type Recognizer,
} from "../voice/speech";

interface Props {
  messages: ChatMessage[];
  sessionId: string | null;
  onSend: (text: string, screenImage?: string | null) => void;
  sending: boolean;
  loadingConvo: boolean;
  error: string | null;
  selectedAgent: string | null;
  workspaceName: string | null;
  voiceMode: boolean;
  onToggleVoice: () => void;
  speaking: boolean;
  onBargeIn: () => void;
}

export function ChatPanel({
  messages,
  sessionId,
  onSend,
  sending,
  loadingConvo,
  error,
  selectedAgent,
  workspaceName,
  voiceMode,
  onToggleVoice,
  speaking,
  onBargeIn,
}: Props) {
  const [input, setInput] = useState("");
  const [screenImage, setScreenImage] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [listening, setListening] = useState(false);
  const recognizerRef = useRef<Recognizer | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const micAvailable = sttSupported();
  const ttsAvailable = ttsSupported();

  // Tap-to-talk: transcribe one utterance and send it. Tapping while Cora is
  // streaming or speaking barges in first (aborts the reply), then listens.
  const toggleMic = () => {
    if (listening) {
      recognizerRef.current?.stop();
      return;
    }
    if (!micAvailable) return;
    if (sending || speaking) onBargeIn();
    const rec = createRecognizer({
      onFinal: (text) => onSend(text),
      onEnd: () => {
        setListening(false);
        recognizerRef.current = null;
      },
      onError: () => {
        setListening(false);
        recognizerRef.current = null;
      },
    });
    if (!rec) return;
    recognizerRef.current = rec;
    setListening(true);
    rec.start();
  };

  useEffect(() => () => recognizerRef.current?.stop(), []);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, sending]);

  const submit = () => {
    if ((!input.trim() && !screenImage) || sending) return;
    onSend(input, screenImage);
    setInput("");
    setScreenImage(null);
  };

  const shareScreen = async () => {
    if (capturing || sending) return;
    setCapturing(true);
    try {
      const frame = await captureScreenFrame();
      if (frame) setScreenImage(frame);
    } finally {
      setCapturing(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <main className="chat">
      <header className="chat__header">
        <div>
          <h1 className="chat__title">Cora</h1>
          <p className="chat__subtitle">
            <span className="chat__orchestration">Orchestration: ATLAS</span>
            <span className="chat__subtitle-sep"> · </span>
            <span>
              Agent:{" "}
              <span className={`agent-badge agent-badge--${(selectedAgent ?? "Cora").toLowerCase()}`}>
                {selectedAgent ?? "Cora"}
              </span>
            </span>
            {workspaceName && (
              <>
                <span className="chat__subtitle-sep"> · </span>
                <span>
                  Workspace:{" "}
                  <span className="agent-badge">{workspaceName}</span>
                </span>
              </>
            )}
            <span className="chat__subtitle-sep"> · </span>
            <span>
              {sessionId
                ? `session ${sessionId.slice(0, 8)}`
                : "AI Operating System"}
            </span>
          </p>
        </div>
      </header>

      <div className="chat__messages" ref={listRef}>
        {loadingConvo && <div className="chat__hint">Loading conversation…</div>}
        {!loadingConvo && messages.length === 0 && (
          <div className="chat__welcome">
            <div className="chat__welcome-mark">◆</div>
            <h2>How can I help?</h2>
            <p>Start a conversation with Cora.</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={m.id ?? i} className={`msg msg--${m.role}`}>
            <div className="msg__role">{m.role === "user" ? "You" : "Cora"}</div>
            {m.role === "user" ? (
              <div className="msg__bubble">{m.content}</div>
            ) : (
              <div className="msg__bubble msg__bubble--md">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ node, ...props }) => (
                      <a {...props} target="_blank" rel="noopener noreferrer" />
                    ),
                  }}
                >
                  {m.content}
                </ReactMarkdown>
              </div>
            )}
          </div>
        ))}
        {sending && messages[messages.length - 1]?.role !== "assistant" && (
          <div className="msg msg--assistant">
            <div className="msg__role">Cora</div>
            <div className="msg__bubble msg__bubble--typing">
              <span /> <span /> <span />
            </div>
          </div>
        )}
      </div>

      {error && <div className="chat__error">{error}</div>}

      {screenImage && (
        <div className="composer__attachment">
          <img src={screenImage} alt="Shared screenshot" className="composer__thumb" />
          <span>Screenshot attached — sent to the local vision model on send.</span>
          <button
            className="composer__attachment-remove"
            onClick={() => setScreenImage(null)}
            disabled={sending}
            aria-label="Remove screenshot"
          >
            ×
          </button>
        </div>
      )}

      {(listening || speaking) && (
        <div className="chat__voice-status">
          {listening ? "● Listening…" : "🔊 Cora is speaking — tap the mic to interrupt"}
        </div>
      )}

      <div className="chat__composer">
        {ttsAvailable && (
          <button
            className={`btn composer__voice${voiceMode ? " composer__voice--on" : ""}`}
            onClick={onToggleVoice}
            title={
              voiceMode
                ? "Voice replies on — Cora speaks her answers"
                : "Turn on spoken replies (TTS-clean, read aloud)"
            }
          >
            {voiceMode ? "🔊 Voice on" : "🔈 Voice"}
          </button>
        )}
        {micAvailable && (
          <button
            className={`btn composer__mic${listening ? " composer__mic--active" : ""}`}
            onClick={toggleMic}
            disabled={capturing}
            title={listening ? "Stop listening" : "Tap to talk"}
            aria-label={listening ? "Stop listening" : "Speak to Cora"}
          >
            {listening ? "● Stop" : "🎤"}
          </button>
        )}
        <button
          className="btn composer__share"
          onClick={shareScreen}
          disabled={sending || capturing}
          title="Share a screenshot with Cora (one frame; you pick what to share)"
        >
          {capturing ? "…" : screenImage ? "Re-share" : "Share screen"}
        </button>
        <textarea
          className="composer__input"
          placeholder="Message Cora…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={sending}
        />
        <button
          className="btn btn--primary composer__send"
          onClick={submit}
          disabled={sending || (!input.trim() && !screenImage)}
        >
          {sending ? "…" : "Send"}
        </button>
      </div>
    </main>
  );
}
