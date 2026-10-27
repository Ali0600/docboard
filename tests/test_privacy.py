"""The privacy gate: the repo is public, so nothing personal may be committed."""

from __future__ import annotations

import subprocess
from pathlib import Path

from docboard import privacy

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
    (tmp_path / "config").mkdir()
    (tmp_path / privacy.PRIVATE_LIST).write_text("# heading\nalpha  # note\n\n beta \n")
    assert privacy.read_private_terms(tmp_path) == ["alpha", "beta"]


def test_scan_repo_finds_an_untracked_file_and_skips_binaries(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "notes.md").write_text(f"path {HOME}\n")
    (tmp_path / "blob.bin").write_bytes(b"\0" + HOME.encode())
    found = privacy.scan_repo(tmp_path)
    assert [(f.path, f.kind) for f in found] == [("notes.md", "home-path")]


def test_this_repo_holds_no_personal_identifiers() -> None:
    found = privacy.scan_repo(REPO_ROOT)
    assert found == [], "\n".join(f"{f.path}:{f.line}: {f.kind}: {f.excerpt}" for f in found)
