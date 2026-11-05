"""Jalankan: uv run streamlit run app.py --server.address 127.0.0.1"""
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import BASE_URL, MODEL, DataSession, run_mission, safe_text, seed_database

st.set_page_config(page_title="LogiCodix · Agent analisis laporan", page_icon="📊", layout="wide")
st.title("Agent analisis laporan kampus")
st.caption("Demo agentic AI · LangChain + LangGraph · OpenRouter free router")
st.info("Seluruh data FIKTIF: 86 laporan, 6 lokasi, September–November 2026. "
        "Bukan laporan UNESA. ‘Bulan ini’ pada demo berarti November 2026.")


@st.cache_resource
def database_path():
    path = Path(__file__).with_name(".cache") / "reports.sqlite"
    seed_database(path)
    return path


if "data_session" not in st.session_state:
    st.session_state.data_session = DataSession(database_path())
session = st.session_state.data_session

examples = [
    "Tunjukkan tiga lokasi dengan laporan paling banyak bulan ini, apa jenis masalah dominannya, dan buat grafiknya.",
    "Bandingkan jumlah laporan per zona pada Oktober dan November 2026. Tampilkan tabel dan grafik.",
    "Sekarang hanya status baru pada bulan lalu. Tampilkan tiga lokasi terbanyak, masalah dominannya, dan grafik baru.",
]


def choose_mission(text):
    st.session_state.mission = text


if "mission" not in st.session_state:
    st.session_state.mission = examples[0]
buttons = st.columns([1, 1, 1, 0.65])
for col, label, text in zip(buttons, ["Contoh: lokasi", "Contoh: zona", "Contoh: follow-up"], examples):
    col.button(label, on_click=choose_mission, args=(text,), width="stretch")
if buttons[3].button("Reset sesi", width="stretch"):
    st.session_state.data_session = DataSession(database_path())
    st.session_state.pop("last_result", None)
    st.session_state.mission = examples[0]
    st.rerun()

st.text_area("Misi", key="mission", height=90, max_chars=1600)
st.caption("Follow-up memakai konteks hingga tiga misi selesai. Tool dan query dipilih model dari hasil sebelumnya. "
           "Maksimal 6 panggilan model dan 12 tool per misi; SQL read-only.")
ready = bool(os.environ.get("OPENROUTER_API_KEY"))
if not ready:
    st.warning("OPENROUTER_API_KEY belum tersedia di environment backend. Tidak ada key yang diminta lewat UI.")

run_col, retry_col, meta_col = st.columns([1, 1, 3])
run = run_col.button("Jalankan misi", type="primary", disabled=not ready, width="stretch")
retry = retry_col.button("Coba lagi manual", disabled=not ready or "last_result" not in st.session_state,
                         width="stretch")
meta_col.caption(f"Route tetap: {MODEL} · {BASE_URL} · tidak ada fallback berbayar")

if run or retry:
    if not st.session_state.mission.strip():
        st.warning("Tulis misi terlebih dahulu.")
    else:
        with st.status("Agent sedang bekerja…", expanded=True) as progress:
            def update(event):
                if event["type"] == "call":
                    progress.write(f"Memanggil `{event['tool']}`")
                elif event["type"] == "result":
                    result = event["result"]
                    if "error" in result:
                        progress.write(f"Tool mengembalikan error: {safe_text(result['error'], 180)}")
                    elif "rows" in result:
                        progress.write(f"Hasil diterima: {len(result['rows'])} baris")
                    else:
                        progress.write("Schema data diterima")
                elif event["type"] == "model":
                    progress.write(f"Dijawab oleh: `{safe_text(event['model'], 120)}`")
                else:
                    progress.write(safe_text(event.get("text", "")))
            try:
                result = run_mission(session, st.session_state.mission, on_event=update)
                st.session_state.last_result = result
                progress.update(label="Misi belum selesai" if result["error"] else "Agent selesai",
                                state="error" if result["error"] else "complete", expanded=False)
            except Exception as exc:
                progress.update(label="Misi tidak dapat dijalankan", state="error")
                st.session_state.last_result = {
                    "mission": st.session_state.mission, "answer": "", "error": safe_text(exc, 350),
                    "events": [], "models": [], "model_calls": 0, "http_calls": 0, "charts": [],
                }

if "last_result" in st.session_state:
    result = st.session_state.last_result
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        st.subheader("Hasil misi")
        st.caption(result["mission"])
        if result["error"]:
            st.error(result["error"])
            st.caption("Tidak ada jawaban pengganti yang dibuat otomatis. Misi tetap tersedia untuk retry manual.")
        else:
            st.markdown(result["answer"])
        for chart in result["charts"]:
            st.caption(f"Grafik dari {chart['query_id']} · x={chart['x']} · y={chart['y']}")
            frame = pd.DataFrame(chart["rows"])
            if chart["kind"] == "bar":
                st.bar_chart(frame, x=chart["x"], y=chart["y"], sort=False, color="#5B56D6")
            else:
                st.line_chart(frame, x=chart["x"], y=chart["y"], color="#5B56D6")
            st.dataframe(frame, hide_index=True, width="stretch")
            if chart["truncated"]:
                st.warning("Grafik hanya memakai baris yang dikembalikan tool; hasil query terpotong.")
        if not result["charts"]:
            queries = [e["result"] for e in result["events"]
                       if e["type"] == "result" and e["tool"] == "query_reports" and "rows" in e["result"]]
            if queries:
                st.dataframe(pd.DataFrame(queries[-1]["rows"]), hide_index=True, width="stretch")
    with right:
        st.subheader("Jejak tindakan")
        st.caption(f"{result['model_calls']} panggilan model · {result['http_calls']} request HTTP · "
                   f"total sesi: {session.total_calls}")
        st.caption("Yang ditampilkan hanya tool, parameter, hasil dan ID model; bukan chain-of-thought.")
        for i, event in enumerate(result["events"], 1):
            if event["type"] == "model":
                st.caption(f"{i}. Model: {safe_text(event['model'], 150)}")
            elif event["type"] == "call":
                st.markdown(f"**{i}. `{event['tool']}`**")
                if "sql" in event["args"]:
                    st.code(event["args"]["sql"], language="sql", wrap_lines=True)
                else:
                    st.code(json.dumps(event["args"], ensure_ascii=False), language="json", wrap_lines=True)
            elif event["type"] == "result":
                value = event["result"]
                if "error" in value:
                    st.warning(safe_text(value["error"], 240))
                elif "rows" in value:
                    st.caption(f"{i}. Hasil {value.get('query_id', '')}: {len(value['rows'])} baris")
                else:
                    st.caption(f"{i}. Schema: {', '.join(value.get('tables', {}))}")
                with st.expander(f"Lihat hasil tool #{i}"):
                    st.json(value, expanded=False)
            else:
                st.caption(safe_text(event.get("text", "")))
    payload = {"source": "live OpenRouter", "requested_model": MODEL,
               "model_calls_total": session.total_calls,
               "completed_turns": [turn["public"] for turn in session.turns],
               "last_attempt": result}
    st.download_button("Unduh trace (JSON, tanpa key)",
                       json.dumps(payload, ensure_ascii=False, indent=2),
                       file_name="logicodix-agent-trace.json", mime="application/json")

with st.expander("Schema dan batas demo"):
    st.json(session.schema(), expanded=False)
    st.write("Tool tidak memiliki shell, Python eval/exec, jaringan, akses environment, atau operasi tulis. "
             "SQLite dibuka mode=ro, dilindungi authorizer, batas baris, ukuran hasil dan progress timeout.")
