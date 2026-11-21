"""Measure authenticated report requests against a temporary synthetic database."""
import concurrent.futures
import http.client
import json
import math
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from app import db
from app.demo import DEMO_DATE, PASSWORD, seed
from app.server import ConsoleServer, Handler

DAY = DEMO_DATE.isoformat()


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


def request(port, path, body, cookie=None):
    connection = http.client.HTTPConnection("127.0.0.1",port,timeout=10)
    headers = {"Content-Type":"application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        connection.request("POST",path,json.dumps(body).encode(),headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, response.getheader("Set-Cookie"), json.loads(data)
    finally:
        connection.close()


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)]


def measure(port, cookie, parallel, count):
    def sample(_):
        start = time.perf_counter()
        try:
            status, _, result = request(port,"/api/reports",{"account":"account-a","start":DAY,"end":DAY},cookie)
            success = status == 200 and result["current"]["rows"] == 3
        except (OSError, ValueError):
            success = False
        return (time.perf_counter()-start)*1000, success
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        results = list(pool.map(sample,range(count)))
    elapsed = time.perf_counter()-start
    good = [duration for duration, success in results if success]
    if not good:
        return [parallel,count,0,None,None,None,1]
    return [parallel,count,round(count/elapsed,1),*[round(percentile(good,p),2) for p in (.5,.95,.99)],
            round((count-len(good))/count,4)]


def main():
    with tempfile.TemporaryDirectory() as work, patch.object(db,"DB_PATH",Path(work)/"benchmark.sqlite3"):
        db.initialize()
        seed()
        with ConsoleServer(("127.0.0.1",0),QuietHandler) as server:
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            port = server.server_port
            try:
                status,cookie,_ = request(port,"/api/login",{"username":"demo-manager","password":PASSWORD})
                assert status == 200
                cookie = cookie.split(";")[0]
                measure(port,cookie,1,10)
                print("concurrency,requests,qps,p50_ms,p95_ms,p99_ms,error_rate")
                for parallel in (10,50,100):
                    print(",".join(str(value) for value in measure(port,cookie,parallel,parallel*3)))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
