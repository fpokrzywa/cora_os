import { useId, useRef, useState } from "react";

interface Props {
  value: File | null;
  onChange: (file: File | null) => void;
  accept?: string;
  maxSizeBytes?: number;
  label?: string;
  helperText?: string;
  disabled?: boolean;
}

function formatKiB(bytes: number): string {
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

// Parse an `accept` string (".txt,.md,text/plain") into extension + mime
// predicates so drag/drop (which bypasses the native input filter) can validate.
function matchesAccept(file: File, accept?: string): boolean {
  if (!accept) return true;
  const tokens = accept
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
  if (tokens.length === 0) return true;
  const name = file.name.toLowerCase();
  const type = (file.type || "").toLowerCase();
  return tokens.some((tok) => {
    if (tok.startsWith(".")) return name.endsWith(tok);
    if (tok.endsWith("/*")) return type.startsWith(tok.slice(0, -1));
    return type === tok;
  });
}

export function FileDropzone({
  value,
  onChange,
  accept,
  maxSizeBytes,
  label = "Upload file",
  helperText,
  disabled = false,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const validate = (file: File): string | null => {
    if (!matchesAccept(file, accept)) {
      return `Unsupported file type. Allowed: ${accept}`;
    }
    if (maxSizeBytes && file.size > maxSizeBytes) {
      return `File is too large (${formatKiB(file.size)}). Max ${formatKiB(
        maxSizeBytes,
      )}.`;
    }
    return null;
  };

  const select = (file: File | null) => {
    if (!file) {
      setError(null);
      onChange(null);
      return;
    }
    const err = validate(file);
    if (err) {
      setError(err);
      onChange(null);
      return;
    }
    setError(null);
    onChange(file);
  };

  const openPicker = () => {
    if (disabled) return;
    inputRef.current?.click();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openPicker();
    }
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    select(e.dataTransfer.files?.[0] ?? null);
  };

  const clear = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (disabled) return;
    setError(null);
    onChange(null);
    if (inputRef.current) inputRef.current.value = "";
  };

  const className =
    "file-dropzone" +
    (dragging ? " file-dropzone--dragging" : "") +
    (disabled ? " file-dropzone--disabled" : "") +
    (value ? " file-dropzone--selected" : "");

  return (
    <div className="file-dropzone-wrap">
      {label && (
        <label className="file-dropzone__label" htmlFor={inputId}>
          {label}
        </label>
      )}
      <div
        className={className}
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        aria-label={label}
        onClick={openPicker}
        onKeyDown={onKeyDown}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          ref={inputRef}
          id={inputId}
          className="file-input-hidden"
          type="file"
          accept={accept}
          disabled={disabled}
          onChange={(e) => select(e.target.files?.[0] ?? null)}
        />

        {value ? (
          <div className="file-dropzone__meta">
            <span className="file-dropzone__meta-icon" aria-hidden>
              📄
            </span>
            <span className="file-dropzone__meta-text">
              <span className="file-dropzone__title">{value.name}</span>
              <span className="file-dropzone__hint">
                {formatKiB(value.size)}
              </span>
            </span>
            <button
              type="button"
              className="file-dropzone__clear"
              onClick={clear}
              disabled={disabled}
              aria-label="Remove selected file"
            >
              ✕
            </button>
          </div>
        ) : (
          <div className="file-dropzone__prompt">
            <span className="file-dropzone__prompt-icon" aria-hidden>
              ⬆
            </span>
            <span className="file-dropzone__title">
              <strong>Click to browse</strong> or drag &amp; drop
            </span>
            {helperText && (
              <span className="file-dropzone__hint">{helperText}</span>
            )}
          </div>
        )}
      </div>
      {error && <div className="file-dropzone__error">{error}</div>}
    </div>
  );
}
