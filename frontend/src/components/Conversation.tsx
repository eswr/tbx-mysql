import { useEffect, useRef } from "react";
import type { ChatResponse } from "../api/types";
import { AnswerCard } from "./AnswerCard";

export interface Turn {
  id: string;
  question: string;
  response: ChatResponse | null;
  error: string | null;
}

export function Conversation({
  turns,
  pending,
  disabled,
  onSuggestion,
}: {
  turns: Turn[];
  pending: boolean;
  disabled: boolean;
  onSuggestion: (question: string) => void;
}) {
  const anchor = useRef<HTMLDivElement>(null);

  useEffect(() => {
    anchor.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending]);

  if (turns.length === 0) {
    return <EmptyState onSelect={onSuggestion} disabled={pending || disabled} />;
  }

  return (
    <div className="space-y-6">
      {turns.map((turn) => (
        <section key={turn.id} className="space-y-3">
          <p className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-sm bg-accent px-4 py-2 font-medium text-accent-ink">
            {turn.question}
          </p>
          {turn.response && <AnswerCard response={turn.response} />}
          {turn.error && (
            <p role="alert" className="rounded-xl border border-red-900/70 bg-red-950/40 p-4 text-sm text-red-300">
              {turn.error}
            </p>
          )}
          {!turn.response && !turn.error && <Thinking />}
        </section>
      ))}
      <div ref={anchor} />
    </div>
  );
}

function Thinking() {
  return (
    <p className="flex items-center gap-2 text-sm text-slate-400" aria-live="polite">
      <span className="h-2 w-2 animate-pulse rounded-full bg-slate-400" />
      Interpreting and querying…
    </p>
  );
}

const EXAMPLES = [
  "What is my total available balance?",
  "How much did I spend last month?",
  "How many accounts per bank?",
  "Show my largest transactions.",
];

function EmptyState({ onSelect, disabled }: { onSelect: (question: string) => void; disabled: boolean }) {
  return (
    <div className="rounded-xl border border-hairline bg-surface p-6">
      <h2 className="text-lg font-medium text-slate-100">Ask about your finances</h2>
      <p className="mt-2 max-w-2xl text-sm text-slate-400">
        Balances, spend, inflow, banks, transaction search — every answer computed directly from the database
        and grounded in evidence.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            disabled={disabled}
            onClick={() => onSelect(example)}
            className="rounded-full border border-hairline-strong bg-surface-raised px-4 py-2 text-sm text-slate-200 transition hover:border-accent-muted hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}
