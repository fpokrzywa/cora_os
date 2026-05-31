import { useState } from "react";
import { login, register } from "../api";
import type { TokenResponse } from "../types";

interface Props {
  onAuth: (result: TokenResponse) => void;
}

type Mode = "login" | "register";

export function Login({ onAuth }: Props) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const result =
        mode === "login"
          ? await login(email.trim(), password)
          : await register(email.trim(), password, displayName.trim() || undefined);
      onAuth(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setSubmitting(false);
    }
  };

  const isRegister = mode === "register";
  const passwordHint = isRegister ? "Min 8 characters" : undefined;
  const submitLabel = submitting
    ? "…"
    : isRegister
      ? "Create admin account"
      : "Sign in";

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="brand__mark">◆</span>
          <span className="brand__name">Cora</span>
        </div>
        <h1 className="login-title">
          {isRegister ? "Create your admin account" : "Sign in to Cora"}
        </h1>
        <p className="login-subtitle">
          {isRegister
            ? "The first user becomes the bootstrap admin."
            : "Cora AI Operating System"}
        </p>

        <label className="login-field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            disabled={submitting}
          />
        </label>

        {isRegister && (
          <label className="login-field">
            <span>Display name (optional)</span>
            <input
              type="text"
              autoComplete="name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              disabled={submitting}
            />
          </label>
        )}

        <label className="login-field">
          <span>Password</span>
          <input
            type="password"
            autoComplete={isRegister ? "new-password" : "current-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={isRegister ? 8 : 1}
            disabled={submitting}
          />
          {passwordHint && (
            <small className="login-hint">{passwordHint}</small>
          )}
        </label>

        {error && <div className="login-error">{error}</div>}

        <button
          type="submit"
          className="btn btn--primary login-submit"
          disabled={submitting || !email || !password}
        >
          {submitLabel}
        </button>

        <button
          type="button"
          className="login-toggle"
          onClick={() => {
            setMode(isRegister ? "login" : "register");
            setError(null);
          }}
          disabled={submitting}
        >
          {isRegister
            ? "Have an account? Sign in"
            : "First time? Bootstrap admin account →"}
        </button>
      </form>
    </div>
  );
}
