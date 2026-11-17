"""Exercise the release gate with real isolated Git histories."""

import subprocess

import pytest

from tools.check_release import check


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release@example.invalid")
    (tmp_path / "app.txt").write_text("baseline\n")
    git("add", "app.txt")
    git("commit", "-m", "Baseline")
    sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", sha)
    return tmp_path, git, sha


def test_current_main_passes_and_generated_files_are_preserved(repo):
    path, _, sha = repo
    generated = path / "map.png"
    generated.write_bytes(b"generated")
    assert "passed" in check(path, sha, audit=True)
    assert generated.read_bytes() == b"generated"


def test_unmerged_production_commit_blocks_deployment(repo):
    path, git, sha = repo
    git("commit", "--allow-empty", "-m", "Production hotfix")
    with pytest.raises(ValueError, match="discard history"):
        check(path, sha)


@pytest.mark.parametrize("branch", ["feature", None])
def test_feature_or_detached_deployment_is_rejected(repo, branch):
    path, git, sha = repo
    if branch:
        git("switch", "-c", branch)
    else:
        git("switch", "--detach")
    with pytest.raises(ValueError, match="must use main"):
        check(path, sha)


def test_dirty_tracked_files_are_rejected(repo):
    path, _, sha = repo
    (path / "app.txt").write_text("local hotfix\n")
    with pytest.raises(ValueError, match="local changes"):
        check(path, sha)


def test_stale_remote_ref_is_rejected(repo):
    path, git, _ = repo
    git("commit", "--allow-empty", "-m", "New main")
    with pytest.raises(ValueError, match="stale"):
        check(path, git("rev-parse", "HEAD"))


def test_fast_forward_is_allowed_but_audit_reports_pending_release(repo):
    path, git, old = repo
    git("commit", "--allow-empty", "-m", "Next release")
    new = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", new)
    git("reset", "--hard", old)
    assert "passed" in check(path, new)
    with pytest.raises(ValueError, match="differs"):
        check(path, new, audit=True)


def test_missing_or_invalid_main_fails_closed(repo):
    path, git, sha = repo
    with pytest.raises(ValueError, match="full SHA"):
        check(path, "main")
    git("update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(subprocess.CalledProcessError):
        check(path, sha)
