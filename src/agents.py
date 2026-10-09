"""Baseline Olist data-analysis agent: Claude + 2 tools (get_schema, run_sql).

Requires:  pip install anthropic duckdb
Set your key first:  export ANTHROPIC_API_KEY=...
Usage:  python src/agent.py "Berapa total revenue 2017?"
"""
import os
import re
import sys
from pathlib import Path

import anthropic

from tools import TOOLS, connect, execute_tool

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("OLIST_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 10


def load_schema_doc() -> str:
    for path in (ROOT / "docs" / "schema.md", ROOT / "schema.md"):
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("schema.md not found in docs/ or the project root")


SYSTEM_PROMPT = f"""Kamu adalah analis data untuk dataset marketplace Olist (Brasil). \
Jawab pertanyaan pengguna dengan menjalankan SQL pada view yang tersedia.

Aturan:
- Ikuti semua definisi metrik dan aturan di dokumen skema di bawah ini.
- Jangan menjawab dari ingatan. Setiap angka harus berasal dari hasil query.
- Jika SQL error, baca pesannya, perbaiki, dan coba lagi.
- Jika pertanyaan tidak bisa dijawab dari data, atau periodenya di luar data yang lengkap, katakan dengan jelas.
- Jawab dalam bahasa yang dipakai pengguna. Sebutkan definisi dan filter yang dipakai, serta SQL utamanya.

Format penutup (wajib): setelah penjelasan, tulis SATU baris terakhir:
JAWABAN_AKHIR: <nilai>
- Untuk satu angka: titik sebagai desimal, tanpa pemisah ribuan, tanpa simbol mata uang atau persen, \
dengan presisi penuh dari hasil query (contoh: 5962902.01, bukan 5,96 juta). Persentase ditulis sebagai angka (6.79).
- Untuk teks tunggal: nilainya saja (contoh: SP).
- Untuk daftar: pisahkan dengan tanda | (contoh: a|b|c).
- Jika pertanyaan tidak bisa dijawab dengan satu nilai (misalnya kamu menolak atau meminta klarifikasi): JAWABAN_AKHIR: -

=== DOKUMEN SKEMA ===
{load_schema_doc()}
"""


def extract_final(text: str):
    found = re.findall(r"JAWABAN_AKHIR:\s*(.+)", text)
    return found[-1].strip() if found else None


def ask(question: str, client=None, model: str = MODEL, max_steps: int = MAX_STEPS) -> dict:
    client = client or anthropic.Anthropic()
    con = connect()
    messages = [{"role": "user", "content": question}]
    queries = []
    try:
        for _ in range(max_steps):
            resp = client.messages.create(
                model=model, max_tokens=2000, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text")
                return {"answer": text, "final": extract_final(text), "queries": queries, "steps": len(queries)}

            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                output, is_error = execute_tool(con, block.name, block.input)
                if block.name == "run_sql":
                    queries.append({"sql": block.input.get("sql", ""), "error": is_error})
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
                )
            messages.append({"role": "user", "content": results})

        return {"answer": "(berhenti: batas langkah tercapai)", "final": None, "queries": queries, "steps": len(queries)}
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python src/agent.py "your question"')
    result = ask(" ".join(sys.argv[1:]))
    print(result["answer"])
    errors = sum(q["error"] for q in result["queries"])
    print(f"\n[{len(result['queries'])} query dijalankan, {errors} error]")
