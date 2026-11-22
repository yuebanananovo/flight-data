import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import round_trips as rt


OUT = "2030-05-07"
BACK = "2030-05-18"
TODAY = dt.date(2030, 4, 1)
WINDOW = {"route": "TYO-BJS", "departure": OUT, "returnDate": BACK, "flexOut": 1, "flexBack": 1}


def flight(number, a, b, day, combined=False):
    def airport(code, time):
        if combined:
            return {"id": code, "time": day + " " + time}
        return {"id": code, "date": day, "time": time}
    return {"flight_number": number, "departure_airport": airport(a, "09:30"), "arrival_airport": airport(b, "12:15")}


def outbound():
    return {"flights": [flight("XY 12", "NRT", "PKX", OUT, True)], "price": 58000, "departure_token": "example-out"}


def inbound():
    return {"flights": [flight("XY 13", "PEK", "HND", BACK)], "price": 60500, "booking_token": "example-back"}


class RoundTripTests(unittest.TestCase):
    def test_calendar_leaves_missing_cells_empty(self):
        lookup = Mock(return_value={"calendar": [
            {"departure": OUT, "return": BACK, "price": 60000}, {"departure": OUT, "return": BACK, "price": 55000},
            {"departure": OUT, "return": "2030-05-01", "price": 999},
        ]})
        cells = rt.calendar(lookup, {"outbound_date": OUT, "return_date": BACK}, WINDOW, TODAY)
        self.assertEqual(len(cells), 9)
        priced = [r for r in cells if r["status"] == "priced"]
        self.assertEqual(priced, [{"departure": OUT, "return": BACK, "price": 55000, "status": "priced"}])
        self.assertTrue(all(r["price"] is None for r in cells if r["status"] != "priced"))
        self.assertEqual(lookup.call_args.args[0]["outbound_date_start"], "2030-05-06")

    def test_pair_uses_selected_return_total_only(self):
        p = SimpleNamespace(name="SearchApi", remaining=2, search=Mock(side_effect=[
            {"other_flights": [dict(outbound(), extensions=["Separate tickets"], price=10), outbound()]},
            {"other_flights": [dict(inbound(), self_transfer=True, price=5), inbound()]},
        ]))
        rows, book, _ = rt.pair(p, {"outbound_date": OUT, "return_date": BACK}, "BJS")
        self.assertEqual(p.search.call_args.args[0]["departure_token"], "example-out")
        self.assertEqual([r["total_jpy"] for r in rows], [60500])
        self.assertEqual(book, "example-back")
        self.assertEqual(rows[0]["outbound"]["flights"][0]["departure_airport"]["time"], "09:30")
        self.assertNotIn("token", json.dumps(rows))

    def test_same_segment_seller_requires_both_directions_and_known_schedule(self):
        out, back = rt.itinerary(outbound()), rt.itinerary(inbound())
        option = {"price": 54000, "book_with": "Example", "is_airline": True}
        different = {"flights": [flight("XY 13", "PEK", "NRT", BACK)]}
        missing = {"flights": [{"flight_number": "XY 13"}]}
        response = {"itineraries": [
            {"legs": [out, different], "booking_options": [option]},
            {"legs": [out, missing], "booking_options": [option]},
            {"legs": [out, back], "booking_options": [option, dict(option, is_split_booking=True)]},
        ]}
        provider = SimpleNamespace(search=lambda q: response)
        dates = {"outbound_date": OUT, "return_date": BACK}
        rows = rt.sky_sellers(provider, dates, "BJS", [{"outbound": out, "inbound": back}, {"outbound": out, "inbound": missing}])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["official"])
        self.assertFalse(rt.seller_rows([dict(option, flight_numbers=["XY 90", "XY 13"])], out, back, OUT, BACK, "Google"))

    def test_provider_rate_limits_block_further_requests(self):
        account = {"account": {"remaining_credits": 50}, "api_usage": {"hourly_rate_limit": 6, "searches_this_hour": 2}}
        with patch.object(rt, "fetch", side_effect=[account, rt.ProviderLimit("HTTP 429")]) as fetch:
            provider = rt.Provider("SearchApi", "placeholder", 12, 45)
            self.assertEqual(provider.remaining, 4)
            with self.assertRaises(rt.ProviderLimit):
                provider.search({})
            with self.assertRaises(rt.Pending):
                provider.search({})
            self.assertEqual((provider.calls, fetch.call_count), (1, 2))

    def test_input_deduplicates_and_rejects_reverse_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.json"
            path.write_text(json.dumps({"version": 1, "requests": [WINDOW, WINDOW]}))
            self.assertEqual(rt.validated_queue(path, TODAY), [WINDOW])
            path.write_text(json.dumps({"version": 1, "requests": [dict(WINDOW, returnDate="2030-05-01")]}))
            with self.assertRaises(ValueError):
                rt.validated_queue(path, TODAY)

    def test_local_resume_and_quota_fallback(self):
        account = {"account": {"remaining_credits": 5}, "api_usage": {"hourly_rate_limit": 5, "searches_this_hour": 0}}
        serp_account = {"total_searches_left": 248, "account_rate_limit_per_hour": 10, "this_hour_searches": 0}
        args = SimpleNamespace(max_searches=1, reserve=0, serp_fallback=0, serp_reserve=246, alternatives=0,
                               sellers=False, google_sellers=False)
        with tempfile.TemporaryDirectory() as folder, patch.dict(rt.os.environ, {"SEARCHAPI_KEY": "test", "SERPAPI_KEY": "test"}):
            path = Path(folder) / "result.json"
            journal = {"version": 1, "batches": []}
            with patch.object(rt, "fetch", side_effect=[account, {"calendar": []}]) as network:
                self.assertFalse(rt.run(args, [WINDOW], path, journal, TODAY))
                self.assertEqual(network.call_count, 2)
            q = json.loads(path.read_text())["batches"][0]
            self.assertEqual(q["calendar_status"], "complete")
            self.assertEqual(q["pair_status"][0]["status"], "pending")
            args.max_searches, args.serp_fallback = 0, 3
            with patch.object(rt, "fetch", side_effect=[serp_account, {"best_flights": [outbound()]},
                                                      {"other_flights": [inbound()]}]) as network:
                self.assertTrue(rt.run(args, [WINDOW], path, journal, TODAY))
                self.assertEqual(network.call_count, 3)
            q = journal["batches"][0]
            self.assertEqual(q["pair_status"][0]["status"], "priced")
            self.assertEqual(q["paired"][0]["total_jpy"], 60500)
            with patch.object(rt, "fetch", side_effect=AssertionError("duplicate call")), patch.object(rt.getpass, "getpass", side_effect=AssertionError("unused key")):
                args.max_searches = 1
                self.assertTrue(rt.run(args, [WINDOW], path, journal, TODAY))
            self.assertEqual(len(q["paired"]), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_absent_flight_list_is_retryable_but_empty_list_is_a_result(self):
        with self.assertRaises(rt.Unavailable):
            list(rt.eligible({"search_metadata": {}}, "TYO", "BJS", OUT))
        self.assertEqual(list(rt.eligible({"best_flights": []}, "TYO", "BJS", OUT)), [])

    def test_resuming_google_seller_keeps_original_selected_itinerary(self):
        q = dict(WINDOW, flexOut=0, flexBack=0)
        args = SimpleNamespace(max_searches=3, reserve=0, serp_fallback=0, serp_reserve=0, alternatives=0,
                               sellers=False, google_sellers=True)
        account = {"account": {"remaining_credits": 10}, "api_usage": {"hourly_rate_limit": 10, "searches_this_hour": 0}}
        saved = {"depart": OUT, "return_date": BACK, "outbound": rt.itinerary(outbound()), "inbound": rt.itinerary(inbound())}
        batch = {"route": q["route"], "observed_date_jst": TODAY.isoformat(), "request": [q["route"], OUT, BACK, 0, 0],
                 "paired": [saved], "calendar": [], "sellers": [], "completed": ["calendar", "pair:" + OUT + "/" + BACK]}
        journal = {"version": 1, "batches": [batch]}
        different = dict(inbound(), flights=[flight("XY 99", "PEK", "HND", BACK)])
        with tempfile.TemporaryDirectory() as folder, patch.dict(rt.os.environ, {"SEARCHAPI_KEY": "test"}):
            path = Path(folder) / "result.json"
            with patch.object(rt, "fetch", side_effect=[account, {"best_flights": [outbound()]}, {"best_flights": [different]}]):
                self.assertFalse(rt.run(args, [q], path, journal, TODAY))
                self.assertFalse(batch["sellers"])
            options = {"booking_options": [{"price": 61000, "book_with": "Example"}]}
            with patch.object(rt, "fetch", side_effect=[account, {"best_flights": [outbound()]}, {"best_flights": [inbound()]}, options]):
                self.assertTrue(rt.run(args, [q], path, journal, TODAY))
                self.assertEqual(batch["sellers"][0]["price"], 61000)
