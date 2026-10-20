"""Isolate evaluator trust/lease state between standalone tests.

Real deployments use ~/.qiqi/evaluator-cwd, but a test that deliberately
preserves an interrupted worker must not poison the next test's lease.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_evaluator_trust_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(tmp_path / "evaluator-trust-root"))
