"""Budget and provenance for scheduled public benchmark searches."""
import datetime as dt
import os

from collect import CALENDAR_LOCATIONS, ROUTES, airline_prices, calendar_prices, calendar_rt_prices
from fare_history import DATA, save
from quote_records import append_public, fingerprint, jst_now, observation
from round_trips import Pending, Provider, Unavailable

CORE = {"TYO-SHE", "SHE-TYO", "TYO-BJS", "BJS-TYO"}


def calendar_days(start, end):
    first, last = map(dt.date.fromisoformat, (start, end))
    return [(first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def fallback_query(params, route):
    origin, destination = route.split("-")
    result = {k: params[k] for k in ("engine", "currency", "gl", "outbound_date") if k in params}
    result.update(departure_id=CALENDAR_LOCATIONS[origin], arrival_id=CALENDAR_LOCATIONS[destination],
                  flight_type="one_way", travel_class="economy", adults=1, hl="en")
    if params["engine"] == "google_flights_calendar":
        result.update(outbound_date=params["date_range_start"], outbound_date_start=params["date_range_start"],
                      outbound_date_end=params["date_range_end"], stops="nonstop" if params.get("stops") == "1" else "any")
    return result


def public_rows(params, response, route, source, when, failure):
    rt = str(params.get("type")) == "1"
    scope = "round_trip" if rt else "one_way"
    fp = fingerprint(dict(params, route=route, scope=scope, trip_days=params.get("round_trip_days")))
    def row(kind, dep, back, value, **extra):
        state = failure or ("priced" if value is not None else "not_returned")
        return observation(source, when, route, scope, kind, dep, back, value, state, fp, **extra)
    response = response or {}
    if params["engine"] == "google_flights_calendar":
        trip = params.get("round_trip_days")
        vals = dict(calendar_rt_prices(response, trip) if rt else calendar_prices(response))
        for dep in calendar_days(params["date_range_start"], params["date_range_end"]):
            back = (dt.date.fromisoformat(dep) + dt.timedelta(days=trip)).isoformat() if rt else None
            yield row("calendar", dep, back, vals.get(dep), stops=params.get("stops", "0"))
    elif "booking_token" in params:
        count = 0
        for option in response.get("booking_options") or []:
            seller = option.get("together") or option.get("departing") or option
            p = seller.get("price")
            if type(p) in (int, float) and p > 0 and seller.get("book_with"):
                count += 1
                yield row("seller", params["outbound_date"], None, p, seller=seller["book_with"])
        if not count:
            yield row("seller", params["outbound_date"], None, None)
    else:
        airlines = airline_prices(response)
        for carrier, p in airlines.items():
            yield row("flight_detail", params["outbound_date"], None, p, airline=carrier)
        if not airlines:
            yield row("flight_detail", params["outbound_date"], None, None)


class DailySources:
    def __init__(self, serp_key, planned, folder=DATA):
        self.folder = folder
        self.providers = {}
        self.states = {"SearchApi": "reserved", "SerpApi": "unchecked"}
        self.failed = False
        self.fallback_routes = set()
        self.rows = []
        self.connect("SerpApi", serp_key, planned, 0)
        # SearchApi's free credits are reserved for dedicated acceptance checks
        # until the owner explicitly grants an independent scheduled budget.
        cap = min(4, max(0, int(os.getenv("DAILY_SEARCHAPI_LIMIT") or 0)))
        if cap:
            reserve = int(os.getenv("SEARCHAPI_RESERVE") or 87)
            self.connect("SearchApi", os.getenv("SEARCHAPI_KEY"), cap, max(0, reserve))
        elif os.getenv("SEARCHAPI_KEY"):
            try:
                # Account checks do not spend a search. No search provider is
                # installed in this branch, even when the account has credit.
                account = Provider("SearchApi", os.environ["SEARCHAPI_KEY"], 0, 0)
                if account.credits <= 4:
                    self.states["SearchApi"] = "low" if account.credits else "blocked"
            except Unavailable:
                self.states["SearchApi"] = "blocked"
        self.report()

    def connect(self, name, key, cap, reserve):
        if not key:
            self.states[name] = "blocked"
            return
        try:
            provider = Provider(name, key, cap, reserve)
            self.providers[name] = provider
            low_threshold = max(14, cap) if name == "SerpApi" else cap
            self.states[name] = "low" if provider.remaining < cap or provider.credits - reserve <= low_threshold else "ok"
            if provider.remaining == 0:
                self.states[name] = "blocked"
        except Unavailable:
            self.states[name] = "blocked"

    def call(self, params, route):
        if route not in ROUTES:
            raise ValueError("非公共航线")
        candidates = ["SerpApi"]
        if (route in CORE and route not in self.fallback_routes and params.get("type") == "2"
                and not params.get("booking_token") and "SearchApi" in self.providers):
            candidates.append("SearchApi")
        result = None
        for provider_name in candidates:
            source = provider_name + " / Google Flights"
            settings = self.providers.get(provider_name)
            failure = None
            try:
                if not settings or not settings.remaining:
                    raise Pending("本轮额度不可用")
                query = params if provider_name == "SerpApi" else fallback_query(params, route)
                if provider_name == "SearchApi":
                    self.fallback_routes.add(route)
                result = settings.search(query)
                if params["engine"] == "google_flights_calendar":
                    valid = any(isinstance(result.get(k), list) for k in ("prices", "calendar"))
                elif params.get("booking_token"):
                    valid = isinstance(result.get("booking_options"), list)
                else:
                    valid = any(isinstance(result.get(k), list) for k in ("best_flights", "other_flights"))
                if not valid:
                    result = None
                    raise Unavailable("返回结构缺少对应报价列表")
            except Unavailable as exc:
                failure = "pending" if isinstance(exc, Pending) else "query_failed"
                if not settings or not settings.remaining:
                    self.states[provider_name] = "blocked"
                else:
                    self.states[provider_name] = "partial"
            self.rows.extend(public_rows(params, result, route, source, jst_now(), failure))
            if failure is None:
                return result
        self.failed = True
        return None

    def report(self):
        state = self.states["SerpApi"]
        if self.failed and state == "ok":
            state = "partial"
        warning = {"blocked": "主接口额度或连接暂不可用，本轮可能缺少报价。",
                   "low": "主接口可用额度偏低，部分报价可能延迟。",
                   "partial": "本轮部分查询未完成，历史报价可以继续查看。"}.get(state, "")
        if self.states["SearchApi"] in ("low", "blocked"):
            warning = (warning + " 备用接口余额偏低或账户暂不可用。").strip()
            if state == "ok":
                state = "low"
        if warning:
            print("::warning::" + warning)
        save(self.folder / "collector-status.json", dict(version=1, observed_at_jst=jst_now(), state=state,
             providers={k: {"state": v} for k, v in self.states.items()}, warning=warning))

    def finish(self):
        for day in sorted({r["observed_date_jst"] for r in self.rows}):
            append_public(self.folder / "quote-observations", [r for r in self.rows if r["observed_date_jst"] == day])
        self.report()
