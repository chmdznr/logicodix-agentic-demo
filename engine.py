"""LangChain agent dengan tool SQLite read-only; tanpa tool shell/Python/network."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from urllib.parse import quote as urlquote

# Tidak mengirim trace ke LangSmith meskipun environment mesin pernah mengaktifkannya.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openrouter import ChatOpenRouter

MODEL = "openrouter/free"
BASE_URL = "https://openrouter.ai/api/v1"
MAX_ROWS = 30
SYSTEM = """Kamu membantu menganalisis DATA DEMO FIKTIF laporan fasilitas kampus.
Periode acuan demo November 2026: 'bulan ini'=2026-11, 'bulan lalu'=2026-10.
Pilih sendiri tool yang perlu dipanggil berdasarkan pertanyaan dan hasil sebelumnya.
Temukan schema jika belum diketahui; jangan mengarang nama tabel/kolom, angka, atau data.
query_reports menjalankan SELECT SQLite. CTE, join, agregat dan window function boleh.
Saat diminta lokasi terbanyak beserta masalah dominan, hitung total per lokasi dan kategori
dominan, bukan hanya jumlah kategori tersebut. Jika ada error, perbaiki query seperlunya.
Untuk grafik panggil chart_query dengan query_id yang benar-benar dikembalikan query_reports;
jangan menyalin angka ke tool grafik. Gunakan alias kolom yang unik dan mudah dibaca.
Hasil kosong adalah temuan, jangan mengganti filter diam-diam. Riwayat berisi konteks follow-up.
Maksimal 6 panggilan model per misi; gabungkan analisis ke query bila masuk akal.
Urutan tool tidak ditentukan program. Jawab singkat dalam bahasa Indonesia, sebut data fiktif,
filter periode/status, hasil penting dan query_id sumber. Jangan tampilkan penalaran internal.
Jika misi sudah terjawab, berhenti dengan jawaban akhir tanpa tool call.
"""


def safe_text(value, limit=1800):
    text = str(value)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        text = text.replace(key, "[key disamarkan]")
    text = re.sub(r"sk-or-[A-Za-z0-9_-]+", "[key disamarkan]", text)
    text = re.sub(r"(?i)authorization\s*[:=]\s*[^\n]+", "Authorization: [disamarkan]", text)
    text = re.sub(r"<think\b[^>]*>.*?(?:</think\s*>|$)", "", text, flags=re.S | re.I)
    # Grafik hanya dirender dari chart_query; jangan muat gambar/URL buatan model.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
    return text[:limit]


def seed_database(path: Path):
    """Membuat seed tepercaya; tidak tersedia sebagai tool model."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with sqlite3.connect(path) as conn:
        conn.executescript(Path(__file__).with_name("data.sql").read_text())


