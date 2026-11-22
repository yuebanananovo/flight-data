"""按指定出发日查询东京到三个目的地的单程航班，附同月价格日历。"""
import argparse
import calendar
import datetime as dt
import json
import os
from pathlib import Path

from fare_history import record
from collect import AIRPORTS, ROOT, account_capacity, calendar_prices, calendar_query, call, jst_today, stop_label


def itineraries(result, destination):
    options = {}
    for offer in (result.get('best_flights') or []) + (result.get('other_flights') or []):
        legs = offer.get('flights') or []
        price = offer.get('price')
        if not legs or not isinstance(price, (int, float)) or price <= 0:
            continue
        origin = (legs[0].get('departure_airport') or {}).get('id')
        end = (legs[-1].get('arrival_airport') or {}).get('id')
        if origin not in AIRPORTS['TYO'].split(',') or end not in AIRPORTS[destination].split(','):
            continue
        numbers = [leg.get('flight_number') or '' for leg in legs]
        dep = legs[0]['departure_airport'].get('time')
        arr = legs[-1]['arrival_airport'].get('time')
        airline = '+'.join(dict.fromkeys(leg['airline'] for leg in legs if leg.get('airline'))) or '航司未提供'
        via = stop_label(legs, offer.get('layovers') or [])
        key = (origin, end, dep, arr, tuple(numbers), airline, via)
        item = {'airline': airline, 'via': via, 'origin': origin, 'destination': end, 'departure': dep,
                'arrival': arr, 'duration': offer.get('total_duration'), 'numbers': numbers, 'price': int(price)}
        if key not in options or price < options[key]['price']:
            options[key] = item
    return sorted(options.values(), key=lambda o: o['price'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('departure', type=dt.date.fromisoformat)
    args = parser.parse_args()
    today = jst_today()
    departure = args.departure
    if not today <= departure <= today + dt.timedelta(days=330):
        parser.error('出发日须在今天到未来 330 天之间')
    key = os.environ.get('SERPAPI_KEY', '').strip()
    if not key:
        parser.error('需要在 GitHub Secrets 设置密钥')
    total, hourly, _ = account_capacity(key)
    if min(total, hourly) < 6:
        parser.error('当前剩余额度不足以完成六项查询')
    path = Path(ROOT) / 'data/trips.json'
    data = json.loads(path.read_text()) if path.exists() else {'dates': {}, 'months': {}}
    date_key = departure.isoformat()
    month = date_key[:7]
    start = max(today, departure.replace(day=1))
    end = departure.replace(day=calendar.monthrange(departure.year, departure.month)[1])
    base = {'engine': 'google_flights', 'type': '2', 'currency': 'JPY', 'hl': 'zh-cn', 'gl': 'jp'}
    count = 0
    for dest in ('SHE', 'BJS', 'SHA'):
        route = 'TYO-' + dest
        params = dict(base, departure_id=AIRPORTS['TYO'], arrival_id=AIRPORTS[dest], outbound_date=date_key, deep_search='true')
        response = call(params, key, name=route + '_exact')
        if response is not None:
            options = itineraries(response, dest)
            hist = (response.get('price_insights') or {}).get('price_history') or []
            history = sorted({dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=9))).date().isoformat(): int(p)
                              for ts, p in hist if isinstance(ts, (int, float)) and isinstance(p, (int, float)) and p > 0}.items())
            record(route, date_key, history, dated=True)
            data['dates'].setdefault(date_key, {})[route] = {'observed': today.isoformat(), 'options': options, 'history': history}
            count += bool(options)
            print(route, date_key, len(options), '条全程报价；历史', len(history), '天', flush=True)
        params = dict(calendar_query(base, route, today), date_range_start=start.isoformat(), date_range_end=end.isoformat())
        cal = call(params, key, name=route + '_month')
        if cal is not None:
            rows = [[d, p] for d, p in calendar_prices(cal) if start.isoformat() <= d <= end.isoformat()]
            data['months'].setdefault(month, {})[route] = {'observed': today.isoformat(), 'prices': rows}
            print(route, month, len(rows), '个有报价出发日', flush=True)
    if not count:
        raise SystemExit('三个方向均未收到匹配机场的航班报价')
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')


if __name__ == '__main__':
    main()
