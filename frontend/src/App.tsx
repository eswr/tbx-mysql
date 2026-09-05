import { useCallback, useRef, useState } from "react";
import { ApiError, askQuestion } from "./api/client";
import { Composer } from "./components/Composer";
import { Conversation, type Turn } from "./components/Conversation";

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState(false);
  // Threading this id is what lets the backend resolve follow-ups like "What about July?".
  const [conversationId, setConversationId] = useState<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const send = useCallback(
    async (question: string) => {
      const id = crypto.randomUUID();
      setTurns((current) => [...current, { id, question, response: null, error: null }]);
      setPending(true);

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      try {
        const response = await askQuestion(question, conversationId, controller.signal);
        setConversationId(response.conversation_id);
        setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, response } : turn)));
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        const message =
          cause instanceof ApiError ? cause.message : "Something went wrong while contacting the backend.";
        setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, error: message } : turn)));
      } finally {
        if (inFlight.current === controller) {
          inFlight.current = null;
          setPending(false);
        }
      }
    },
    [conversationId],
  );

  const reset = useCallback(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    setTurns([]);
    setConversationId(null);
    setPending(false);
  }, []);

  return (
    <div className="mx-auto flex h-full max-w-4xl flex-col px-4">
      <header className="flex items-center justify-between gap-4 border-b border-slate-800 py-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Artha</h1>
          <p className="text-sm text-slate-400">Database-grounded financial Q&amp;A</p>
        </div>
        <div className="flex items-center gap-3">
          {conversationId && (
            <span className="hidden font-mono text-xs text-slate-600 sm:inline" title="Conversation id">
              {conversationId.slice(0, 8)}
            </span>
          )}
          <button
            type="button"
            onClick={reset}
            disabled={turns.length === 0}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 transition hover:border-slate-500 hover:text-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            New conversation
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto py-6">
        <Conversation turns={turns} pending={pending} />
      </main>

      <footer className="border-t border-slate-800 py-4">
        <Composer onSubmit={send} disabled={pending} />
        <p className="mt-2 text-xs text-slate-600">
          Answers come from allowlisted query plans over the configured database. Account numbers and UTRs are
          masked before they leave the engine.
        </p>
      </footer>
    </div>
  );
}
