import type { Turn } from "../components/Conversation";

const CONVERSATIONS_KEY = "artha.conversations";
const MAX_CONVERSATIONS = 50;

export interface SavedConversation {
  id: string;
  title: string;
  turns: Turn[];
  updatedAt: number;
}

export function readConversations(): SavedConversation[] {
  try {
    const value: unknown = JSON.parse(globalThis.sessionStorage.getItem(CONVERSATIONS_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];

    return value
      .filter(isSavedConversation)
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, MAX_CONVERSATIONS);
  } catch {
    return [];
  }
}

export function writeConversations(conversations: SavedConversation[]): void {
  try {
    globalThis.sessionStorage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify(
        [...conversations]
          .sort((left, right) => right.updatedAt - left.updatedAt)
          .slice(0, MAX_CONVERSATIONS),
      ),
    );
  } catch {
    // Storage may be disabled, unavailable, or full.
  }
}

export function conversationTitle(turns: Turn[], fallbackId: string): string {
  const firstQuestion = turns[0]?.question.trim();
  if (firstQuestion) return firstQuestion;
  return `Conversation ${fallbackId.slice(0, 8)}`;
}

function isSavedConversation(value: unknown): value is SavedConversation {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<SavedConversation>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.updatedAt === "number" &&
    Array.isArray(candidate.turns)
  );
}
