#!/usr/bin/env python3
"""每天采集一次中日航线的机票价格，写入 data/prices.json，网站直接读这个文件。

数据来源：SerpApi 的 Google Flights 接口（https://serpapi.com/google-flights-api）。
需要环境变量 SERPAPI_KEY，在 GitHub 仓库的 Settings → Secrets and variables → Actions 里设置。

按 SerpApi 套餐分三个采集档位，用环境变量 PLAN 选（GitHub 仓库 Settings → Variables 里设）：
  free     免费套餐（250 次/月）：东京与沈阳、北京、上海 6 个方向，每天查单程“提前 30 天”；每周一查单程和往返价格日历，最多 246 次/月（常规排程）
  starter  Starter（1,000 次/月）：全部 28 个方向，每天查单程“提前 30 天”；每月 1 号和 16 号查单程和往返价格日历，约 980 次/月
  full     Developer（5,000 次/月）：全部 28 个方向，每天查单程提前 7/14/30/60 天；单数日查单程和往返价格日历、
           官网/平台比价；每周一另查一次“只看直飞”的价格日历，约 4,960 次/月
往返：网站上任意“去几天回”（1–120 天）都能用两张单程拼出来；真正的往返票按 RT_LENS 里的几种天数
轮流查（不同航线错开），每次一条航线查一种天数。
搜索结果包含转机航班（比如经首尔转机的大韩航空），网站上可以切换“含转机 / 只看直飞”。
只用 Python 自带的库，不需要安装任何东西。

用法:
  python scripts/collect.py                 正常采集
  PLAN=starter python scripts/collect.py    指定档位（不写默认 free）
  python scripts/collect.py --limit 2       只采前 2 个航线方向（试用、省额度）
  python scripts/collect.py --no-calendar   不采价格日历（每天少 56 次调用）
  python scripts/collect.py --fixture DIR   用本地的假响应测试解析，不联网
  python scripts/collect.py --backfill      一次性回填过去约两个月的价格（只需要跑一次）：
                                            每个航线方向查未来 2 个月里每隔 3 天的出发日期，
                                            用 Google 返回的历史价格拼出过去的走势。
                                            free 档最多 120 次，starter / full 档约 560 次
"""
import argparse
import calendar
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "prices.json")
API = "https://serpapi.com/search.json"

LEADS = [7, 14, 30, 60]  # 提前几天：和网站上的“提前购买”按钮一致，按航司分别记录
# 价格日历里这些提前天数的价格也存下来（记作航司 "*"），网站用它算“什么时候买最划算”
CAL_LEADS = [7, 14, 21, 30, 45, 60, 90, 120, 150, 180]
JP = ["TYO", "OSA"]
CN = ["SHE", "DLC", "TAO", "SHA", "BJS", "TSN", "HKG"]
# 一个城市有多个机场时用逗号连起来，一起搜
AIRPORTS = {
    "TYO": "HND,NRT", "OSA": "KIX,ITM",
    "SHE": "SHE", "DLC": "DLC", "TAO": "TAO", "SHA": "PVG,SHA",
    "BJS": "PEK,PKX", "TSN": "TSN", "HKG": "HKG",
}
# 日历接口接受单个机场或城市的 Google location kgmid。
CALENDAR_LOCATIONS = {**AIRPORTS, "TYO": "/m/07dfk", "OSA": "/m/0dqyw",
                      "SHA": "/m/06wjf", "BJS": "/m/01914"}
# 常见转机地的中文名，其他机场用接口给的名字
HUBS = {"ICN": "首尔", "GMP": "首尔", "PUS": "釜山", "CJU": "济州", "PVG": "上海", "SHA": "上海", "PEK": "北京",
        "PKX": "北京", "TPE": "台北", "TSA": "台北", "KHH": "高雄", "HKG": "香港", "MFM": "澳门", "CAN": "广州",
        "SZX": "深圳", "DLC": "大连", "TAO": "青岛", "SHE": "沈阳", "TSN": "天津", "NRT": "东京", "HND": "东京",
        "KIX": "大阪", "ITM": "大阪", "NGO": "名古屋", "FUK": "福冈", "MNL": "马尼拉", "BKK": "曼谷", "SGN": "胡志明市"}
