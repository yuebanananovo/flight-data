"""Run a paid schedule inside an explicitly activated private GitHub repository."""
import argparse
import calendar
import datetime as dt
import json
import os
from pathlib import Path
import sys

from collect import CALENDAR_LOCATIONS, jst_today
from fare_history import DATA, read, save
from paid_plan import CITIES, DAY, FOCUS_CARRIERS, holiday_windows, month_report, ordered, originals, schedule
from quote_records import append_public, fingerprint, jst_now, observation, private_key_for
from round_trips import Pending, ProviderLimit, Unavailable, amount, fetch, itinerary, pair


def public_missing():
    path = DATA / "history" / "holiday-coverage.json"
    if not path.exists():
        return []
    status = read(path)
    missing = []
    for key, days in status.get("observations", {}).items():
        route, iso = key.split("/")
        if not days and not read(DATA/"history"/(iso[:7]+".json")).get(route, {}).get(iso, {}).get("google"):
            missing.append((route, dt.date.fromisoformat(iso)))
    return missing


class PaidProvider:
    name = "SearchApi"

    def __init__(self, config, folder, day):
        self.folder = folder
        self.day = day
        self.path = folder / ("budget-" + day.isoformat()[:7] + ".json")
        self.ledger = read(self.path)
        self.headers = {"Authorization":"Bearer " + os.environ["SEARCHAPI_KEY"]}
        acc = fetch("https://www.searchapi.io/api/v1/me", {}, self.headers)
        plan, usage = acc["account"], acc["api_usage"]
        limit = int(config.get("monthly_limit",1000))
        reserve = int(config.get("reserve",100))
        daily_max = int(config.get("daily_limit", 40))
        if not 0 <= reserve < limit or daily_max < 0 or int(plan["monthly_allowance"]) <= 0:
            raise Pending("未确认付费套餐或预算")
        # Account balance includes free credits; exclude them from paid spending.
        paid_left = int(plan["remaining_searches"]) - int(plan.get("remaining_credits", 0))
        monthly_left = min(limit - sum(self.ledger.values()), paid_left) - reserve
        days_left = calendar.monthrange(day.year,day.month)[1] - day.day + 1
        day_used = self.ledger.get(day.isoformat(),0)
        paced = max(0, (monthly_left + day_used)//days_left - day_used)
        hourly = int(usage["hourly_rate_limit"]) - int(usage["searches_this_hour"])
        self.remaining = max(0, min(monthly_left, daily_max-day_used, paced, hourly))
        self.stopped = False

    def search(self, params):
        if self.remaining <= 0:
            raise Pending("本日或本月额度不足")
        self.remaining -= 1
        day = self.day.isoformat()
        self.ledger[day] = self.ledger.get(day,0) + 1
        save(self.path,self.ledger)
        try:
            return fetch("https://www.searchapi.io/api/v1/search", params, self.headers)
        except ProviderLimit:
            self.remaining = 0
            self.stopped = True
            raise


def query_base(route):
    a,b = route.split("-")
    return dict(departure_id=CALENDAR_LOCATIONS[a],arrival_id=CALENDAR_LOCATIONS[b],travel_class="economy",
                adults=1,gl="jp",hl="en",currency="JPY")


def date_list(start, end):
    return [(start+i*DAY).isoformat() for i in range((end-start).days+1)]


def collect_task(provider, task):
    params = dict(query_base(task.route),engine="google_flights_calendar",flight_type="one_way",
                  outbound_date=task.start.isoformat(),outbound_date_start=task.start.isoformat(),
                  outbound_date_end=task.end.isoformat(),stops=task.stops)
    if task.airline:
        params["included_airlines"] = task.airline
    if task.kind == "history":
        params = dict(query_base(task.route),engine="google_flights",flight_type="one_way",outbound_date=task.start.isoformat())
        points = (provider.search(params).get("price_insights") or {}).get("price_history") or []
        return [dict(date=p["iso_date"][:10],price=p["price"]) for p in points if isinstance(p, dict)
                and isinstance(p.get("iso_date"),str) and amount(p.get("price"))]
    if task.back_start:
        params.update(flight_type="round_trip",return_date=task.back_start.isoformat(),
                      return_date_start=task.back_start.isoformat(),return_date_end=task.back_end.isoformat())
    result = provider.search(params)
    if not isinstance(result.get("calendar"),list):
        raise Unavailable("未收到日历")
    prices = {}
    for r in result["calendar"]:
        key = r.get("departure"), r.get("return")
        if amount(r.get("price")):
            prices[key] = min(prices.get(key,r["price"]),r["price"])
    cells = []
    for d in date_list(task.start,task.end):
        for back in date_list(task.back_start,task.back_end) if task.back_start else [None]:
            if back and back < d:
                continue
            value = prices.get((d,back))
            cells.append(dict(departure=d, **{"return":back}, price=value,status="priced" if value else "not_returned"))
    return cells


def previous_candidates(folder, day, current):
    # Per route, use the most recent private calendar containing a valid future
    # pair. The calendar's timestamp travels with the candidate, never its price.
    found = {}
    historical = sorted((p for p in folder.glob("????-??-??.json") if p.stem < day.isoformat()), reverse=True)
    for index in range(len(historical)+1):
        report = current if not index else read(historical[index-1])
        today_prices = {}
        for b in report.get("batches",[]):
            route = b["route"]
            if route in found:
                continue
            when = b.get("observed_at_jst") or b.get("observed_date_jst")
            for r in b.get("calendar",[]) + b.get("filtered_calendar",[]):
                out, back, p = r.get("departure"),r.get("return"),r.get("price")
                if not (amount(p) and isinstance(out,str) and isinstance(back,str) and day.isoformat() <= out <= back):
                    continue
                key = (route,out,back)
                if key not in today_prices or p < today_prices[key][0]:
                    today_prices[key] = (p,when)
        for key, info in today_prices.items():
            found.setdefault(key[0],{})[key] = info
        if len(found)==len(CITIES):
            break
    return {k:v for group in found.values() for k,v in group.items()}


def execute(provider, day, config, folder, tasks):
    path = folder/(day.isoformat()+".json")
    report = read(path) or {"version":1, "batches":[], "completed":[], "history":[]}
    done = set(report["completed"])
    secret = private_key_for(folder/"observations")
    observations = []
    attempted = set()
    def capture(route,scope,kind,out,back,price,status,criteria,**extra):
        fp = fingerprint(dict(criteria,route=route,scope=scope,type=kind),secret)
        observations.append(observation("SearchApi / Google Flights",jst_now(),route,scope,kind,out,back,price,status,fp,**extra))
    def checkpoint(key):
        done.add(key)
        report["completed"] = sorted(done)
        save(path,report)
    def batch(route):
        return dict(route=route,observed_date_jst=day.isoformat(), observed_at_jst=jst_now(),currency="JPY",
                    source="SearchApi / Google Flights",calendar=[],paired=[],sellers=[])
    def verify(picks):
        for route,out,back,carrier,stops,reason,observed in picks:
            key = "/".join(("pair:"+route,out,back,carrier,stops))
            if key in done:
                continue
            statuses = report.setdefault("pair_coverage",{})
            status = dict(route=route,depart=out,return_date=back,included_airlines=carrier,stops=stops,reason=reason,
                          candidate_observed_at_jst=observed)
            statuses[key]=status
            if provider.remaining < 2:
                status["status"]="quota_insufficient"
                continue
            if key in attempted:
                status["status"]="query_failed"
                continue
            attempted.add(key)
            params = dict(query_base(route),flight_type="round_trip",outbound_date=out,return_date=back,stops=stops)
            eligible = None
            if carrier:
                params["included_airlines"] = carrier
                allowed = set(carrier.split(","))
                def eligible(r):
                    leg = itinerary(r)
                    return (all(s["marketing_airline"] in allowed for s in leg["segments"])
                            and (stops!="nonstop" or leg["routing"]=="nonstop"))
            elif route=="TYO-DLC" and not any(abs((dt.date.fromisoformat(out)-d).days)<=2
                                             for d,_ in holiday_windows(config)):
                params["included_airlines"] = "NH,JL,CA"
                def eligible(r):
                    leg = itinerary(r)
                    codes = {s["marketing_airline"] for s in leg["segments"]}
                    return codes=={"CA"} or codes in ({"NH"},{"JL"}) and leg["routing"]=="nonstop"
            try:
                quotes,_,_ = pair(provider,params,route.split("-")[1],accept_offer=eligible)
            except Unavailable:
                status["status"]="query_failed"
                capture(route,"round_trip","paired",out,back,None,"query_failed",params)
                continue
            status["status"]="priced" if quotes else "not_returned"
            if not quotes:
                capture(route,"round_trip","paired",out,back,None,"not_returned",params)
            for q in quotes:
                capture(route,"round_trip","paired",out,back,q["total_jpy"],"priced",params,
                        outbound=q["outbound"],inbound=q["inbound"])
            result = batch(route)
            result.update(paired=quotes, included_airlines=carrier, stops=stops, candidate_source=reason,
                          candidate_observed_at_jst=observed, pair_status=[dict(status)])
            report["batches"].append(result)
            checkpoint(key)

    def run_tasks(sequence):
        for task in sequence:
            if not provider.remaining:
                break
            key = task.identity()
            if key in done:
                continue
            try:
                cells = collect_task(provider,task)
            except Unavailable:
                capture(task.route,"round_trip" if task.back_start else "one_way",task.kind,task.start.isoformat(),
                        task.back_start.isoformat() if task.back_start else None,None,"query_failed",task_criteria(task))
                continue
            if task.kind=="history":
                report["history"].append(dict(route=task.route,departure=task.start.isoformat(),points=cells,scope="one_way",
                                              observed_at_jst=jst_now(),source="SearchApi / Google Flights"))
                # Empty attempts are also stored; no repeat charge on the next day.
                save(folder/"holiday-checked.json", dict(read(folder/"holiday-checked.json"), **{key:len(cells)}))
            else:
                result = batch(task.route)
                criteria = task_criteria(task)
                for cell in cells:
                    capture(task.route,"round_trip" if task.back_start else "one_way","calendar",
                            cell["departure"],cell["return"],cell["price"],cell["status"],criteria,
                            included_airlines=task.airline,stops=task.stops)
                result.update(scope="round_trip" if task.back_start else "one_way", included_airlines=task.airline,
                              stops=task.stops, kind=task.kind, calendar_status="complete")
                # The website's ordinary round-trip importer reads only the unfiltered calendar.
                field = "calendar" if task.back_start and not task.airline else "filtered_calendar" if task.back_start else "one_way_calendar"
                result[field] = cells
                report["batches"].append(result)
            checkpoint(key)

    own = originals(day,config)
    verify([(*k,"","any","private_original",None) for k in own])
    candidates = previous_candidates(folder,day,report)
    history_done = read(folder/"holiday-checked.json")
    work = [t for t in tasks if t.kind!="history" or t.identity() not in history_done]
    focus = [(city,carrier,stops) for city,groups in FOCUS_CARRIERS.items() for carrier,stops in groups]
    focus = ordered(focus, day.toordinal())
    # On the first run seed only missing routes before paired lookups. Once a
    # route has a private date or a still-valid past window, no seed is needed.
    active_routes = {k[0] for k in own} | {k[0] for k in candidates}
    for city in [*dict.fromkeys(row[0] for row in focus),"SHA"]:
        route = "TYO-"+city
        if route in active_routes or provider.remaining < 3:
            continue
        seed = next((t for t in work if t.route==route and t.back_start and t.identity() not in done),None)
        if seed is not None:
            run_tasks([seed])
    candidates = previous_candidates(folder,day,report)
    desired = int(config.get("lowest_pairs",2))
    def select_pairs(pool):
        ranked = sorted(pool,key=lambda k:(pool[k][0],k))
        goals=[]
        gaps=[]
        selected = report.setdefault("daily_pair_targets",{})
        for city,carrier,stops in focus:
            route="TYO-"+city
            slot = route+":"+carrier+":"+stops
            if slot in selected:
                goals.append(selected[slot])
                continue
            choices=[k for k in own if k[0]==route] or [k for k in ranked if k[0]==route]
            if not choices:
                gaps.append(dict(route=route,included_airlines=carrier,stops=stops,status="no_candidate"))
                continue
            k=choices[0]
            origin,when=("private_original",None) if k in own else ("calendar",pool[k][1])
            selected[slot] = [*k,carrier,stops,origin,when]
            goals.append(selected[slot])
        verify(goals)
        extras = selected.setdefault("lowest",[])
        existing = set(tuple(target[:3]) for target in extras)
        for k in ranked:
            if len(extras)>=desired:
                break
            if k not in own and k not in existing:
                extras.append([*k,"","any","calendar",pool[k][1]])
        verify(extras)
        if len(extras)<desired:
            gaps.append(dict(reason="lowest",status="no_candidate",missing=desired-len(extras)))
        report["candidate_gaps"]=gaps

    select_pairs(candidates)
    # If a new calendar must supply a candidate, reserve the corresponding
    # paired budget while processing today's affordable remaining windows.
    missing = sum(g.get("missing",1) for g in report["candidate_gaps"])
    held=min(2*missing,max(0,provider.remaining-1))
    provider.remaining-=held
    try:
        priority=[t for t in work if t.kind in ("holiday","history")]
        run_tasks(priority+[t for t in work if t.kind not in ("holiday","history")])
    finally:
        if not getattr(provider,"stopped",False):
            provider.remaining+=held
    if missing:
        select_pairs(previous_candidates(folder,day,report))
    report["scheduled_tasks"] = len(tasks)
    report["completed_tasks"] = sum(t.identity() in done for t in tasks)
    for observed in sorted({r["observed_date_jst"] for r in observations}):
        append_public(folder/"observations",[r for r in observations if r["observed_date_jst"]==observed])
    save(path,report)


def task_criteria(task):
    return dict(outbound_date_start=task.start.isoformat(),outbound_date_end=task.end.isoformat(),
                return_date_start=task.back_start.isoformat() if task.back_start else None,
                return_date_end=task.back_end.isoformat() if task.back_end else None,included_airlines=task.airline,
                stops=task.stops,travel_class="economy",currency="JPY",adults=1)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--estimate", action="store_true")
    args=parser.parse_args(argv)
    day=jst_today()
    config=json.loads(os.getenv("PRIVATE_FLIGHT_PLAN") or "{}")
    missing=public_missing()
    report=month_report(day,config,missing)
    if args.estimate:
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return
    if os.getenv("ENABLE_PAID_FLIGHTS") != "true":
        print("付费采集未启用。")
        return
    event=read(Path(os.environ["GITHUB_EVENT_PATH"]))
    repo=event.get("repository",{})
    if os.getenv("GITHUB_ACTIONS") != "true" or repo.get("private") is not True or repo.get("full_name")!=os.getenv("GITHUB_REPOSITORY"):
        raise Pending("执行必须在已确认私有的 GitHub 仓库")
    folder=Path(os.environ["GITHUB_WORKSPACE"])/"private-flight-data"
    folder.mkdir(exist_ok=True)
    provider=PaidProvider(config,folder,day)
    save(folder/"monthly-estimate.json",report)
    execute(provider,day,config,folder,schedule(day,config,missing))
    print("已在私有仓库保存本轮结果。")


if __name__=="__main__":
    try:
        main()
    except Exception:
        sys.exit("付费采集未完成，请核对私有环境和账户配置。")
