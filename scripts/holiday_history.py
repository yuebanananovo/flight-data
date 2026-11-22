"""Recover existing searches, then fill the public 2026/27 holiday benchmark within free quota."""
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from collect import AIRPORTS, PLANS, airline_prices, jst_today
from fare_history import DATA, JST, import_saved, read, record, save

FIRST = dt.date(2026, 12, 15)
LAST = dt.date(2027, 1, 8)
ROUTES = ["TYO-SHE", "SHE-TYO", "TYO-BJS", "BJS-TYO"]
CITIES = {v: k for k, v in AIRPORTS.items()}
PUBLIC_DATES = [(r, (FIRST + dt.timedelta(days=i)).isoformat())
                for i in range((LAST - FIRST).days + 1) for r in ROUTES]


def api(path, key, **params):
    url = "https://serpapi.com/" + path + "?" + urllib.parse.urlencode(dict(params, api_key=key))
    try:
        with urllib.request.urlopen(url, timeout=35) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print("SerpApi 响应", e.code, path.split("/")[0], flush=True)
    except (OSError, ValueError):
        print("SerpApi 读取未完成", path.split("/")[0], flush=True)
    return None


def restore(key):
    # Only restore benchmark queries already present in public collection data, or in the holiday list.
    allowed = set(PUBLIC_DATES)
    allowed.update((r, d) for _, r, d, *_ in read(DATA / "prices.json").get("rows", []))
    allowed.update((r, d) for d, routes in read(DATA / "trips.json").get("dates", {}).items() for r in routes)
    seen = set()
    updated = 0
    cursor = {}
    for _ in range(30):
        page = api("searches.json", key, **cursor)
        if isinstance(page, dict):
            records = page.get("searches") or page.get("data") or []
            if not records:
                print("归档列表字段", sorted(page)[:12], flush=True)
        else:
            records = page or []
        if not records or not isinstance(records, list):
            break
        ids = []
        for item in records:
            meta = item if isinstance(item, dict) else {}
            sid = item if isinstance(item, str) else meta.get("id") or (meta.get("search_metadata") or {}).get("id")
            if not isinstance(sid, str) or not re.fullmatch("[a-fA-F0-9]{24}", sid):
                continue
            ids.append(sid)
            if sid in seen:
                continue
            seen.add(sid)
            hint = meta.get("engine") or (meta.get("search_parameters") or {}).get("engine")
            if hint and hint != "google_flights":
                continue
            res = api("searches/" + sid + ".json", key) or {}
            p = res.get("search_parameters") or {}
            route = str(CITIES.get(p.get("departure_id"))) + "-" + str(CITIES.get(p.get("arrival_id")))
            dep = p.get("outbound_date")
            if (route, dep) not in allowed or p.get("engine") != "google_flights" or str(p.get("type")) != "2":
                continue
            if p.get("currency") != "JPY" or p.get("include_airlines") or p.get("exclude_airlines") or str(p.get("stops", 0)) != "0" or str(p.get("travel_class", 1)) != "1":
                continue
            stamp = res.get("search_metadata", {}).get("created_at", "")
            observed = None
            try:
                observed = dt.datetime.fromisoformat(stamp.replace(" UTC", "+00:00")).astimezone(JST).date().isoformat()
            except (ValueError, TypeError):
                pass
            if record(route, dep, (res.get("price_insights") or {}).get("price_history"), observed=observed, airlines=airline_prices(res)):
                updated += 1
        if not ids or cursor.get("before") == ids[-1]:
            break
        cursor = {"before": ids[-1]}
    print(f"读到 {len(seen)} 条归档索引，恢复 {updated} 条含历史的公开查询。", flush=True)


def main():
    key = os.environ["SERPAPI_KEY"]
    import_saved()
    restore(key)
    today = jst_today()
    account = api("account.json", key) or {}
    name = str(account.get("plan_name", "")).lower()
    left = int(account.get("total_searches_left") or 0)
    try:
        renewal = dt.date.fromisoformat(account["plan_renewal_date"][:10])
        days = min(31, max(1, (renewal - today).days))
    except (KeyError, TypeError, ValueError):
        days = 31
    reserve = sum(len(PLANS["free"]["routes"]) * (1 + 2 * ((today + dt.timedelta(days=i)).weekday() == 0)) for i in range(days))
    hourly = int(account.get("account_rate_limit_per_hour") or 0) - int(account.get("this_hour_searches") or 0)
    budget = max(0, min(left - reserve, hourly)) if "free" in name and os.getenv("PLAN", "free") == "free" else 0
    print(f"免费档确认：{'free' in name}；剩余 {left}，为常规采集预留 {reserve}，本批最多 {budget} 次新搜索。", flush=True)
    attempted_path = DATA / "history" / "holiday-coverage.json"
    previous = read(attempted_path)
    queried = dict(previous.get("queried", {}))
    series = {}
    for route, dep in PUBLIC_DATES:
        entry = read(DATA / "history" / (dep[:7] + ".json")).get(route, {}).get(dep, {})
        n = len(entry.get("google", {}))
        tag = route + "/" + dep
        if not n and not queried.get(tag) and budget > 0 and dep >= today.isoformat():
            a, b = route.split("-")
            budget -= 1
            res = api("search.json", key, engine="google_flights", type="2", currency="JPY", hl="zh-cn", gl="jp",
                      departure_id=AIRPORTS[a], arrival_id=AIRPORTS[b], outbound_date=dep)
            if isinstance(res, dict) and not res.get("error"):
                n = record(route, dep, (res.get("price_insights") or {}).get("price_history"),
                           observed=today.isoformat(), airlines=airline_prices(res))
                queried[tag] = today.isoformat()
            elif not res:
                budget = 0
        series[tag] = n
    save(attempted_path, {"start": FIRST.isoformat(), "end": LAST.isoformat(), "routes": ROUTES,
                          "queried": queried, "observations": series})
    print(f"重点时段有 Google 历史的组合：{sum(n > 0 for n in series.values())}/{len(series)}。", flush=True)


if __name__ == "__main__":
    main()
