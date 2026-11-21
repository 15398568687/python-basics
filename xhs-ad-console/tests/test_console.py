import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import db as database
from app.db import Problem, connect, initialize
from app.demo import DEMO_DATE, PASSWORD, sample_csv, seed
from app.intelligence import run_once
from app.reports import parse_csv
from app.server import Handler, ThreadingHTTPServer

DAY = DEMO_DATE.isoformat()
SMALL = "计划,消耗,曝光,点击,线索数\nA,100,1000,20,4\nB,50,400,4,1\n".encode()


class ParserTests(unittest.TestCase):
    def test_aliases_and_skip_total(self):
        raw = '计划名称,消费,展现量,点击量,直播间留资量\nA,"1,200",3000,15,3\n合计,1200,3000,15,3\n'
        rows, _ = parse_csv(raw.encode("gb18030"), DAY)
        self.assertEqual(rows, [(DAY,"A",120000,3000,15,3,None,None,None)])

    def test_optional_missing_is_null(self):
        raw = "计划,消耗,曝光,点击,线索数\nA,0,0,0,\n"
        rows, _ = parse_csv(raw.encode(), DAY)
        self.assertIsNone(rows[0][6])

    def test_invalid_rows_are_atomic(self):
        for later in ("A,1,2,3,0", "B,foo,20,1,2", "B,-1,20,1,2", "B,1.005,20,1,2", "B,1,2,3"):
            with self.subTest(later=later), self.assertRaisesRegex(Problem,"第 4 行"):
                parse_csv(SMALL+later.encode(), DAY)

    def test_requires_date(self):
        with self.assertRaisesRegex(Problem,"day"):
            parse_csv(SMALL)

    def test_validates_explicit_date_column_even_when_fallback_given(self):
        raw = "日期,计划,消耗,曝光,点击\n,A,100,1000,20\n".encode()
        with self.assertRaisesRegex(Problem,"日期"):
            parse_csv(raw,DAY)

    def test_custom_mapping(self):
        raw = "计划,广告费,曝光,点击\nA,100,1000,20\n".encode()
        rows, _ = parse_csv(raw, DAY, {"spend":"广告费"})
        self.assertEqual(rows[0][2],10000)


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(database,"DB_PATH",Path(cls.temp.name)/"test.sqlite3")
        cls.db_patch.start()
        cls.env = patch.dict(os.environ,{"LLM_PRIMARY_KEY":"","LLM_BACKUP_KEY":"","APP_ORIGIN":"","REPORT_WEBHOOKS":"{}"})
        cls.env.start()
        initialize()
        seed()
        cls.server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.cookies = {}
        for role in ("manager","operator","client","other"):
            status, _, headers = cls.request("/api/login",{"username":"demo-"+role,"password":PASSWORD}, role=None)
            assert status == 200
            cls.cookies[role] = headers["Set-Cookie"].split(";")[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.env.stop()
        cls.db_patch.stop()
        cls.temp.cleanup()

    @classmethod
    def request(cls, path, body=None, role="manager", extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1",cls.server.server_port,timeout=4)
        headers = {"Cookie":cls.cookies[role]} if role else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        try:
            connection.request("GET" if body is None else "POST",path,
                               json.dumps(body).encode() if body is not None else None,headers)
            response = connection.getresponse()
            raw = response.read()
            result = json.loads(raw) if response.headers["Content-Type"].startswith("application/json") else raw
            return response.status,result,response.headers
        finally:
            connection.close()

    def api(self, resource, body=None, role="manager", expected=200):
        status, result, _ = self.request("/api/"+resource,body,role)
        self.assertEqual(status,expected,result)
        return result

    def test_static_files_and_health(self):
        self.assertEqual(self.request("/",role=None)[0],200)
        self.assertEqual(self.request("/app.js",role=None)[0],200)
        self.assertEqual(self.request("/health",role=None)[1],{"status":"ok"})

    def test_login_required_and_cross_origin_blocked(self):
        self.assertEqual(self.request("/api/me",role=None)[0],401)
        self.assertEqual(self.request("/api/logout", {}, extra_headers={"Origin":"https://other.invalid"})[0],403)
        self.assertEqual(self.request("/api/me",role="manager")[0],200)

    def test_permissions(self):
        user = self.api("me",role="operator")
        self.assertEqual([a["id"] for a in user["accounts"]],["account-a"])
        for role, account in [("manager","account-b"),("client","account-b"),("other","account-a")]:
            for name in ("reports","jobs/list"):
                self.api(name,{"account":account,"start":DAY,"end":DAY},role,403)
        self.api("import",{"account":"account-a"},"client",403)
        self.api("management",role="operator",expected=403)
        self.api("documents",{"account":"account-a"},"operator",403)
        self.api("knowledge",{"account":"account-a","question":"素材"},"client",403)

    def test_dashboard_ratios(self):
        date = (DEMO_DATE-timedelta(days=1)).isoformat()
        result = self.api("reports",{"account":"account-a","start":date,"end":date})
        self.assertEqual(result["current"]["rows"],3)
        self.assertAlmostEqual(result["current"]["ctr"],result["current"]["clicks"]/result["current"]["impressions"],places=6)
        self.assertTrue(result["markdown"].startswith("# 投放报表"))
        self.assertEqual(len(result["plans"]),3)

    def test_duplicate_import(self):
        result = self.api("import",{"account":"account-a","source":"聚光",
                                    "file":base64.b64encode(sample_csv()).decode()})
        self.assertTrue(result["unchanged"])

    def test_import_conflict_replace_and_preserve_version(self):
        date = "2026-10-01"
        data = {"account":"account-a","source":"聚光","day":date,"file":base64.b64encode(SMALL).decode()}
        one = self.api("import",data)
        self.assertEqual(one["rows"],2)
        changed = {**data,"file":base64.b64encode(SMALL.replace(b",100,",b",150,")).decode()}
        self.api("import",changed,expected=409)
        two = self.api("import",{**changed,"replace":True})
        self.assertNotEqual(one["import_id"],two["import_id"])
        result = self.api("reports",{"account":"account-a","start":date,"end":date})
        self.assertEqual(result["current"]["spend"],200)
        history = self.api("history",{"account":"account-a"})
        self.assertIn(one["import_id"],[row["id"] for row in history["imports"]])

    def test_bad_inputs(self):
        self.api("reports",{"account":"account-a","start":"0001-01-01","end":DAY},expected=422)
        self.api("import",{"account":"account-a","source":"聚光","file":"!!!"},expected=422)
        self.api("ask",{"account":"account-a","start":DAY,"end":DAY,"question":"无关问题"},expected=422)

    def test_ask_safe_ranking(self):
        result = self.api("ask",{"account":"account-a","start":DAY,"end":DAY,"question":"CPL 最低的计划"})
        self.assertEqual(result["metric"],"cpl")
        values = [item["value"] for item in result["data"]]
        self.assertEqual(values,sorted(values))

    def test_knowledge_scoped_to_tenant(self):
        self.api("documents",{"account":"account-a","title":"海棠特有术语","source":"合成来源","body":"海棠特有术语"})
        result = self.api("knowledge",{"account":"account-a","question":"海棠特有术语"})
        self.assertEqual(result["evidence"][0]["title"],"海棠特有术语")
        result = self.api("knowledge",{"account":"account-b","question":"海棠特有术语"},"other")
        self.assertEqual(result["evidence"],[])

    def test_zero_division_and_nullable_metrics(self):
        raw = "计划,消耗,曝光,点击,线索数\n只有消耗,50,0,0,\n".encode()
        day = "2026-10-03"
        self.api("import",{"account":"account-a","source":"聚光","day":day,"file":base64.b64encode(raw).decode()})
        result = self.api("reports",{"account":"account-a","start":day,"end":day})["current"]
        self.assertEqual(result["spend"],50)
        self.assertTrue(all(result[k] is None for k in ("ctr","cpc","cpl")))

    def test_bad_upload_preserves_active_version(self):
        body = {"account":"account-a","start":DAY,"end":DAY}
        before = self.api("reports",body)["current"]
        source = b"plan,spend,impressions,clicks\nTest,not-a-number,8,1\n"
        self.api("import",{"account":"account-a","source":"通用","day":DAY,"replace":True,
                           "file":base64.b64encode(source).decode()},expected=422)
        self.assertEqual(before,self.api("reports",body)["current"])

    def test_tenant_quota(self):
        with connect() as db:
            db.execute("UPDATE tenants SET daily_quota=0 WHERE id='demo-b'")
        try:
            self.api("jobs",{"account":"account-b","day":DAY},"other",429)
        finally:
            with connect() as db:
                db.execute("UPDATE tenants SET daily_quota=30 WHERE id='demo-b'")

    def test_publish_only_selected_and_resume(self):
        data = {"account":"account-a","day":DAY}
        job = self.api("jobs",data,"operator")["id"]
        path = f"jobs/{job}"
        self.api(path,role="client",expected=403)
        self.api(path,role="other",expected=403)
        self.assertTrue(run_once("worker-one"))
        self.assertEqual(self.api(path)["step"],1)
        self.assertTrue(run_once("worker-after-restart"))
        self.assertTrue(run_once("worker-after-restart"))
        result = self.api(path)
        self.assertEqual(result["state"],"review")
        self.assertEqual(result["work"]["stage"],"点击")
        self.assertEqual(len(result["work"]["suggestions"]),3)
        self.api(path+"/review",{"accept":[0]},"client",403)
        self.api(path+"/review",{"accept":[3]},expected=422)
        result = self.api(path+"/review",{"accept":[1]})
        self.assertEqual(len(result["work"]["suggestions"]),1)
        client = self.api(path,role="client")
        self.assertEqual(client,result)
        self.assertTrue(all(item["excerpt"] == "" for item in client["work"]["evidence"]))
        self.assertEqual(self.api("jobs",data,"operator")["id"],job)

    def test_audit_and_offline_model(self):
        result = self.api("insight",{"account":"account-a","start":DAY,"end":DAY})
        self.assertFalse(result["model_used"])
        self.assertTrue(result["text"])
        result = self.api("management")
        self.assertTrue(all(r["account_id"] == "account-a" for r in result["events"]))
        self.assertEqual(result["usage"],[])
        self.api("push",{"account":"account-a","start":DAY,"end":DAY},expected=409)


if __name__ == "__main__":
    unittest.main()
