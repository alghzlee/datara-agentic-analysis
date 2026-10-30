"""Olist data-analysis agent for any OpenAI-compatible LLM (free options: Ollama, Gemini, Groq).

Requires:  pip install openai duckdb
Configure in a .env file in the project root (see .env.example), then run:
    python src/agent.py "Berapa total revenue delivered sepanjang 2017?"

Presets (set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL):
  Ollama (local, free):   http://localhost:11434/v1   key: ollama   model: the name from `ollama list`
  Gemini (free tier):     https://generativelanguage.googleapis.com/v1beta/openai/   key: AI Studio key
  Groq (free tier):       https://api.groq.com/openai/v1   key: Groq key
Pick a model that supports tool/function calling. Free tiers have rate limits: the agent retries on HTTP 429,
and LLM_MIN_INTERVAL=<seconds> adds a pause before every call.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from openai import InternalServerError, OpenAI, RateLimitError

from tools import TOOLS, connect, execute_tool

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None):
    """Load KEY=VALUE lines from .env into os.environ. Values in .env WIN over terminal exports,
    so a stale `export` left in an old terminal cannot silently override the file."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_env()

MAX_STEPS = 6
MAX_SERVER_RETRIES = 3
# total API requests made by this process (every attempt counts toward the quota)
REQUESTS = 0
MIN_INTERVAL = float(os.environ.get("LLM_MIN_INTERVAL", "0"))

# tools.py describes tools in one neutral format. Convert to the OpenAI "function" format.
OPENAI_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS
]


def make_client(prefix: str = "LLM", need_model: bool = True):
    """Build (client, model) from <PREFIX>_BASE_URL / _API_KEY / _MODEL, falling back to the LLM_* values."""
    base_url = os.environ.get(f"{prefix}_BASE_URL") or os.environ.get(
        "LLM_BASE_URL", "http://localhost:11434/v1")
    api_key = os.environ.get(f"{prefix}_API_KEY") or os.environ.get(
        "LLM_API_KEY", "ollama")
    model = os.environ.get(f"{prefix}_MODEL") or os.environ.get("LLM_MODEL")
    if not model and need_model:
        raise SystemExit(
            "Set LLM_MODEL (and LLM_BASE_URL / LLM_API_KEY) in .env. See .env.example.")
    return OpenAI(base_url=base_url, api_key=api_key), model


def chat(client, **kwargs):
    """chat.completions.create with a pause and retries on rate limits (429) and busy servers (5xx).

    Failed attempts can still count toward a free-tier daily quota, so 5xx errors are retried at most
    MAX_SERVER_RETRIES times.
    """
    global REQUESTS
    server_errors = 0
    for attempt in range(6):
        REQUESTS += 1
        if MIN_INTERVAL:
            time.sleep(MIN_INTERVAL)
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, InternalServerError) as exc:
            # Heuristic: a per-day quota error will not recover by waiting a few seconds.
            if isinstance(exc, RateLimitError) and "perday" in str(exc).lower().replace(" ", ""):
                raise RuntimeError(
                    "Kuota harian habis. Berhenti dan lanjutkan setelah reset (lihat --resume)."
                ) from exc
            if isinstance(exc, InternalServerError):
                server_errors += 1
                if server_errors >= MAX_SERVER_RETRIES:
                    raise RuntimeError(
                        "Server API sibuk (5xx) berulang. Berhenti agar kuota harian tidak terbuang. "
                        "Coba lagi nanti atau ganti model."
                    ) from exc
            kind = "rate limit" if isinstance(
                exc, RateLimitError) else "server sibuk (5xx)"
            wait = min(60, 5 * 2 ** attempt)
            print(f"  [{kind}, menunggu {wait} detik]", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(
        "API tetap menolak setelah 6 percobaan. Coba lagi nanti atau ganti model.")


def load_schema_doc() -> str:
    for path in (ROOT / "docs" / "schema.md", ROOT / "schema.md"):
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("schema.md not found in docs/ or the project root")


SYSTEM_PROMPT = f"""Kamu adalah analis data untuk dataset marketplace Olist (Brasil). \
Jawab pertanyaan pengguna dengan menjalankan SQL pada view yang tersedia.

Aturan:
- Ikuti semua definisi metrik dan aturan di dokumen skema di bawah ini.
- Untuk pertanyaan yang butuh angka, SELALU panggil run_sql dulu. Jangan menjawab dari ingatan atau menebak.
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


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def extract_final(text: str):
    found = re.findall(r"JAWABAN_AKHIR:\s*(.+)", text)
    return found[-1].strip() if found else None


def assistant_message(msg) -> dict:
    """Rebuild the assistant message to send back. Keeps provider-specific extra_content if present."""
    calls = []
    for tc in msg.tool_calls:
        call = {"id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
        extra = getattr(tc, "extra_content", None)
        if extra:
            call["extra_content"] = extra
        calls.append(call)
    return {"role": "assistant", "content": msg.content or "", "tool_calls": calls}


def ask(question: str, client=None, model: str = None, max_steps: int = MAX_STEPS) -> dict:
    if client is None:
        client, model = make_client("LLM")
    con = connect()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question}]
    queries = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
    try:
        for _ in range(max_steps):
            resp = chat(client, model=model, messages=messages, tools=OPENAI_TOOLS,
                        temperature=0, max_tokens=1500)
            usage["requests"] += 1
            used = getattr(resp, "usage", None)
            if used:
                usage["prompt_tokens"] += getattr(used,
                                                  "prompt_tokens", 0) or 0
                usage["completion_tokens"] += getattr(
                    used, "completion_tokens", 0) or 0
            msg = resp.choices[0].message

            if not msg.tool_calls:
                text = strip_think(msg.content or "")
                return {"answer": text, "final": extract_final(text), "queries": queries, "usage": usage}

            messages.append(assistant_message(msg))
            for tc in msg.tool_calls:
                args = {}
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    output, is_error = execute_tool(
                        con, tc.function.name, args)
                except json.JSONDecodeError:
                    output, is_error = "Argumen bukan JSON yang valid. Kirim ulang dengan format yang benar.", True
                if tc.function.name == "run_sql":
                    queries.append({"sql": args.get("sql", "") if isinstance(
                        args, dict) else "", "error": is_error})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": f"ERROR: {output}" if is_error else output})

        return {"answer": "(berhenti: batas langkah tercapai)", "final": None, "queries": queries, "usage": usage}
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: python src/agent.py "your question"   |   python src/agent.py --models')
    if sys.argv[1] == "--models":
        listing_client, _ = make_client("LLM", need_model=False)
        print(f"Model yang tersedia di {listing_client.base_url}:\n")
        print("\n".join(sorted(m.id for m in listing_client.models.list())))
        raise SystemExit(0)
    client, model = make_client("LLM")
    print(f"[model: {model} @ {client.base_url}]\n")
    result = ask(" ".join(sys.argv[1:]), client=client, model=model)
    print(result["answer"])
    errors = sum(q["error"] for q in result["queries"])
    u = result["usage"]
    print(f"\n[{len(result['queries'])} query dijalankan, {errors} error | "
          f"{u['requests']} request, {u['prompt_tokens']} token masuk, {u['completion_tokens']} token keluar]")
