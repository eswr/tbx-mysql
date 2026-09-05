const CONVERSATION_ID_KEY = "artha.conversation_id";

export function readConversationId(): string | null {
  try {
    return globalThis.sessionStorage.getItem(CONVERSATION_ID_KEY);
  } catch {
    return null;
  }
}

export function writeConversationId(id: string): void {
  try {
    globalThis.sessionStorage.setItem(CONVERSATION_ID_KEY, id);
  } catch {
    // Storage may be disabled or unavailable in private browsing modes.
  }
}

export function clearConversationId(): void {
  try {
    globalThis.sessionStorage.removeItem(CONVERSATION_ID_KEY);
  } catch {
    // Storage may be disabled or unavailable in private browsing modes.
  }
}
