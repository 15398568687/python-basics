"""Read a single export locally with no server or API key."""
import argparse
from collections import defaultdict
from pathlib import Path

from app.db import Problem
from app.reports import display, parse_csv, ratio


def main():
    parser = argparse.ArgumentParser(description="读取聚光/乘风计划 CSV，输出汇总和 Top 10")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--day", help="CSV 无日期列时填写 YYYY-MM-DD")
    args = parser.parse_args()
    try:
        rows, _ = parse_csv(args.csv.read_bytes(), args.day)
    except (OSError, Problem) as exc:
        parser.exit(1, str(exc) + "\n")
    plans = defaultdict(int)
    spend = impressions = clicks = leads = 0
    complete = True
    for _, plan, cost, views, taps, contacts, *_ in rows:
        plans[plan] += cost
        spend += cost
        impressions += views
        clicks += taps
        leads += contacts or 0
        complete &= contacts is not None
    print("总消耗", display("spend", spend / 100))
    print("CTR", display("ctr", ratio(clicks, impressions)))
    print("CPC", display("cpc", ratio(spend / 100, clicks)))
    print("CPL", display("cpl", ratio(spend / 100, leads) if complete else None))
    print("不同计划数", len(plans))
    for plan, amount in sorted(plans.items(), key=lambda item: (-item[1],item[0]))[:10]:
        print(plan, display("spend", amount / 100))


if __name__ == "__main__":
    main()
