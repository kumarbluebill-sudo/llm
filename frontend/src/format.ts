import type { Segment } from "./types";

export const inr = (n: number) => `₹${n.toLocaleString("en-IN")}`;

export interface Card {
  label: string;
  title: string;
  lines: string[];
}

const str = (v: unknown) => String(v ?? "");

/** Turns a raw segment into display text. */
export function describe(s: Segment): Card {
  const d = s.details;
  switch (s.type) {
    case "flight":
      return {
        label: d.direction === "out" ? "Outbound flight" : "Return flight",
        title: `${str(d.origin)} → ${str(d.dest)}`,
        lines: [
          `${str(d.day)} · ${str(d.depart)}${d.carrier ? ` · ${str(d.carrier)}` : ""}${d.flight_number ? ` ${str(d.flight_number)}` : ""}`,
          `${inr(Number(d.price))} per traveller`,
          Number(d.layovers) === 0 ? "Non-stop" : `${str(d.layovers)} stop`,
        ],
      };
    case "hotel":
      return {
        label: "Hotel",
        title: str(d.name),
        lines: [
          `${"★".repeat(Number(d.stars))}`,
          `${str(d.nights)} nights · ${inr(Number(d.nightly_price))} per room per night`,
        ],
      };
    case "transfer":
      return {
        label: d.direction === "in" ? "Transfer in" : "Transfer out",
        title: `${str(d.airport)} ⇄ ${str(d.venue)}`,
        lines: [`About ${str(d.minutes)} min by road`],
      };
    case "event":
      return { label: "Event", title: str(d.name), lines: [`${str(d.venue)} · ${str(d.date)}`] };
  }
}
