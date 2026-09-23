"""The live X experiment reader preserves the official cohort boundaries."""

import qa_experiments


def test_report_separates_nominal_arm_results_and_missingness(monkeypatch):
    queries = []

    def execute_sql(query):
        queries.append(query)
        return {
            "status": "ok",
            "columns": [
                "experiment_key", "enabled", "expires_at", "assignment_mode", "arm",
                "posted_eligible", "not_posted", "collected_outcomes", "nominal_reads",
                "late_reads", "root_impressions", "audience_reactions",
                "source_reply_impressions", "source_reply_clicks", "source_reply_reads",
            ],
            "rows": [
                ["alex-zero-x-v3", True, "2026-09-23 16:00:00+00:00", "balanced",
                 "arm_1", 33, 1, 29, 29, 0, 9331, 98, None, None, 0],
                ["alex-zero-x-v3", True, "2026-09-23 16:00:00+00:00", "balanced",
                 "arm_2", 34, 0, 30, 30, 0, 9954, 90, 2366, 59, 30],
            ],
            "truncated": False,
        }

    monkeypatch.setattr(qa_experiments, "execute_sql", execute_sql)
    report = qa_experiments.report_x_experiment("alex-zero-x-v3")

    assert len(queries) == 1
    assert "analysis_eligible" in queries[0]
    assert "nominal_window_eligible" in queries[0]
    assert report["arms"]["arm_1"]["nominal_reads"] == 29
    assert report["arms"]["arm_2"]["nominal_reads"] == 30
    assert report["arms"]["arm_1"]["missing_outcomes"] == 4
    assert report["arms"]["arm_2"]["missing_outcomes"] == 4
    assert report["arms"]["arm_1"]["source_reply_ctr"] is None
    assert report["arms"]["arm_2"]["source_reply_ctr"] == 59 / 2366
    assert report["arms"]["arm_1"]["avg_root_impressions"] == 9331 / 29
    assert report["arms"]["arm_2"]["avg_audience_reactions"] == 3


def test_report_rejects_untrusted_key_without_query(monkeypatch):
    monkeypatch.setattr(qa_experiments, "execute_sql", lambda _: (_ for _ in ()).throw(AssertionError("SQL reached")))
    assert qa_experiments.report_x_experiment("alex-zero-x-v3'; SELECT 1") == {"error": "invalid_experiment_key"}


def test_report_refuses_partial_or_unavailable_data(monkeypatch):
    monkeypatch.setattr(qa_experiments, "execute_sql", lambda _: {"error": "wrapper_error"})
    assert qa_experiments.report_x_experiment("alex-zero-x-v3") == {"error": "experiment_report_unavailable"}
    monkeypatch.setattr(qa_experiments, "execute_sql", lambda _: {"status": "ok", "rows": [], "columns": [], "truncated": True})
    assert qa_experiments.report_x_experiment("alex-zero-x-v3") == {"error": "experiment_report_incomplete"}
