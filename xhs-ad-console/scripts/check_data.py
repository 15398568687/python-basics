"""Verify local CSV compatibility without printing file contents."""
import argparse
from pathlib import Path

from app.reports import parse_csv

parser = argparse.ArgumentParser()
parser.add_argument("day")
parser.add_argument("files", nargs="+", type=Path)
args = parser.parse_args()
for i, path in enumerate(args.files, 1):
    rows, missing = parse_csv(path.read_bytes(), args.day)
    print(f"Sample {i}, valid rows {len(rows)}, unmapped optional fields {len(missing)}")
