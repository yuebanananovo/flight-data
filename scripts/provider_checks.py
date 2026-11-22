"""One manual, bounded provider check. Only de-identified findings reach Actions."""
import json
import os
from collections import Counter

from collect import CALENDAR_LOCATIONS
from round_trips import Unavailable, fetch, official_evidence

AIRLINES = ("CA", "CZ", "NH", "JL", "MU")
ROOT = "https://www.searchapi.io/api/v1/"


def options(data):
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


def same_airline(row, code):
    return bool(row.get("flights")) and all(
        str(f.get("flight_number", "")).replace(" ", "").startswith(code) for f in row["flights"])


def seller_summary(rows, source):
    facts = Counter()
    for row in rows:
        row = row.get("together") or row
        if not isinstance(row, dict) or not row.get("price") or not row.get("book_with"):
            continue
        official, _ = official_evidence(row, source)
        facts["official" if official is True else "agency" if official is False else "unknown"] += 1
    return dict(facts)


def run():
    config = json.loads(os.environ["PROBE_CONTEXT"])
    headers = {"Authorization": "Bearer " + os.environ["SEARCHAPI_KEY"]}

    def balance():
        data = fetch(ROOT+"me", {}, headers)
        return int(data["account"]["remaining_credits"])

    first, second = balance(), balance()
    if first != config["expected_credits"] or first < 15 or second != first:
        raise RuntimeError("赠送次数或余额自检未通过，未开始搜索。")
    used = 0
    findings = {"balance_before": first, "balance_after_account_check": second}

    def search(params):
        nonlocal used
        if used >= 15:
            return {}
        used += 1
        try:
            return fetch(ROOT+"search", params, headers)
        except Unavailable:
            return {}

    base = dict(engine="google_flights", departure_id=CALENDAR_LOCATIONS["TYO"], arrival_id="SHE",
                flight_type="round_trip", travel_class="economy", currency="JPY", hl="en", gl="jp",
                adults=1, outbound_date=config["out"], return_date=config["back"])
    beijing = dict(base, arrival_id=CALENDAR_LOCATIONS["BJS"], included_airlines="NH,JL,CA")
    beijing_options = options(search(beijing))
    google = {}
    for code in AIRLINES:
        params = beijing if code in ("CA","NH","JL") else base
        booking = config.get("cz_booking") if code == "CZ" else None
        if code == "CZ":
            params = dict(base, included_airlines="CZ")
        if not booking:
            outbound = next((r.get("departure_token") for r in beijing_options if same_airline(r,code) and r.get("departure_token")), None) if params is beijing else config.get("mu_outbound")
            if not outbound:
                google[code] = {"status":"outbound_not_returned"}
                continue
            backs = options(search(dict(params, departure_token=outbound)))
            booking = next((r.get("booking_token") for r in backs if same_airline(r,code) and r.get("booking_token")), None)
        if booking:
            response = search(dict(params, booking_token=booking))
            data = response.get("booking_options")
            google[code] = {"status":"returned" if isinstance(data,list) else "no_booking_response",
                            "seller_flags":seller_summary(data or [],"Google Flights")}
        else:
            google[code] = {"status":"matched_inbound_not_returned"}
    findings["google"] = google
    sky_params = dict(base, engine="skyscanner_flights", departure_id="TYO", arrival_id="BJS")
    sky = search(sky_params)
    findings["sky_beijing"] = {}
    for code in AIRLINES:
        match = next((r for r in sky.get("itineraries") or [] if len(r.get("legs",[]))==2 and
                      all(same_airline(leg,code) for leg in r["legs"])), None)
        findings["sky_beijing"][code] = None if match is None else seller_summary(match.get("booking_options") or [],"Skyscanner")
    grid = dict(base, engine="google_flights_calendar", included_airlines="CZ",
                outbound_date_start=base["outbound_date"],outbound_date_end=base["outbound_date"],
                return_date_start=base["return_date"],return_date_end=base["return_date"])
    cz = {}
    for stop in ("any","nonstop"):
        result = search(dict(grid,stops=stop))
        vals = [r["price"] for r in result.get("calendar",[]) if r.get("departure")==base["outbound_date"] and
                r.get("return")==base["return_date"] and isinstance(r.get("price"),(int,float))]
        cz[stop] = min(vals, default=None)
    single = dict(base, flight_type="one_way", included_airlines="CZ")
    del single["return_date"]
    response = search(single)
    cz["one_way_reference"] = min((r["price"] for r in options(response) if same_airline(r,"CZ") and r.get("price")),default=None)
    # Emit relative checks only; private departure dates and raw quotes stay off GitHub.
    findings["cz"] = {"same_calendar_as_saved":cz["any"]==config["cz_calendar"] if cz["any"] is not None else None,
                      "nonstop_returned":cz["nonstop"] is not None,
                      "nonstop_equals_all":cz["nonstop"]==cz["any"] if None not in (cz["nonstop"],cz["any"]) else None,
                      "one_way_returned":cz["one_way_reference"] is not None}
    findings["calls_attempted"] = used
    findings["balance_after"] = balance()
    print("PROBE_RESULT "+json.dumps(findings,sort_keys=True))


if __name__ == "__main__":
    run()