RT_LENS = [1, 3, 7, 14, 30, 60, 120]  # 往返票轮流查的“去几天回”
ROUTES = [r for j in JP for c in CN for r in (f"{j}-{c}", f"{c}-{j}")]
PLANS = {
    "free": {"routes": ["TYO-SHE", "SHE-TYO", "TYO-BJS", "BJS-TYO", "TYO-SHA", "SHA-TYO"], "leads": [30],
             "cal": lambda d: d.weekday() == 0, "cal_direct": lambda d: False, "booking": lambda d: False},
    "starter": {"routes": ROUTES, "leads": [30],
                "cal": lambda d: d.day in (1, 16), "cal_direct": lambda d: False, "booking": lambda d: False},
    "full": {"routes": ROUTES, "leads": LEADS,
             "cal": lambda d: d.day % 2 == 1, "cal_direct": lambda d: d.weekday() == 0, "booking": lambda d: d.day % 2 == 1},
}
KEEP_DAYS = 800
BACKFILL_DEPS = list(range(3, 61, 3))  # 回填时查的出发日期：3、6、9…60 天后  # 保留最近约两年的数据，文件不会无限变大


def jst_today():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()


def calendar_query(base, route, today):
    origin, dest = route.split("-")
    return dict(base, engine="google_flights_calendar", departure_id=CALENDAR_LOCATIONS[origin],
                arrival_id=CALENDAR_LOCATIONS[dest], date_range_start=(today + dt.timedelta(days=1)).isoformat(),
                date_range_end=(today + dt.timedelta(days=180)).isoformat())


def round_trip_length(plan, route, today):
    if plan == "free":
        slot = today.toordinal() // 7
    elif plan == "starter":
        slot = (today.year * 12 + today.month) * 2 + int(today.day >= 16)
    else:
        # 普通年有 186 个单数日，闰年多一个。跨月时按实际天数轮换。
        years = today.year - 1
        previous_months = sum((calendar.monthrange(today.year, m)[1] + 1) // 2 for m in range(1, today.month))
        slot = years * 186 + calendar.leapdays(1, today.year) + previous_months + (today.day - 1) // 2
    return RT_LENS[(slot + ROUTES.index(route)) % len(RT_LENS)]


def backfill_allowance(key, remaining):
    """回填前读取免费账户接口，仅在小时额度足够时继续。"""
    url = "https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": key})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                account = json.load(response)
            if int(account["total_searches_left"]) < remaining:
                sys.exit("剩余搜索额度不足以完成本次回填，停止。")
            available = int(account["account_rate_limit_per_hour"]) - int(account["this_hour_searches"])
        except (OSError, ValueError, KeyError, TypeError):
            sys.exit("无法确认账户额度，停止回填。")
        if available > 0:
            return available
        if attempt < 2:
            print("等待小时额度恢复（约 60 分钟）。", flush=True)
            time.sleep(3605)
    sys.exit("小时额度仍不可用，停止回填。")


