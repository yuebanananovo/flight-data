"""Isolated daily observations for the owner's original Shenyang round trip."""
import argparse
import datetime as dt
import json
import os
import urllib.request

from collect import AIRPORTS, CALENDAR_LOCATIONS
from round_trips import Provider, NoRedirect, eligible, itinerary, Unavailable
from quote_records import jst_now

CARRIERS = ("CZ", "KE", "CA", "MU")
URL = "https://yuebananan-jp-helper.com/api/flights/shenyang/worker"
ALIASES = {"mu", "fm", "china eastern", "china eastern airlines", "shanghai airlines",
           "中国东方航空", "上海航空", "中国东方航空公司"}


def update(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer "+os.environ["HELPER_FLIGHT_QUEUE_KEY"], "Content-Type":"application/json"})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=40) as res:
        return json.load(res)


def eligible_carrier(row, carrier):
    flights = row["flights"]
    if carrier == "CZ" and (len(flights) != 1 or itinerary(row)["routing"] != "nonstop"
            or any(f.get("stops") or f.get("stopovers") or f.get("technical_stops") for f in flights)):
        return False
    for flight in flights:
        number = flight.get("flight_number") or ""
        if carrier == "MU":
            op = flight.get("operating_airline") or flight.get("operated_by") or flight.get("operating_airline_code")
            if isinstance(op, dict):
                op = op.get("code") or op.get("name")
            if str(op or "").lower().strip() not in ALIASES:
                return False
        elif not number.replace(" ","").upper().startswith(carrier):
            return False
    return True


def candidates(response, origin, dest, day, carrier, token=False):
    return sorted((r for r in eligible(response,origin,dest,day) if eligible_carrier(r,carrier)
                   and (not token or r.get("departure_token"))), key=lambda r:r["price"])


def collect(account, job, mode):
    route = job["route"]
    if route not in ("TYO-SHE","SHE-TYO"):
        raise ValueError("unsupported route")
    origin, dest = route.split("-")
    out, back = job["departure"], job["returnDate"]
    if not dt.date.fromisoformat(out) <= dt.date.fromisoformat(back):
        raise ValueError("date order")
    base = dict(engine="google_flights", departure_id=CALENDAR_LOCATIONS[origin],arrival_id=CALENDAR_LOCATIONS[dest],
                flight_type="round_trip", travel_class="economy", adults=1, currency="JPY", gl="jp", hl="en",
                outbound_date=out,return_date=back)
    shared = mode == "probe" or job["strategy"] == "shared"
    common = account.search(dict(base,included_airlines="CZ,KE,CA,MU,FM")) if shared else None
    if mode == "probe":
        found = [c for c in CARRIERS if candidates(common,origin,dest,out,c,True)]
        return dict(probe={"strategy":"shared" if len(found)==4 else "per_airline","eligible":found,
                           "has_price_and_token":len(found),"calls":account.calls})
    when = jst_now()
    batch = dict(kind="shenyang_free",currency="JPY",route=route,observed_date_jst=when[:10],observed_at_jst=when,
                 paired=[],pair_status=[],calendar=[],source="SearchApi / Google Flights")
    for carrier in CARRIERS:
        params = dict(base,included_airlines="MU,FM" if carrier=="MU" else carrier)
        if carrier=="CZ":
            params["stops"]="nonstop"
        if shared:
            params=dict(base,included_airlines="CZ,KE,CA,MU,FM")
        outbound=candidates(common if shared else account.search(params),origin,dest,out,carrier,True)
        inbound=[]
        if outbound:
            selected=outbound[0]
            inbound=candidates(account.search(dict(params,departure_token=selected["departure_token"])),dest,origin,back,carrier)
            if inbound:
                quote=inbound[0]
                batch["paired"].append(dict(carrier=carrier,depart=out,return_date=back,total_jpy=quote["price"],
                    selected_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    source=batch["source"],outbound=itinerary(selected),inbound=itinerary(quote)))
        batch["pair_status"].append(dict(carrier=carrier,depart=out,return_date=back,status="priced" if inbound else "not_returned"))
    return dict(batch=batch)


def run(mode):
    key=os.environ["SEARCHAPI_KEY"]
    account=Provider("SearchApi",key,8,0)
    now=lambda credits: dict(remaining=credits,observed_at_jst=jst_now())
    update(dict(action="balance",balance=now(account.credits)))
    job=update(dict(action="plan",mode=mode)).get("job")
    if not job:
        print("没有待执行的原日期任务。")
        return
    cap=1 if mode=="probe" else 5 if job["strategy"]=="shared" else 8
    receipt=dict(action="finish",mode=mode,id=job["id"],lease=job["lease"],state="insufficient")
    if account.remaining>=cap:
        try:
            receipt.update(collect(account,job,mode))
            receipt["state"]="complete"
        except Unavailable:
            receipt["state"]="failed"
    try:
        after=Provider("SearchApi",key,0,0)
        receipt["balance"]=now(after.credits)
        charged=account.credits-after.credits
    except Unavailable:
        charged="待账户复核"
    for attempt in range(3):
        try:
            update(receipt)
            break
        except OSError:
            if attempt==2:
                raise
    result=receipt.get("probe",{})
    print("状态",receipt["state"],"实际搜索请求",account.calls,"余额差",charged,
          "策略",result.get("strategy",job.get("strategy")),"一次返回的合规航司",result.get("eligible",[]))
    if receipt["state"]=="failed":
        raise Unavailable()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode",choices=["daily","probe"])
    try:
        run(parser.parse_args().mode)
    except Exception:
        raise SystemExit("本轮未完成，私有端点、额度或接口异常。")
