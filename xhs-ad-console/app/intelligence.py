import json
import logging
import math
import os
import re
import time
from collections import Counter
from datetime import timedelta
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.db import Problem, account_scope, audit, connect
from app.reports import METRICS, day_value, display, totals

LOG = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


outbound = build_opener(NoRedirect())

PLAYBOOK = {
    "曝光": ["核对预算是否撞线、计划是否暂停和素材审核状态", "检查 CPM、出价与定向近期是否变化", "选一组计划做小额放量对照"],
    "点击": ["按素材拆分 CTR，找下降最明显的素材", "核对素材投放天数和人群覆盖变化", "用同定向测试两版新封面或首帧"],
    "承接": ["复跑落地页或私信链路，确认表单和按钮可用", "对比各素材点击转线索率和流量来源", "对一个计划做承接页或开场话术对照"],
    "转化": ["核对订单归因窗口和订单同步情况", "按计划检查成交和商品库存、价格是否有变化", "选一个商品做同预算对照"],
    "稳定": ["维持当前计划并记录当天表现", "继续观察三天的量级和成本趋势", "复核成交与线索是否按相同口径上报"],
    "数据不足": ["补齐至少三个历史投放日的报表", "检查当前日期是否为完整投放日", "确认线索或成交列的统计口径"],
}


def classify(current, baseline):
    if not current["rows"] or baseline["days"] < 3:
        return "数据不足"
    if baseline["impressions"] and current["impressions"] < .7 * baseline["impressions"] / baseline["days"]:
        return "曝光"
    for metric, label in (("ctr", "点击"), ("conversion", "承接")):
        if baseline[metric] and current[metric] is not None and current[metric] < .8 * baseline[metric]:
            return label
    if (baseline["orders"] and baseline["clicks"] and current["orders"] is not None
            and current["clicks"] and current["orders"] / current["clicks"]
            < .8 * baseline["orders"] / baseline["clicks"]):
        return "转化"
    return "稳定"


def words(value):
    counts = Counter(re.findall(r"[a-z0-9]+", value.lower()))
    for block in re.findall(r"[\u3400-\u9fff]+", value):
        counts.update(block)
        counts.update(block[i:i+2] for i in range(len(block)-1))
    return counts


def chunks(documents, size=400, overlap=60):
    for document in documents:
        body = document["body"]
        for i in range(0, len(body), size-overlap):
            yield {"id": f"{document['id']}.{i+1}", "title": document["title"],
                   "source": document["source"], "excerpt": body[i:i+size]}
            if i+size >= len(body):
                break


