import tomllib
from pathlib import Path

from doneproof import __version__
from doneproof.adapters.github import GitHubAdapter
from doneproof.app import VERSION


def test_release_version_is_consistent_everywhere():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["version"] == __version__ == VERSION


def test_provider_user_agent_uses_current_release_version():
    assert GitHubAdapter()._headers()["User-Agent"] == f"doneproof/{__version__}"


def test_customer_docs_do_not_report_a_stale_current_release():
    readme = Path("README.md").read_text()
    assert f"`{__version__}` is intended for controlled design-partner" in readme
