import datetime as dt
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import daily_sources as daily
import collect
import fare_history
import round_trips as rt
from quote_records import fingerprint, private_key_for
from test_round_trips import OUT, flight


PARAMS = {"engine": "google_flights_calendar", "type": "2", "date_range_start": OUT,
          "date_range_end": "2030-05-08", "currency": "JPY", "gl": "jp", "stops": "0"}
SERP = {"total_searches_left": 250, "account_rate_limit_per_hour": 60, "this_hour_searches": 2}
SEARCH = {"account": {"remaining_credits": 100}, "api_usage": {"hourly_rate_limit": 20, "searches_this_hour": 0}}


class DailySourcesTests(unittest.TestCase):
    def test_normal_entry_keeps_historical_prices_on_failed_second_run(self):
        factory = daily.DailySources
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "prices.json"
            output.write_text(json.dumps({"rows":[["2030-05-01", "TYO-SHE", "2030-07-01", "Example", 20000]]}))
            args = ["collect.py", "--date", OUT, "--calendar-only", "--limit", "1"]
            env = {"PLAN": "free", "SERPAPI_KEY": "test", "SEARCHAPI_KEY": "", "DAILY_SEARCHAPI_LIMIT": ""}
            with patch.object(sys, "argv", args), patch.dict(os.environ, env), patch.object(collect, "DATA", root), \
                 patch.object(fare_history, "DATA", root), patch.object(collect, "OUT", str(output)), \
                 patch.object(daily, "DailySources", side_effect=lambda key, n: factory(key, n, root)):
                with patch.object(rt, "fetch", side_effect=[SERP, {"prices": [{"outbound_date": OUT, "price": 22000}]}]):
                    self.assertEqual(collect.main(), 0)
                old = output.read_bytes()
                self.assertTrue(json.loads(old)["rows"])
                self.assertEqual(json.loads(old)["calendar"]["routes"]["TYO-SHE"], [[OUT, 22000]])
                with patch.object(rt, "fetch", side_effect=[SERP, rt.Unavailable("synthetic failure")]):
                    self.assertEqual(collect.main(), 1)
                self.assertEqual(output.read_bytes(), old)
                self.assertTrue(json.loads((root / "collector-status.json").read_text())["warning"])

    def test_free_daily_preflight_checks_both_accounts_only_searches_serp(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"SEARCHAPI_KEY": "test", "DAILY_SEARCHAPI_LIMIT": ""}):
            dest = Path(folder)
            with patch.object(rt, "fetch", side_effect=[SERP, SEARCH, {"prices": [{"outbound_date": OUT, "price": 12000}]}]) as api:
                sources = daily.DailySources("test", 1, dest)
                sources.call(PARAMS, "TYO-SHE")
                sources.finish()
                self.assertEqual(api.call_count, 3)
                self.assertTrue(api.call_args_list[-1].args[0].startswith("https://serpapi.com/search"))
                self.assertTrue(api.call_args_list[1].args[0].endswith("/me"))
            with gzip.open(next((dest / "quote-observations").glob("*.gz")), "rt") as f:
                records = [json.loads(line) for line in f]
            self.assertEqual({r["status"] for r in records}, {"priced", "not_returned"})
            self.assertTrue(all(r["return_date"] is None and r["stay_days"] is None and r["source"].startswith("SerpApi") for r in records))
            self.assertEqual(len({r["parameter_fingerprint"] for r in records}), 1)
            status = json.loads((dest / "collector-status.json").read_text())
            self.assertEqual(status["providers"]["SearchApi"]["state"], "reserved")
            self.assertNotIn(OUT, json.dumps(status))

    def test_granted_backup_is_limited_to_core_and_preserves_source(self):
        env = {"SEARCHAPI_KEY": "test", "DAILY_SEARCHAPI_LIMIT": "2", "SEARCHAPI_RESERVE": "87"}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env):
            with patch.object(rt, "fetch", side_effect=[dict(SERP, total_searches_left=0), SEARCH,
                                   {"calendar": [{"departure": OUT, "price": 5000}]}]) as api:
                sources = daily.DailySources("test", 2, Path(folder))
                self.assertIsNone(sources.call(PARAMS, "TYO-SHA"))
                self.assertTrue(sources.call(PARAMS, "TYO-SHE"))
                self.assertIsNone(sources.call(PARAMS, "TYO-SHE"))
                self.assertEqual(api.call_count, 3)
                self.assertEqual({r["source"] for r in sources.rows if r["status"] == "priced"}, {"SearchApi / Google Flights"})
                self.assertTrue(sources.failed)
                sources.finish()
                self.assertTrue(json.loads((Path(folder) / "collector-status.json").read_text())["warning"])

    def test_malformed_response_is_not_missing_price(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"DAILY_SEARCHAPI_LIMIT": "", "SEARCHAPI_KEY": ""}):
            with patch.object(rt, "fetch", side_effect=[SERP, {"search_metadata": {}}]):
                sources = daily.DailySources("test", 1, Path(folder))
                sources.call(PARAMS, "TYO-SHE")
                self.assertEqual({r["status"] for r in sources.rows}, {"query_failed"})

    def test_round_trip_calendar_has_exact_return_and_stay(self):
        params = dict(PARAMS, type="1", round_trip_days=7)
        priced = {"prices": [{"outbound_date": OUT, "return_date": "2030-05-14", "price": 40000}]}
        rows = list(daily.public_rows(params, priced, "TYO-SHE", "SerpApi / Google Flights", "2030-04-01T14:30:33+09:00", None))
        self.assertEqual(rows[0]["scope"], "round_trip")
        self.assertEqual((rows[0]["return_date"], rows[0]["stay_days"], rows[0]["price"]), ("2030-05-14", 7, 40000))
        self.assertEqual(rows[1]["status"], "not_returned")

    def test_connections_keep_cross_date_and_unknown_policy(self):
        first = flight("XY 20", "HND", "ICN", OUT)
        first["operating_airline"] = "Example Operating"
        second = flight("XY 20", "ICN", "SHE", "2030-05-08")
        itinerary = rt.itinerary({"flights": [first, second], "layovers": [{"id": "ICN", "duration": 1200}]})
        self.assertEqual(itinerary["segments"][0]["marketing_airline"], "XY")
        self.assertEqual(itinerary["segments"][0]["operating_airline"], "Example Operating")
        self.assertIsNone(itinerary["segments"][1]["operating_airline"])
        self.assertEqual(itinerary["segments"][1]["departure_airport"]["date"], "2030-05-08")
        self.assertEqual(itinerary["routing"], "same_number_stop")
        self.assertEqual(itinerary["connections"][0]["duration_minutes"], 1200)
        self.assertTrue(all(itinerary[k] is None for k in ("same_ticket", "refund_rules", "change_rules", "baggage")))
        second["flight_number"] = "XY 30"
        self.assertEqual(rt.itinerary({"flights": [first, second]})["routing"], "transfer")

    def test_private_fingerprint_not_reconstructible_from_public_query(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "prices.json"
            secret = private_key_for(p)
            params = dict(PARAMS, route="TYO-SHE")
            self.assertNotEqual(fingerprint(params), fingerprint(params, secret))
            self.assertEqual(fingerprint(params, secret), fingerprint(params, private_key_for(p)))
            self.assertNotEqual(fingerprint(params, secret), fingerprint(params, private_key_for(Path(folder) / "other.json")))

    def test_expired_private_request_is_recorded_without_search(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(rt, "fetch", side_effect=AssertionError("expired search")):
            rows = {"version": 1, "batches": []}
            request = dict(route="TYO-BJS", departure=OUT, returnDate="2030-05-08", flexOut=0, flexBack=0)
            path = Path(folder) / "quotes.json"
            self.assertTrue(rt.run(object(), [request], path, rows, dt.date(2031, 1, 1)))
            self.assertEqual({r["status"] for r in rows["batches"][0]["records"]}, {"out_of_range"})
