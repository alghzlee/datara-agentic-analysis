"""Run eval/questions.csv through the agent and score the answers.

Scoring:
  - normal questions: deterministic. The agent's JAWABAN_AKHIR line must match expected_answer
    (numbers within tolerance, text case-insensitive).
  - trap and why questions: an LLM judge compares the answer with expected_behavior.
    With --no-judge they are NOT judged (pass = blank) and you review them in the .jsonl file.
    Use a stronger model for the judge via JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL.
  - if must_include is set, those keywords must also appear in the answer.

Judge the saved answers later, without calling the agent again (judge model from JUDGE_* in .env):
    python eval/run_eval.py --name baseline --rejudge

Free-tier quota controls:
  --budget N   stop before the process would exceed N API requests (agent calls + judge calls)
  --resume     continue a previous run with the same --name, skipping questions already answered
Results are saved after every question, so an interrupted run loses nothing.

Usage (from the project root):
    python eval/run_eval.py --name baseline --no-judge --budget 60
    python eval/run_eval.py --name baseline --no-judge --budget 60 --resume     # next day
    python eval/run_eval.py --name quick --ids N01,N12,T03
"""
import argparse
import csv
import datetime
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import agent  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS = EVAL_DIR / "questions.csv"
RESULTS_DIR = EVAL_DIR / "results"
WORST_CASE_REQUESTS = agent.MAX_STEPS + 1  # agent steps + one judge call

FIELDS = ["id", "type", "pass", "detail", "final", "expected", "n_sql", "n_sql_errors",
          "requests", "prompt_tokens", "completion_tokens", "seconds", "model", "date", "judge_model"]

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
    keywords = [k.strip().lower()
                for k in row["must_include"].split(";") if k.strip()]
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
    resp = agent.chat(client, model=model, messages=[{"role": "user", "content": prompt}],
                      temperature=0, max_tokens=300)
    text = agent.strip_think(resp.choices[0].message.content or "")
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return False, f"judge tidak mengembalikan JSON: {text[:80]!r}"
    verdict = json.loads(match.group())
    return bool(verdict.get("pass")), str(verdict.get("reason", ""))


def run_one(row, client, model, judge_client, judge_model):
    """judge_client=None means: do not judge trap/why questions (pass stays None)."""
    start = time.time()
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
    try:
        res = agent.ask(row["question"], client=client, model=model)
        answer, queries, usage = res["answer"], res["queries"], res["usage"]
        if row["type"] == "normal":
            passed, detail = score_deterministic(row, res["final"])
        elif judge_client is None:
            passed, detail = None, "perlu ditinjau manual (tanpa judge)"
        else:
            passed, detail = judge(judge_client, judge_model, row, answer)
        missing = missing_keywords(row, answer)
        if missing:
            passed, detail = False, f"{detail} | kata kunci wajib tidak ada: {missing}"
    except Exception as exc:  # keep the run going, record the failure
        answer, queries, res = "", [], {"final": None}
        passed, detail = False, f"error: {type(exc).__name__}: {exc}"
    return {
        "id": row["id"], "type": row["type"], "pass": passed, "detail": detail,
        "final": res.get("final"), "expected": row["expected_answer"],
        "n_sql": len(queries), "n_sql_errors": sum(q["error"] for q in queries),
        "requests": usage["requests"], "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"], "seconds": round(time.time() - start, 1),
        "model": model, "date": datetime.date.today().isoformat(),
        "answer": answer, "queries": queries,
    }


