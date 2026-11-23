import type { Itinerary, Manifest, Stage } from "./types";

// Empty in dev (Vite proxy); set VITE_API_URL to the deployed API origin in production.
const API = import.meta.env.VITE_API_URL ?? "";

export interface Handlers {
  onStage: (s: Stage) => void;
  onManifest: (m: Manifest) => void;
  onItinerary: (i: Itinerary) => void;
  onError: (message: string) => void;
}

/** Opens the SSE stream and returns a function that closes it. */
export function streamPlan(query: string, h: Handlers): () => void {
  const es = new EventSource(`${API}/plan/stream?query=${encodeURIComponent(query)}`);
  let finished = false;
  const finish = () => {
    finished = true;
    es.close();
  };
  es.addEventListener("stage", (e) => h.onStage(JSON.parse((e as MessageEvent).data).stage));
  es.addEventListener("manifest", (e) => h.onManifest(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("itinerary", (e) => h.onItinerary(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", () => {
    finish();
    h.onStage("done");
  });
  es.addEventListener("error", (e) => {
    if (finished) return;
    const data = (e as MessageEvent).data;
    finish();
    h.onError(data ? JSON.parse(data).message : "Connection to the planner was lost.");
  });
  return finish;
}
