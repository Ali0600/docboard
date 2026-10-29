"""The privacy gate: the repo is public, so nothing personal may be committed."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from docboard import cli, privacy

# Built at run time so this file's own text holds no identifier.
HOME = "/" + "Users" + "/alice/projects/x"
PLACEHOLDER_HOME = "/" + "Users" + "/you/projects/x"
EMAIL = "alice" + "@" + "mail.test.com"
NOREPLY = "1+alice" + "@" + "users.noreply.github.com"

REPO_ROOT = Path(__file__).resolve().parents[1]


def kinds(text: str, terms: tuple[str, ...] = ()) -> list[str]:
    return [f.kind for f in privacy.scan_text("f.md", text, terms)]


def test_home_path_of_a_real_user_is_found() -> None:
    assert kinds(f"see {HOME}/file") == ["home-path"]


def test_placeholder_home_path_is_allowed() -> None:
    assert kinds(f"see {PLACEHOLDER_HOME}/file") == []


def test_email_is_found_and_noreply_is_allowed() -> None:
    assert kinds(f"{EMAIL} and {NOREPLY}") == ["email"]


def test_private_term_is_found_case_insensitively_without_echoing_it() -> None:
    found = privacy.scan_text("f.md", "Secret Project here", ["secret project"])
    assert [f.kind for f in found] == ["private-term"]
    assert "secret" not in found[0].excerpt.casefold()


def test_line_numbers_point_at_the_match() -> None:
    found = privacy.scan_text("f.md", f"clean\n{HOME}\n", [])
    assert [(f.line, f.kind) for f in found] == [(2, "home-path")]


def test_private_list_reads_terms_and_skips_comments(tmp_path: Path) -> None:
    terms = tmp_path / "private-identifiers.txt"
    terms.write_text("# heading\nalpha  # note\n\n beta \n")
    assert privacy.read_private_terms(terms) == ["alpha", "beta"]


def test_scan_repo_finds_an_untracked_file_and_skips_binaries(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "notes.md").write_text(f"path {HOME}\n")
    (tmp_path / "blob.bin").write_bytes(b"\0" + HOME.encode())
    found = privacy.scan_repo(tmp_path)
    assert [(f.path, f.kind) for f in found] == [("notes.md", "home-path")]


def test_this_repo_holds_no_personal_identifiers(live_terms: list[str]) -> None:
    found = privacy.scan_repo(REPO_ROOT, live_terms)
    assert found == [], "\n".join(f"{f.path}:{f.line}: {f.kind}: {f.excerpt}" for f in found)


# --- Windows paths, commits, pushes -------------------------------------------------------

WIN = "C:" + "\\" + "Users" + "\\" + "alice" + "\\code"
WIN_SLASH = "c:/" + "Users" + "/alice/code"
WIN_JSON = "C:" + "\\\\" + "Users" + "\\\\" + "alice" + "\\\\code"
WIN_YOU = "C:" + "\\" + "Users" + "\\" + "you" + "\\code"
CLAUDE = "Co-Authored-By: Claude <noreply" + "@" + "anthropic.com>"


def test_windows_home_paths_are_found() -> None:
    for text in (WIN, WIN_SLASH, f'"cwd":"{WIN_JSON}"'):
        assert kinds(f"see {text}") == ["home-path"], text


def test_windows_placeholder_home_is_allowed() -> None:
    assert kinds(f"see {WIN_YOU}") == []


def test_claude_trailer_email_is_allowed() -> None:
    assert kinds(CLAUDE) == []


def git(repo: Path, *args: str, email: str = "t@example.com") -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_EMAIL": email, "PATH": __import__("os").environ["PATH"]}  # fmt: skip
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          env=env, text=True)  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def pushing(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.md").write_text("clean\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "clean start")
    return repo


def push_line(repo: Path, branch: str = "main", base: str = "0" * 40) -> str:
    head = git(repo, "rev-parse", "HEAD")
    return f"refs/heads/{branch} {head} refs/heads/{branch} {base}\n"


def test_pre_push_blocks_a_home_path_in_a_message(pushing: Path) -> None:
    base = git(pushing, "rev-parse", "HEAD")
    git(pushing, "commit", "-q", "--allow-empty", "-m", f"fix {HOME}")
    found = privacy.scan_push(pushing, push_line(pushing, base=base))
    assert [f.kind for f in found] == ["home-path"]


def test_pre_push_blocks_a_term_added_then_removed(pushing: Path) -> None:
    base = git(pushing, "rev-parse", "HEAD")
    (pushing / "a.md").write_text("Codename Bluebird\n")
    git(pushing, "commit", "-q", "-am", "add")
    (pushing / "a.md").write_text("clean\n")
    git(pushing, "commit", "-q", "-am", "remove")
    found = privacy.scan_push(pushing, push_line(pushing, base=base), ["bluebird"])
    assert [f.kind for f in found] == ["private-term"]


def test_pre_push_blocks_a_private_branch_name(pushing: Path) -> None:
    git(pushing, "checkout", "-q", "-b", "bluebird-launch")
    found = privacy.scan_push(pushing, push_line(pushing, "bluebird-launch"), ["bluebird"])
    assert "private-term" in [f.kind for f in found]


def test_pre_push_blocks_an_author_email(pushing: Path) -> None:
    base = git(pushing, "rev-parse", "HEAD")
    git(pushing, "commit", "-q", "--allow-empty", "-m", "x", email=EMAIL)
    found = privacy.scan_push(pushing, push_line(pushing, base=base))
    assert {f.kind for f in found} == {"email"}


def test_pre_push_passes_clean_and_ignores_deletes(pushing: Path) -> None:
    base = git(pushing, "rev-parse", "HEAD")
    git(pushing, "commit", "-q", "--allow-empty", "-m", f"tidy\n\n{CLAUDE}")
    assert privacy.scan_push(pushing, push_line(pushing, base=base), ["bluebird"]) == []
    delete = f"(delete) {'0' * 40} refs/heads/old {git(pushing, 'rev-parse', 'HEAD')}\n"
    assert privacy.scan_push(pushing, delete, ["bluebird"]) == []


def test_pre_push_refuses_without_the_terms_file(
    pushing: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(push_line(pushing)))
    assert cli.main(["privacy", "--root", str(pushing), "--pre-push"]) == 2
    assert "refused" in capsys.readouterr().err


def test_privacy_exit_codes(pushing: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("bluebird\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(push_line(pushing)))
    assert cli.main(["privacy", "--root", str(pushing), "--pre-push", "--terms", str(terms)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(f"PR title\n{HOME}\n"))
    assert cli.main(["privacy", "--stdin"]) == 1
    assert cli.main(["privacy", "--root", str(pushing), "--commits", "HEAD"]) == 0


def test_the_hook_is_tracked_executable_and_runs_the_scan() -> None:
    entry = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-s", ".githooks/pre-push"],
                           check=True, capture_output=True, text=True).stdout  # fmt: skip
    assert entry.startswith("100755 ")
    text = (REPO_ROOT / ".githooks" / "pre-push").read_text()
    assert "docboard privacy --pre-push" in text and "\r" not in text
