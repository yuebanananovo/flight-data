import datetime as dt
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import paid_collect as pc
import paid_plan as pp
import queue_worker as worker
import round_trips as rt
from quote_records import fingerprint, observation

DAY = dt.date(2030, 3, 1)


class PrivateScheduleTests(unittest.TestCase):
    def test_queue_checks_balance_and_retries_only_the_receipt(self):
        env = dict(HELPER_FLIGHT_QUEUE_URL="https://example.test/api/flights/queue/worker",
                   HELPER_FLIGHT_QUEUE_KEY="placeholder",SERPAPI_KEY="placeholder")
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,dict(env,RUNNER_TEMP=temp)):
            with patch.object(worker,"Provider",return_value=Mock(remaining=0)), patch.object(worker,"post") as post:
                worker.main()
                post.assert_not_called()
            account=Mock(remaining=1)
            account.search.return_value={"best_flights":[]}
            job={"id":"synthetic-id","lease":"synthetic-lease","route":"TYO-SHE","departure":"2031-03-01"}
            with patch.object(worker,"Provider",return_value=account), patch.object(worker,"jst_today",return_value=DAY), \
                 patch.object(worker,"post",side_effect=[{"job":job},OSError(),{"ok":True}]) as post, \
                 patch.object(worker.time,"sleep"), redirect_stdout(io.StringIO()) as output:
                worker.main()
                self.assertEqual(account.search.call_count,1)
                self.assertEqual(post.call_args_list[1],post.call_args_list[2])
                self.assertEqual(post.call_args.args[-1]["state"],"not_returned")
                self.assertNotIn(job["departure"],output.getvalue())
                self.assertFalse(list(Path(temp).iterdir()))

    def test_disabled_workers_use_no_network_and_public_repo_cannot_execute(self):
        with patch.dict(os.environ,{"ENABLE_PAID_FLIGHTS":"","HELPER_FLIGHT_QUEUE_URL":""}), \
             patch.object(pc,"PaidProvider",side_effect=AssertionError),patch.object(worker,"Provider",side_effect=AssertionError):
            pc.main([])
            worker.main()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"event.json"
            path.write_text(json.dumps({"repository":{"private":False,"full_name":"public/example"}}))
            with patch.dict(os.environ,dict(ENABLE_PAID_FLIGHTS="true",GITHUB_EVENT_PATH=str(path),GITHUB_ACTIONS="true",
                                            GITHUB_REPOSITORY="public/example")),patch.object(pc,"PaidProvider",side_effect=AssertionError):
                with self.assertRaises(rt.Pending):
                    pc.main([])

    def test_four_holiday_grids_each_hold_25_combinations(self):
        q={"holidays":[{"departure":"2030-04-11","returnDate":"2030-05-02"}]}
        holiday=[t for t in pp.schedule(DAY,q) if t.kind=="holiday"]
        self.assertEqual({t.route for t in holiday},{"TYO-"+c for c in pp.CITIES})
        p=Mock()
        p.search.return_value={"calendar":[]}
        self.assertEqual([len(pc.collect_task(p,t)) for t in holiday],[25]*4)
        self.assertEqual(p.search.call_count,4)
        self.assertTrue(all(not t.airline for t in holiday))

    def test_14x14_windows_include_last_day_and_ordinary_dalian_shortlist(self):
        day=next(DAY+pp.DAY*n for n in range(6) if (DAY+pp.DAY*n).toordinal()%6==0)
        tasks=pp.schedule(day,{})
        windows=list(pp.fortnight_windows(day))
        self.assertEqual(windows[-1][1],pp.six_months_after(day))
        self.assertTrue(all((b-a).days<=13 for a,b in windows))
        ordinary=[t for t in tasks if t.kind=="ordinary"]
        self.assertEqual({t.stay for t in ordinary},{7,14,21,30})
        sample=next(t for t in ordinary if (t.end-t.start).days==13 and t.stay==30)
        p=Mock(); p.search.return_value={"calendar":[]}
        self.assertEqual(len(pc.collect_task(p,sample)),196)
        self.assertEqual({(t.airline,t.stops) for t in ordinary if t.route=="TYO-DLC"},set(pp.FOCUS_CARRIERS["DLC"]))
        self.assertTrue(any(t.kind=="carrier" and t.airline=="MU,FM" for t in tasks))
        self.assertEqual({t.route for t in tasks if t.kind=="one_way"},{"TYO-"+c for c in pp.CITIES}|{c+"-TYO" for c in pp.CITIES})

    def test_pair_selection_original_first_then_lowest_and_resume(self):
        config={"requests":[dict(route="TYO-SHE",departure="2030-03-10",returnDate="2030-04-10")],"lowest_pairs":1}
        t=pp.Task("holiday","TYO-SHE",DAY,DAY,DAY+7*pp.DAY,DAY+7*pp.DAY)
        p=Mock(remaining=10,stopped=False)
        r=dict(departure=DAY.isoformat(),**{"return":(DAY+7*pp.DAY).isoformat()},price=50000,status="priced")
        with tempfile.TemporaryDirectory() as temp, patch.object(pc,"pair",return_value=([],None,None)) as get_pair, \
             patch.object(pc,"collect_task",return_value=[r]) as collect:
            folder=Path(temp)
            pc.execute(p,DAY,config,folder,[t])
            self.assertEqual([c.args[1]["outbound_date"] for c in get_pair.call_args_list],
                             [config["requests"][0]["departure"],DAY.isoformat()])
            pc.execute(p,DAY,config,folder,[t])
            self.assertEqual(collect.call_count,1)
            self.assertEqual(get_pair.call_count,2)
            result=json.loads((folder/(DAY.isoformat()+".json")).read_text())
            self.assertTrue(any(b["calendar"] for b in result["batches"]))

    def test_paid_guard_excludes_free_credits_and_reserves_budget(self):
        free={"account":{"monthly_allowance":0,"remaining_credits":100,"remaining_searches":100},
              "api_usage":{"hourly_rate_limit":100,"searches_this_hour":0}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{"SEARCHAPI_KEY":"placeholder"}):
            with patch.object(pc,"fetch",return_value=free):
                with self.assertRaises(rt.Pending):
                    pc.PaidProvider({},Path(temp),DAY)
            paid=dict(free,account={"monthly_allowance":1000,"remaining_credits":80,"remaining_searches":180})
            with patch.object(pc,"fetch",return_value=paid):
                pr=pc.PaidProvider({},Path(temp),DAY)
                self.assertEqual(pr.remaining,0)

    def test_history_is_deduplicated_and_monthly_totals_use_civil_days(self):
        config={"stays":[7,14,21,30],"monthly_limit":1000,"reserve":100}
        hist=[("TYO-SHE",dt.date(2030,8,1))]
        report=pp.month_report(DAY,config,hist)
        self.assertEqual(report["days"],31)
        self.assertEqual(report["by_category"]["history"],1)
        self.assertGreater(report["full_max"],report["usable"])
        self.assertEqual(report["usable"],900)
        self.assertTrue(pp.month_report(dt.date(2032,2,1),config)["days"]==29)

    def test_technical_stop_unknowns_and_semantic_fingerprints(self):
        opt={"flights":[{"flight_number":"XY 1","stopovers":[{"id":"ICN"}]}]}
        leg=rt.itinerary(opt)
        self.assertNotEqual(leg["routing"],"nonstop")
        self.assertEqual(leg["segments"][0]["intermediate_stops"][0]["id"],"ICN")
        row=observation("test","2030-01-01T00:00:00+09:00","TYO-SHE","one_way","calendar",
                        DAY.isoformat(),None,None,"not_returned","example")
        self.assertTrue(all(row[k]=="unknown" for k in ("baggage","same_ticket","official","operating_airline","change_rules","refund_rules")))
        self.assertNotEqual(fingerprint({"included_airlines":"CA"}),fingerprint({"included_airlines":"NH"}))
        self.assertNotEqual(fingerprint({"return_date_end":"2030-03-01"}),fingerprint({"return_date_end":"2030-03-02"}))
        self.assertEqual(fingerprint({"api_key":"example"}),fingerprint({"api_key":"other"}))
