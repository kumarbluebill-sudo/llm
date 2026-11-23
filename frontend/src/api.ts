import type { BookingStatus, Itinerary, Manifest, Stage } from "./types";

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
    h.onError(data ? JSON.parse(data).message : "The planner service is not reachable right now. If you are viewing the public demo, the backend has not been deployed yet.");
  });
  return finish;
}

async function json<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ?? `Request failed (${res.status})`);
  return body as T;
}

export async function startBooking(itineraryId: string): Promise<{ booking_id: string }> {
  return json(
    await fetch(`${API}/book`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      // Demo token. A real payment provider's hosted fields would produce this; cards never reach us.
      body: JSON.stringify({ itinerary_id: itineraryId, payment_token: "tok_demo" }),
    }),
  );
}

export async function getBooking(id: string): Promise<BookingStatus> {
  return json(await fetch(`${API}/booking/${id}`));
}

export async function sendApproval(id: string, decision: "approved" | "rejected") {
  return json(
    await fetch(`${API}/booking/${id}/approval`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision }),
    }),
  );
}
