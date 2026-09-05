import { useEffect, useRef } from "react";
import type { ChatResponse } from "../api/types";
import { AnswerCard } from "./AnswerCard";

export interface Turn {
  id: string;
  question: string;
  response: ChatResponse | null;
  error: string | null;
}

export function Conversation({ turns, pending }: { turns: Turn[]; pending: boolean }) {
  const anchor = useRef<HTMLDivElement>(null);

  useEffect(() => {
    anchor.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending]);

  if (turns.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="space-y-6">
      {turns.map((turn) => (
        <section key={turn.id} className="space-y-2">
          <p className="ml-auto w-fit max-w-[85%] rounded-xl rounded-br-sm bg-slate-700/70 px-4 py-2 text-slate-100">
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
  "How much did I spend in August 2026?",
  "Show my largest transactions.",
  "What did I spend at Selection Electronics?",
  "What is my available balance?",
];

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 p-8 text-center">
      <h2 className="text-lg font-medium text-slate-200">Ask a grounded question</h2>
      <p className="mx-auto mt-2 max-w-md text-sm text-slate-400">
        Every answer is computed from the database by an allowlisted query plan. Artha shows how it read your
        question and the records behind the number, and refuses when it cannot answer.
      </p>
      <ul className="mt-4 space-y-1 text-sm text-slate-500">
        {EXAMPLES.map((example) => (
          <li key={example} className="font-mono">
            {example}
          </li>
        ))}
      </ul>
    </div>
  );
}
