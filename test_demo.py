"""Pemeriksaan developer offline; tidak mengakses key atau jaringan."""
import json
import tempfile
import unittest
from pathlib import Path

try:
    from engine import DataSession, seed_database, run_mission
except ModuleNotFoundError:
    DataSession = None


class DemoChecks(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DataSession, "Engine read-only belum dibuat.")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "demo.sqlite"
        seed_database(self.path)
        self.session = DataSession(self.path)

    def test_sql_cannot_mutate_escape_or_run_unbounded_work(self):
        before = self.path.read_bytes()
        for sql in [
            "DELETE FROM reports", "DROP TABLE reports", "PRAGMA user_version=4",
            "ATTACH DATABASE ':memory:' AS other", "SELECT load_extension('x')",
            "SELECT readfile('/etc/passwd')", "SELECT randomblob(1000000000)",
            "SELECT * FROM reports; DELETE FROM reports",
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT * FROM n",
        ]:
            with self.subTest(sql=sql):
                self.assertIn("error", self.session.query(sql))
        self.assertEqual(before, self.path.read_bytes())
        huge = self.session.query("SELECT * FROM reports")
        self.assertTrue(huge["truncated"])
        self.assertLessEqual(len(huge["rows"]), 30)
        self.assertLess(len(json.dumps(huge)), 13000)
        expensive = self.session.query(
            "SELECT COUNT(*) FROM reports a, reports b, reports c, reports d, reports e"
        )
        self.assertIn("error", expensive)

    def test_chart_uses_only_returned_rows_and_valid_columns(self):
        result = self.session.query(
            "SELECT l.name AS lokasi, COUNT(*) AS jumlah FROM reports r "
            "JOIN locations l ON l.id=r.location_id GROUP BY l.id ORDER BY jumlah DESC LIMIT 3"
        )
        chart = self.session.chart(result["query_id"], "lokasi", "jumlah", "bar")
        self.assertEqual(chart["rows"], result["rows"])
        for args in [("invented", "lokasi", "jumlah", "bar"),
                     (result["query_id"], "lokasi", "made_up", "bar"),
                     (result["query_id"], "lokasi", "lokasi", "bar")]:
            self.assertIn("error", self.session.chart(*args))
        empty = self.session.query("SELECT id, category FROM reports WHERE 1=0")
        self.assertIn("error", self.session.chart(empty["query_id"], "category", "id", "bar"))

    def test_sql_intermediate_values_are_bounded_before_result_rendering(self):
        parts = ["s0(x) AS MATERIALIZED (SELECT '" + "x" * 1024 + "')"]
        parts += [f"s{i}(x) AS MATERIALIZED (SELECT x||x FROM s{i-1})" for i in range(1, 15)]
        query = "WITH " + ",".join(parts) + " SELECT length(x) FROM s14"
        self.assertIn("error", self.session.query(query))

    def test_hidden_thinking_and_credentials_are_removed_from_public_text(self):
        from engine import safe_text
        self.assertEqual(safe_text("<think>INTERNAL_SENTINEL"), "")
        self.assertEqual(safe_text("<think>internal</think>Jawaban"), "Jawaban")
        self.assertNotIn("sk-or-v1-example", safe_text("sk-or-v1-example"))
        self.assertEqual(safe_text("Jawaban ![grafik](https://example.invalid/fake.png)"), "Jawaban ")

    def test_real_provider_wiring_reports_transport_failure_without_network(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "offline-test-key"}), \
             patch("httpx.Client.send", side_effect=RuntimeError("OFFLINE_TRANSPORT_STOP")):
            result = run_mission(self.session, "Baca schema laporan")
        self.assertIn("OFFLINE_TRANSPORT_STOP", result["error"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["http_calls"], 0)
        self.assertFalse(self.session.turns)

    def test_actual_agent_graph_observes_tools_recovers_and_keeps_followup(self):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        class ObservingModel(BaseChatModel):
            @property
            def _llm_type(self):
                return "deterministic-offline-check"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                last = messages[-1]
                current = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
                followup = "baru" in current
                if isinstance(last, ToolMessage):
                    observed = json.loads(last.content)
                    if last.name == "inspect_schema":
                        name, args = "query_reports", {"sql": "SELECT nonexistent FROM reports"}
                    elif last.name == "query_reports" and "error" not in observed:
                        name, args = "chart_query", {"query_id": observed["query_id"],
                                                       "x": "lokasi", "y": "jumlah", "kind": "bar"}
                    elif last.name == "chart_query":
                        return ChatResult(generations=[ChatGeneration(message=AIMessage(
                            content="Hasil data demo sudah ditampilkan.",
                            response_metadata={"model_name": "offline-fake"}))])
                    else:
                        name, args = "query_reports", {"sql":
                            "SELECT l.name AS lokasi, COUNT(*) AS jumlah FROM reports r "
                            "JOIN locations l ON l.id=r.location_id GROUP BY l.id LIMIT 3"}
                elif followup:
                    name, args = "query_reports", {"sql":
                        "SELECT l.name AS lokasi, COUNT(*) AS jumlah FROM reports r "
                        "JOIN locations l ON l.id=r.location_id WHERE r.status='baru' "
                        "GROUP BY l.id LIMIT 3"}
                else:
                    name, args = "inspect_schema", {}
                message = AIMessage(content="", tool_calls=[{
                    "name": name, "args": args, "id": f"call-{len(messages)}", "type": "tool_call"}],
                    response_metadata={"model_name": "offline-fake"})
                return ChatResult(generations=[ChatGeneration(message=message)])

        first = run_mission(self.session, "Tiga lokasi terbanyak dan grafik", model=ObservingModel())
        self.assertFalse(first["error"])
        self.assertEqual(first["model_calls"], 5)
        self.assertEqual([e["tool"] for e in first["events"] if e["type"] == "result"],
                         ["inspect_schema", "query_reports", "query_reports", "chart_query"])
        second = run_mission(self.session, "Sekarang hanya yang baru", model=ObservingModel())
        self.assertFalse(second["error"])
        self.assertEqual(second["model_calls"], 3)
        self.assertTrue(second["charts"])
        self.assertIn("status='baru'", second["charts"][0]["sql"])
        self.assertEqual(len(self.session.turns), 2)
        limited = run_mission(self.session, "Tiga lokasi", model=ObservingModel(), call_limit=1)
        self.assertTrue(limited["error"])
        self.assertEqual(limited["model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
