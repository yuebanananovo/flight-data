"""Publish history coverage and historically observed changes over a week at each lead."""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from collect import CAL_LEADS, jst_today
from market_history import write_market
from fare_history import DATA, read, save


def main():
    today = jst_today()
    calendars = sorted((DATA / "calendars").glob("????-??.json"))
    histories = sorted((DATA / "history").glob("????-??.json"))
    days = defaultdict(lambda: defaultdict(dict))
    for f in calendars:
        for observed, routes in read(f).items():
            for route, deps in routes.items():
                for dep, price in deps.items():
                    days[route, dep]["calendar"][observed] = price
    for f in histories:
        for route, deps in read(f).items():
            for dep, fields in deps.items():
                days[route, dep]["google"].update(fields.get("google", {}))
    data = read(DATA / "prices.json")
    as_of = max((row[0] for row in data.get("rows", [])), default=today.isoformat())
    write_market(days, CAL_LEADS, as_of)
    buckets = defaultdict(list)
    for (route, dep), sources in days.items():
        depart = date.fromisoformat(dep)
        if depart >= today:
            continue
        # Compare like with like; count each completed departure at most once per lead.
        observed_leads = set()
        for source in ("calendar", "google"):
            series = sources.get(source, {})
            for when, price in series.items():
                obs = date.fromisoformat(when)
                lead = (depart - obs).days
                after = series.get((obs + timedelta(days=7)).isoformat())
                if 7 <= lead <= 365 and price > 0 and after and lead not in observed_leads:
                    buckets[route, str(lead)].append(after / price - 1)
                    observed_leads.add(lead)
    timing = defaultdict(dict)
    for (route, lead), ratios in buckets.items():
        lower = [v for v in ratios if v < 0]
        higher = [v for v in ratios if v > 0]
        timing[route][lead] = {"n": len(ratios), "lower": len(lower), "higher": len(higher),
                               "saving": round(-median(lower), 4) if lower else 0,
                               "increase": round(median(higher), 4) if higher else 0}
    save(DATA / "history-index.json", {"calendar": [f.stem for f in calendars],
                                      "google": [f.stem for f in histories],
                                      "timing": timing, "asOf": today.isoformat()})


if __name__ == "__main__":
    main()
