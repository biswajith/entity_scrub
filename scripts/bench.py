"""Measure detection speed on synthetic data with the real model (SPEC §9).

Usage: uv run python scripts/bench.py [rows]
"""
from __future__ import annotations

import csv
import random
import sys
import tempfile
import time
from pathlib import Path

from faker import Faker

from scrub.detect.gliner_adapter import get_model
from scrub.detect.pipeline import detect_units
from scrub.formats import open_handler
from scrub.models import Options


def make_csv(path: Path, rows: int) -> None:
    f = Faker("en_US")
    f.seed_instance(1)
    random.seed(1)
    people = [f.name() for _ in range(max(50, rows // 20))]
    companies = [f.company() for _ in range(max(20, rows // 50))]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Company", "City", "Email", "Phone", "Notes"])
        for _ in range(rows):
            p, c = random.choice(people), random.choice(companies)
            w.writerow([p, c, f.city(), f.email(), f.phone_number(),
                        f"{p} met {random.choice(people)} from {c} about {f.bs()}."])


def main() -> None:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    t = time.time()
    model = get_model()
    print(f"model load: {time.time() - t:.1f}s on {model.device}")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bench.csv"
        make_csv(p, rows)
        units = open_handler(p).units()
        print(f"{rows} rows, {p.stat().st_size / 1e6:.1f} MB, {len(units)} cells")
        t = time.time()
        det = detect_units(units, Options(), model)
        dt = time.time() - t
        n = sum(len(v) for v in det.values())
        print(f"detection: {dt:.1f}s  ({len(units) / dt:.0f} cells/s, {n} spans)")


if __name__ == "__main__":
    main()
