import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, askQuestion } from "./api/client";
import { Composer } from "./components/Composer";
import { Conversation, type Turn } from "./components/Conversation";
import { ConversationList } from "./components/ConversationList";
import { DatabaseStatus, DatabaseStatusAlert } from "./components/DatabaseStatus";
import { useDatabaseStatus } from "./hooks/useDatabaseStatus";
import {
  conversationTitle,
  readConversations,
  type SavedConversation,
  writeConversations,
} from "./lib/conversations";
import { clearConversationId, readConversationId, writeConversationId } from "./lib/session";

export default function App() {
  const [initialState] = useState(() => {
    const id = readConversationId();
    const saved = readConversations();
    const existing = id ? saved.find((conversation) => conversation.id === id) : undefined;
    const conversations =
      id && !existing
        ? [{ id, title: conversationTitle([], id), turns: [], updatedAt: Date.now() }, ...saved]
        : saved;
    return { id, conversations, turns: existing?.turns ?? [] };
  });
  const [turns, setTurns] = useState<Turn[]>(initialState.turns);
  const [conversations, setConversations] = useState<SavedConversation[]>(initialState.conversations);
  const [pending, setPending] = useState(false);
  // Threading this id is what lets the backend resolve follow-ups like "What about July?".
  const [conversationId, setConversationId] = useState<string | null>(initialState.id);
  const restoredConversationId = useRef(initialState.id);
  const inFlight = useRef<AbortController | null>(null);
  const databaseStatus = useDatabaseStatus();

  useEffect(() => writeConversations(conversations), [conversations]);

  const send = useCallback(
    async (question: string) => {
      if (databaseStatus.phase !== "healthy") return;
      const id = crypto.randomUUID();
      const nextTurns = [...turns, { id, question, response: null, error: null } satisfies Turn];
      setTurns(nextTurns);
      setPending(true);

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      try {
        const response = await askQuestion(question, conversationId, controller.signal);
        setConversationId(response.conversation_id);
        writeConversationId(response.conversation_id);
        const completedTurns = nextTurns.map((turn) => (turn.id === id ? { ...turn, response } : turn));
        setTurns(completedTurns);
        setConversations((current) => {
          const withoutCurrent = current.filter(
            (conversation) => conversation.id !== response.conversation_id && conversation.id !== conversationId,
          );
          return [
            {
              id: response.conversation_id,
              title: conversationTitle(completedTurns, response.conversation_id),
              turns: completedTurns,
              updatedAt: Date.now(),
            },
            ...withoutCurrent,
          ];
        });
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
    [conversationId, databaseStatus.phase, databaseStatus.revalidate, turns],
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

  const selectConversation = useCallback((conversation: SavedConversation) => {
    inFlight.current?.abort();
    inFlight.current = null;
    setPending(false);
    setConversationId(conversation.id);
    setTurns(conversation.turns);
    restoredConversationId.current = null;
    writeConversationId(conversation.id);
  }, []);

  return (
    <div className="mx-auto flex h-full max-w-7xl flex-col lg:flex-row">
      <ConversationList
        conversations={conversations}
        activeId={conversationId}
        disabled={pending}
        onNew={reset}
        onSelect={selectConversation}
      />

      <div className="flex min-w-0 flex-1 flex-col px-4">
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
    </div>
  );
}
