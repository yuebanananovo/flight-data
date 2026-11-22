"""Build the market view using observations already saved by the collector."""
from collections import defaultdict
from datetime import date
from statistics import mean

from fare_history import DATA, read, save


def write_market(days, leads, as_of):
    # Map a historical quote to the same advance-purchase interval as the existing
    # index. A 60-day history of one departure contributes at most one quote per lead.
    rows = []
    for (route, departure), sources in sorted(days.items()):
        dep = date.fromisoformat(departure)
        quotes = {**sources.get("google", {}), **sources.get("calendar", {})}
        for observed, price in sorted(quotes.items()):
            if observed <= as_of and (dep - date.fromisoformat(observed)).days in leads and price > 0:
                rows.append([observed, route, departure, "*", price])

    window = read(DATA / "history" / "holiday-coverage.json")
    cohort = {}
    if window:
        samples = []
        for route in window["routes"]:
            for (r, dep), sources in days.items():
                if r == route and window["start"] <= dep <= window["end"] and sources.get("google"):
                    samples.append(sources["google"])
        daily = defaultdict(list)
        for sample in samples:
            for observed, price in sample.items():
                daily[observed].append(price)
        cohort = {"start": window["start"], "end": window["end"], "routes": window["routes"],
                  "eligible": len(window["observations"]), "samples": len(samples),
                  "observations": sum(map(len, samples)),
                  "days": [[d, len(v), round(mean(v), 2) if len(v) == len(samples) else None]
                           for d, v in sorted(daily.items())]}
    save(DATA / "market-history.json", {"sourceUpdated": read(DATA / "prices.json").get("updated"),
                                        "asOf": as_of, "rows": rows, "cohort": cohort})
