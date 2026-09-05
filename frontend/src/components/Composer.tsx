import { useState, type FormEvent, type KeyboardEvent } from "react";

interface ComposerProps {
  onSubmit: (question: string) => void;
  disabled: boolean;
}

export function Composer({ onSubmit, disabled }: ComposerProps) {
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
        placeholder="How much did I spend in August 2026?"
        className="max-h-40 min-h-[2.75rem] flex-1 resize-y rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 text-slate-100 placeholder:text-slate-600 focus:border-slate-500 focus:ring-1 focus:ring-slate-500 focus:outline-none disabled:opacity-60"
      />
      <button
        type="submit"
        disabled={disabled || value.trim().length === 0}
        className="h-11 rounded-lg bg-slate-200 px-5 font-medium text-slate-900 transition hover:bg-white disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500"
      >
        {disabled ? "Asking…" : "Ask"}
      </button>
    </form>
  );
}
