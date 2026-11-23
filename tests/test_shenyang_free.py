from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import shenyang_free as worker

OUT = "2098-05-01"
BACK = "2098-06-03"
PLAN = dict(route="TYO-SHE", departure=OUT, returnDate=BACK, strategy="per_airline")


def sample(carrier, inbound=False, operator=None):
    start, end, day = ("SHE", "HND", BACK) if inbound else ("HND", "SHE", OUT)
    flight = dict(flight_number=carrier + "123", departure_airport=dict(id=start, date=day),
                  arrival_airport=dict(id=end, date=day))
    if operator:
        flight["operating_airline"] = operator
    return dict(flights=[flight], price=31415, departure_token="synthetic")


class Account:
    def __init__(self, fail=None):
        self.remaining = 8
        self.calls = 0
        self.fail = fail

    def search(self, params):
        self.remaining -= 1
        self.calls += 1
        carrier = params["included_airlines"].split(",")[0]
        if carrier == self.fail:
            raise worker.Unavailable("HTTP 502")
        return {"best_flights": [sample(carrier, "departure_token" in params, "Shanghai Airlines")]}


class Tracking(unittest.TestCase):
    def test_missing_operator_and_cz_stop(self):
        self.assertEqual(worker.choose({"best_flights":[sample("FM")]}, "TYO","SHE",OUT,"MU",True)[1], "unverified_operator")
        row = sample("CZ")
        row["flights"][0]["stops"] = ["ICN"]
        self.assertFalse(worker.eligible_carrier(row,"CZ"))

    def test_only_confirmed_returns(self):
        account = Account("CZ")
        batch = worker.collect(account,PLAN,"daily")["batch"]
        self.assertEqual(account.calls, 7)
        self.assertEqual([r["carrier"] for r in batch["paired"]], ["KE","CA","MU"])
        self.assertEqual(batch["pair_status"][0]["status"],"query_failed")
        self.assertEqual(len(batch["pair_status"]),4)

    def test_repair_counts_first_attempt_toward_daily_cap(self):
        account = Account()
        batch = worker.collect(account, dict(PLAN, prior={"carrier":"CZ","calls":1}), "daily")["batch"]
        self.assertLessEqual(account.calls + 1, 8)
        self.assertEqual(batch["pair_status"][-1]["reason"], "previous_attempt_failed")

    def test_insufficient_credits_start_no_search(self):
        account = Account()
        account.credits = account.remaining = 7
        sent = []
        def update(message):
            sent.append(message)
            return {"job":dict(PLAN,id="job",lease="lease")} if message["action"]=="plan" else {}
        with patch.dict(worker.os.environ, SEARCHAPI_KEY="synthetic"), patch.object(worker,"Provider",return_value=account), patch.object(worker,"update",side_effect=update):
            worker.run("daily")
        self.assertEqual(account.calls,0)
        self.assertEqual(sent[-1]["state"],"insufficient")
        self.assertNotIn("batch",sent[-1])
