export type Tier = "budget" | "mid" | "premium";

export interface Manifest {
  trip_id: string;
  intent: string;
  origin: string | null;
  event: { name: string; venue: string; date: string; time: string | null } | null;
  travelers: { adults: number; children: number };
  budget: { tier: Tier; max_total: number; currency: string };
  constraints: { refundable_only: boolean; max_layovers: number; arrival_buffer_hours: number };
}

export interface Segment {
  type: "flight" | "hotel" | "transfer" | "event";
  details: Record<string, unknown>;
  refundable: boolean | null;
  cost: number;
}

export interface Suggestion {
  code: string;
  title: string;
  from_price: number;
  currency: string;
  duration_min: number | null;
  rating: number | null;
  reviews: number | null;
  url: string | null;
  image: string | null;
  source: string;
}

export interface Itinerary {
  itinerary_id: string;
  summary: string;
  tier: Tier;
  total_cost: number;
  currency: string;
  within_budget: boolean;
  tier_totals: Record<Tier, number>;
  segments: Segment[];
  warnings: string[];
  booking_actions: { label: string; action: string }[];
  suggestions: Suggestion[];
}

export type Stage = "idle" | "planning" | "pricing" | "done" | "error";

export interface BookingEvent {
  seq: number;
  type: string;
  state: string | null;
  data: Record<string, unknown>;
}

export interface BookingStatus {
  booking_id: string;
  state: string;
  done: boolean;
  events: BookingEvent[];
}
