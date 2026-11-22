"""Collect a browser-exported round-trip queue into an offline, importable journal."""
import argparse
import datetime as dt
import fcntl
import getpass
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from collect import AIRPORTS, CALENDAR_LOCATIONS, jst_today
from quote_records import fingerprint, jst_now, observation, private_key_for


CITIES = {"SHE", "BJS", "SHA", "DLC"}


class Unavailable(Exception):
    """A provider cannot serve the request within the available allowance."""


class Pending(Unavailable):
    """A request has not been sent due to the available budget or credentials."""


class ProviderLimit(Pending):
    """Authentication or quota failure blocks further requests to this provider."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, params, headers):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    client = urllib.request.build_opener(NoRedirect())
    try:
        with client.open(urllib.request.Request(url, headers=headers), timeout=80) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            raise ProviderLimit(f"HTTP {exc.code}") from None
        raise Unavailable(f"HTTP {exc.code}") from None
    except (OSError, ValueError):
        raise Unavailable("服务连接或响应异常") from None
    if isinstance(result, dict) and "hasn't returned any results" in str(result.get("error", "")):
        return {"best_flights": [], "other_flights": [], "calendar": [], "booking_options": []}
    if not isinstance(result, dict) or result.get("error"):
        raise Unavailable("服务未返回有效结果")
    return result


class Provider:
    def __init__(self, name, key, limit, reserve):
        self.name = name
        self.key = key
        self.remaining = 0
        self.calls = 0
        self.headers = {"Authorization": "Bearer " + key} if name == "SearchApi" else {}
        if name == "SearchApi":
            account = fetch("https://www.searchapi.io/api/v1/me", {}, self.headers)
            credits = account.get("account", {}).get("remaining_credits")
            usage = account.get("api_usage", {})
            hourly = usage.get("hourly_rate_limit")
            used = usage.get("searches_this_hour")
        else:
            account = fetch("https://serpapi.com/account.json", {"api_key": key}, {})
            credits = account.get("total_searches_left")
            hourly = account.get("account_rate_limit_per_hour")
            used = account.get("this_hour_searches")
        if not all(type(n) is int and n >= 0 for n in (credits, hourly, used)):
            raise Unavailable(name + " 余额或小时用量无法确认")
        self.credits = credits
        self.remaining = max(0, min(limit, credits - reserve, hourly - used))
        print(f"{name} 剩余 {credits} 次，本次最多可发 {self.remaining} 次搜索。")

    def search(self, params):
        if self.remaining <= 0:
            raise Pending(self.name + " 本轮可用额度已用完")
        self.remaining -= 1
        self.calls += 1
        try:
            if self.name == "SearchApi":
                return fetch("https://www.searchapi.io/api/v1/search", params, self.headers)
            return fetch("https://serpapi.com/search.json", dict(params, api_key=self.key), {})
        except ProviderLimit:
            self.remaining = 0
            raise


def amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validated_queue(path, today):
    obj = json.loads(path.read_text())
    if not isinstance(obj, dict) or obj.get("version") != 1 or not isinstance(obj.get("requests"), list):
        raise ValueError("文件不是机票页导出的待查队列")
    requests = []
    for r in obj["requests"]:
        route = r["route"]
        if route not in {"TYO-" + city for city in CITIES}:
            raise ValueError("只支持从东京往返沈阳、北京、上海或大连")
        depart, back = (dt.date.fromisoformat(r[key]) for key in ("departure", "returnDate"))
        if back < depart:
            raise ValueError("请检查去回程顺序")
        flex = [r.get(key, 0) for key in ("flexOut", "flexBack")]
        if any(type(n) is not int or not 0 <= n <= 2 for n in flex):
            raise ValueError("日期可调整范围为 0–2 天")
        q = dict(route=route, departure=depart.isoformat(), returnDate=back.isoformat(),
                 flexOut=flex[0], flexBack=flex[1])
        if q not in requests:
            requests.append(q)
    return requests


def calendar(search, base, q, today):
    start, end = [dt.date.fromisoformat(base[k]) for k in ("outbound_date", "return_date")]
    def date_window(day, flex):
        return [max(today, day + dt.timedelta(days=i)).isoformat() for i in range(-flex, flex + 1)]
    outs, backs = date_window(start, q["flexOut"]), date_window(end, q["flexBack"])
    expected = sorted({(d, r) for d in outs for r in backs if d <= r})
    result = search(dict(base, engine="google_flights_calendar", outbound_date_start=min(outs),
                         outbound_date_end=max(outs), return_date_start=min(backs), return_date_end=max(backs)))
    if not isinstance(result.get("calendar"), list):
        raise Unavailable("日历响应缺少 calendar 列表")
    prices = {}
    for row in result["calendar"]:
        key = (row.get("departure"), row.get("return"))
        if key in expected and amount(row.get("price")):
            prices[key] = min(prices.get(key, row["price"]), row["price"])
    return [dict(departure=d, **{"return": r}, price=prices.get((d, r)),
                 status="priced" if (d, r) in prices else "not_returned") for d, r in expected]


def airport_time(airport):
    day, time = airport.get("date"), airport.get("time", "")
    if not day and isinstance(time, str) and len(time) >= 16 and time[10] in ("T", " "):
        day, time = time[:10], time[11:]
    return day, time


def itinerary(offer):
    result = {k: offer[k] for k in ("type", "total_duration", "layovers", "baggage", "separate_tickets") if k in offer}
    result["flights"] = []
    result["segments"] = []
    for flight in offer["flights"]:
        leg = dict(flight)
        for k in ("departure_airport", "arrival_airport"):
            place = flight.get(k, {})
            day, time = airport_time(place)
            leg[k] = dict(place, date=day, time=time)
        result["flights"].append(leg)
        number = leg.get("flight_number")
        airline_code = re.match(r"^([A-Z0-9]{2})\s*\d", number or "")
        result["segments"].append(dict(marketing_airline=leg.get("marketing_airline") or
                (airline_code.group(1) if airline_code else None), flight_number=number,
                reported_airline=leg.get("airline"), operating_airline=leg.get("operating_airline"),
                departure_airport=leg["departure_airport"], arrival_airport=leg["arrival_airport"],
                duration_minutes=leg.get("duration")))
    segments = result["segments"]
    connections = []
    for i, (arriving, onward) in enumerate(zip(segments, segments[1:])):
        a, b = arriving["arrival_airport"].get("id"), onward["departure_airport"].get("id")
        numbers = [str(s["flight_number"] or "").replace(" ", "").upper() for s in (arriving, onward)]
        kind = "unconfirmed"
        if a and b and (a != b or (all(numbers) and numbers[0] != numbers[1])):
            kind = "transfer"
        elif a and a == b and numbers[0] and numbers[0] == numbers[1]:
            kind = "same_number_stop"
        layovers = offer.get("layovers") or []
        layover = layovers[i] if i < len(layovers) else {}
        connections.append(dict(arrival_airport=a, next_departure_airport=b, kind=kind,
                                duration_minutes=layover.get("duration") if layover.get("id") == a else None))
    result["connections"] = connections
    types = {c["kind"] for c in connections}
    if not connections:
        routing = "nonstop" if not offer.get("layovers") and not any(
            f.get("stops") or f.get("technical_stops") for f in offer["flights"]) else "unconfirmed"
    else:
        routing = "transfer" if "transfer" in types else "same_number_stop" if types == {"same_number_stop"} else "unconfirmed"
    result.update(routing=routing, baggage=offer.get("baggage"), same_ticket=offer.get("same_ticket"),
                  refund_rules=offer.get("refund_rules"), change_rules=offer.get("change_rules"))
    return result


def split_booking(row):
    if any(row.get(k) for k in ("separate_tickets", "self_transfer", "is_split_booking")):
        return True
    notes = [str(row.get("type", ""))] + list(row.get("extensions") or [])
    return any(word in text.lower() for text in notes if isinstance(text, str)
               for word in ("separate ticket", "self-transfer", "self transfer", "split ticket"))


def eligible(response, source, target, day):
    if not any(isinstance(response.get(k), list) for k in ("best_flights", "other_flights")):
        raise Unavailable("航班响应缺少行程列表")
    offers = (response.get("best_flights") or []) + (response.get("other_flights") or [])
    for row in offers:
        legs = row.get("flights") or []
        if not legs or not amount(row.get("price")) or split_booking(row):
            continue
        depart = legs[0].get("departure_airport", {})
        arrival = legs[-1].get("arrival_airport", {})
        local_date, _ = airport_time(depart)
        if depart.get("id") in AIRPORTS[source].split(",") and arrival.get("id") in AIRPORTS[target].split(",") and local_date == day:
            yield row


def flight_numbers(leg):
    return [f.get("flight_number", "") for f in leg.get("flights", [])]


def pair(provider, base, dest, booking_by_pair=None):
    if provider.remaining < 2:
        raise Pending(provider.name + " 可用额度不足两次配对")
    if provider.name == "SerpApi":
        params = {k:base[k] for k in ("outbound_date", "return_date", "currency", "hl", "gl", "adults")}
        params.update(type="1", travel_class="1", departure_id=AIRPORTS["TYO"], arrival_id=AIRPORTS[dest])
    else:
        params = dict(base)
    params["engine"] = "google_flights"
    candidates = list(eligible(provider.search(params), "TYO", dest, base["outbound_date"]))
    if not candidates:
        return [], None, None
    outbound = [r for r in candidates if r.get("departure_token")]
    if not outbound:
        raise Unavailable("去程结果缺少可选择回程的标识")
    selected = min(outbound, key=lambda r:r["price"])
    result = provider.search(dict(params, departure_token=selected["departure_token"]))
    inbound = sorted(eligible(result, dest, "TYO", base["return_date"]), key=lambda r:r["price"])
    observed = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = [dict(depart=base["outbound_date"], return_date=base["return_date"], total_jpy=r["price"],
                 selected_at_utc=observed, source=provider.name + " / Google Flights", ticket_issuance="unconfirmed",
                 outbound=itinerary(selected), inbound=itinerary(r)) for r in inbound]
    if booking_by_pair is not None:
        for row, original in zip(rows, inbound):
            key = pair_signature(row)
            if all(key) and original.get("booking_token"):
                booking_by_pair[key] = original["booking_token"]
    return rows, inbound[0].get("booking_token") if inbound else None, params


def official_evidence(opt, source):
    fields = ["is_official"]
    if source == "Google Flights":
        fields.extend(("is_airline", "airline"))
    evidence = {k: opt[k] for k in fields if type(opt.get(k)) is bool}
    if source == "Skyscanner" and opt.get("agent_type") in ("airline", "travel_agent"):
        evidence["agent_type"] = opt["agent_type"] == "airline"
    if len(set(evidence.values())) != 1:
        return None, evidence
    return next(iter(evidence.values())), evidence


def seller_rows(options, out, back, departure, return_date, source):
    rows = []
    selected_numbers = flight_numbers(out) + flight_numbers(back)
    def normalized(numbers):
        return [str(n).replace(" ", "").upper() for n in numbers]
    for opt in options:
        if not amount(opt.get("price")) or not isinstance(opt.get("book_with"), str) or split_booking(opt):
            continue
        if opt.get("flight_numbers") and normalized(opt["flight_numbers"]) != normalized(selected_numbers):
            continue
        official, evidence = official_evidence(opt, source)
        record = dict(departure=departure, **{"return": return_date}, flight_numbers_out=flight_numbers(out),
                      flight_numbers_back=flight_numbers(back), book_with=opt["book_with"], price=opt["price"],
                      source=source, official=official, official_evidence=evidence, currency="JPY",
                      seller_type="unknown" if official is None else ("airline" if official else "agent"),
                      paired_signature=[signature(out), signature(back)], observed_at_jst=jst_now())
        for field in ("baggage", "baggage_prices", "fare_type", "same_ticket", "refund_rules", "change_rules"):
            record[field] = opt.get(field)
        rows.append(record)
    return rows


def signature(leg):
    def segment(f):
        dep, arr = (f.get(k, {}) for k in ("departure_airport", "arrival_airport"))
        return (str(f.get("flight_number") or "").replace(" ", "").upper(), dep.get("id"),
                *airport_time(dep), arr.get("id"), *airport_time(arr))
    segments = tuple(segment(f) for f in leg.get("flights", []))
    return segments if segments and all(all(part) for part in segments) else None


def pair_signature(pair):
    return signature(pair["outbound"]), signature(pair["inbound"])


def signature_text(key):
    return json.dumps(key, ensure_ascii=True, separators=(",", ":"))


def google_supplements(get_provider, base, dest, batch, pairs, tokens, params, checks, record_seller):
    known_official = {signature_text(s.get("paired_signature")) for s in batch["sellers"]
                      if s.get("official") is True and s.get("official_evidence")}
    completed = {signature_text(c.get("paired_signature")) for c in checks
                 if c.get("source") == "Google Flights" and c.get("status") in ("matched", "unmatched")}
    missing = {pair_signature(p): p for p in pairs if all(pair_signature(p))
               and signature_text(pair_signature(p)) not in known_official | completed}
    if not missing:
        return
    provider = get_provider()
    if not provider:
        raise Pending("无可用 SearchApi")
    if params is None:
        if provider.remaining < 3:
            raise Pending("重新选择同一行程和比价至少需要三次额度")
        _, _, params = pair(provider, base, dest, tokens)
    unselected = False
    for key, quote in missing.items():
        check = dict(source="Google Flights", departure=base["outbound_date"], **{"return":base["return_date"]},
                     paired_signature=key, flight_numbers_out=flight_numbers(quote["outbound"]),
                     flight_numbers_back=flight_numbers(quote["inbound"]), observed_at_jst=jst_now())
        if key not in tokens:
            checks.append(dict(check, status="not_selectable"))
            unselected = True
            continue
        options = provider.search(dict(params, booking_token=tokens[key])).get("booking_options")
        if not isinstance(options, list):
            raise Unavailable("Google 购票渠道未返回")
        sellers = seller_rows(options, quote["outbound"], quote["inbound"], base["outbound_date"],
                              base["return_date"], "Google Flights")
        checks.append(dict(check, status="matched" if sellers else "unmatched", unmatched_options=len(options)-len(sellers)))
        record_seller(sellers, "Google Flights", key)
    if unselected:
        raise Unavailable("部分行程未返回同班预订标识")


def sky_sellers(provider, base, dest, pairs, checks=None):
    query = dict(base, engine="skyscanner_flights", departure_id="TYO", arrival_id=dest)
    result = provider.search(query)
    if not isinstance(result.get("itineraries"), list):
        raise Unavailable("Skyscanner 响应缺少行程列表")
    known = {pair_signature(p) for p in pairs}
    known = {pair_key for pair_key in known if all(pair_key)}
    rows = []
    unmatched = 0
    for opt in result["itineraries"]:
        legs = opt.get("legs", [])
        if len(legs) == 2 and not split_booking(opt) and tuple(signature(l) for l in legs) in known:
            rows.extend(seller_rows(opt.get("booking_options", []), *legs, base["outbound_date"],
                                    base["return_date"], "Skyscanner"))
        else:
            unmatched += 1
    if checks is not None:
        checks.append(dict(source="Skyscanner", departure=base["outbound_date"], **{"return":base["return_date"]},
                           observed_at_jst=jst_now(), unmatched_itineraries=unmatched,
                           status="matched" if rows else "unmatched"))
    return rows


def save(path, batch_file):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        try:
            json.dump(batch_file, out, ensure_ascii=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(out.name)
            raise
    os.replace(out.name, path)


def run(args, requests, path, journal, today):
    primary = backup = None
    checked_primary = checked_backup = False
    pending = 0
    hmac_key = private_key_for(path) if requests else None

    def get_primary():
        nonlocal primary, checked_primary
        if not checked_primary and args.max_searches:
            checked_primary = True
            key = os.getenv("SEARCHAPI_KEY") or getpass.getpass("SearchApi key（本次使用）: ")
            if key:
                try:
                    primary = Provider("SearchApi", key, args.max_searches, args.reserve)
                except Unavailable as exc:
                    print("SearchApi 暂不可用:", exc)
        return primary

    def get_backup():
        nonlocal backup, checked_backup
        if not checked_backup and args.serp_fallback and os.getenv("SERPAPI_KEY"):
            checked_backup = True
            backup = Provider("SerpApi", os.environ["SERPAPI_KEY"], args.serp_fallback, args.serp_reserve)
        return backup

    for q in requests:
        key = [q[k] for k in ("route", "departure", "returnDate", "flexOut", "flexBack")]
        b = next((item for item in journal["batches"] if item.get("request") == key and item["observed_date_jst"] == today.isoformat()), None)
        if b is None:
            b = dict(route=q["route"], observed_date_jst=today.isoformat(), currency="JPY", request=key,
                     calendar=[], paired=[], sellers=[], completed=[])
            journal["batches"].append(b)
        dest = q["route"].split("-")[1]
        def add_record(kind, out, back, price, state, source, **details):
            criteria = dict(q, departure=out, return_date=back, scope="round_trip", engine=source,
                            travel_class="economy", currency="JPY", adults=1, type=kind)
            if "flight_numbers" in details:
                criteria["flight_numbers"] = details["flight_numbers"]
            fp = fingerprint(criteria, hmac_key)
            b.setdefault("records", []).append(observation(source, jst_now(), q["route"], "round_trip",
                     kind, out, back, price, state, fp, **details))

        def record_sellers(sellers, source, key=None):
            for s in sellers:
                add_record("seller", out, back, s["price"], "priced", "SearchApi / " + source,
                           seller=s["book_with"], flight_numbers=s["flight_numbers_out"] + s["flight_numbers_back"],
                           paired_signature=s["paired_signature"], official=s["official"], official_evidence=s["official_evidence"],
                           same_ticket=s["same_ticket"], baggage=s["baggage"], baggage_prices=s["baggage_prices"],
                           refund_rules=s["refund_rules"], change_rules=s["change_rules"])
            if not sellers:
                add_record("seller", out, back, None, "not_returned", "SearchApi / " + source, paired_signature=key)
            b["sellers"].extend(sellers)
            save(path, journal)

        if q["departure"] < today.isoformat():
            if "out_of_range" not in b["completed"]:
                b["calendar_status"] = "out_of_range"
                b["pair_status"] = [dict(depart=q["departure"], return_date=q["returnDate"], status="out_of_range")]
                for kind in ("calendar", "paired"):
                    add_record(kind, q["departure"], q["returnDate"], None, "out_of_range", "local_validation")
                b["completed"].append("out_of_range")
            continue
        base = dict(departure_id=CALENDAR_LOCATIONS["TYO"], arrival_id=CALENDAR_LOCATIONS[dest],
                    flight_type="round_trip", outbound_date=q["departure"], return_date=q["returnDate"],
                    gl="jp", hl="en", currency="JPY", adults=1, travel_class="economy")
        if "calendar" not in b["completed"]:
            try:
                if not get_primary():
                    raise Pending("无可用 SearchApi")
                b["calendar"] = calendar(primary.search, base, q, today)
                for cell in b["calendar"]:
                    add_record("calendar", cell["departure"], cell["return"], cell["price"],
                               cell["status"], "SearchApi / Google Flights")
                b["calendar_status"] = "complete"
                b["completed"].append("calendar")
                save(path, journal)
            except Unavailable as exc:
                pending += 1
                b["calendar_status"] = "pending" if isinstance(exc, Pending) else "query_failed"
                add_record("calendar", q["departure"], q["returnDate"], None, b["calendar_status"], "SearchApi / Google Flights")
                print("日历未取得:", exc)
        targets = [(q["departure"], q["returnDate"])]
        for r in sorted((r for r in b["calendar"] if amount(r.get("price"))), key=lambda r:r["price"]):
            dates = (r["departure"], r["return"])
            if dates not in targets and len(targets) < 1 + args.alternatives:
                targets.append(dates)
        for out, back in targets:
            marker = out + "/" + back
            exact = dict(base, outbound_date=out, return_date=back)
            rows = [p for p in b["paired"] if (p["depart"], p["return_date"]) == (out, back)]
            gparams = None
            tokens = {}
            if "pair:" + marker not in b["completed"]:
                pair_states = b.setdefault("pair_status", [])
                state = next((r for r in pair_states if (r["depart"], r["return_date"]) == (out, back)), None)
                if state is None:
                    state = dict(depart=out, return_date=back, status="pending")
                    pair_states.append(state)
                try:
                    source = "SearchApi / Google Flights"
                    if not get_primary():
                        raise Pending("无可用 SearchApi")
                    rows, _, gparams = pair(primary, exact, dest, tokens)
                except Unavailable as exc:
                    first_state = "pending" if isinstance(exc, Pending) else "query_failed"
                    add_record("paired", out, back, None, first_state, source)
                    print("主要配对未完成:", exc)
                    try:
                        fallback = get_backup() if dest in ("SHE", "BJS") else None
                        if not fallback:
                            raise Pending("无可用备用额度")
                        source = "SerpApi / Google Flights"
                        rows, _, _ = pair(fallback, exact, dest)
                    except Unavailable as backup_exc:
                        print("该日期配对待下次继续:", backup_exc)
                        state["status"] = "pending" if isinstance(exc, Pending) and isinstance(backup_exc, Pending) else "query_failed"
                        if source.startswith("SerpApi"):
                            add_record("paired", out, back, None, "pending" if isinstance(backup_exc, Pending) else "query_failed", source)
                        pending += 1
                        continue
                state["status"] = "priced" if rows else "not_returned"
                if not rows:
                    add_record("paired", out, back, None, "not_returned", source)
                for quote in rows:
                    add_record("paired", out, back, quote["total_jpy"], "priced", source,
                               flight_numbers=flight_numbers(quote["outbound"]) + flight_numbers(quote["inbound"]),
                               outbound=quote["outbound"], inbound=quote["inbound"], same_ticket=None,
                               refund_rules=None, change_rules=None)
                b["paired"].extend(rows)
                b["completed"].append("pair:" + marker)
                save(path, journal)
            checks = b.setdefault("seller_checks", [])
            if (args.sellers or args.google_sellers) and rows and "sky:" + marker not in b["completed"]:
                try:
                    if not get_primary():
                        raise Pending("无可用 SearchApi")
                    sellers = sky_sellers(primary, exact, dest, rows, checks)
                    b["completed"].append("sky:" + marker)
                    record_sellers(sellers, "Skyscanner")
                except Unavailable as exc:
                    add_record("seller", out, back, None, "pending" if isinstance(exc, Pending) else "query_failed", "SearchApi / Skyscanner")
                    pending += 1
                    print("卖家报价未取得:", exc)
            if args.google_sellers and rows and "sky:" + marker in b["completed"]:
                try:
                    google_supplements(get_primary, exact, dest, b, rows, tokens, gparams, checks, record_sellers)
                except Unavailable as exc:
                    add_record("seller", out, back, None, "pending" if isinstance(exc, Pending) else "query_failed", "SearchApi / Google Flights")
                    pending += 1
                    print("Google 同班卖家报价待下次继续:", exc)
    save(path, journal)
    print("本轮搜索请求数:", (primary.calls if primary else 0) + (backup.calls if backup else 0))
    if pending:
        print(f"尚有 {pending} 个步骤待查，已取得的结果可以导入。同一天复用此输出文件可继续。")
    return not pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queue", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-searches", type=int, required=True, help="本次 SearchApi 搜索请求上限")
    parser.add_argument("--reserve", type=int, default=0, help="SearchApi 保留次数")
    parser.add_argument("--serp-fallback", type=int, default=0, help="额外授权的 SerpApi 配对查询次数，默认关闭")
    parser.add_argument("--serp-reserve", type=int, default=246, help="为原有日程保留的 SerpApi 次数")
    parser.add_argument("--alternatives", type=int, default=2, help="除原日期外，核实几组低日历价")
    parser.add_argument("--sellers", action="store_true", help="给已选配对增加 Skyscanner 同班卖家报价")
    parser.add_argument("--google-sellers", action="store_true", help="为选定的同一去回航班查询 Google 卖家")
    args = parser.parse_args()
    if min(args.max_searches, args.reserve, args.serp_fallback, args.serp_reserve, args.alternatives) < 0:
        parser.error("额度和候选数须为非负数")
    repo = Path(__file__).resolve().parents[1]
    path = args.output.expanduser().resolve()
    queue = args.queue.expanduser().resolve()
    if path == queue or any(p.is_relative_to(repo) for p in (queue, path)):
        parser.error("队列和报价应分别保存在公开仓库以外的本机文件")
    os.umask(0o077)
    today = jst_today()
    requests = validated_queue(queue, today)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = json.loads(path.read_text()) if path.exists() else {"version":1, "batches":[]}
        if journal.get("version") != 1 or not isinstance(journal.get("batches"), list):
            parser.error("已有文件不是本采集器生成的往返报价")
        if not run(args, requests, path, journal, today):
            sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except (Unavailable, OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        sys.exit("采集未完成：" + str(exc))
