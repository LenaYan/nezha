import json
import pathlib
import subprocess

import pytest


@pytest.fixture(scope="session")
def repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo usable as ``workspace.repo``."""
    repo = tmp_path / "src"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(repo)])
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.check_call(["git", "-C", str(repo), "add", "-A"])
    subprocess.check_call([
        "git", "-C", str(repo), "-c", "user.email=t@example.com",
        "-c", "user.name=t", "commit", "-qm", "init"])
    return repo


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.json"
    path.write_text(json.dumps({"issues": [
        {"id": "T-1", "identifier": "T-1", "title": "First", "state": "Todo",
         "labels": ["nezha"], "description": "do the thing"},
        {"id": "T-2", "identifier": "T-2", "title": "Second", "state": "Done"},
        {"id": "T-3", "identifier": "T-3", "title": "Third", "state": "Backlog"},
        {"id": "T-4", "identifier": "T-4", "title": "Unlabelled", "state": "Todo"},
    ]}), encoding="utf-8")
    return path
