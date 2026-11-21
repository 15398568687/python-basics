import base64
import binascii
import csv
import hashlib
import io
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.db import Problem, audit

ALIASES = {
    "day": ["日期", "时间", "统计日期", "数据日期", "day", "date"],
    "plan": ["计划", "计划名称", "推广计划", "推广计划名称", "campaign", "plan"],
    "spend": ["消耗", "消费", "花费", "消耗(元)", "消耗（元）", "spend"],
    "impressions": ["曝光", "曝光量", "展现量", "展示量", "impressions"],
    "clicks": ["点击", "点击量", "点击次数", "clicks"],
    "leads": ["线索数", "线索量", "线索", "直播间留资量", "leads"],
    "interactions": ["互动", "互动量", "互动数", "interactions"],
    "orders": ["成交量", "成交订单数", "支付订单数", "orders"],
    "revenue": ["成交金额", "支付金额", "成交金额(元)", "GMV", "revenue"],
}
MAX_CSV = 4 * 1024 * 1024
MAX_ROWS = 50_000
METRICS = {
    "spend": "消耗", "impressions": "曝光", "clicks": "点击", "leads": "线索",
    "ctr": "CTR", "cpc": "CPC", "cpl": "CPL", "cpm": "CPM",
    "conversion": "点击转线索率", "interactions": "互动", "orders": "成交",
    "revenue": "成交金额", "roas": "ROAS",
}


def day_value(value):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        day = date.fromisoformat(value)
        if not 2000 <= day.year <= 2100 or day.isoformat() != value:
            raise ValueError()
        return day
    except ValueError:
        raise Problem(422, "日期请使用 YYYY-MM-DD") from None


def numeric(raw, field, optional=False):
    raw = raw.strip().replace(",", "").replace("，", "")
    if optional and raw in {"", "-", "—"}:
        return None
    try:
        n = Decimal(raw)
        if not n.is_finite() or n < 0 or n > 10**10:
            raise ValueError()
        scaled = n * 100 if field in {"spend", "revenue"} else n
        if scaled != scaled.to_integral_value():
            raise ValueError()
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} 数值无效") from None


def parse_csv(raw, fallback=None, mapping=None):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CSV:
        raise Problem(422, "CSV 文件须在 4 MiB 以内且包含数据")
    fallback = day_value(fallback).isoformat() if fallback else None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise Problem(422, "CSV 编码须为 UTF-8 或 GB18030")
    if "\x00" in decoded:
        raise Problem(422, "CSV 包含无效字符")
    reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
    try:
        heads = [h.strip() for h in (reader.fieldnames or [])]
        reader.fieldnames = heads
        if not heads or len(set(heads)) != len(heads):
            raise Problem(422, "CSV 缺少表头或存在重复表头")
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict) or any(
            k not in ALIASES or not isinstance(v, str) or v not in heads for k, v in mapping.items()
        ):
            raise Problem(422, "列映射须由标准字段映射到 CSV 中的列名")
        columns = {key: mapping.get(key) or next((c for c in aliases if c in heads), None)
                   for key, aliases in ALIASES.items()}
        mapped = [c for c in columns.values() if c]
        if len(mapped) != len(set(mapped)):
            raise Problem(422, "同一 CSV 列不能映射到多个指标")
        required = ["plan", "spend", "impressions", "clicks"]
        if not fallback:
            required.append("day")
        missing = [k for k in required if not columns[k]]
        if missing:
            raise Problem(422, "缺少必需列：" + "、".join(missing))
        lines, warnings, seen = [], [], set()
        invalid = 0
        for row in reader:
            if reader.line_num > MAX_ROWS + 1:
                raise Problem(422, "最多导入 50,000 行")
            def value(key):
                return (row.get(columns[key]) or "").strip() if columns[key] else ""
            plan = value("plan")
            if plan in {"合计", "总计", "汇总", "Total", "total"}:
                continue
            try:
                if None in row or any(v is None for v in row.values()) or not plan or len(plan) > 200:
                    raise ValueError("列数或计划名称无效")
                day = day_value(value("day") if columns["day"] else fallback).isoformat()
                key = (day, plan)
                if key in seen:
                    raise ValueError("日期和计划重复，请导出按天和计划聚合的报表")
                amounts = tuple(numeric(value(k), k, k not in required) for k in
                                ("spend", "impressions", "clicks", "leads", "interactions", "orders", "revenue"))
                seen.add(key)
                lines.append((day, plan, *amounts))
            except (Problem, ValueError) as exc:
                invalid += 1
                if len(warnings) < 10:
                    warnings.append(f"第 {reader.line_num} 行 {exc}")
        if invalid:
            raise Problem(422, f"{invalid} 行有误，本次未写入。{' / '.join(warnings)}")
        if not lines:
            raise Problem(422, "没有可导入的明细行")
        return lines, [k for k, v in columns.items() if v is None and k not in required]
    except csv.Error as exc:
        raise Problem(422, f"CSV 格式错误：{exc}") from None


def decode_upload(value):
    if not isinstance(value, str) or len(value) > MAX_CSV * 4 // 3 + 8:
        raise Problem(422, "文件超过 4 MiB 或编码无效")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise Problem(422, "文件编码无效") from None


