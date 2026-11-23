"""Tiny static knowledge base with keyword retrieval (v0.2). Swap for ChromaDB later
behind the same `retrieve` signature."""

FACTS: list[dict[str, str]] = [
    {"city": "Dharamshala", "text": "HPCA Stadium hosts IPL matches. Nearest airport: Gaggal (DGH). Weather is cold and rain-prone in April; allow a 4h arrival buffer."},
    {"city": "Mumbai", "text": "Wankhede Stadium hosts IPL matches. Airport: BOM. Heavy traffic on match days; prefer metro/rail for transfers."},
    {"city": "Bengaluru", "text": "M. Chinnaswamy Stadium hosts RCB home matches. Airport: BLR is ~40 km away; allow 90+ min for transfers."},
    {"city": "Chennai", "text": "MA Chidambaram Stadium hosts CSK home matches. Airport: MAA. Suburban rail and metro serve the venue."},
    {"city": "Kolkata", "text": "Eden Gardens hosts KKR home matches. Airport: CCU."},
    {"city": "Delhi", "text": "Arun Jaitley Stadium hosts DC home matches. Airport: DEL. Metro serves the venue."},
    {"city": "Ahmedabad", "text": "Narendra Modi Stadium hosts GT home matches. Airport: AMD, ~10 km away."},
    {"city": "Hyderabad", "text": "Rajiv Gandhi International Stadium hosts SRH home matches. Airport: HYD."},
    {"city": "Jaipur", "text": "Sawai Mansingh Stadium hosts RR home matches. Airport: JAI."},
    {"city": "Lucknow", "text": "Ekana Stadium hosts LSG home matches. Airport: LKO."},
]

CITIES = [f["city"] for f in FACTS]

# Nearest commercial airport per city (Dharamshala is served by Gaggal).
AIRPORTS = {
    "Dharamshala": "DGH", "Mumbai": "BOM", "Bengaluru": "BLR", "Chennai": "MAA",
    "Kolkata": "CCU", "Delhi": "DEL", "Ahmedabad": "AMD", "Hyderabad": "HYD",
    "Jaipur": "JAI", "Lucknow": "LKO",
}  # fmt: skip


def retrieve(query: str, k: int = 3) -> list[str]:
    low = query.lower()
    hits = [f["text"] for f in FACTS if f["city"].lower() in low]
    return hits[:k]
