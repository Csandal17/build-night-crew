import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import board
import dashboard
import llm
import pipeline
import workspace


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own board, dashboard files, library and output folder."""
    monkeypatch.setattr(board, "DB_PATH", tmp_path / "board.db")
    monkeypatch.setattr(dashboard, "OUT_PATH", tmp_path / "dashboard.html")
    monkeypatch.setattr(dashboard, "DATA_PATH", tmp_path / "dashboard_data.js")
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path / "output")
    llm._reserved.clear()
    yield tmp_path
