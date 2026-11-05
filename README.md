# Demo agentic AI LogiCodix

Contoh teknis untuk sesi **AI Integration for Application Development**: agent
memilih tool untuk menganalisis laporan fasilitas kampus, membaca hasilnya, lalu
menjawab dengan tabel/grafik. Seluruh data **fiktif**, bukan laporan UNESA.
Contoh ini dapat diadaptasi; tidak menentukan produk yang harus dibuat peserta.

Repo: [chmdznr/logicodix-agentic-demo](https://github.com/chmdznr/logicodix-agentic-demo).

## Jalankan di komputer sendiri

Gunakan Python **3.12** dan uv. Dependency dipin di `uv.lock`. Dari terminal:

```sh
git clone https://github.com/chmdznr/logicodix-agentic-demo.git
cd logicodix-agentic-demo
uv sync --frozen --python 3.12
export OPENROUTER_API_KEY='GANTI_DENGAN_API_KEY_OPENROUTER_MILIK_SENDIRI'
uv run streamlit run app.py --server.address 127.0.0.1 --server.port 8507 --server.headless true --browser.gatherUsageStats false
```

Ganti placeholder dengan API key OpenRouter milik sendiri pada environment proses
backend. Buka **http://127.0.0.1:8507**. Pada PowerShell, pengisian environment
menggunakan `$env:OPENROUTER_API_KEY = 'GANTI_DENGAN_API_KEY_OPENROUTER_MILIK_SENDIRI'`;
perintah lainnya sama.

App membaca environment; **tidak otomatis memuat file `.env`**. Jangan masukkan key
ke source, UI, screenshot, atau commit. Database runtime dibuat dari `data.sql`
di `.cache/reports.sqlite`; tidak perlu menyalin database atau file pribadi dari mentor.

## Coba misi dan follow-up

1. Jalankan: “Tunjukkan tiga lokasi dengan laporan paling banyak bulan ini,
   apa jenis masalah dominannya, dan buat grafiknya.”
2. Pada sesi yang sama, lanjutkan: “Sekarang hanya status baru pada bulan lalu.
   Tampilkan tiga lokasi terbanyak, masalah dominannya, dan grafik baru.”
3. Buka **Jejak tindakan** untuk melihat pilihan tool, parameter, hasil, ID model
   aktual jika dikirim provider, dan jumlah panggilan. **Reset sesi** menghapus konteks.

Model memilih tindakan dari observasi, bukan mengikuti urutan schema/query/chart
yang dipaksa kode. Tiga tool yang tersedia:

- `inspect_schema()`: struktur SQLite, cakupan tanggal/status dan asal data fiktif.
- `query_reports(sql)`: satu query read-only; hasil memuat rows dan `query_id`.
- `chart_query(query_id, x, y, kind)`: chart memakai rows query yang sudah tersimpan,
  bukan angka atau rows baru yang dikirim model. Kolom dan tipe angka divalidasi.

Jawaban bahasa alami tetap bisa salah. Periksa SQL, filter, tabel, dan sumber chart.
Trace menampilkan tindakan/hasil publik, tanpa chain-of-thought atau auth headers.

## Komponen dan data

- **LangChain 1.4.9**: integrasi model, `@tool`, `create_agent`, dan middleware.
- **LangGraph 1.2.20**: runtime loop/state di bawah `create_agent`. App tidak memakai
  custom graph atau checkpointer permanen.
- **langchain-openrouter 0.2.9**: akses model hosted. **Streamlit 1.66.1**: UI,
  tabel dan chart. SQLite memakai Python stdlib; LangSmith tidak diperlukan.

`data.sql` adalah seed deterministik: **86 laporan, 6 lokasi, September-November
2026**, semuanya fiktif. `locations` menyimpan nama/zona; `reports` memuat lokasi,
tanggal, kategori dan status. Acuan demo tetap: “bulan ini” November 2026,
“bulan lalu” Oktober 2026.

`engine.py` menghubungkan model, tools, agent, events dan history; `app.py`
menampilkan hasil. History hanya dalam sesi Streamlit: maksimal tiga giliran
sukses, dengan pemangkasan giliran utuh jika konteks melewati 40.000 karakter.
Ini bukan penyimpanan percakapan lintas restart.

## Route gratis dan batas eksekusi

Semua model calls memakai **`openrouter/free`** pada
**`https://openrouter.ai/api/v1`**. Router dapat memilih model berbeda per call;
ketersediaan/tool support dapat berubah. Tidak ada fallback berbayar atau model lokal.
Menjalankan ulang misi tetap memakai kuota akun OpenRouter masing-masing.

Per misi: maksimal 6 model calls, 12 tool calls, dan recursion limit 24. Timeout
request 35 detik. SDK retry otomatis dimatikan; satu retry 429 hanya dilakukan
dalam budget jika `Retry-After` numerik tidak lebih dari 5 detik. Selain itu error
ditampilkan untuk retry manual. App tidak mengarang jawaban pengganti saat gagal.

SQL dibatasi oleh koneksi `mode=ro`, `query_only`, authorizer tabel/fungsi,
batas waktu/VM instructions, serta ukuran input/hasil. Maksimal 30 rows per query;
chart hanya memakai rows yang dikembalikan. Operasi tulis, shell, Python eval/exec,
akses file, environment dan jaringan tidak tersedia sebagai tool model.

## Pemeriksaan offline

Tidak membutuhkan API key atau request provider:

```sh
uv run python -m unittest -v
```

Enam pemeriksaan mencakup penolakan SQL mutasi/escape/query berat, batas nilai
SQLite, integritas data chart, sanitasi keluaran, wiring SDK tanpa jaringan, serta
loop `create_agent` dengan fake model deterministik: observasi error, perbaikan
query, chart, berhenti, dan follow-up. Fake model hanya untuk check developer;
bukan mode jawaban live pada aplikasi. Hasil offline bukan jaminan layanan provider.

## Rujukan

- [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents)
- [LangChain streaming](https://docs.langchain.com/oss/python/langchain/streaming)
- [ChatOpenRouter](https://docs.langchain.com/oss/python/integrations/chat/openrouter)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [OpenRouter free router](https://openrouter.ai/docs/guides/routing/routers/free-router)

Contoh ini telah diperiksa secara lokal; tidak ada klaim deployment produksi atau
manfaat pengguna yang sudah diukur.
