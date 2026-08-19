import asyncio
import unittest

from web_app.main import _FORECAST_JOBS, _forecast_job_public, _update_forecast_job_progress


class ForecastJobProgressTest(unittest.TestCase):
    def test_public_job_exposes_completed_and_active_stage_history(self) -> None:
        job_id = "test-progress"
        _FORECAST_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "created_at": 1.0,
            "updated_at": 1.0,
            "progress": [{"stage": "queued", "message": "대기", "state": "active", "started_at": 1.0}],
        }
        try:
            asyncio.run(_update_forecast_job_progress(job_id, "fact_graph", "그래프 생성"))
            payload = _forecast_job_public(_FORECAST_JOBS[job_id], include_result=False)
            self.assertEqual("completed", payload["progress"][0]["state"])
            self.assertEqual("fact_graph", payload["progress"][1]["stage"])
            self.assertEqual("active", payload["progress"][1]["state"])
            asyncio.run(_update_forecast_job_progress(job_id, "fact_graph", "문장/표 행 3/8"))
            payload = _forecast_job_public(_FORECAST_JOBS[job_id], include_result=False)
            self.assertEqual(2, len(payload["progress"]))
            self.assertEqual("문장/표 행 3/8", payload["progress"][1]["message"])
            self.assertIn("updated_at", payload["progress"][1])
        finally:
            _FORECAST_JOBS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
