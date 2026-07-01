"""Spider 基准评估 —— Text-to-SQL 准确率评估工具。"""

import json
import sqlite3
import os
import sys
import importlib
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime


class TestCase:
    def __init__(self, id, db_id, question, gold_sql, difficulty):
        self.id = id
        self.db_id = db_id
        self.question = question
        self.gold_sql = gold_sql
        self.difficulty = difficulty

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["db_id"], d["question"], d["gold_sql"], d.get("difficulty", "unknown"))


class EvalResult:
    def __init__(self, test_case, generated_sql, exact_match, execution_match=None, error=None):
        self.test_case = test_case
        self.generated_sql = generated_sql
        self.exact_match = exact_match
        self.execution_match = execution_match
        self.error = error


def normalize_sql(sql: str) -> str:
    import re
    sql = sql.lower().strip()
    sql = re.sub(r";\s*$", "", sql)
    sql = re.sub(r"\s+", " ", sql)
    sql = sql.replace('"', "'")
    return sql.strip()


def extract_result_set(db_path: str, sql: str):
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA query_only = ON;")
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        return set(rows)
    except Exception:
        return None


def load_dataset(path: str):
    with open(path) as f:
        data = json.load(f)
    if "test_cases" in data:
        return [TestCase.from_dict(tc) for tc in data["test_cases"]]
    return [TestCase.from_dict(tc) for tc in data]


def run_evaluation(
    generator_fn: Callable,
    dataset_path: str = "datasets/spider_eval_100.json",
    db_dir: str = "",
    use_execution: bool = False,
):
    dataset = load_dataset(dataset_path)
    total = len(dataset)
    results = []

    print(f"\n{'='*60}")
    print(f"  DataPilot Evaluation - {total} test cases")
    print(f"{'='*60}\n")

    for i, tc in enumerate(dataset):
        print(f"  [{i+1}/{total}] {tc.id}  ", end="", flush=True)
        try:
            generated = generator_fn(tc.question, tc.db_id)
        except Exception as e:
            results.append(EvalResult(tc, "", False, None, f"Generator error: {e}"))
            print(f"  ERROR: {e}")
            continue

        norm_gen = normalize_sql(generated)
        norm_gold = normalize_sql(tc.gold_sql)
        em_pass = norm_gen == norm_gold

        ex_pass = None
        if use_execution and db_dir:
            db_path = os.path.join(db_dir, tc.db_id, f"{tc.db_id}.sqlite")
            if os.path.exists(db_path):
                gold_res = extract_result_set(db_path, tc.gold_sql)
                gen_res = extract_result_set(db_path, generated)
                ex_pass = (gold_res is not None and gen_res is not None and gold_res == gen_res)

        results.append(EvalResult(tc, generated, em_pass, ex_pass))
        icon = "PASS" if em_pass else "FAIL"
        print(f"  EM={icon}", end="")
        if ex_pass is not None:
            print(f" EX={'PASS' if ex_pass else 'FAIL'}", end="")
        print()

    em_pass = sum(1 for r in results if r.exact_match)
    ex_pass = sum(1 for r in results if r.execution_match is True)
    ex_total = sum(1 for r in results if r.execution_match is not None)

    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"\n  Overall:")
    print(f"    Exact Match (EM):  {em_pass}/{total} = {em_pass/total*100:.1f}%")
    if ex_total > 0:
        print(f"    Execution (EX):    {ex_pass}/{ex_total} = {ex_pass/ex_total*100:.1f}%")

    for diff in ["simple", "moderate", "challenging"]:
        dr = [r for r in results if r.test_case.difficulty == diff]
        if not dr:
            continue
        dem = sum(1 for r in dr if r.exact_match)
        dex = sum(1 for r in dr if r.execution_match is True)
        dex_t = sum(1 for r in dr if r.execution_match is not None)
        print(f"\n  [{diff.capitalize()}] ({len(dr)} cases)")
        print(f"    EM: {dem}/{len(dr)} = {dem/len(dr)*100:.1f}%")
        if dex_t > 0:
            print(f"    EX: {dex}/{dex_t} = {dex/dex_t*100:.1f}%")
    print()

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DataPilot Evaluation Runner")
    parser.add_argument("--dataset", default="datasets/spider_eval_100.json")
    parser.add_argument("--execution", action="store_true")
    parser.add_argument("--db-dir", default="")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(__file__))
    from generator import generate_sql
    run_evaluation(generate_sql, args.dataset, args.db_dir, args.execution)
