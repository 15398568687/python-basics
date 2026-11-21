import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from app.db import Problem, account_scope, audit, connect, initialize, password_hash, token_hash
from app.intelligence import ask, enqueue, insight, outbound, run_once, search
from app.reports import day_value, decode_upload, import_report, summary

WEB = Path(__file__).resolve().parent.parent / "web"
LOG = logging.getLogger(__name__)
MAX_BODY = 6 * 1024 * 1024
LOGIN_LOCK = threading.Lock()
LOGIN_TRIES = {}
DUMMY_HASH = password_hash("dummy")


def login(data, address):
    name = field(data, "username", 100)
    password = field(data, "password", 256)
    now = int(time.time())
    with LOGIN_LOCK:
        for key in list(LOGIN_TRIES):
            if LOGIN_TRIES[key][0] < now - 60:
                LOGIN_TRIES.pop(key, None)
        started, count = LOGIN_TRIES.get(address, (now, 0))
        if count >= 12:
            raise Problem(429, "登录尝试过多，请一分钟后再试")
        LOGIN_TRIES[address] = started, count + 1
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        stored = user["password"] if user else DUMMY_HASH
        matched = hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)
        if not matched or user is None:
            raise Problem(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), user["id"], now+86400))
    return token


def field(data, key, limit=200):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Problem(422, f"字段 {key} 必须填写且长度不超过 {limit}")
    return value.strip()


def dates(data):
    first = day_value(data.get("start"))
    last = day_value(data.get("end"))
    if not 0 <= (last-first).days <= 92 or first.year < 2000:
        raise Problem(422, "请选择 2000 年后的 1–93 天日期范围")
    return first.isoformat(), last.isoformat()


def published(job):
    work = json.loads(job["work"])
    work["evidence"] = [{**item, "excerpt": ""} for item in work["evidence"]]
    return {"id": job["id"], "account": job["account_id"], "day": job["day"], "state": job["state"],
            "work": {**work, "suggestions": json.loads(job["accepted"])}}


