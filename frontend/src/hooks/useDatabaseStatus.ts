import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchCapabilities, fetchHealth } from "../api/client";
import type { CapabilitiesResponse, HealthResponse } from "../api/types";

export type DatabasePhase = "loading" | "healthy" | "degraded" | "unreachable";

export interface DatabaseStatusState {
  phase: DatabasePhase;
  health: HealthResponse | null;
  capabilities: CapabilitiesResponse | null;
  capabilitiesError: string | null;
  error: string | null;
}

const INITIAL_STATE: DatabaseStatusState = {
  phase: "loading",
  health: null,
  capabilities: null,
  capabilitiesError: null,
  error: null,
};

export function useDatabaseStatus() {
  const [state, setState] = useState<DatabaseStatusState>(INITIAL_STATE);
  const controllerRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);

  const revalidate = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const generation = ++generationRef.current;
    setState(INITIAL_STATE);

    try {
      const health = await fetchHealth(controller.signal);
      if (generation !== generationRef.current) return;
      if (health.status !== "healthy") {
        setState({
          phase: "degraded",
          health,
          capabilities: null,
          capabilitiesError: null,
          error: `Backend reported database status: ${health.database}.`,
        });
        return;
      }

      setState({ ...INITIAL_STATE, phase: "healthy", health });
      try {
        const capabilities = await fetchCapabilities(controller.signal);
        if (generation !== generationRef.current) return;
        setState({
          phase: "healthy",
          health,
          capabilities,
          capabilitiesError: null,
          error: null,
        });
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        if (generation !== generationRef.current) return;
        const message = cause instanceof ApiError ? cause.message : "Capabilities are unavailable.";
        setState({
          phase: "healthy",
          health,
          capabilities: null,
          capabilitiesError: message,
          error: null,
        });
      }
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (generation !== generationRef.current) return;
      const message = cause instanceof ApiError ? cause.message : "Could not check database health.";
      setState({
        phase: "unreachable",
        health: null,
        capabilities: null,
        capabilitiesError: null,
        error: message,
      });
    }
  }, []);

  useEffect(() => {
    void revalidate();
    return () => {
      generationRef.current += 1;
      controllerRef.current?.abort();
    };
  }, [revalidate]);

  return { ...state, revalidate };
}
