import { useRef, useState } from "react";
import { streamPlan } from "./api";
import { describe, inr } from "./format";
import type { Itinerary, Manifest, Stage, Tier } from "./types";

const EXAMPLES = [
  "RCB vs GT, Dharamshala, 2 people, 25k",
  "MI vs CSK at Mumbai on 12 May for 4 friends under ₹60,000",
  "Weekend in Jaipur for 2, 25k",
];

const STAGE_TEXT: Record<Stage, string> = {
  idle: "",
  planning: "Understanding your request…",
  pricing: "Finding refundable flights and hotels…",
  done: "",
  error: "",
};

const TIERS: Tier[] = ["budget", "mid", "premium"];

export default function App() {
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [stage, setStage] = useState<Stage>("idle");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [itinerary, setItinerary] = useState<Itinerary | null>(null);
  const [error, setError] = useState("");
  const close = useRef<(() => void) | null>(null);

  const busy = stage === "planning" || stage === "pricing";

  function submit(q: string) {
    close.current?.();
    setManifest(null);
    setItinerary(null);
    setError("");
    setStage("planning");
    close.current = streamPlan(q, {
      onStage: setStage,
      onManifest: setManifest,
      onItinerary: setItinerary,
      onError: (m) => {
        setError(m);
        setStage("error");
      },
    });
  }

  return (
    <main>
      <header>
        <h1>Plan a trip</h1>
        <p className="sub">Describe it the way you would to a friend. Refundable options only by default.</p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim().length >= 3) submit(query.trim());
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Trip request"
          placeholder="e.g. RCB vs GT, Dharamshala, 2 people, 25k"
        />
        <button disabled={busy || query.trim().length < 3}>{busy ? "Planning…" : "Plan"}</button>
      </form>
      <div className="chips">
        {EXAMPLES.map((ex) => (
          <button key={ex} type="button" className="chip" disabled={busy} onClick={() => setQuery(ex)}>
            {ex}
          </button>
        ))}
      </div>

      {busy && <p className="status" role="status">{STAGE_TEXT[stage]}</p>}
      {error && <p className="error" role="alert">{error}</p>}

      {manifest && <ManifestSummary m={manifest} />}
      {stage === "done" && manifest && !itinerary && (
        <p className="status">No destination found in that request. Try naming a city.</p>
      )}
      {itinerary && <ItineraryView it={itinerary} />}
    </main>
  );
}

function ManifestSummary({ m }: { m: Manifest }) {
  const pax = m.travelers.adults + m.travelers.children;
  return (
    <section className="chips summary" aria-label="What we understood">
      {m.event && <span className="tag">{m.event.name} · {m.event.venue} · {m.event.date}</span>}
      <span className="tag">{pax} traveller{pax > 1 ? "s" : ""}</span>
      <span className="tag">Budget {inr(m.budget.max_total)}</span>
      <span className="tag">{m.constraints.refundable_only ? "Refundable only" : "Non-refundable allowed"}</span>
    </section>
  );
}

function ItineraryView({ it }: { it: Itinerary }) {
  return (
    <section aria-label="Itinerary">
      <div className="total">
        <div>
          <h2>{it.summary}</h2>
          <span className={it.within_budget ? "ok" : "bad"}>
            {it.within_budget ? "Within budget" : "Over budget"}
          </span>
        </div>
        <strong>{inr(it.total_cost)}</strong>
      </div>

      <div className="tiers">
        {TIERS.map((t) => (
          <div key={t} className={t === it.tier ? "tier active" : "tier"}>
            <span>{t}</span>
            <b>{inr(it.tier_totals[t])}</b>
          </div>
        ))}
      </div>

      <div className="cards">
        {it.segments.map((s, i) => {
          const c = describe(s);
          return (
            <article className="card" key={i}>
              <span className="label">{c.label}</span>
              <h3>{c.title}</h3>
              {c.lines.map((l) => <p key={l}>{l}</p>)}
              <footer>
                <span>{s.cost > 0 ? inr(s.cost) : "—"}</span>
                {s.refundable === true && <span className="badge">Refundable</span>}
              </footer>
            </article>
          );
        })}
      </div>

      {it.warnings.length > 0 && (
        <ul className="warnings">
          {it.warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}

      <div className="actions">
        {it.booking_actions.map((a) => (
          <button key={a.action} disabled title="Booking arrives in v0.5">{a.label}</button>
        ))}
        <small>Booking is not live yet. Prices shown are sample data.</small>
      </div>
    </section>
  );
}