class DataSession:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.results = {}
        self.charts = []
        self.turns = deque(maxlen=3)
        self.counter = 0
        self.total_calls = 0
        self.lock = threading.RLock()

    def schema(self):
        with closing(sqlite3.connect(f"file:{urlquote(str(self.path))}?mode=ro", uri=True)) as conn:
            tables = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            period = conn.execute("SELECT MIN(reported_at), MAX(reported_at), COUNT(*) FROM reports").fetchone()
            statuses = [x[0] for x in conn.execute("SELECT DISTINCT status FROM reports ORDER BY status")]
        return {"provenance": "FIKTIF, khusus demo; bukan laporan UNESA",
                "tables": dict(tables), "report_count": period[2],
                "dates": list(period[:2]), "statuses": statuses, "reference_month": "2026-11"}

    def query(self, sql: str):
        if not isinstance(sql, str) or len(sql) > 4000 or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            return {"error": "Hanya satu SELECT/WITH read-only, maksimal 4000 karakter."}
        allowed_functions = {"count", "sum", "avg", "min", "max", "total", "round", "abs",
                             "coalesce", "ifnull", "nullif", "lower", "upper", "length", "substr",
                             "substring", "strftime", "date", "datetime", "julianday", "like",
                             "row_number", "rank", "dense_rank", "trim"}

        def authorize(action, arg1, arg2, db, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and arg1 in {"locations", "reports"} and db == "main":
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in allowed_functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        deadline = time.monotonic() + 0.3
        ticks = 0

        def progress():
            nonlocal ticks
            ticks += 1
            return int(ticks >= 300 or time.monotonic() > deadline)

        try:
            with closing(sqlite3.connect(f"file:{urlquote(str(self.path))}?mode=ro", uri=True, timeout=0.1)) as conn:
                conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
                conn.execute("PRAGMA query_only=ON")
                conn.set_authorizer(authorize)
                conn.set_progress_handler(progress, 1000)
                cursor = conn.execute(sql)
                columns = [x[0] for x in cursor.description or []]
                if not columns or len(columns) > 12 or len(set(columns)) != len(columns):
                    return {"error": "Gunakan 1-12 kolom dengan alias unik."}
                fetched = cursor.fetchmany(MAX_ROWS + 1)
            rows = [dict(zip(columns, [v[:160] if isinstance(v, str) else v for v in row]))
                    for row in fetched[:MAX_ROWS]]
            packet = {"columns": columns, "rows": rows, "truncated": len(fetched) > MAX_ROWS}
            while rows and len(json.dumps(packet)) > 10000:
                rows.pop()
                packet["truncated"] = True
            with self.lock:
                self.counter += 1
                packet["query_id"] = f"q{self.counter}"
                self.results[packet["query_id"]] = {**packet, "sql": sql}
                while len(self.results) > 8:
                    del self.results[next(iter(self.results))]
            return packet
        except sqlite3.Error as exc:
            return {"error": safe_text(exc, 240),
                    "hint": "Cek schema/alias; operasi selain SELECT, fungsi berbahaya dan query terlalu berat ditolak."}

    def chart(self, query_id, x, y, kind="bar"):
        with self.lock:
            result = self.results.get(query_id)
            if result is None:
                return {"error": "query_id tidak ditemukan atau sudah keluar dari riwayat; jalankan query lagi."}
            if kind not in {"bar", "line"} or x == y or x not in result["columns"] or y not in result["columns"]:
                return {"error": "Pilih bar/line, kolom x dan kolom y berbeda dari hasil query."}
            if not result["rows"] or any(
                type(row[y]) not in (int, float) or not math.isfinite(row[y])
                or not isinstance(row[x], (str, int, float)) for row in result["rows"]
            ):
                return {"error": "Grafik butuh baris hasil query, label x dan angka y yang valid."}
            chart = {**result, "x": x, "y": y, "kind": kind}
            self.charts.append(chart)
            self.charts[:] = self.charts[-3:]
            return chart


class CallBudgetExceeded(RuntimeError):
    pass


def run_mission(session: DataSession, mission: str, model=None, on_event=None, call_limit=6):
    """create_agent menentukan loop; UI hanya membaca event tool dan jawaban akhir."""
    mission = mission.strip()
    if not mission or len(mission) > 1600:
        raise ValueError("Misi harus berisi 1-1600 karakter.")
    stats = {"model_calls": 0, "http_calls": 0, "retried": False}
    events, new_messages, models = [], [], []
    session.charts = []
    tool_count = 0
    tool_lock = threading.Lock()

    def emit(event):
        events.append(event)
        if on_event:
            on_event(event)

    def guard_tool():
        nonlocal tool_count
        with tool_lock:
            tool_count += 1
            return tool_count <= 12

    @tool
    def inspect_schema() -> dict:
        """Lihat schema SQLite aktual, rentang tanggal, status dan asal data demo fiktif."""
        return session.schema() if guard_tool() else {"error": "Batas 12 tool tercapai; berhenti."}

    @tool
    def query_reports(sql: str) -> dict:
        """Jalankan satu SELECT SQLite read-only; maksimal 30 baris. Hasil punya query_id untuk grafik.

        Args:
            sql: SELECT/WITH untuk analisis, join, filter, agregat atau window function.
        """
        return session.query(sql) if guard_tool() else {"error": "Batas 12 tool tercapai; berhenti."}

    @tool
    def chart_query(query_id: str, x: str, y: str, kind: str = "bar") -> dict:
        """Buat grafik dari baris query yang sudah diterima; tidak menerima angka/rows dari model.

        Args:
            query_id: ID hasil query_reports yang sudah tersedia.
            x: Nama kolom label dari hasil tersebut.
            y: Nama kolom angka dari hasil tersebut.
            kind: bar atau line.
        """
        return session.chart(query_id, x, y, kind) if guard_tool() else {"error": "Batas 12 tool tercapai; berhenti."}

    client = None
    if model is None:
        import httpx
        from openrouter import OpenRouter
        from openrouter.utils import RetryConfig

        def count_http(request):
            if str(request.url).split("?")[0] != BASE_URL + "/chat/completions":
                raise ValueError("Endpoint selain OpenRouter chat completions ditolak.")
            if stats["http_calls"] >= call_limit:
                raise CallBudgetExceeded("Batas request API tercapai.")
            stats["http_calls"] += 1

        # Satu request SDK per langkah; retry 429 hanya dikelola guard di bawah.
        client = httpx.Client(timeout=35, follow_redirects=False, event_hooks={"request": [count_http]})
        sdk = OpenRouter(api_key=os.environ.get("OPENROUTER_API_KEY", ""), server_url=BASE_URL,
                         client=client, timeout_ms=35000,
                         retry_config=RetryConfig(strategy="none", backoff=None,
                                                  retry_connection_errors=False))
        model = ChatOpenRouter(model=MODEL, api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                               base_url=BASE_URL, client=sdk, max_retries=0, timeout=35000,
                               max_tokens=2500, temperature=0.1, reasoning={"exclude": True})

    @wrap_model_call
    def bounded_call(request, handler):
        while True:
            if stats["model_calls"] >= call_limit:
                raise CallBudgetExceeded("Batas panggilan model tercapai. Persempit misi lalu coba manual.")
            stats["model_calls"] += 1
            session.total_calls += 1
            try:
                return handler(request)
            except Exception as exc:
                response = getattr(exc, "raw_response", None) or getattr(exc, "response", None)
                status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
                headers = getattr(response, "headers", {})
                retry_after = headers.get("retry-after", "2")
                try:
                    delay = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 6.0  # Tidak menebak waktu bila format bukan angka.
                if status == 429 and not stats["retried"] and delay <= 5 and stats["model_calls"] < call_limit:
                    stats["retried"] = True
                    emit({"type": "notice", "text": f"HTTP 429; menunggu {delay:g} detik sebelum satu retry."})
                    time.sleep(delay)
                    continue
                raise

    graph = create_agent(model, tools=[inspect_schema, query_reports, chart_query],
                         system_prompt=SYSTEM, middleware=[bounded_call])
    retained = list(session.turns)
    # Drop whole turns so tool call/result pairs stay intact.
    while retained and sum(len(str(m.content)) for turn in retained for m in turn["messages"]) > 40000:
        retained.pop(0)
    history = [message for turn in retained for message in turn["messages"]]
    human = HumanMessage(content=mission)
    new_messages.append(human)
    answer, error = "", ""
    try:
        for chunk in graph.stream({"messages": history + [human]},
                                  {"recursion_limit": 24}, stream_mode="updates"):
            for update in chunk.values():
                if not isinstance(update, dict):
                    continue
                for message in update.get("messages", []):
                    new_messages.append(message)
                    if isinstance(message, AIMessage):
                        used = message.response_metadata.get("model_name") or message.response_metadata.get("model")
                        if used and used not in models:
                            models.append(used)
                        emit({"type": "model", "model": used or "ID aktual tidak dikirim provider"})
                        if message.tool_calls:
                            for call in message.tool_calls:
                                emit({"type": "call", "tool": call["name"], "args": call["args"]})
                        else:
                            answer = safe_text(message.text, 4000)
                    elif isinstance(message, ToolMessage):
                        try:
                            result = json.loads(message.content)
                        except (TypeError, ValueError):
                            result = {"error": safe_text(message.content, 500)}
                        emit({"type": "result", "tool": message.name, "result": result})
        if not answer:
            error = "Model belum mengirim jawaban akhir. Hasil tool tetap dapat diperiksa; coba misi lebih singkat."
    except Exception as exc:
        error = safe_text(exc, 500)
        answer = ""
    finally:
        if client is not None:
            client.close()
    result = {"mission": mission, "answer": answer, "error": error,
              "events": events, "models": models, "model_calls": stats["model_calls"],
              "http_calls": stats["http_calls"], "charts": list(session.charts)}
    # Riwayat gagal tidak dimasukkan: pasangan tool call/result yang belum lengkap tidak dikirim ulang.
    if not error:
        session.turns.append({"messages": new_messages, "public": result})
    return result