def search(db, tenant, query, size=400, rerank=True):
    query_tokens = words(query)
    documents = db.execute("SELECT * FROM documents WHERE tenant_id=? ORDER BY id LIMIT 200", (tenant,))
    candidates = list(chunks(documents, size, min(60, size//4)))
    if not candidates or not query_tokens:
        return []
    features = [words(item["title"] + " " + item["excerpt"]) for item in candidates]
    df = Counter(token for term_set in features for token in query_tokens if token in term_set)
    avglen = sum(sum(t.values()) for t in features) / len(features)
    scores = []
    for item, terms in zip(candidates, features):
        norm = .25 + .75 * sum(terms.values()) / max(avglen, 1)
        rank = sum(math.log(1 + (len(candidates)-df[t]+.5)/(df[t]+.5))
                   * terms[t] * 2.2 / (terms[t] + 1.2*norm)
                   * (2 if len(t) > 1 else .1) for t in query_tokens if terms[t])
        meaningful = query.strip() in item["title"] + " " + item["excerpt"] or any(
            len(t) > 1 and t in terms for t in query_tokens)
        if rank and meaningful:
            rank += (2 if query.strip() in item["title"] else 0) if rerank else 0
            scores.append((rank, item))
    scores.sort(key=lambda p: -p[0])
    results, seen = [], set()
    for _, item in scores:
        document_id = item["id"].split(".")[0]
        if document_id not in seen:
            results.append(item)
            seen.add(document_id)
        if len(results) == 3:
            break
    return results


def model(prompt, tenant, account, feature):
    providers = [p for p in ("PRIMARY", "BACKUP") if os.environ.get(f"LLM_{p}_KEY")]
    if not providers or len(prompt.encode("utf-8")) > 16_000:
        return None
    token_budget = min(120_000, max(0, int(os.getenv("LLM_DAILY_TOKEN_BUDGET", "120000"))))
    output_limit = 600
    reserved = len(prompt.encode("utf-8")) + 256 + output_limit
    for provider in providers:
        key = os.environ[f"LLM_{provider}_KEY"]
        url = os.getenv(f"LLM_{provider}_URL", "https://api.deepseek.com/chat/completions")
        name = os.getenv(f"LLM_{provider}_MODEL", "deepseek-flash")
        if urlsplit(url).scheme != "https":
            continue
        input_rate = float(os.getenv(f"LLM_{provider}_INPUT_RATE", "2"))
        output_rate = float(os.getenv(f"LLM_{provider}_OUTPUT_RATE", "8"))
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute(
                """SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage
                WHERE tenant_id=? AND date(created)=date('now')""", (tenant,)
            ).fetchone()[0]
            if used + reserved > token_budget:
                return None
            usage = db.execute("INSERT INTO usage(tenant_id,account_id,feature,model,input_tokens,output_tokens,cost)"
                               " VALUES(?,?,?,?,?,?,?)", (tenant, account, feature, name, reserved, 0,
                               reserved * max(input_rate, output_rate) / 1_000_000)).lastrowid
        payload = {"model": name, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": output_limit, "temperature": .2,
                   "response_format": {"type": "json_object"}}
        if name.startswith("deepseek"):
            payload["thinking"] = {"type": "disabled"}
        req = Request(url, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with outbound.open(req, timeout=7) as response:
                result = json.loads(response.read(128_000))
            stats = result.get("usage")
            if stats and all(isinstance(stats.get(k), int) and stats[k] >= 0
                             for k in ("prompt_tokens", "completion_tokens")):
                used_in, used_out = stats["prompt_tokens"], stats["completion_tokens"]
                with connect() as db:
                    db.execute("UPDATE usage SET input_tokens=?,output_tokens=?,cost=? WHERE id=?",
                               (used_in, used_out, (used_in*input_rate+used_out*output_rate)/1_000_000, usage))
            return json.loads(result["choices"][0]["message"]["content"])
        except (OSError, ValueError, TypeError, KeyError, IndexError, HTTPError) as exc:
            LOG.info("Model provider %s unavailable: %s", provider, type(exc).__name__)
    return None


def insight(summary, tenant, account):
    allowed = [key for key in ("spend", "impressions", "clicks", "ctr", "cpl", "conversion")
               if summary["changes"][key] is not None]
    ranked = sorted(allowed, key=lambda key: abs(summary["changes"][key]), reverse=True)
    data = model("根据程序已计算的变化选择一到三个最值得关注的字段，仅返回 JSON "
                 '{"metrics":["ctr"]}。有效字段和变化：' +
                 json.dumps({k: summary["changes"][k] for k in allowed}), tenant, account, "insight") if allowed else None
    selected = data.get("metrics") if isinstance(data, dict) else None
    valid = isinstance(selected, list) and bool(selected) and all(k in allowed for k in selected)
    if valid:
        ranked = list(dict.fromkeys(selected))
    items = [f"{METRICS[k]}本期 {display(k, summary['current'][k])}，较上期 "
             f"{summary['changes'][k]:+.1%}。" for k in ranked[:3]]
    return {"text": " ".join(items) or "当前没有足够的对比数据。", "model_used": valid}


def ask(db, account, start, end, question, tenant):
    q = question.lower()
    if not any(s in q for s in ("消耗", "消费", "曝光", "点击", "线索", "成交", "ctr", "cpc", "cpl", "cpm", "roas", "趋势", "计划")):
        raise Problem(422, "可查询消耗、曝光、点击、线索、成交和成本，例如：消耗最高的计划")
    known = [("点击转线索", "conversion"), ("点击率","ctr"), ("cpl","cpl"), ("线索成本","cpl"),
             ("cpc","cpc"), ("点击成本","cpc"), ("cpm","cpm"), ("roas","roas"),
             ("ctr","ctr"), ("曝光","impressions"), ("成交金额","revenue"),
             ("成交","orders"), ("线索","leads"), ("点击","clicks")]
    metric = next((field for word, field in known if word in q), "spend")
    group = "plan" if any(s in q for s in ("计划","最高","最低","排名")) else (
        "day" if any(s in q for s in ("趋势","按天","每日")) else None)
    ascending = "最低" in q
    choice = model('将用户问题映射到只读查询。仅返回 JSON，字段 metric、group、ascending。'
                   'metric 允许 ' + ','.join(METRICS) + '，group 允许 plan、day 或 null。'
                   '用户问题：' + question, tenant, account, "ask")
    if (isinstance(choice, dict) and choice.get("metric") in list(METRICS)
            and choice.get("group") in (None, "day", "plan") and type(choice.get("ascending")) is bool):
        metric, group, ascending = choice["metric"], choice["group"], choice["ascending"]
    rows = totals(db, account, start, end, group, metric, ascending)
    if group is None:
        rows = [{"label": "选定区间", **rows}]
    if group == "plan":
        rows = [row for row in rows if row[metric] is not None]
    result = [{"label": row["label"], "value": row[metric]} for row in rows[:20 if group == "plan" else 93]]
    return {"metric": metric, "name": METRICS[metric], "group": group, "data": result}


def enqueue(db, user, account, day):
    day_value(day)
    db.execute("BEGIN IMMEDIATE")
    existing = db.execute("SELECT id FROM jobs WHERE account_id=? AND actor=? AND day=?",
                          (account["id"], user["id"], day)).fetchone()
    if existing:
        db.execute("""UPDATE jobs SET state='queued',error=NULL,lease=NULL,lease_until=NULL
                      WHERE id=? AND state='failed'""", (existing["id"],))
        return existing["id"]
    count = db.execute("""SELECT COUNT(*) FROM jobs j JOIN accounts a ON j.account_id=a.id
        WHERE a.tenant_id=? AND date(j.created)=date('now')""", (user["tenant_id"],)).fetchone()[0]
    quota = db.execute("SELECT daily_quota FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()[0]
    if count >= quota:
        raise Problem(429, "本租户今日诊断配额已用完")
    job = db.execute("INSERT INTO jobs(account_id,actor,day) VALUES(?,?,?)",
                     (account["id"], user["id"], day)).lastrowid
    audit(db, user, "diagnosis", f"任务 {job}", account["id"])
    return job


def advance(job, lease):
    state = job["state"]
    step = job["step"]
    work = json.loads(job["work"])
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (job["actor"],)).fetchone()
        account = account_scope(db, user, job["account_id"], write=True)
        day = day_value(job["day"])
        if step == 0:
            work["current"] = totals(db, account["id"], job["day"], job["day"])
            work["baseline"] = totals(db, account["id"], (day-timedelta(days=7)).isoformat(),
                                      (day-timedelta(days=1)).isoformat())
            work["stage"] = classify(work["current"], work["baseline"])
        elif step == 1:
            related = {"承接": "落地页 线索 私信", "点击": "点击率 素材", "曝光": "曝光 掉量 预算",
                       "转化": "成交 订单", "稳定": "稳定 日报", "数据不足": "数据 口径 报表"}
            work["evidence"] = search(db, account["tenant_id"], related[work["stage"]])
    if step == 2:
        actions = PLAYBOOK[work["stage"]]
        evidence = work["evidence"]
        proposal = model(
            '按给定规则为建议排序，仅返回 JSON {"order":[0,1,2]}。规则：' +
            json.dumps(actions, ensure_ascii=False) + "。已算指标：" +
            json.dumps({k: work["current"][k] for k in ("ctr","cpl","impressions","conversion")}) +
            "。历史资料标题：" + json.dumps([e["title"] for e in evidence], ensure_ascii=False),
            account["tenant_id"], account["id"], "diagnosis")
        order = proposal.get("order") if isinstance(proposal, dict) else None
        if not (isinstance(order, list) and len(order) == 3 and
                all(type(i) is int for i in order) and sorted(order) == [0,1,2]):
            order = [0,1,2]
        work["suggestions"] = [{"text": actions[i], "references": [e["id"] for e in evidence]} for i in order]
        state = "review"
    with connect() as db:
        db.execute("""UPDATE jobs SET state=?,step=?,work=?,lease=NULL,lease_until=NULL
                      WHERE id=? AND lease=?""",
                   (state, step+1, json.dumps(work, ensure_ascii=False), job["id"], lease))


def run_once(lease):
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT * FROM jobs WHERE state IN ('queued','running')
        AND (lease_until IS NULL OR lease_until<?) ORDER BY id LIMIT 1""", (now,)).fetchone()
        if row is None:
            return False
        db.execute("UPDATE jobs SET state='running',lease=?,lease_until=? WHERE id=?", (lease, now+120, row["id"]))
    job = dict(row)
    job["state"] = "running"
    try:
        advance(job, lease)
    except Exception:
        LOG.exception("Diagnosis job %s failed", job["id"])
        with connect() as db:
            db.execute("""UPDATE jobs SET state='failed',error='诊断执行失败，可重试',
                          lease=NULL,lease_until=NULL WHERE id=? AND lease=?""", (job["id"], lease))
    return True
