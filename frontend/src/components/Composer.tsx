import { useState, type FormEvent, type KeyboardEvent } from "react";

interface ComposerProps {
  onSubmit: (question: string) => void;
  disabled: boolean;
  busy?: boolean;
}

export function Composer({ onSubmit, disabled, busy = false }: ComposerProps) {
  const [value, setValue] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    const question = value.trim();
    if (!question || disabled) return;
    onSubmit(question);
    setValue("");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      submit(event);
    }
  }

  return (
    <form onSubmit={submit} className="flex items-end gap-3">
      <label htmlFor="question" className="sr-only">
        Ask a question about your transactions
      </label>
      <textarea
        id="question"
        rows={1}
        value={value}
        disabled={disabled}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask about your finances…"
        className="max-h-40 min-h-[2.75rem] flex-1 resize-y rounded-xl border border-hairline bg-surface px-4 py-2.5 text-slate-100 placeholder:text-slate-500 focus:border-accent-muted focus:ring-1 focus:ring-accent-muted focus:outline-none disabled:opacity-60"
      />
      <button
        type="submit"
        disabled={disabled || value.trim().length === 0}
        className="h-11 rounded-xl bg-accent px-6 font-medium text-accent-ink transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:bg-accent-muted disabled:text-slate-400"
      >
        {busy ? "Sending…" : "Send"}
      </button>
    </form>
  );
}