class Handler(BaseHTTPRequestHandler):
    server_version = "AdConsole"

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def send(self, body, status=200, mime="application/json; charset=utf-8", cookie=None):
        if mime.startswith("application/json"):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Problem(400, "请求长度无效") from None
        if not 0 < size <= MAX_BODY:
            raise Problem(413, "请求为空或超过大小限制")
        if self.headers.get_content_type() != "application/json":
            raise Problem(415, "请发送 application/json")
        origin = self.headers.get("Origin")
        if origin:
            expected = os.getenv("APP_ORIGIN") or f"http://{self.headers.get('Host')}"
            if origin != expected:
                raise Problem(403, "请求来源不匹配")
        try:
            data = json.loads(self.rfile.read(size))
        except (ValueError, RecursionError):
            raise Problem(400, "JSON 无效") from None
        if not isinstance(data, dict):
            raise Problem(422, "请求须为 JSON 对象")
        return data

    def session(self, db):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except ValueError:
            raise Problem(401, "请登录") from None
        item = cookies.get("ad_session")
        hashed = token_hash(item.value) if item else ""
        user = db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                             WHERE token_hash=? AND expires>?""", (hashed,int(time.time()))).fetchone()
        if user is None:
            raise Problem(401, "请登录")
        return user, hashed

    @staticmethod
    def cookie(token, age):
        secure = "; Secure" if os.getenv("APP_ORIGIN", "").startswith("https://") else ""
        return f"ad_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"

    def do_GET(self):
        self.handle_route("GET")

    def do_POST(self):
        self.handle_route("POST")

    def handle_route(self, method):
        path = urlsplit(self.path).path
        try:
            if method == "GET" and path in {"/", "/app.js", "/style.css"}:
                files = {"/": ("index.html","text/html"), "/app.js": ("app.js","application/javascript"),
                         "/style.css": ("style.css","text/css")}
                filename, kind = files[path]
                self.send((WEB / filename).read_bytes(), mime=kind+"; charset=utf-8")
                return
            if method == "GET" and path == "/health":
                with connect() as db:
                    db.execute("SELECT 1 FROM tenants").fetchone()
                self.send({"status":"ok"})
                return
            data = self.payload() if method == "POST" else {}
            if method == "POST" and path == "/api/login":
                token = login(data, self.client_address[0])
                self.send({"ok": True}, cookie=self.cookie(token, 86400))
                return
            with connect() as db:
                user, hashed = self.session(db)
                if method == "POST" and path == "/api/logout":
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hashed,))
                    result = {"ok": True}
                    cookie = self.cookie("", 0)
                else:
                    result = self.api(db, user, method, path, data)
                    cookie = None
            self.send(result, cookie=cookie)
        except Problem as exc:
            self.send({"error": str(exc)}, status=exc.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOG.info("Client disconnected")
        except Exception:
            LOG.exception("Request failed")
            self.send({"error": HTTPStatus.INTERNAL_SERVER_ERROR.phrase}, status=500)

    def api(self, db, user, method, path, data):
        if (method, path) == ("GET","/api/me"):
            accounts = [dict(row) for row in db.execute(
                """SELECT a.id,a.name,MAX(d.day) AS latest,MIN(d.day) AS earliest
                   FROM accounts a LEFT JOIN active_days d ON d.account_id=a.id WHERE a.tenant_id=?
                   AND (?='manager' OR EXISTS(SELECT 1 FROM access WHERE user_id=? AND account_id=a.id))
                   GROUP BY a.id ORDER BY a.name""", (user["tenant_id"],user["role"],user["id"]))]
            return {"username": user["name"], "role": user["role"], "accounts": accounts}
        match = re.fullmatch(r"/api/jobs/([1-9][0-9]{0,17})(/review)?", path)
        if match:
            return self.job(db,user,method,match,data)
        if (method,path) in {("GET","/api/management"),("POST","/api/management")}:
            if user["role"] != "manager":
                raise Problem(403,"此页仅供主管查看")
            usage = [dict(row) for row in db.execute(
                """SELECT account_id,feature,model,SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,ROUND(SUM(cost),6) AS cost FROM usage
                WHERE tenant_id=? AND created>=datetime('now','-30 days') GROUP BY account_id,feature,model""",
                (user["tenant_id"],))]
            events = [dict(row) for row in db.execute(
                """SELECT u.name,a.account_id,a.action,a.detail,a.created FROM audit a
                JOIN users u ON a.actor=u.id WHERE a.tenant_id=? ORDER BY a.id DESC LIMIT 50""",
                (user["tenant_id"],))]
            return {"usage": usage, "events": events}
        if method != "POST" or path not in {
            "/api/reports", "/api/import", "/api/insight", "/api/ask", "/api/knowledge",
            "/api/documents", "/api/jobs", "/api/jobs/list", "/api/history", "/api/push"
        }:
            raise Problem(404,"接口不存在")
        account_id = field(data, "account")
        if user["role"] == "client" and path not in {"/api/reports","/api/jobs/list"}:
            raise Problem(403, "只读账号只能查看授权报表和已发布诊断")
        account = account_scope(db,user,account_id,
                                write=path in {"/api/import","/api/documents","/api/jobs","/api/push","/api/insight"})
        if path == "/api/import":
            if "replace" in data and type(data["replace"]) is not bool:
                raise Problem(422,"replace 须为布尔值")
            return import_report(db,user,account_id,decode_upload(data.get("file")),field(data,"source"),
                                 data.get("day"),data.get("mapping"),data.get("replace",False))
        if path == "/api/history":
            return {"imports":[dict(row) for row in db.execute(
                """SELECT i.id,i.source,i.created,u.name AS actor,
                (SELECT COUNT(*) FROM active_days d WHERE d.import_id=i.id) AS active_days
                FROM imports i JOIN users u ON i.actor=u.id WHERE i.account_id=?
                ORDER BY i.id DESC LIMIT 50""",(account_id,))]}
        if path == "/api/knowledge":
            results = search(db,account["tenant_id"],field(data,"question",300))
            return {"evidence": results, "message": "找到以下有出处的片段" if results else "知识库暂缺足够证据"}
        if path == "/api/documents":
            if user["role"] != "manager":
                raise Problem(403,"只有主管可以录入租户共享知识")
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM documents WHERE tenant_id=?", (user["tenant_id"],)).fetchone()[0]
            if count >= 200:
                raise Problem(409,"知识库已达到 200 篇上限")
            doc = db.execute("INSERT INTO documents(tenant_id,title,source,body) VALUES(?,?,?,?)",
                (user["tenant_id"],field(data,"title"),field(data,"source"),field(data,"body",12000))).lastrowid
            audit(db,user,"document",f"文档 {doc}",account_id)
            return {"id":doc}
        if path == "/api/jobs":
            return {"id":enqueue(db,user,account,field(data,"day",10))}
        if path == "/api/jobs/list":
            status_condition = {"client": "AND j.state='published'", "manager": "", "operator": "AND j.actor=?"}
            params = (account_id, user["id"]) if user["role"] == "operator" else (account_id,)
            sql = "SELECT id,day,state,created FROM jobs j WHERE j.account_id=? "
            return {"jobs":[dict(row) for row in db.execute(
                sql+status_condition[user["role"]]+" ORDER BY id DESC LIMIT 50", params)]}
        start,end = dates(data)
        if path == "/api/ask":
            return ask(db,account_id,start,end,field(data,"question",300),account["tenant_id"])
        report = summary(db,account_id,start,end)
        if path == "/api/insight":
            return insight(report,user["tenant_id"],account_id)
        if path == "/api/push":
            if not report["current"]["rows"]:
                raise Problem(422,"所选日期没有报表数据")
            return self.push(db,user,account_id,report["markdown"])
        return report

    def job(self,db,user,method,match,data):
        job_id = int(match[1])
        if method == "POST":
            db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job:
            raise Problem(404,"诊断任务不存在")
        account_scope(db,user,job["account_id"],write=method=="POST")
        if user["role"] == "operator" and job["actor"] != user["id"]:
            raise Problem(403,"无权查看该任务")
        if method == "GET" and not match[2]:
            if user["role"] == "client":
                if job["state"] != "published":
                    raise Problem(403,"诊断报告尚未发布")
                return published(job)
            if job["state"] == "published":
                return published(job)
            return {k:job[k] for k in ("id","day","state","step","error")} | {"work":json.loads(job["work"])}
        if method != "POST" or not match[2]:
            raise Problem(404,"接口不存在")
        if job["state"] != "review":
            raise Problem(409,"只有待审核的诊断可以发布")
        work = json.loads(job["work"])
        picks = data.get("accept")
        if not isinstance(picks,list) or not picks or any(type(i) is not int or not 0<=i<len(work["suggestions"]) for i in picks):
            raise Problem(422,"请选择要采纳的建议")
        feedback = data.get("feedback", "")
        if not isinstance(feedback,str) or len(feedback) > 500:
            raise Problem(422,"复核备注最多 500 字")
        chosen = [work["suggestions"][i] for i in sorted(set(picks))]
        db.execute("UPDATE jobs SET state='published',accepted=?,feedback=? WHERE id=?",
                   (json.dumps(chosen,ensure_ascii=False),feedback,job_id))
        audit(db,user,"publish",f"任务 {job_id} · 采纳 {len(chosen)} 条",job["account_id"])
        return published(db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone())

    def push(self, db, user, account, markdown):
        hooks = json.loads(os.getenv("REPORT_WEBHOOKS") or "{}")
        url = hooks.get(account)
        if not url:
            raise Problem(409,"请先为此账户配置群机器人")
        target = urlsplit(url)
        if target.scheme != "https" or target.username or target.password or target.port not in {None,443}:
            raise Problem(422,"Webhook 必须是支持的 HTTPS 群机器人地址")
        if target.hostname == "open.feishu.cn" and target.path.startswith("/open-apis/bot/v2/hook/"):
            body = {"msg_type":"interactive","card":{"elements":[{"tag":"markdown","content":markdown}]}}
        elif target.hostname == "qyapi.weixin.qq.com" and target.path == "/cgi-bin/webhook/send":
            body = {"msgtype":"markdown","markdown":{"content":markdown}}
        else:
            raise Problem(422,"仅支持飞书和企业微信机器人的官方地址")
        if len(markdown.encode()) > 4096 and target.hostname == "qyapi.weixin.qq.com":
            raise Problem(422,"企业微信 Markdown 超过 4096 字节，请缩小日报范围")
        request = Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        try:
            with outbound.open(request,timeout=5) as response:
                reply = json.loads(response.read(8192))
        except (OSError,ValueError):
            raise Problem(502,"推送未确认，请先检查群内是否收到再重试") from None
        if not isinstance(reply,dict) or reply.get("code",reply.get("errcode",reply.get("StatusCode"))) != 0:
            raise Problem(502,"机器人拒绝了日报，请核对配置")
        audit(db,user,"push","日报已推送",account)
        return {"ok":True}


def worker(stop):
    lease = secrets.token_hex(16)
    while not stop.is_set():
        try:
            if not run_once(lease):
                stop.wait(.5)
        except Exception:
            LOG.exception("Worker error")
            stop.wait(2)


class ConsoleServer(ThreadingHTTPServer):
    request_queue_size = 128


def serve(host="127.0.0.1",port=8080):
    logging.basicConfig(level=logging.INFO)
    initialize()
    stop = threading.Event()
    with ConsoleServer((host, port), Handler) as server:
        thread = threading.Thread(target=worker,args=(stop,),daemon=True)
        thread.start()
        print(f"Ad Console listening on http://{host}:{port}",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
