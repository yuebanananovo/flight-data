"""Store complete Google price-insight series, keyed by route, departure and observation date."""
import datetime as dt
import json
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
DATA = Path(__file__).resolve().parents[1] / "data"


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def record(route, dep, points, *, dated=False, airlines=None, observed=None):
    path = DATA / "history" / (dep[:7] + ".json")
    book = read(path)
    entry = book.setdefault(route, {}).setdefault(dep, {})
    for point in points or []:
        try:
            when, price = point
            day = dt.date.fromisoformat(when) if dated else dt.datetime.fromtimestamp(when, JST).date()
            if not isinstance(price, (int, float)) or not 0 < price < 100000000 or day.isoformat() > dep:
                continue
            entry.setdefault("google", {})[day.isoformat()] = int(price)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    if airlines and observed:
        entry.setdefault("airlines", {}).setdefault(observed, {}).update(airlines)
    if entry:
        save(path, book)
    return len(entry.get("google", {}))


def import_saved():
    prices = read(DATA / "prices.json")
    for route, item in prices.get("insights", {}).items():
        record(route, item["dep"], item.get("history"))
    for dep, routes in read(DATA / "trips.json").get("dates", {}).items():
        for route, item in routes.items():
            record(route, dep, item.get("history"), dated=True)
    # Legacy * rows mix calendar observations and Google observations. Keep their
    # original pooled provenance; never label them as daily Google history.
    for observed, route, dep, carrier, price in prices.get("rows", []):
        if carrier not in ("*", "*D"):
            record(route, dep, [], airlines={carrier: price}, observed=observed)


if __name__ == "__main__":
    import_saved()
