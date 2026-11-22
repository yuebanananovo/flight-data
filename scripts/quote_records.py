"""Versioned observations alongside the existing website and import formats."""
import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import tempfile

JST = dt.timezone(dt.timedelta(hours=9))
RESULT_STATES = {"priced", "not_returned", "query_failed", "out_of_range", "pending"}


def jst_now():
    return dt.datetime.now(JST).isoformat(timespec="seconds")


def fingerprint(params, private_key=None):
    # Only normalized, non-secret search criteria enter either fingerprint.
    allowed = ("route", "departure", "return_date", "outbound_date", "date_range_start", "date_range_end",
               "flexOut", "flexBack", "scope", "trip_days", "stops", "engine", "type", "travel_class",
               "currency", "gl", "hl", "adults", "airline", "flight_type", "flight_numbers",
               "included_airlines","include_airlines","excluded_airlines","departure_id","arrival_id",
               "outbound_date_start","outbound_date_end","return_date_start","return_date_end","round_trip_days")
    stable = json.dumps({k: params[k] for k in allowed if k in params}, sort_keys=True,
                        separators=(",", ":")).encode()
    return (hmac.new(private_key, stable, hashlib.sha256).hexdigest() if private_key is not None
            else hashlib.sha256(stable).hexdigest())


def observation(source, when, route, scope, kind, depart, back, price, status, fp, **evidence):
    if status not in RESULT_STATES or (scope == "one_way" and back is not None):
        raise ValueError("invalid observation scope or status")
    if status == "priced" and (type(price) not in (int, float) or not 0 < price < 1000000000):
        raise ValueError("invalid price")
    stay = (dt.date.fromisoformat(back) - dt.date.fromisoformat(depart)).days if back and depart else None
    for k in ("baggage","same_ticket","refund_rules","change_rules","operating_airline","official"):
        if evidence.get(k) is None:
            evidence[k] = "unknown"
    return dict(version=1, source=source, observed_at_jst=when, observed_date_jst=when[:10],
                route=route, scope=scope, price_type=kind, departure=depart, return_date=back,
                stay_days=stay, price=price if status == "priced" else None, currency="JPY",
                status=status, parameter_fingerprint=fp, **evidence)


def append_public(folder, records):
    if not records:
        return
    day = records[0]["observed_date_jst"]
    if not all(r["observed_date_jst"] == day for r in records):
        raise ValueError("multiple observation days in one batch")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (day + ".jsonl.gz")
    existing = set()
    if dest.exists():
        with gzip.open(dest, "rt", encoding="utf-8") as previous:
            existing.update(previous)
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in records]
    new = set(lines) - existing
    if not new:
        return
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
        try:
            with gzip.GzipFile(filename="", fileobj=tmp, mode="wb", mtime=0) as gz:
                for line in sorted(existing | new):
                    gz.write(line.encode("utf-8"))
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, dest)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)


def private_key_for(output):
    key_path = output.with_suffix(output.suffix + ".hmac-key")
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        secret = key_path.read_bytes()
        if len(secret) != 32:
            raise ValueError("本机报价指纹密钥格式异常")
        return secret
    secret = os.urandom(32)
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    return secret
