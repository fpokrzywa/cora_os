import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../types";

interface Props {
  messages: ChatMessage[];
  sessionId: string | null;
  onSend: (text: string) => void;
  sending: boolean;
  loadingConvo: boolean;
  error: string | null;
  selectedAgent: string | null;
  workspaceName: string | null;
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
}: Props) {
  const [input, setInput] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, sending]);

  const submit = () => {
    if (!input.trim() || sending) return;
    onSend(input);
    setInput("");
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
        {sending && (
          <div className="msg msg--assistant">
            <div className="msg__role">Cora</div>
            <div className="msg__bubble msg__bubble--typing">
              <span /> <span /> <span />
            </div>
          </div>
        )}
      </div>

      {error && <div className="chat__error">{error}</div>}

      <div className="chat__composer">
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
          disabled={sending || !input.trim()}
        >
          {sending ? "…" : "Send"}
        </button>
      </div>
    </main>
  );
}