def call(params, key, fixture=None, name=""):
    """调用一次接口。失败会重试两次，再失败就返回 None，不让整个任务停掉。"""
    if fixture:
        path = os.path.join(fixture, name + ".json")
        if not os.path.exists(path):
            path = os.path.join(fixture, params["engine"] + ".json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    q = dict(params, api_key=key)
    url = API + "?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                data = json.load(r)
            if data.get("error"):
                # “没有结果”不算错误，其他错误重试
                if "hasn't returned any results" in data["error"]:
                    return data
                raise RuntimeError(data["error"])
            return data
        except Exception as e:  # noqa: BLE001
            status = e.code if isinstance(e, urllib.error.HTTPError) else type(e).__name__
            print(f"  ! {name} 第 {attempt + 1} 次失败: {status}", file=sys.stderr)
            if status in (401, 403, 429):
                sys.exit(f"采集暂停，接口返回 HTTP {status}。请检查密钥和额度。")
            if status == 400:
                return None
            time.sleep(3 * (attempt + 1))
    return None


def stop_label(legs, layovers):
    """保留整程报价，用航段和机场区分经停、转乘与未确认的中途停站。"""
    if len(legs) == 1 and not layovers:
        return ""
    via = []
    for airport in layovers or [leg.get("arrival_airport") or {} for leg in legs[:-1]]:
        code = airport.get("id")
        city = HUBS.get(code) or airport.get("name") or code
        if city and city not in via:
            via.append(city)
    place = "、".join(via) or "中途站"
    numbers = ["".join((leg.get("flight_number") or "").upper().split()) for leg in legs]
    airlines = [leg.get("airline") for leg in legs]
    arrivals = [(leg.get("arrival_airport") or {}).get("id") for leg in legs[:-1]]
    departures = [(leg.get("departure_airport") or {}).get("id") for leg in legs[1:]]
    kind = "中转方式待确认"
    if arrivals and all(arrivals) and all(departures):
        if arrivals != departures:
            kind = "换机场转机"
        elif all(numbers) and all(airlines):
            if len(set(numbers)) == 1 and len(set(airlines)) == 1:
                kind = "同航班号经停"
            elif len(set(numbers)) > 1:
                kind = "转乘不同航班"
    return f"（经{place}·{kind}）"


def airline_prices(res):
    """从一次搜索结果里取出每家航司的最低价。
    直飞记作航司名，比如“全日空”；转机记作“大韩航空（经首尔）”；两家航司拼的记作“甲+乙（经某地）”。"""
    best = {}
    for group in ("best_flights", "other_flights"):
        for opt in res.get(group) or []:
            price = opt.get("price")
            legs = opt.get("flights") or []
            if not isinstance(price, (int, float)) or price <= 0 or not legs:
                continue
            names = []
            for leg in legs:
                a = leg.get("airline")
                if a and a not in names:
                    names.append(a)
            name = "+".join(names) or "其他"
            name += stop_label(legs, opt.get("layovers") or [])
            if name not in best or price < best[name]:
                best[name] = int(price)
    return best


def cheapest_option(res):
    """最便宜的一个选项（用来查它在官网和各平台的价格）。"""
    best = None
    for group in ("best_flights", "other_flights"):
        for opt in res.get(group) or []:
            if opt.get("booking_token") and isinstance(opt.get("price"), (int, float)):
                if best is None or opt["price"] < best["price"]:
                    best = opt
    return best


def booking_sellers(res, airlines):
    """预订选项：同一班飞机在航司官网、Trip.com 等各卖多少钱，每个方案一行。"""
    out = []
    for item in res.get("booking_options") or []:
        parts = [item.get("together")] if item.get("together") else [item.get("departing")]
        for o in parts:
            if not o or not isinstance(o.get("price"), (int, float)):
                continue
            seller = o.get("book_with") or "?"
            official = bool(o.get("airline")) or any(a and a in seller for a in airlines)
            out.append([seller, int(o["price"]), o.get("option_title") or "", 1 if official else 0])
    return sorted(out, key=lambda x: x[1])


def calendar_rt_prices(res, trip):
    """往返价格日历：只留“回程日 - 出发日 = trip”的。"""
    out = []
    for item in res.get("prices") or res.get("calendar") or []:
        d, back, p = item.get("outbound_date") or item.get("date"), item.get("return_date"), item.get("price")
        if not d or not isinstance(p, (int, float)) or p <= 0:
            continue
        try:
            if not back or (dt.date.fromisoformat(back) - dt.date.fromisoformat(d)).days != trip:
                continue
        except ValueError:
            continue
        out.append([d, int(p)])
    return sorted(out)


def calendar_prices(res):
    out = []
    for item in res.get("prices") or res.get("calendar") or []:
        d, p = item.get("outbound_date") or item.get("date"), item.get("price")
        if d and isinstance(p, (int, float)) and p > 0:
            out.append([d, int(p)])
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-calendar", action="store_true")
    ap.add_argument("--fixture")
    ap.add_argument("--date", help="假装今天是这一天（测试用），格式 2026-11-23")
    ap.add_argument("--backfill", action="store_true", help="一次性回填过去约两个月的价格")
    args = ap.parse_args()

    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key and not args.fixture:
        print("没有设置 SERPAPI_KEY，这次跳过采集（网站会继续显示模拟数据）。")
        return 1

    today = dt.date.fromisoformat(args.date) if args.date else jst_today()
    plan_name = os.environ.get("PLAN", "free").strip().lower() or "free"
    if plan_name not in PLANS:
        print(f"PLAN={plan_name} 不认识，改用 free。可选：free / starter / full")
        plan_name = "free"
    plan = PLANS[plan_name]
    routes = plan["routes"][: args.limit] if args.limit else plan["routes"]
    print(f"采集档位：{plan_name}，{len(routes)} 个航线方向，提前 {plan['leads']} 天")

    data = {"rows": [], "calendar": {}, "insights": {}}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    rows = {(r[0], r[1], r[2], r[3]): r[4] for r in data.get("rows", [])}
    insights = data.get("insights", {})
    booking = data.get("booking", {})
    cal = {"date": today.isoformat(), "routes": {}, "routesDirect": {}, "routesRT": {}}

    base = {"engine": "google_flights", "type": "2", "currency": "JPY", "hl": "zh-cn", "gl": "jp"}
    calls = ok = 0
    if args.backfill:
        remaining = len(routes) * len(BACKFILL_DEPS)
        allowance = 0
        routes_to_do = routes
        print(f"回填：{len(routes_to_do)} 个航线方向 × {len(BACKFILL_DEPS)} 个出发日期，约 {len(routes_to_do) * len(BACKFILL_DEPS)} 次调用")
        routes = []  # 回填时不做每天的常规采集
        for route in routes_to_do:
            a, b = route.split("-")
            got = 0
            for k in BACKFILL_DEPS:
                if not args.fixture and allowance == 0:
                    if calls:
                        print("本批小时额度已用完，约 60 分钟后继续回填。", flush=True)
                        time.sleep(3605)
                    allowance = backfill_allowance(key, remaining)
                allowance -= 1
                remaining -= 1
                dep_d = today + dt.timedelta(days=k)
                dep = dep_d.isoformat()
                res = call(dict(base, departure_id=AIRPORTS[a], arrival_id=AIRPORTS[b], outbound_date=dep), key, args.fixture, f"{route}_bf{k}")
                calls += 1
                if not res:
                    continue
                if k in LEADS:  # 今天的价格也顺便存下
                    for airline, p in airline_prices(res).items():
                        rows[(today.isoformat(), route, dep, airline)] = p
                for pt in (res.get("price_insights") or {}).get("price_history") or []:
                    try:
                        obs = (dt.datetime.fromtimestamp(pt[0], dt.timezone.utc) + dt.timedelta(hours=9)).date()
                        if (dep_d - obs).days in CAL_LEADS and obs < today and int(pt[1]) > 0:
                            rows.setdefault((obs.isoformat(), route, dep, "*"), int(pt[1]))
                            got += 1
                    except (TypeError, ValueError, IndexError, OSError):
                        pass
            ok += int(got > 0)
            print(f"{route} 回填：拿到 {got} 个过去的价格点")
    for route in routes:
        a, b = route.split("-")
        for lead in plan["leads"]:
            dep = (today + dt.timedelta(days=lead)).isoformat()
            params = dict(base, departure_id=AIRPORTS[a], arrival_id=AIRPORTS[b], outbound_date=dep)
            res = call(params, key, args.fixture, f"{route}_{lead}")
            calls += 1
            if not res:
                continue
            prices = airline_prices(res)
            for airline, p in prices.items():
                rows[(today.isoformat(), route, dep, airline)] = p
            pi = res.get("price_insights")
            main = lead == (30 if 30 in plan["leads"] else plan["leads"][-1])
            if pi and main:
                insights[route] = {
                    "date": today.isoformat(), "dep": dep,
                    "lowest": pi.get("lowest_price"), "level": pi.get("price_level"),
                    "typical": pi.get("typical_price_range"), "history": pi.get("price_history"),
                }
                # Google 的历史价格：这一天出发的票，过去每天的最低价。
                # 提前天数刚好对上的点也存进去，“买票时机”能更早算出来
                dep_d = dt.date.fromisoformat(dep)
                for pt in pi.get("price_history") or []:
                    try:
                        obs = (dt.datetime.fromtimestamp(pt[0], dt.timezone.utc) + dt.timedelta(hours=9)).date()
                        if (dep_d - obs).days in CAL_LEADS and obs < today and int(pt[1]) > 0:
                            rows.setdefault((obs.isoformat(), route, dep, "*"), int(pt[1]))
                    except (TypeError, ValueError, IndexError, OSError):
                        pass
            if main and plan["booking"](today):
                opt = cheapest_option(res)
                if opt:
                    names = [leg.get("airline") for leg in opt.get("flights") or []]
                    bres = call(dict(params, booking_token=opt["booking_token"]), key, args.fixture, f"{route}_booking")
                    calls += 1
                    sellers = booking_sellers(bres or {}, names)
                    if sellers:
                        label = "+".join(dict.fromkeys(n for n in names if n)) or "?"
                        booking.setdefault(route, []).append([today.isoformat(), dep, label, sellers])
                        booking[route] = booking[route][-120:]
                        print(f"{route} 比价: {len(sellers)} 个卖家，最低 {sellers[0][0]} ¥{sellers[0][1]:,}")
            ok += 1 if prices else 0
            print(f"{route} 提前{lead}天 {dep}: {len(prices)} 家航司" + (f"，最低 ¥{min(prices.values()):,}" if prices else ""))
        if not args.no_calendar:
            # 两次：含转机（stops=0，不限）和只看直飞（stops=1）
            for stops, field, tag in (("0", "routes", "*"), ("1", "routesDirect", "*D")):
                if not plan["cal" if tag == "*" else "cal_direct"](today):
                    continue
                params = dict(calendar_query(base, route, today), stops=stops)
                res = call(params, key, args.fixture, f"{route}_calendar{tag}")
                calls += 1
                if not res:
                    continue
                lst = calendar_prices(res)
                if lst:
                    cal[field][route] = lst
                    want = {(today + dt.timedelta(days=L)).isoformat() for L in CAL_LEADS}
                    for d, p in lst:
                        if d in want:
                            rows[(today.isoformat(), route, d, tag)] = p
                print(f"{route} 价格日历（{'直飞' if tag == '*D' else '含转机'}）: {len(lst)} 天")
            # 往返票价格日历：这次查哪种“去几天回”，按日期和航线轮换
            if plan["cal"](today):
                trip = round_trip_length(plan_name, route, today)
                params = dict(calendar_query(base, route, today), type="1", round_trip_days=trip)
                res = call(params, key, args.fixture, f"{route}_calendarRT")
                calls += 1
                lst = calendar_rt_prices(res or {}, trip)
                if lst:
                    cal["routesRT"].setdefault(route, {})[str(trip)] = lst
                print(f"{route} 往返价格日历（去 {trip} 天回）: {len(lst)} 天")

    if not ok:
        print("没有取到本次所需的价格，请检查接口、密钥和账户额度。", file=sys.stderr)
        return 1

    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    out_rows = sorted([k[0], k[1], k[2], k[3], v] for k, v in rows.items() if k[0] >= cutoff)
    old_cal = data.get("calendar") or {}
    # 今天没查价格日历（或没查到）的部分，保留上一次的
    for field in ("routes", "routesDirect"):
        for r, lst in (old_cal.get(field) or {}).items():
            cal[field].setdefault(r, lst)
    for r, by_len in (old_cal.get("routesRT") or {}).items():
        for trip, lst in by_len.items():
            cal["routesRT"].setdefault(r, {}).setdefault(trip, lst)
    out = {
        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "currency": "JPY",
        "note": "rows 每行: [查询日期, 航线, 出发日期, 航司, 最低单程价]",
        "rows": out_rows,
        "calendar": cal,
        "insights": insights,
        "booking": booking,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"完成：请求 {calls} 项，文件共 {len(out_rows)} 行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
