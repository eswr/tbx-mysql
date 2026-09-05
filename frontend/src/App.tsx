import { useCallback, useRef, useState } from "react";
import { ApiError, askQuestion } from "./api/client";
import { Composer } from "./components/Composer";
import { Conversation, type Turn } from "./components/Conversation";
import { DatabaseStatus, DatabaseStatusAlert } from "./components/DatabaseStatus";
import { useDatabaseStatus } from "./hooks/useDatabaseStatus";
import { clearConversationId, readConversationId, writeConversationId } from "./lib/session";

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState(false);
  // Threading this id is what lets the backend resolve follow-ups like "What about July?".
  const [conversationId, setConversationId] = useState<string | null>(readConversationId);
  const restoredConversationId = useRef(conversationId);
  const inFlight = useRef<AbortController | null>(null);
  const databaseStatus = useDatabaseStatus();

  const send = useCallback(
    async (question: string) => {
      if (databaseStatus.phase !== "healthy") return;
      const id = crypto.randomUUID();
      setTurns((current) => [...current, { id, question, response: null, error: null }]);
      setPending(true);

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      try {
        const response = await askQuestion(question, conversationId, controller.signal);
        setConversationId(response.conversation_id);
        writeConversationId(response.conversation_id);
        setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, response } : turn)));
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        if (cause instanceof ApiError && cause.status === undefined) void databaseStatus.revalidate();
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
    [conversationId, databaseStatus.phase, databaseStatus.revalidate],
  );

  const reset = useCallback(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    setTurns([]);
    setConversationId(null);
    restoredConversationId.current = null;
    clearConversationId();
    setPending(false);
  }, []);

  return (
    <div className="mx-auto flex h-full max-w-4xl flex-col px-4">
      <header className="flex items-center justify-between gap-4 border-b border-hairline py-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Artha</h1>
          <p className="text-sm text-slate-400">
            AI Finance Assistant — every answer grounded in your financial data
          </p>
          <div className="mt-1">
            <DatabaseStatus status={databaseStatus} />
          </div>
        </div>
        <div className="flex items-center gap-3">
          {conversationId && (
            <span className="hidden text-xs text-slate-600 sm:inline" title="Conversation id">
              <span className="font-mono">{conversationId.slice(0, 8)}</span>
              {restoredConversationId.current && <span className="ml-1">· resumed</span>}
            </span>
          )}
          <button
            type="button"
            onClick={reset}
            disabled={turns.length === 0 && conversationId === null}
            className="rounded-lg border border-hairline-strong px-3 py-1.5 text-sm text-slate-300 transition hover:border-accent-muted hover:text-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            New conversation
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto py-6">
        <Conversation
          turns={turns}
          pending={pending}
          disabled={databaseStatus.phase !== "healthy"}
          onSuggestion={send}
        />
      </main>

      <footer className="border-t border-hairline py-4">
        <DatabaseStatusAlert status={databaseStatus} onRetry={() => void databaseStatus.revalidate()} />
        <Composer onSubmit={send} disabled={pending || databaseStatus.phase !== "healthy"} busy={pending} />
        <p className="mt-2 text-xs text-slate-600">
          Answers come from allowlisted query plans over the configured database. Account numbers and UTRs are
          masked before they leave the engine.
        </p>
      </footer>
    </div>
  );
}
