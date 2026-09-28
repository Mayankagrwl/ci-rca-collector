"""Offline eval harness for ci-rca-collector (eval-spec §7–§11).

May import from ``tools.rca``; ``tools.rca`` must never import from here (§10).
This is the offline half — it replays captured goldens through the real
analysis path (``analyze.analyze_summary`` / ``diagnose.diagnose``) and reads
metrics off the ``AnalysisRecord`` fields; it does not reimplement analysis.
"""
