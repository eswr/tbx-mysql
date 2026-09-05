import type { SavedConversation } from "../lib/conversations";

interface ConversationListProps {
  conversations: SavedConversation[];
  activeId: string | null;
  disabled: boolean;
  onNew: () => void;
  onSelect: (conversation: SavedConversation) => void;
}

export function ConversationList({
  conversations,
  activeId,
  disabled,
  onNew,
  onSelect,
}: ConversationListProps) {
  return (
    <aside className="border-b border-hairline bg-surface/60 lg:w-72 lg:shrink-0 lg:border-r lg:border-b-0">
      <div className="flex items-center justify-between gap-3 px-4 py-4 lg:block">
        <h2 className="text-sm font-semibold text-slate-300">Conversations</h2>
        <button
          type="button"
          onClick={onNew}
          aria-label="New conversation"
          className="rounded-lg border border-hairline-strong px-3 py-1.5 text-sm text-slate-200 transition hover:border-accent-muted hover:text-white disabled:cursor-not-allowed disabled:opacity-40 lg:mt-3 lg:w-full"
        >
          + New conversation
        </button>
      </div>

      <nav aria-label="Conversations" className="flex gap-2 overflow-x-auto px-4 pb-4 lg:block lg:space-y-1 lg:overflow-y-auto">
        {conversations.length === 0 ? (
          <p className="min-w-56 text-sm text-slate-500 lg:min-w-0">Your conversations will appear here.</p>
        ) : (
          conversations.map((conversation) => {
            const active = conversation.id === activeId;
            return (
              <button
                key={conversation.id}
                type="button"
                disabled={disabled}
                aria-current={active ? "page" : undefined}
                onClick={() => onSelect(conversation)}
                title={conversation.title}
                className={`min-w-56 rounded-lg px-3 py-2 text-left transition lg:block lg:w-full lg:min-w-0 ${
                  active
                    ? "bg-accent-muted/60 text-white"
                    : "text-slate-400 hover:bg-surface-raised hover:text-slate-200"
                } disabled:cursor-not-allowed disabled:opacity-50`}
              >
                <span className="block truncate text-sm font-medium">{conversation.title}</span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  {conversation.turns.length} {conversation.turns.length === 1 ? "turn" : "turns"}
                </span>
              </button>
            );
          })
        )}
      </nav>
    </aside>
  );
}
