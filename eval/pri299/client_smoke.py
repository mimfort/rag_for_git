"""Exercise the structured brief MCP boundary and file write in both installed CLIs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from eval.pri299.claude_fixture import local_messages
from reviewer.config.settings import Settings
from reviewer.entrypoints.mcp_server import create_server
from reviewer.mcp import task_context
from reviewer.mcp.service import MCPReviewService
from reviewer.metrics.brief_quality import briefs

MODULE = "eval.pri299.client_smoke"
ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = {
    "code": "// src/module.py#f (src/module.py:2-4)\n"
    "    2 | def f():\n    3 |     return 1\n    4 |     # cut\n[...truncated]",
    "test_exemplars": "// tests/test_module.py#test_f (tests/test_module.py:7-7)\n"
    "    7 | def test_f(): pass",
}
REFERENCES = [
    {"section": "code", "path": "src/module.py", "line": 3,
     "reason": "Проверить вызов `other.py:9` из пояснения.", "role": "edit"},
    {"section": "code", "path": "src/module.py", "line": 4,
     "reason": "Эта строка обрезана."},
    {"section": "test_exemplars", "path": "tests/test_module.py", "line": 7,
     "reason": "Прочитать пример теста."},
]
PROMPT = """This is a local MCP integration smoke. Call reviewer prepare_task_context
with repo "example/repo", key "PRI-299-smoke", warm_board false, include_brief_evidence true.
Next call reviewer verify_task_brief with that returned brief_evidence_id as evidence_id
and the references array below exactly as provided.
Write the returned markdown field verbatim into brief.md in the current directory.
Do not reconstruct Markdown, change the citations, or write any other file.
If using a shell, write with a single quoted here-document:
cat > brief.md <<'PRI299_MARKDOWN'
followed by the Markdown and the closing PRI299_MARKDOWN on its own line.
Read the saved file back and verify it equals the returned markdown. Repair a
failed write using the original response. Then reply with DONE.
"""


class SmokeService(MCPReviewService):
    """Use the real MCP tool and verifier with fixed, local retrieval excerpts."""

    def _resolve_repo_branch(self, repo, branch):
        return repo, branch or "main"

    def verify_task_brief(self, *args, **kwargs):
        output = super().verify_task_brief(*args, **kwargs)
        destination = Path(os.environ["REVIEWER_SMOKE_OUTPUT"])
        destination.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        return output


def serve():
    task_context.build_task_context = lambda *args, **kwargs: PAYLOAD.copy()
    settings = Settings(_env_file=None)
    create_server(SmokeService(settings, SimpleNamespace())).run()


def run_cli(client: str, executable: str, work: Path, *, fixture: bool = False) -> dict:
    work.mkdir()
    python = sys.executable
    args = ["-m", MODULE, "--server"]
    env = {
        "PYTHONPATH": str(ROOT),
        "REVIEWER_SMOKE_OUTPUT": str(work / "verified.json"),
    }
    if client == "codex":
        command = [
            executable, "-a", "never", "exec", "--ephemeral", "--skip-git-repo-check",
            "--json", "--sandbox", "workspace-write", "--disable", "hooks",
            "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
            "-c", "mcp_servers={}", "-c", f"mcp_servers.reviewer.command={json.dumps(python)}",
            "-c", f"mcp_servers.reviewer.args={json.dumps(args)}",
            "-c", 'mcp_servers.reviewer.tools.prepare_task_context.approval_mode="approve"',
            "-c", 'mcp_servers.reviewer.tools.verify_task_brief.approval_mode="approve"',
            *[part for key, value in env.items()
              for part in ("-c", f"mcp_servers.reviewer.env.{key}={json.dumps(value)}")],
            "-",
        ]
    else:
        config = work / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"reviewer": {
            "command": python, "args": args, "env": env,
        }}}), encoding="utf-8")
        command = [
            executable, "-p", "--verbose", "--output-format", "stream-json",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", str(config),
            "--setting-sources", "", "--permission-mode", "acceptEdits", "--tools", "Write",
            "--allowedTools",
            "Write,mcp__reviewer__prepare_task_context,mcp__reviewer__verify_task_brief",
        ]
        if fixture:
            command.extend(["--model", "sonnet"])
    endpoint = local_messages(REFERENCES, work / "brief.md") if fixture else nullcontext({})
    with endpoint as model_env:
        result = subprocess.run(
            command, cwd=work, input=PROMPT + json.dumps(REFERENCES, ensure_ascii=False),
            env={**os.environ, **model_env}, capture_output=True, text=True, timeout=240,
            check=False,
        )
    (work / "events.jsonl").write_text(result.stdout, encoding="utf-8")
    (work / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    result.check_returncode()

    verified = json.loads((work / "verified.json").read_text(encoding="utf-8"))
    written = (work / "brief.md").read_bytes()
    markdown = written.decode("utf-8")
    verdicts = [row["status"] for row in verified["citations"]]
    if verified["render_status"] != "ok" or verdicts != [
        "verified", "outside_excerpt", "verified",
    ]:
        raise ValueError(f"{client}: unexpected verification result")
    if written != verified["markdown"].encode("utf-8"):
        raise ValueError(f"{client}: written Markdown differed from the MCP response")
    code = briefs.parse_rendered_citations(markdown, briefs.RELEVANT_HEADER)
    tests = briefs.parse_rendered_citations(markdown, briefs.TEST_HEADER)
    if code != [("src/module.py", 3)] or tests != [("tests/test_module.py", 7)]:
        raise ValueError(f"{client}: saved Markdown did not retain exactly the verified anchors")
    if briefs.extract_section_paths(markdown, briefs.RELEVANT_HEADER) != {"src/module.py"}:
        raise ValueError(f"{client}: production metrics did not isolate the verified path")
    return {
        "version": subprocess.check_output([executable, "--version"], text=True).strip(),
        "inference": "local Messages API fixture" if fixture else "live",
        "render_status": verified["render_status"],
        "verdicts": verdicts,
        "code": code,
        "test_exemplars": tests,
        "markdown_sha256": hashlib.sha256(written).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work", type=Path, help="directory for raw smoke outputs")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--claude-fixture", action="store_true")
    args = parser.parse_args()
    if args.server:
        serve()
        return
    if args.work is None:
        parser.error("--work is required")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = {
            client: executor.submit(
                run_cli, client, binary, work / client,
                fixture=client == "claude" and args.claude_fixture,
            )
            for client, binary in (("codex", args.codex), ("claude", args.claude))
        }
        results = {client: job.result() for client, job in jobs.items()}
    if results["codex"]["markdown_sha256"] != results["claude"]["markdown_sha256"]:
        raise ValueError("CLI outputs differ")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
