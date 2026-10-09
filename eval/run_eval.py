"""Run eval/questions.csv through the agent and score the answers.

Scoring:
  - normal questions: deterministic. The agent's JAWABAN_AKHIR line must match expected_answer
    (numbers within tolerance, text case-insensitive).
  - trap and why questions: an LLM judge compares the answer with expected_behavior.
  - if must_include is set, those keywords must also appear in the answer.

Usage (from the project root):
    python eval/run_eval.py --name baseline
    python eval/run_eval.py --name quick --ids N01,N12,T03
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import agent  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS = EVAL_DIR / "questions.csv"
RESULTS_DIR = EVAL_DIR / "results"

JUDGE_PROMPT = """Kamu adalah penguji yang menilai jawaban sebuah agent analisis data.

Pertanyaan: {question}
Perilaku yang diharapkan: {behavior}
{extra}
Jawaban agent:
\"\"\"
{answer}
\"\"\"

Nilai apakah jawaban memenuhi perilaku yang diharapkan. Jawab HANYA dengan JSON satu baris:
{{"pass": true atau false, "reason": "alasan singkat"}}"""


def parse_number(text):
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


def score_deterministic(row, final):
    if final is None or final == "-":
        return False, "tidak ada nilai di JAWABAN_AKHIR"
    if row["answer_type"] == "number":
        got, want = parse_number(final), float(row["expected_answer"])
        if got is None:
            return False, f"bukan angka: {final!r}"
        tol = max(float(row["tolerance"]), 1e-9 * abs(want))
        return abs(got - want) <= tol, f"got {got} | expected {want} | tol {tol}"
    ok = final.strip().lower() == row["expected_answer"].strip().lower()
    return ok, f"got {final!r} | expected {row['expected_answer']!r}"


def missing_keywords(row, answer):
    keywords = [k.strip().lower() for k in row["must_include"].split(";") if k.strip()]
    return [k for k in keywords if k not in answer.lower()]


def judge(client, model, row, answer):
    extra = ""
    if row["answer_type"] == "list":
        extra = ("Kategori kontributor yang benar (urutan tidak wajib persis; lulus jika minimal 2 dari 3 "
                 f"disebut sebagai kontributor terbesar): {row['expected_answer']}\n")
    elif row["expected_answer"]:
        extra = f"Nilai hasil query yang benar (boleh disebut oleh agent): {row['expected_answer']}\n"
    prompt = JUDGE_PROMPT.format(question=row["question"], behavior=row["expected_behavior"],
                                 extra=extra, answer=answer)
    resp = client.messages.create(model=model, max_tokens=300, messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return False, f"judge tidak mengembalikan JSON: {text[:80]!r}"
    verdict = json.loads(match.group())
    return bool(verdict.get("pass")), str(verdict.get("reason", ""))


def run_one(row, client, model, judge_model):
    start = time.time()
    try:
        res = agent.ask(row["question"], client=client, model=model)
        answer = res["answer"]
        if row["type"] == "normal":
            passed, detail = score_deterministic(row, res["final"])
        else:
            passed, detail = judge(client, judge_model, row, answer)
        missing = missing_keywords(row, answer)
        if missing:
            passed, detail = False, f"{detail} | kata kunci wajib tidak ada: {missing}"
        queries = res["queries"]
    except Exception as exc:  # keep the run going, record the failure
        answer, queries, res = "", [], {"final": None}
        passed, detail = False, f"error: {type(exc).__name__}: {exc}"
    return {
        "id": row["id"], "type": row["type"], "pass": passed, "detail": detail,
        "final": res.get("final"), "expected": row["expected_answer"],
        "n_sql": len(queries), "n_sql_errors": sum(q["error"] for q in queries),
        "seconds": round(time.time() - start, 1), "answer": answer, "queries": queries,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="label for this run, e.g. baseline")
    parser.add_argument("--ids", default="", help="comma-separated question ids (default: all)")
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--judge-model", default=agent.MODEL)
    args = parser.parse_args()

    with open(QUESTIONS, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        rows = [r for r in rows if r["id"] in wanted]

    client = anthropic.Anthropic()
    results = []
    for row in rows:
        result = run_one(row, client, args.model, args.judge_model)
        results.append(result)
        print(f"{'PASS' if result['pass'] else 'FAIL'}  {result['id']}  {result['type']:<7} {result['detail'][:110]}")

    RESULTS_DIR.mkdir(exist_ok=True)
    with open(RESULTS_DIR / f"{args.name}.csv", "w", newline="", encoding="utf-8") as fh:
        fields = ["id", "type", "pass", "detail", "final", "expected", "n_sql", "n_sql_errors", "seconds"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    with open(RESULTS_DIR / f"{args.name}.jsonl", "w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    by_type = defaultdict(lambda: [0, 0])
    for result in results:
        by_type[result["type"]][1] += 1
        by_type[result["type"]][0] += result["pass"]
    print(f"\nRun '{args.name}' | model {args.model}")
    for qtype, (ok, total) in by_type.items():
        print(f"  {qtype:<7} {ok}/{total}")
    total_ok = sum(r["pass"] for r in results)
    print(f"  TOTAL   {total_ok}/{len(results)}")
    print(f"Saved to {RESULTS_DIR / (args.name + '.csv')}")


if __name__ == "__main__":
    main()