def import_report(db, user, account_id, raw, source, fallback=None, mapping=None, replace=False):
    if source not in {"聚光", "乘风", "通用"}:
        raise Problem(422, "报表来源请选择聚光、乘风或通用")
    rows, missing = parse_csv(raw, fallback, mapping)
    days = sorted({row[0] for row in rows})
    digest = hashlib.sha256(json.dumps([source, rows], ensure_ascii=False).encode()).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    current = dict(db.execute(
        """SELECT a.day, i.digest FROM active_days a JOIN imports i ON a.import_id=i.id
        WHERE a.account_id=? AND a.day BETWEEN ? AND ?""", (account_id, days[0], days[-1])
    ))
    overlap = [d for d in days if d in current]
    if len(overlap) == len(days) and all(current[d] == digest for d in days):
        return {"days": days, "rows": len(rows), "missing": missing, "unchanged": True}
    if overlap and not replace:
        raise Problem(409, "日期已有版本，确认覆盖后重试。覆盖会替换所选日期的整日明细。")
    import_id = db.execute("INSERT INTO imports(account_id,actor,source,digest) VALUES(?,?,?,?)",
                           (account_id, user["id"], source, digest)).lastrowid
    db.executemany("INSERT INTO lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                   [(import_id, *row) for row in rows])
    db.executemany(
        """INSERT INTO active_days VALUES(?,?,?) ON CONFLICT(account_id,day)
        DO UPDATE SET import_id=excluded.import_id""", [(account_id, day, import_id) for day in days]
    )
    audit(db, user, "import", f"版本 {import_id} · {len(rows)} 行", account_id)
    return {"import_id": import_id, "days": days, "rows": len(rows), "missing": missing}


def ratio(numerator, denominator):
    return round(numerator / denominator, 6) if numerator is not None and denominator else None


def totals(db, account, start, end, by=None, ranking="spend", ascending=False):
    group_columns = {None: "", "day": "l.day", "plan": "l.plan"}
    group = group_columns[by]
    select = (group + " AS label,") if group else ""
    group_by = (" GROUP BY " + group) if group else ""
    aggregate = """COUNT(*) AS rows, COUNT(DISTINCT l.day) AS days,
        SUM(spend_cents) AS spend, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
        CASE WHEN COUNT(leads)=COUNT(*) THEN SUM(leads) END AS leads,
        CASE WHEN COUNT(interactions)=COUNT(*) THEN SUM(interactions) END AS interactions,
        CASE WHEN COUNT(orders)=COUNT(*) THEN SUM(orders) END AS orders,
        CASE WHEN COUNT(revenue_cents)=COUNT(*) THEN SUM(revenue_cents) END AS revenue"""
    ranking = {key: key for key in METRICS}[ranking]
    expressions = {"ctr":"1.0*clicks/NULLIF(impressions,0)","cpc":"1.0*spend/NULLIF(clicks,0)",
                   "cpl":"1.0*spend/NULLIF(leads,0)","cpm":"1000.0*spend/NULLIF(impressions,0)",
                   "conversion":"1.0*leads/NULLIF(clicks,0)","roas":"1.0*revenue/NULLIF(spend,0)"}
    # Use the aggregate aliases in an outer query so ratios rank on grouped totals.
    expression = expressions.get(ranking, ranking)
    order = f" ORDER BY ({expression}) IS NULL, {expression} {'ASC' if ascending else 'DESC'}, label LIMIT 100" if by == "plan" else (
        " ORDER BY label LIMIT 100" if by == "day" else "")
    rows = db.execute(
        f"""SELECT * FROM (SELECT {select}{aggregate} FROM active_days a JOIN lines l
        ON l.import_id=a.import_id AND l.day=a.day
        WHERE a.account_id=? AND a.day BETWEEN ? AND ?{group_by}){order}""",
        (account, start, end),
    )
    result = []
    for row in rows:
        item = dict(row)
        for field in ("spend", "revenue"):
            if item[field] is not None:
                item[field] /= 100
        spend, impressions, clicks, leads = (item[k] for k in ("spend", "impressions", "clicks", "leads"))
        item.update(ctr=ratio(clicks, impressions), cpc=ratio(spend, clicks), cpl=ratio(spend, leads),
                    cpm=ratio(spend * 1000 if spend is not None else None, impressions),
                    conversion=ratio(leads, clicks), roas=ratio(item["revenue"], spend))
        result.append(item)
    return result if group else result[0]


def display(key, value):
    if value is None:
        return "—"
    if key in {"ctr", "conversion"}:
        return f"{value:.2%}"
    return f"{value:,.2f}" if key in {"spend", "cpc", "cpl", "cpm", "revenue", "roas"} else str(value)


def summary(db, account, start, end):
    first, last = day_value(start), day_value(end)
    if not 0 <= (last-first).days < 93:
        raise Problem(422, "日期范围须为 1–93 天")
    offset = timedelta(days=(last-first).days + 1)
    plans = totals(db, account, start, end, "plan")[:10]
    current = totals(db, account, start, end)
    previous = totals(db, account, (first-offset).isoformat(), (last-offset).isoformat())
    week_ago = totals(db, account, (first-timedelta(days=7)).isoformat(), (last-timedelta(days=7)).isoformat())
    changes = {}
    for field in METRICS:
        before, after = previous[field], current[field]
        changes[field] = round((after-before)/before, 4) if before and after is not None else None
    result = {"current": current, "previous": previous, "week_ago": week_ago, "changes": changes,
              "plans": plans, "trend": totals(db, account, start, end, "day")}
    lines = [f"# 投放报表 {start} ~ {end}", "", "| 指标 | 本期 | 上期 | 上周对应区间 |",
             "|---|---:|---:|---:|"]
    lines.extend(f"| {title} | {display(k, current[k])} | {display(k, previous[k])} | {display(k, week_ago[k])} |"
                 for k, title in METRICS.items())
    lines.extend(["", "## 计划消耗 Top 10"])
    for rank, plan in enumerate(plans, 1):
        label = " ".join(plan["label"].split()).replace("<", "&lt;").replace(">", "&gt;").replace("@", "＠")
        lines.append(f"{rank}. {label}：{display('spend', plan['spend'])}")
    result["markdown"] = "\n".join(lines)
    return result
