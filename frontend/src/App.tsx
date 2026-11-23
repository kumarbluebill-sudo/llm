import { useEffect, useRef, useState } from "react";
import { getBooking, sendApproval, startBooking, streamPlan } from "./api";
import { describe, inr } from "./format";
import type { BookingStatus, Itinerary, Manifest, Stage, Tier } from "./types";

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

      <BookingPanel it={it} />
    </section>
  );
}

const STATE_TEXT: Record<string, string> = {
  DRAFT: "Starting…",
  QUOTED: "Quote locked",
  HELD: "Flights and hotel held",
  AWAITING_APPROVAL: "Waiting for your approval",
  APPROVED: "Approved",
  AUTHORIZED: "Payment authorised (not charged yet)",
  CONFIRMED: "Hotel confirmed",
  TICKETED: "Tickets issued",
  COMPENSATING: "Something failed, releasing everything…",
  CANCELLED: "Cancelled. Nothing was charged.",
  NEEDS_MANUAL: "Needs manual attention from our team",
};

function BookingPanel({ it }: { it: Itinerary }) {
  const [id, setId] = useState<string | null>(null);
  const [status, setStatus] = useState<BookingStatus | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    setId(null);
    setStatus(null);
    setError("");
  }, [it.itinerary_id]);

  useEffect(() => {
    if (!id) return;
    let stop = false;
    const tick = async () => {
      try {
        const st = await getBooking(id);
        if (stop) return;
        setStatus(st);
        if (!st.done) setTimeout(tick, 1000);
      } catch (e) {
        if (!stop) setError((e as Error).message);
      }
    };
    tick();
    return () => {
      stop = true;
    };
  }, [id]);

  async function book() {
    setError("");
    setStarting(true);
    try {
      setId((await startBooking(it.itinerary_id)).booking_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  const reasons = (status?.events.filter((e) => e.state === "AWAITING_APPROVAL").at(-1)?.data.reasons ??
    []) as string[];
  const flightsAndHotel = it.segments.filter((s) => s.type === "flight" || s.type === "hotel");
  const bookable = flightsAndHotel.reduce((n, s) => n + s.cost, 0);

  return (
    <div className="booking">
      {!id && (
        <>
          <button onClick={book} disabled={starting || !it.within_budget}>
            {starting ? "Starting…" : `Book flights and hotel (${inr(bookable)})`}
          </button>
          <small>
            {it.within_budget
              ? "Demo only: no real booking or payment happens. Transfers are not booked."
              : "This plan is over budget. Adjust the request to book."}
          </small>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {status && (
        <div role="status">
          <h3>{STATE_TEXT[status.state] ?? status.state}</h3>
          <ol className="timeline">
            {status.events
              .filter((e) => e.state)
              .map((e) => <li key={e.seq}>{STATE_TEXT[e.state!] ?? e.state}</li>)}
            {status.state === "TICKETED" && status.done && <li>Payment captured</li>}
          </ol>
          {status.state === "AWAITING_APPROVAL" && (
            <div className="approval">
              <ul>{reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              <button onClick={() => id && sendApproval(id, "approved")}>Approve and continue</button>
              <button className="secondary" onClick={() => id && sendApproval(id, "rejected")}>
                Reject and release
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
