"""Bounded read-only experiment summaries for the Fleet evidence broker.

The model chooses when to request a report and authors the answer. This reader
supplies current cohort measurements through the existing restricted SQL role.
It mirrors the eligibility and nominal-window boundaries of Squid Bot's
``experiment_report`` without exposing row-level content or write access.
"""
from __future__ import annotations

import re

from qa_sql import execute_sql


_EXPERIMENT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_COLUMNS = (
    "experiment_key", "enabled", "expires_at", "assignment_mode", "arm",
    "posted_eligible", "not_posted", "collected_outcomes", "nominal_reads",
    "late_reads", "root_impressions", "audience_reactions",
    "source_reply_impressions", "source_reply_clicks", "source_reply_reads",
)
_POSTED = "r.status='posted' AND (r.result->>'analysis_eligible')='true'"
_COLLECTED = _POSTED + " AND o.status='collected'"
_NOMINAL = _COLLECTED + " AND o.nominal_window_eligible IS TRUE"
_LATE = _COLLECTED + " AND o.nominal_window_eligible IS FALSE"


def _query(key: str) -> str:
    # Slug validation excludes quotes and SQL punctuation before interpolation.
    return f"""
SELECT e.key AS experiment_key, e.enabled, e.expires_at, e.assignment_mode,
       i.arm,
       COUNT(r.id) FILTER (WHERE {_POSTED}) AS posted_eligible,
       COUNT(r.id) FILTER (WHERE r.status <> 'posted') AS not_posted,
       COUNT(o.id) FILTER (WHERE {_COLLECTED}) AS collected_outcomes,
       COUNT(o.id) FILTER (WHERE {_NOMINAL}) AS nominal_reads,
       COUNT(o.id) FILTER (WHERE {_LATE}) AS late_reads,
       SUM(o.root_impressions) FILTER (WHERE {_NOMINAL}) AS root_impressions,
       SUM(COALESCE(o.root_audience_engagements, o.root_engagements, 0))
           FILTER (WHERE {_NOMINAL}) AS audience_reactions,
       SUM(o.reply_impressions)
           FILTER (WHERE {_NOMINAL} AND r.reply_x_message_id IS NOT NULL)
           AS source_reply_impressions,
       SUM(o.qualified_clicks)
           FILTER (WHERE {_NOMINAL} AND r.reply_x_message_id IS NOT NULL)
           AS source_reply_clicks,
       COUNT(o.id)
           FILTER (WHERE {_NOMINAL} AND r.reply_x_message_id IS NOT NULL)
           AS source_reply_reads
FROM x_integration_xexperiment e
LEFT JOIN x_integration_xexperimentreceipt r ON r.experiment_id=e.id
LEFT JOIN x_integration_xexperimentitem i ON i.id=r.item_id
LEFT JOIN x_integration_xexperimentoutcome o ON o.receipt_id=r.id
WHERE e.key='{key}'
GROUP BY e.key, e.enabled, e.expires_at, e.assignment_mode, i.arm
ORDER BY i.arm
""".strip()


def report_x_experiment(key: str) -> dict:
    """Return nominal +24h per-arm results and explicit missingness."""
    if type(key) is not str or not _EXPERIMENT_KEY.fullmatch(key):
        return {"error": "invalid_experiment_key"}
    result = execute_sql(_query(key))
    if "error" in result:
        return {"error": "experiment_report_unavailable"}
    if result.get("truncated") or result.get("status") != "ok":
        return {"error": "experiment_report_incomplete"}
    if result.get("columns") != list(_COLUMNS) or not isinstance(result.get("rows"), list):
        return {"error": "experiment_report_incomplete"}
    rows = result["rows"]
    if not rows:
        return {"error": "experiment_not_found"}
    if len(rows) > 2 or any(not isinstance(row, list) or len(row) != len(_COLUMNS) for row in rows):
        return {"error": "experiment_report_incomplete"}
    first = dict(zip(_COLUMNS, rows[0]))
    if first["experiment_key"] != key:
        return {"error": "experiment_report_incomplete"}
    arms = {}
    for values in rows:
        row = dict(zip(_COLUMNS, values))
        arm = row["arm"]
        if arm is None:
            continue  # Known experiment with no receipts yet.
        if arm not in {"arm_1", "arm_2"} or arm in arms:
            return {"error": "experiment_report_incomplete"}
        try:
            counts = {name: int(row[name] or 0) for name in (
                "posted_eligible", "not_posted", "collected_outcomes",
                "nominal_reads", "late_reads", "root_impressions",
                "audience_reactions", "source_reply_reads",
            )}
            reply_impressions = (None if row["source_reply_impressions"] is None
                                 else int(row["source_reply_impressions"]))
            reply_clicks = (None if row["source_reply_clicks"] is None
                            else int(row["source_reply_clicks"]))
        except (TypeError, ValueError):
            return {"error": "experiment_report_incomplete"}
        n = counts["nominal_reads"]
        if any(value < 0 for value in counts.values()) or counts["collected_outcomes"] > counts["posted_eligible"]:
            return {"error": "experiment_report_incomplete"}
        arms[arm] = {
            **counts,
            "missing_outcomes": counts["posted_eligible"] - counts["collected_outcomes"],
            "avg_root_impressions": counts["root_impressions"] / n if n else None,
            "avg_audience_reactions": counts["audience_reactions"] / n if n else None,
            "source_reply_impressions": reply_impressions,
            "source_reply_clicks": reply_clicks,
            "source_reply_ctr": (
                reply_clicks / reply_impressions
                if reply_clicks is not None and reply_impressions else None
            ),
        }
    return {
        "experiment": key,
        "enabled": first["enabled"],
        "expires_at": first["expires_at"],
        "assignment_mode": first["assignment_mode"],
        "sample": "posted analysis-eligible receipts with collected nominal +24h outcomes",
        "arms": arms,
    }
