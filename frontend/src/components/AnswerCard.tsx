import type { ChatResponse, Confidence, Refusal } from "../api/types";
import { formatMilliseconds, humanizeKey } from "../lib/format";
import { EvidencePanel } from "./EvidencePanel";
import { InterpretationPanel } from "./InterpretationPanel";

const CONFIDENCE_STYLES: Record<Confidence["level"], string> = {
  high: "border-emerald-700/60 bg-emerald-950/50 text-emerald-300",
  medium: "border-amber-700/60 bg-amber-950/50 text-amber-300",
  low: "border-slate-600 bg-slate-800 text-slate-300",
};

/**
 * A refusal is not an answer. `no_data` still carries evidence of the executed
 * query, so it is rendered as a grounded card with a notice; every other reason
 * is rendered as a refusal with its suggestions.
 */
export function AnswerCard({ response }: { response: ChatResponse }) {
  const refusal = response.refusal;
  const isHardRefusal = refusal !== null && refusal.reason !== "no_data";

  return (
    <article
      className={`rounded-xl border p-4 ${
        isHardRefusal ? "border-slate-600 bg-slate-800/60" : "border-slate-700 bg-slate-800/40"
      }`}
    >
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold tracking-wide text-slate-400 uppercase">
          {isHardRefusal ? "Refused" : "Answer"}
        </span>
        <span
          className={`rounded-full border px-2 py-0.5 text-xs ${CONFIDENCE_STYLES[response.confidence.level]}`}
          title={response.confidence.basis.join(", ")}
        >
          {response.confidence.level} confidence
        </span>
        {refusal && (
          <span className="rounded-full border border-slate-600 bg-slate-900 px-2 py-0.5 font-mono text-xs text-slate-300">
            {refusal.reason}
          </span>
        )}
      </header>

      <p className="text-base leading-relaxed text-slate-100">{response.answer}</p>

      {refusal && <RefusalDetail refusal={refusal} />}

      {response.confidence.basis.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">Basis: {response.confidence.basis.join(" · ")}</p>
      )}

      {response.interpretation && <InterpretationPanel interpretation={response.interpretation} />}
      {response.evidence && <EvidencePanel evidence={response.evidence} />}

      <MetaFooter response={response} />
    </article>
  );
}

function RefusalDetail({ refusal }: { refusal: Refusal }) {
  if (refusal.suggestions.length === 0 && !refusal.supported_capabilities) return null;
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-slate-700/70 bg-slate-900/50 px-4 py-3">
      {refusal.suggestions.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold tracking-wide text-slate-400 uppercase">Try instead</h4>
          <ul className="mt-1 list-inside list-disc text-sm text-slate-300">
            {refusal.suggestions.map((suggestion) => (
              <li key={suggestion}>{suggestion}</li>
            ))}
          </ul>
        </div>
      )}
      {refusal.supported_capabilities && refusal.supported_capabilities.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs font-semibold tracking-wide text-slate-400 uppercase">
            What is supported
          </summary>
          <ul className="mt-1 space-y-0.5 text-xs text-slate-400">
            {refusal.supported_capabilities.map((capability) => (
              <li key={capability}>{capability}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function MetaFooter({ response }: { response: ChatResponse }) {
  const { meta } = response;
  const entries: string[] = [];
  if (meta.engine) entries.push(`engine ${meta.engine}`);
  const understanding = formatMilliseconds(meta.understanding_ms);
  if (understanding) entries.push(`understanding ${understanding}`);
  const queryMs = formatMilliseconds(meta.query_ms);
  if (queryMs) entries.push(`query ${queryMs}`);
  entries.push(meta.llm_calls ? `${meta.llm_calls} LLM call${meta.llm_calls === 1 ? "" : "s"}` : "0 LLM calls");
  if (meta.ollama_model) entries.push(`model ${meta.ollama_model}`);
  if (response.calculation) entries.push(humanizeKey(response.calculation));

  return <footer className="mt-3 font-mono text-xs text-slate-500">{entries.join(" · ")}</footer>;
}
