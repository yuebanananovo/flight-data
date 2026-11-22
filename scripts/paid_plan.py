"""Private paid collection schedule. Planning is offline and uses civil JST days."""
import calendar
from collections import Counter
from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
import json

from collect import six_months_after

DAY = dt.timedelta(days=1)
CITIES = ("SHE", "BJS", "SHA", "DLC")
FOCUS_CARRIERS = {"SHE": (("CZ","nonstop"),("KE","any"),("CA","any"),("MU,FM","any")),
                  "BJS": (("NH","any"),("JL","any"),("CA","any")),
                  "DLC": (("NH","nonstop"),("JL","nonstop"),("CA","any"))}


@dataclass(frozen=True)
class Task:
    kind: str
    route: str
    start: dt.date
    end: dt.date
    back_start: dt.date = None
    back_end: dt.date = None
    airline: str = ""
    stops: str = "any"
    stay: int = 0

    def identity(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


def fortnight_windows(day):
    end = six_months_after(day)
    cursor = day
    while cursor <= end:
        last = min(cursor+13*DAY, end)
        yield cursor, last
        cursor = last + DAY


def ordered(items, seed):
    if not items:
        return []
    pivot = seed % len(items)
    return items[pivot:] + items[:pivot]


def holiday_windows(config):
    windows = set()
    for setting in config.get("holidays", []):
        begin, end = (dt.date.fromisoformat(setting[k]) for k in ("departure","returnDate"))
        if begin > end:
            raise ValueError("假期去回顺序无效")
        windows.add((begin,end))
    return sorted(windows)


def schedule(day, config, public_missing=()):
    tasks = []
    focus = set()
    horizon = six_months_after(day)
    for out, back in holiday_windows(config):
        if day <= out+2*DAY and out-2*DAY <= horizon and back+2*DAY >= day:
            for city in CITIES:
                route = "TYO-"+city
                tasks.append(Task("holiday",route,max(day,out-2*DAY),out+2*DAY,
                                  max(day,back-2*DAY),back+2*DAY))
            focus.update(out+n*DAY for n in range(-2,3))
    if day.toordinal() % 3 == 0:
        for route in [r for city in CITIES for r in ("TYO-"+city, city+"-TYO")]:
            dest = route.split("-")[1] if route.startswith("TYO-") else route.split("-")[0]
            # Dalian has an ordinary public shortlist; holidays retain all carriers.
            choices = FOCUS_CARRIERS["DLC"] if dest == "DLC" else (("", "any"),)
            for carrier, stops in choices:
                tasks.append(Task("one_way",route,day,horizon,airline=carrier,stops=stops))
            if dest != "DLC":
                for carrier, stops in FOCUS_CARRIERS.get(dest, ()):
                    tasks.append(Task("carrier",route,day,horizon,airline=carrier,stops=stops))
    ordinary = []
    stays = tuple(dict.fromkeys(config.get("stays", [7,14,21,30])))
    if not stays or any(type(n) is not int or not 1 <= n <= 120 for n in stays):
        raise ValueError("停留天数应为1–120")
    for start, end in fortnight_windows(day):
        is_focus = any(start <= d <= end for d in focus)
        if not is_focus and day.toordinal() % 2:
            continue
        for city in CITIES:
            choices = FOCUS_CARRIERS["DLC"] if city == "DLC" and not is_focus else (("", "any"),)
            for stay in stays:
                for carrier, stops in choices:
                    ordinary.append(Task("ordinary","TYO-"+city,start,end,start+stay*DAY,end+stay*DAY,
                                         carrier,stops,stay))
    tasks.extend(ordered(ordinary, day.toordinal()*17))
    for route, dep in public_missing:
        if day <= dep:
            tasks.append(Task("history", route, dep, dep))
    return list(dict.fromkeys(tasks))


def originals(day, config):
    pairs = set()
    for q in config.get("requests", []):
        route = q["route"]
        out, back = map(dt.date.fromisoformat, (q["departure"],q["returnDate"]))
        if route not in {"TYO-"+c for c in CITIES} or out > back:
            raise ValueError("私有原日期无效")
        if day <= out <= six_months_after(day):
            pairs.add((route,out.isoformat(),back.isoformat()))
    return sorted(pairs)


def month_report(day, config, public_missing=()):
    days = calendar.monthrange(day.year, day.month)[1]
    total = Counter()
    max_pairs = int(config.get("lowest_pairs",2))
    if not 0 <= max_pairs <= 20:
        raise ValueError("低价配对数应为0–20")
    remaining_history = list(public_missing)
    for n in range(1, days+1):
        date = day.replace(day=n)
        tasks = schedule(date, config, remaining_history)
        total.update(t.kind for t in tasks)
        completed = {(t.route,t.start) for t in tasks if t.kind=="history"}
        remaining_history = [h for h in remaining_history if h not in completed]
        total["original_pair_calls_max"] += 2*len(originals(date,config))
        total["extra_pair_calls_max"] += 2*max_pairs*int(any(t.kind in ("ordinary","holiday") for t in tasks))
    reserve = int(config.get("reserve",100))
    limit = int(config.get("monthly_limit",1000))
    if not 0 <= reserve < limit:
        raise ValueError("额度保留值必须低于月度上限")
    full_max = sum(total.values())
    lower = full_max - total["extra_pair_calls_max"] - total["original_pair_calls_max"]//2
    return dict(days=days, by_category=dict(total), full_max=full_max, full_min=lower,
                cap=limit, reserve=reserve, usable=limit-reserve,
                unserved_at_max=max(0,full_max-(limit-reserve)))
