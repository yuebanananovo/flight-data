"""Consume one anonymous private queue job; never log a job, date, token or offer."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from collect import AIRPORTS, airline_prices, jst_today
from round_trips import NoRedirect, Provider, Unavailable, save


def post(url, token, payload):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path != "/api/flights/queue/worker"):
        raise ValueError("队列端点配置无效")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        return json.load(response)


def main():
    url, token = os.getenv("HELPER_FLIGHT_QUEUE_URL"), os.getenv("HELPER_FLIGHT_QUEUE_KEY")
    serp = os.getenv("SERPAPI_KEY") or os.getenv("FLY")
    if not (url and token and serp):
        print("本次未启用私有队列。")
        return
    checkpoint = Path(os.environ["RUNNER_TEMP"]) / "flight-private-receipt.json"
    os.umask(0o077)
    if checkpoint.exists():
        receipt = json.loads(checkpoint.read_text())
    else:
        account = Provider("SerpApi", serp, 1, int(os.getenv("QUEUE_SERP_RESERVE") or 0))
        if account.remaining < 1:
            print("可用额度不足，本次未领取。")
            return
        job = post(url, token, {"action":"claim"})["job"]
        if job is None:
            print("暂无可领取请求。")
            return
        receipt = dict(action="finish", id=job["id"], lease=job["lease"], state="query_failed",
                       result=dict(currency="JPY", source="SerpApi / Google Flights", offers=[]))
        save(checkpoint, receipt)
        try:
            a, b = job["route"].split("-")
            day = dt.date.fromisoformat(job["departure"])
            if {a,b} <= AIRPORTS.keys() and "TYO" in (a,b) and a != b and jst_today() <= day:
                result = account.search(dict(engine="google_flights", type="2", currency="JPY", gl="jp", hl="en",
                    departure_id=AIRPORTS[a], arrival_id=AIRPORTS[b], outbound_date=day.isoformat()))
                if any(isinstance(result.get(k), list) for k in ("best_flights","other_flights")):
                    prices = airline_prices(result)
                    receipt["result"]["offers"] = [dict(airline=c, price=v) for c,v in sorted(prices.items(), key=lambda x:x[1])]
                    receipt["state"] = "priced" if prices else "not_returned"
        except (KeyError, ValueError, TypeError, Unavailable):
            pass
        save(checkpoint, receipt)
    for attempt in range(4):
        try:
            if post(url, token, receipt).get("ok") is True:
                checkpoint.unlink()
                print("队列回执已确认。")
                return
        except (OSError, ValueError, KeyError):
            pass
        if attempt < 3:
            time.sleep(2 ** attempt)
    raise RuntimeError("队列回执未确认，已保留本机临时凭据")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit("私有队列未确认完成。")