def save(results, name):
    RESULTS_DIR.mkdir(exist_ok=True)
    with open(RESULTS_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    with open(RESULTS_DIR / f"{name}.jsonl", "w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def print_summary(results, name, model):
    print(f"\nRun '{name}' | model {model}")
    for qtype in dict.fromkeys(r["type"] for r in results):
        group = [r for r in results if r["type"] == qtype]
        ok = sum(r["pass"] is True for r in group)
        pending = sum(r["pass"] is None for r in group)
        print(f"  {qtype:<7} {ok}/{len(group)}" +
              (f"  ({pending} menunggu tinjauan manual)" if pending else ""))
    print(
        f"  TOTAL   {sum(r['pass'] is True for r in results)}/{len(results)}")
    print(f"  request: {sum(r['requests'] for r in results)} agent | "
          f"token masuk {sum(r['prompt_tokens'] for r in results):,} | "
          f"token keluar {sum(r['completion_tokens'] for r in results):,}")


def rejudge(name):
    """Judge saved trap/why answers (pass is blank) without running the agent again."""
    with open(QUESTIONS, newline="", encoding="utf-8") as fh:
        questions = {r["id"]: r for r in csv.DictReader(fh)}
    jsonl = RESULTS_DIR / f"{name}.jsonl"
    if not jsonl.exists():
        raise SystemExit(
            f"Belum ada hasil bernama '{name}' (mencari {jsonl}).\n"
            f"Jalankan tahap 1 dulu:  python3 eval/run_eval.py --name {name} --no-judge --budget 40"
        )
    results = [json.loads(line) for line in jsonl.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    pending = [r for r in results if r["pass"] is None]
    if not pending:
        print("Tidak ada jawaban yang menunggu penilaian.")
        return
    judge_client, judge_model = agent.make_client("JUDGE")
    print(f"Judge: {judge_model} @ {judge_client.base_url}")
    print(f"{len(pending)} jawaban akan dinilai ({len(pending)} request).\n")
    for result in pending:
        try:
            passed, detail = judge(
                judge_client, judge_model, questions[result["id"]], result["answer"])
        except Exception as exc:
            print(
                f"BERHENTI di {result['id']}: {type(exc).__name__}: {exc}. Hasil sebelumnya tersimpan; ulangi nanti.")
            break
        result["pass"], result["detail"], result["judge_model"] = passed, detail, judge_model
        print(
            f"{'PASS' if passed else 'FAIL':<6} {result['id']}  {result['type']:<7} {detail[:100]}")
        save(results, name)
    save(results, name)
    print_summary(results, name, results[0].get("model"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True,
                        help="label for this run, e.g. baseline")
    parser.add_argument("--ids", default="",
                        help="comma-separated question ids (default: all)")
    parser.add_argument("--no-judge", action="store_true",
                        help="do not call the LLM judge (saves requests)")
    parser.add_argument("--budget", type=int, default=0,
                        help="max API requests for this process (0 = no limit)")
    parser.add_argument("--resume", action="store_true",
                        help="skip questions already in results/<name>.jsonl")
    parser.add_argument("--rejudge", action="store_true",
                        help="judge saved answers without re-running the agent")
    args = parser.parse_args()
    if args.rejudge:
        return rejudge(args.name)

    with open(QUESTIONS, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        rows = [r for r in rows if r["id"] in wanted]

    client, model = agent.make_client("LLM")
    judge_client, judge_model = (
        None, None) if args.no_judge else agent.make_client("JUDGE")
    print(f"Agent: {model} @ {client.base_url}")
    print(
        f"Judge: {'(dimatikan)' if args.no_judge else f'{judge_model} @ {judge_client.base_url}'}\n")

    results, jsonl = [], RESULTS_DIR / f"{args.name}.jsonl"
    if args.resume and jsonl.exists():
        results = [json.loads(line) for line in jsonl.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        if any(r.get("model") != model for r in results):
            print(
                "PERINGATAN: hasil sebelumnya memakai model berbeda. Skor antar-model tidak boleh digabung.\n")
        print(f"Melanjutkan: {len(results)} soal sudah selesai.\n")
    done = {r["id"] for r in results}

    for row in rows:
        if row["id"] in done:
            continue
        if args.budget and agent.REQUESTS + WORST_CASE_REQUESTS > args.budget:
            print(f"\nBerhenti: sisa budget ({args.budget - agent.REQUESTS} request) lebih kecil dari "
                  f"kebutuhan terburuk satu soal ({WORST_CASE_REQUESTS}). Lanjutkan nanti dengan --resume.")
            break
        result = run_one(row, client, model, judge_client, judge_model)
        results.append(result)
        flag = {True: "PASS", False: "FAIL", None: "TINJAU"}[result["pass"]]
        print(
            f"{flag:<6} {result['id']}  {result['type']:<7} {result['detail'][:100]}")
        save(results, args.name)

    save(results, args.name)
    print_summary(results, args.name, model)
    print(
        f"Saved to {RESULTS_DIR / (args.name + '.csv')}  |  request yang dipakai proses ini: {agent.REQUESTS}")


if __name__ == "__main__":
    main()
