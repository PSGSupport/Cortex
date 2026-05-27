"""Tests for mcp_server.shared.domain_mapping — path/env → canonical domain.

Covers the Docker-deployment path (`CORTEX_DEV_ROOT` + `CORTEX_HOST_HOME`)
where the host project tree is bind-mounted at a non-default location and
the cwd reaching this module may be a Windows-style path that has to be
translated before resolution.

`_build_registry` uses `lru_cache`, so any test that mutates env vars must
call `_build_registry.cache_clear()` before invoking the cached path.
"""

from pathlib import Path

import pytest

from mcp_server.shared import domain_mapping as dm
from mcp_server.shared.domain_mapping import (
    _build_registry,
    _normalize_path,
    _resolve_dev_root,
    _to_container_path,
    resolve_cwd,
)


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Ensure each test sees a freshly-built registry."""
    _build_registry.cache_clear()
    yield
    _build_registry.cache_clear()


# ── _resolve_dev_root ────────────────────────────────────────────────────


class TestResolveDevRoot:
    def test_honors_env_var(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        assert _resolve_dev_root() == Path("/host")

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "  /host  ")
        assert _resolve_dev_root() == Path("/host")

    def test_ignores_whitespace_only_env(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "   ")
        assert _resolve_dev_root() == Path.home() / "Developments"

    def test_falls_back_to_home_developments(self, monkeypatch):
        monkeypatch.delenv("CORTEX_DEV_ROOT", raising=False)
        assert _resolve_dev_root() == Path.home() / "Developments"


# ── _normalize_path ──────────────────────────────────────────────────────


class TestNormalizePath:
    def test_empty_passthrough(self):
        assert _normalize_path("") == ""

    def test_replaces_backslashes(self):
        assert _normalize_path(r"C:\Users\michael") == "C:/Users/michael"

    def test_collapses_double_slashes(self):
        assert _normalize_path("C:\\\\Users\\\\michael") == "C:/Users/michael"

    def test_strips_trailing_slash(self):
        assert _normalize_path("/host/") == "/host"

    def test_strips_trailing_backslash(self):
        assert _normalize_path("C:\\Users\\michael\\") == "C:/Users/michael"


# ── _to_container_path ───────────────────────────────────────────────────


class TestToContainerPath:
    def test_empty_passthrough(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        assert _to_container_path("") == ""

    def test_passthrough_when_no_env(self, monkeypatch):
        monkeypatch.delenv("CORTEX_DEV_ROOT", raising=False)
        monkeypatch.delenv("CORTEX_HOST_HOME", raising=False)
        cwd = r"C:\Users\michael.crawford\Dispatch-Agent"
        assert _to_container_path(cwd) == cwd

    def test_passthrough_when_only_one_env_set(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.delenv("CORTEX_HOST_HOME", raising=False)
        cwd = r"C:\Users\michael.crawford\Dispatch-Agent"
        assert _to_container_path(cwd) == cwd

    def test_exact_home_match_returns_dev_root(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        assert _to_container_path(r"C:\Users\michael.crawford") == "/host"

    def test_subdir_translation(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        cwd = r"C:\Users\michael.crawford\Dispatch-Agent"
        assert _to_container_path(cwd) == "/host/Dispatch-Agent"

    def test_mixed_slashes_translate(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        cwd = "C:/Users/michael.crawford/Dispatch-Agent"
        assert _to_container_path(cwd) == "/host/Dispatch-Agent"

    def test_trailing_separator_normalized(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host/")
        monkeypatch.setenv("CORTEX_HOST_HOME", "C:\\Users\\michael.crawford\\")
        assert _to_container_path("C:\\Users\\michael.crawford\\") == "/host"

    def test_double_separators_collapsed(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        cwd = "C:\\\\Users\\\\michael.crawford\\\\Teams-Integrations\\\\data"
        assert _to_container_path(cwd) == "/host/Teams-Integrations/data"

    def test_case_insensitive_match(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        cwd = r"c:\users\Michael.Crawford\Dispatch-Agent"
        # Tail preserves caller casing — downstream lookup is case-insensitive.
        assert _to_container_path(cwd) == "/host/Dispatch-Agent"

    def test_not_under_host_home_unchanged(self, monkeypatch):
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        cwd = r"D:\Other\repo"
        assert _to_container_path(cwd) == cwd

    def test_substring_not_prefix(self, monkeypatch):
        """Host home `C:\\Users\\mike` must NOT match `C:\\Users\\mikey\\repo`."""
        monkeypatch.setenv("CORTEX_DEV_ROOT", "/host")
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\mike")
        cwd = r"C:\Users\mikey\repo"
        assert _to_container_path(cwd) == cwd


# ── _build_registry ──────────────────────────────────────────────────────


class TestBuildRegistry:
    def test_uses_env_dev_root(self, monkeypatch, tmp_path):
        """Pointing CORTEX_DEV_ROOT at a tmp_path with fake .git dirs
        discovers repos from there, not from ~/Developments."""
        repo_a = tmp_path / "alpha"
        repo_b = tmp_path / "beta"
        (repo_a / ".git").mkdir(parents=True)
        (repo_b / ".git").mkdir(parents=True)
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        registry = _build_registry()
        canonicals = {r.canonical for r in registry.repos}
        assert "alpha" in canonicals
        assert "beta" in canonicals

    def test_cache_hit_returns_same_instance(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        first = _build_registry()
        second = _build_registry()
        assert first is second


# ── resolve_cwd ──────────────────────────────────────────────────────────


class TestResolveCwd:
    def test_empty_returns_empty(self):
        assert resolve_cwd("") == ""

    def test_uses_git_root_when_available(self, monkeypatch, tmp_path):
        """When _git_root returns a known repo path, use its canonical."""
        repo = tmp_path / "myrepo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        monkeypatch.setattr(dm, "_git_root", lambda p: str(repo))
        assert resolve_cwd(str(repo)) == "myrepo"

    def test_prefix_fallback_when_git_unavailable(self, monkeypatch, tmp_path):
        """When _git_root returns None, prefix-match into a known repo."""
        repo = tmp_path / "myrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / "subdir").mkdir()
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        monkeypatch.setattr(dm, "_git_root", lambda p: None)
        assert resolve_cwd(str(repo / "subdir")) == "myrepo"

    def test_longest_prefix_wins_for_nested_repos(self, monkeypatch, tmp_path):
        """Two repos /parent and /parent/child; cwd inside child resolves to child."""
        parent = tmp_path / "parent"
        child = parent / "child"
        (parent / ".git").mkdir(parents=True)
        (child / ".git").mkdir(parents=True)
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        # Both repos must be in the registry; force depth-2 discovery by
        # making `parent` an org-style container alongside its own .git.
        # Since _discover_repos returns once it finds .git at level 1,
        # we manually inject both via direct repo construction.
        from mcp_server.shared.domain_mapping import RepoInfo, DomainRegistry
        repos = [
            RepoInfo(fs_path=str(parent), dir_name="parent", remote_name="parent", canonical="parent"),
            RepoInfo(fs_path=str(child), dir_name="child", remote_name="child", canonical="child"),
        ]
        monkeypatch.setattr(
            dm,
            "_build_registry",
            lambda: DomainRegistry(repos, {}, {}, {}),
        )
        monkeypatch.setattr(dm, "_git_root", lambda p: None)
        assert resolve_cwd(str(child / "data")) == "child"

    def test_translates_windows_cwd_before_match(self, monkeypatch, tmp_path):
        """With CORTEX_HOST_HOME and CORTEX_DEV_ROOT set, a Windows cwd
        resolves via the container-path equivalent."""
        repo = tmp_path / "Dispatch-Agent"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        monkeypatch.setenv("CORTEX_HOST_HOME", r"C:\Users\michael.crawford")
        _build_registry.cache_clear()
        monkeypatch.setattr(dm, "_git_root", lambda p: None)
        cwd = r"C:\Users\michael.crawford\Dispatch-Agent"
        assert resolve_cwd(cwd) == "dispatch-agent"

    def test_unknown_repo_returns_empty(self, monkeypatch, tmp_path):
        """Cwd outside any registered repo returns '' so callers can
        fall through to explicit domain hints."""
        monkeypatch.setenv("CORTEX_DEV_ROOT", str(tmp_path))
        _build_registry.cache_clear()
        monkeypatch.setattr(dm, "_git_root", lambda p: None)
        assert resolve_cwd("/some/path/we/dont/know") == ""
