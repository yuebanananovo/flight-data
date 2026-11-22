"""Recover existing searches, then fill the public 2026/27 holiday benchmark within free quota."""
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from collect import AIRPORTS, PLANS, airline_prices, jst_today
from fare_history import DATA, import_saved, read, record, save

FIRST = dt.date(2026, 12, 15)
LAST = dt.date(2027, 1, 8)
ROUTES = ["TYO-SHE", "SHE-TYO", "TYO-BJS", "BJS-TYO"]
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


def main():
    key = os.environ["SERPAPI_KEY"]
    import_saved()
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
    deep_checked = dict(previous.get("deep_checked", {}))
    series = {}
    for route, dep in PUBLIC_DATES:
        entry = read(DATA / "history" / (dep[:7] + ".json")).get(route, {}).get(dep, {})
        n = len(entry.get("google", {}))
        tag = route + "/" + dep
        if not n and not deep_checked.get(tag) and budget > 0 and dep >= today.isoformat():
            deep = tag in queried
            a, b = route.split("-")
            budget -= 1
            res = api("search.json", key, engine="google_flights", type="2", currency="JPY", hl="zh-cn", gl="jp",
                      departure_id=AIRPORTS[a], arrival_id=AIRPORTS[b], outbound_date=dep,
                      **({"deep_search": "true"} if deep else {}))
            if isinstance(res, dict) and not res.get("error"):
                n = record(route, dep, (res.get("price_insights") or {}).get("price_history"),
                           observed=today.isoformat(), airlines=airline_prices(res))
                queried[tag] = today.isoformat()
                if deep:
                    deep_checked[tag] = today.isoformat()
            elif not res:
                budget = 0
        series[tag] = n
    save(attempted_path, {"start": FIRST.isoformat(), "end": LAST.isoformat(), "routes": ROUTES,
                          "queried": queried, "deep_checked": deep_checked, "observations": series})
    print(f"重点时段有 Google 历史的组合：{sum(n > 0 for n in series.values())}/{len(series)}。", flush=True)


if __name__ == "__main__":
    main()
