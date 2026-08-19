import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_FILE = ROOT / "web_app" / "static" / "forecast_correction.html"
MAIN_FILE = ROOT / "web_app" / "main.py"


class ForecastCorrectionUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = UI_FILE.read_text(encoding="utf-8")
        cls.main_source = MAIN_FILE.read_text(encoding="utf-8")

    def test_markdown_file_is_read_locally_with_size_and_type_guards(self) -> None:
        self.assertIn('accept=".md,.markdown,.txt,text/markdown,text/plain"', self.html)
        self.assertIn("const MAX_FILE_BYTES = 5 * 1024 * 1024;", self.html)
        self.assertIn("const text = await file.text();", self.html)
        self.assertIn(".md, .markdown 또는 .txt 파일만 선택할 수 있습니다.", self.html)
        self.assertNotIn("/api/upload", self.html)

    def test_correction_api_receives_markdown_mode_and_optional_consensus(self) -> None:
        self.assertIn('const API_URL = "/api/forecasts/operating-profit/correct";', self.html)
        self.assertIn('headers: { "Content-Type": "application/json" }', self.html)
        self.assertIn("markdown_text: markdownText", self.html)
        self.assertIn("graph_mode: selectedGraphMode", self.html)
        self.assertIn("max_factreasoner_candidates: 10", self.html)
        self.assertIn("payload.consensus_won = consensus;", self.html)
        self.assertIn('value="llm" checked', self.html)
        self.assertIn('value="fast"', self.html)
        self.assertIn('nli_mode: selectedGraphMode === "fast" ? "fast" : "all_pairs"', self.html)
        self.assertIn("다국어 embedding gate", self.html)

    def test_result_surfaces_corrections_and_manual_review(self) -> None:
        self.assertIn("data.corrected_markdown", self.html)
        self.assertIn("stats.scale_cells_changed", self.html)
        self.assertIn("stats.factreasoner_corrections_applied", self.html)
        self.assertIn("stats.manual_review_required", self.html)
        self.assertIn("scale.review_items", self.html)
        self.assertIn("factreasoner.manual_review", self.html)
        self.assertIn("factreasoner.error", self.html)
        self.assertIn('id="originalOutput"', self.html)
        self.assertIn('id="correctedOutput"', self.html)

    def test_async_job_progress_is_rendered_as_pipeline_stages(self) -> None:
        self.assertIn('id="progressPanel"', self.html)
        self.assertIn('id="progressList"', self.html)
        self.assertIn("const renderProgress", self.html)
        self.assertIn("renderProgress(job);", self.html)
        self.assertIn("Fact Graph 생성", self.html)
        self.assertIn('"progress": deepcopy(job.get("progress") or [])', self.main_source)
        self.assertIn("_update_forecast_job_progress", self.main_source)

    def test_corrected_markdown_can_be_copied_and_downloaded(self) -> None:
        self.assertIn("navigator.clipboard.writeText", self.html)
        self.assertIn('new Blob([markdown], { type: "text/markdown;charset=utf-8" })', self.html)
        self.assertIn('link.download = `${baseName}.corrected.md`;', self.html)
        self.assertIn("URL.revokeObjectURL", self.html)

    def test_fastapi_route_serves_the_static_page(self) -> None:
        self.assertIn('@app.get("/forecast-correction")', self.main_source)
        self.assertIn('return FileResponse(str(_STATIC / "forecast_correction.html"))', self.main_source)

    def test_root_redirects_to_forecast_correction(self) -> None:
        self.assertIn('@app.get("/")', self.main_source)
        self.assertIn('return RedirectResponse("/forecast-correction")', self.main_source)


if __name__ == "__main__":
    unittest.main()
