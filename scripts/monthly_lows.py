"""从已有单程价格日历汇总每月最低价和全部并列出发日。"""
import json
from datetime import date
from math import isfinite
from pathlib import Path

from collect import PLANS, six_months_after


def _to_date(value):
    return date.fromisoformat(str(value)[:10])


def monthly_lows(prices, start, end):
    """首月及末月按范围裁剪，同日重复报价取较低值。"""
    start, end = _to_date(start), _to_date(end)
    if start > end:
        raise ValueError("start 不能晚于 end")
    routes = PLANS["free"]["routes"]
    months = {}
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months[f"{year:04d}-{month:02d}"] = {route: [None, []] for route in routes}
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    calendar_routes = (prices.get("calendar") or {}).get("routes") or {}
    for route in routes:
        by_day = {}
        for entry in calendar_routes.get(route) or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                day = _to_date(entry[0])
            except (TypeError, ValueError):
                continue
            price = entry[1]
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
                continue
            if start <= day <= end and (day not in by_day or price < by_day[day]):
                by_day[day] = price

        for day in sorted(by_day):
            price = by_day[day]
            key = day.isoformat()
            slot = months[key[:7]][route]
            if slot[0] is None or price < slot[0]:
                slot[:] = [price, [key]]
            elif price == slot[0]:
                slot[1].append(key)
    return months


def main():
    folder = Path(__file__).resolve().parents[1] / "data"
    prices = json.loads((folder / "prices.json").read_text(encoding="utf-8"))
    snapshot = (prices.get("calendar") or {}).get("date")
    if not snapshot:
        raise SystemExit("没有价格日历查询日期")
    start = _to_date(snapshot)
    end = six_months_after(start)
    result = {"source_updated": prices["updated"], "start": start.isoformat(), "end": end.isoformat(),
              "months": monthly_lows(prices, start, end)}
    output = folder / "monthly-lows.json"
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    temp.replace(output)


if __name__ == "__main__":
    main()
