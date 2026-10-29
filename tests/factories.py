"""Synthetic transcript records, built here and never copied from a real session."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("/" + "srv/work/demo-repo")  # never a real home path


def base(kind: str, session: str, uuid: str, ts: str, **extra: object) -> dict:
    rec = {
        "type": kind,
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": str(ROOT),
        "gitBranch": extra.pop("branch", "main"),
        "isSidechain": extra.pop("sidechain", False),
    }
    rec.update(extra)
    return rec


def assistant_text(session: str, uuid: str, ts: str, text: str, **kw: object) -> dict:
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return base("assistant", session, uuid, ts, message=msg, **kw)


def tool_use(
    session: str, uuid: str, ts: str, name: str, tid: str, args: dict, **kw: object
) -> dict:
    block = {"type": "tool_use", "id": tid, "name": name, "input": args}
    return base(
        "assistant", session, uuid, ts, message={"role": "assistant", "content": [block]}, **kw
    )


def tool_result(
    session: str, uuid: str, ts: str, tid: str, text: str, tur: object = None, **kw: object
) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    rec = base("user", session, uuid, ts, message={"role": "user", "content": [block]}, **kw)
    if tur is not None:
        rec["toolUseResult"] = tur
    return rec


def human(
    session: str, uuid: str, ts: str, text: str, origin: str | None = "human", **kw: object
) -> dict:
    msg = {"role": "user", "content": [{"type": "text", "text": text}]}
    rec = base("user", session, uuid, ts, message=msg, **kw)
    if origin is not None:
        rec["origin"] = {"kind": origin}
    return rec


def pr_link(session: str, n: int, ts: str, repo: str = "acme/demo") -> dict:
    return {
        "type": "pr-link",
        "sessionId": session,
        "prNumber": n,
        "prUrl": f"https://github.com/{repo}/pull/{n}",
        "prRepository": repo,
        "timestamp": ts,
    }


def plan_approval(session: str, ts: str, tid: str, plan: str, file: str = "p.md") -> list[dict]:
    """An ExitPlanMode call and its approval, as the app records them."""
    return [
        tool_use(
            session, f"u-{tid}", ts, "ExitPlanMode", tid, {"plan": plan, "planFilePath": file}
        ),
        tool_result(
            session,
            f"r-{tid}",
            ts,
            tid,
            "User has approved your plan. Go.",
            {"plan": plan, "filePath": file, "isAgent": False},
        ),
    ]


def needs_block(*items: str) -> str:
    lines = ["Done with the work.", "", "## Needs you", ""]
    for i, item in enumerate(items, start=1):
        lines += [f"**{i}. {item}**", "", "*Why:* reasons.", ""]
    return "\n".join(lines)


def dumps(record: dict) -> str:
    """As Claude Code writes a transcript line: compact separators, raw UTF-8 (checked
    2026-10-27 on a real transcript: 785 `"type":"` and no `"type": "`; em dashes unescaped)."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def write(path: Path, records: list[dict]) -> Path:
    """Bytes, not text: on Windows write_text would turn each "\\n" into "\\r\\n"."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(dumps(r) + "\n" for r in records).encode("utf-8"))
    return path


def fake_exe(folder: Path, name: str, body: str) -> Path:
    """A stand-in program (for gh) that runs `body` as Python, on every OS: a script with a
    shebang on macOS and Linux, a .cmd shim beside it on Windows."""
    import os
    import sys

    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f"{name}.py"
    script.write_bytes(f"#!{sys.executable}\nimport sys, time\n{body}\n".encode())
    if os.name == "nt":
        shim = folder / f"{name}.cmd"
        shim.write_bytes(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n'.encode())
        return shim
    script.chmod(0o755)
    return script
