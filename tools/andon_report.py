"""Andon-style static-analysis report.

Runs every check in the static-analysis battery sequentially, captures outcome +
duration + summary metric + raw output for each, then writes a single
self-contained HTML file (`andon-report.html`) so an operator can see the
state of the codebase at a glance — green / yellow / red per tool, like a
factory floor's andon board.

Status semantics:
  GREEN   — tool exited 0; no findings or findings within configured limits.
  YELLOW  — tool found something but is non-blocking (warning band: tier-3
            tools, soft-thresholded measures, or summary-only reports).
  RED     — tool exited non-zero in a blocking band (security, CVE, type
            errors, test failures, coverage under threshold).

Run:  uv run python tools/andon_report.py
Out:  andon-report.html  (and an exit status equal to the worst RED count)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class Status(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass
class CheckResult:
    name: str
    command: str
    status: Status
    summary: str  # one-line metric the operator reads at a glance
    duration_s: float
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    blocking: bool = True  # if False, never fails CI / never RED on the dashboard

    # Set later by the renderer to a stable DOM id for the collapsible.
    anchor: str = field(default="")


@dataclass
class Check:
    """One andon-board entry. `summarize` turns a CompletedProcess into the
    short metric line + decides green/yellow/red based on returncode and
    output content."""

    name: str
    cmd: list[str]
    blocking: bool = True
    summarize: callable = None  # (CompletedProcess) -> tuple[Status, str]
    cwd: Path = REPO_ROOT


# ------------------------------------------------------------------ summarizers


def _generic_summary(
    proc: subprocess.CompletedProcess[str], *, ok_msg: str = "passed"
) -> tuple[Status, str]:
    if proc.returncode == 0:
        return Status.GREEN, ok_msg
    return Status.RED, f"failed (exit {proc.returncode})"


def _bandit_summary(
    proc: subprocess.CompletedProcess[str],
) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    # Bandit prints "Total issues (by severity):" with counts; pull HIGH count.
    high = re.search(r"High:\s*(\d+)", out)
    med = re.search(r"Medium:\s*(\d+)", out)
    low = re.search(r"Low:\s*(\d+)", out)
    h = int(high.group(1)) if high else 0
    m = int(med.group(1)) if med else 0
    l = int(low.group(1)) if low else 0
    if h:
        return Status.RED, f"{h} HIGH / {m} medium / {l} low"
    if proc.returncode != 0:
        return Status.RED, f"failed (exit {proc.returncode})"
    if m:
        return Status.YELLOW, f"0 HIGH / {m} medium / {l} low"
    return Status.GREEN, f"clean ({l} low)" if l else "clean"


def _ruff_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    if proc.returncode == 0:
        return Status.GREEN, "all checks passed"
    found = re.search(r"Found (\d+) error", proc.stdout + proc.stderr)
    n = found.group(1) if found else "?"
    return Status.RED, f"{n} errors"


def _mypy_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    if proc.returncode == 0:
        m = re.search(r"checked (\d+) source files?", proc.stdout)
        files = m.group(1) if m else "?"
        return Status.GREEN, f"0 issues / {files} files"
    found = re.search(r"Found (\d+) error", proc.stdout + proc.stderr)
    n = found.group(1) if found else "?"
    return Status.RED, f"{n} errors"


def _pyright_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    errors = re.search(r"(\d+) errors?", out)
    warns = re.search(r"(\d+) warnings?", out)
    e = int(errors.group(1)) if errors else 0
    w = int(warns.group(1)) if warns else 0
    if e == 0 and w == 0:
        return Status.GREEN, "clean"
    if e == 0:
        return Status.YELLOW, f"0 errors / {w} warnings"
    return Status.YELLOW, f"{e} errors / {w} warnings (non-blocking)"


def _pytest_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) passed(?:.*?(\d+) failed)?", out)
    if m:
        passed = m.group(1)
        failed = m.group(2) or "0"
        if proc.returncode == 0:
            return Status.GREEN, f"{passed} passed"
        return Status.RED, f"{failed} failed / {passed} passed"
    return _generic_summary(proc, ok_msg="passed")


def _coverage_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    pct = re.search(r"TOTAL.*?(\d+)%", out)
    if pct:
        coverage = int(pct.group(1))
        if proc.returncode == 0:
            return Status.GREEN, f"{coverage}% (above floor)"
        if "Required test coverage" in out or "FAIL Required" in out:
            return Status.RED, f"{coverage}% (under floor)"
        return Status.RED, f"{coverage}% + tests failed"
    return _generic_summary(proc, ok_msg="ok")


def _vulture_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout
    lines = [ln for ln in out.splitlines() if ln.strip() and ":" in ln]
    n = len(lines)
    if n == 0:
        return Status.GREEN, "no dead code"
    return Status.YELLOW, f"{n} candidate(s)"


def _xenon_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    errs = [ln for ln in out.splitlines() if "ERROR:xenon" in ln]
    if proc.returncode == 0:
        return Status.GREEN, "within complexity budget"
    return Status.YELLOW, f"{len(errs)} hot spot(s)"


def _interrogate_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    pct = re.search(r"actual:\s*(\d+(?:\.\d+)?)%", out)
    p = float(pct.group(1)) if pct else 0.0
    if proc.returncode == 0:
        return Status.GREEN, f"{p:.1f}% docstring coverage"
    return Status.YELLOW, f"{p:.1f}% (under threshold)"


def _import_linter_summary(
    proc: subprocess.CompletedProcess[str],
) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    m = re.search(r"Contracts:\s*(\d+) kept,\s*(\d+) broken", out)
    if m:
        kept, broken = int(m.group(1)), int(m.group(2))
        if broken == 0:
            return Status.GREEN, f"{kept}/{kept + broken} contracts kept"
        return Status.RED, f"{broken} contract(s) broken"
    return _generic_summary(proc, ok_msg="all contracts kept")


def _pip_audit_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout + proc.stderr
    if "No known vulnerabilities" in out or proc.returncode == 0:
        return Status.GREEN, "no known CVEs"
    # pip-audit prints `Found N vulnerabilities`
    m = re.search(r"Found (\d+) known vulnerabilit", out)
    if m:
        return Status.RED, f"{m.group(1)} CVE(s)"
    return _generic_summary(proc, ok_msg="clean")


def _radon_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    out = proc.stdout
    # Radon output format `F LINE:COL FN_NAME - <RANK> (CC)`
    blocks = re.findall(r"-\s+([A-F])\s+\(\d+\)", out)
    if not blocks:
        return Status.GREEN, "all green"
    counts = {r: blocks.count(r) for r in "ABCDEF"}
    note = ", ".join(f"{r}={counts[r]}" for r in "ABCDEF" if counts[r])
    # Radon never fails; it's purely informational.
    return Status.GREEN, note or "all green"


def _pydeps_summary(proc: subprocess.CompletedProcess[str]) -> tuple[Status, str]:
    return _generic_summary(proc, ok_msg="graph generated")


# ------------------------------------------------------------------ check list


def _build_checks(*, with_pydeps: bool) -> list[Check]:
    """Define the andon battery. Order matters: cheap fast checks first so
    operators see green dots quickly while expensive checks finish."""
    checks: list[Check] = [
        Check(
            name="ruff format",
            cmd=["uv", "run", "ruff", "format", "--check", "src", "tests"],
            summarize=_ruff_summary,
        ),
        Check(
            name="ruff lint",
            cmd=["uv", "run", "ruff", "check", "src", "tests"],
            summarize=_ruff_summary,
        ),
        Check(
            name="mypy --strict",
            cmd=["uv", "run", "mypy", "--strict", "src/i3xua"],
            summarize=_mypy_summary,
        ),
        Check(
            name="pyright (non-blocking)",
            cmd=["uv", "run", "pyright"],
            blocking=False,
            summarize=_pyright_summary,
        ),
        Check(
            name="import-linter",
            cmd=["uv", "run", "lint-imports"],
            summarize=_import_linter_summary,
        ),
        Check(
            name="bandit (security)",
            # `--severity-level high` makes bandit exit non-zero ONLY on HIGH
            # severity. Low / medium findings still appear in the report
            # detail and surface as YELLOW on the andon board, but don't
            # block CI. Promote individual rules with `# nosec` if needed.
            cmd=[
                "uv",
                "run",
                "bandit",
                "-r",
                "src/i3xua",
                "-c",
                ".bandit",
                "--severity-level",
                "high",
            ],
            summarize=_bandit_summary,
        ),
        Check(
            name="pip-audit (CVE scan)",
            # `--skip-editable` skips our own i3xua; do NOT pass
            # `--strict` (it would make the editable-skip itself fatal).
            #
            # Documented ignores (revisit when each upstream patches):
            #   PYSEC-2022-42969 — `py 1.11.0` regex DoS in `py.path.svnwc`.
            #     Pulled in transitively by `interrogate` (T12.7). The
            #     vulnerable code is Subversion path handling; neither we
            #     nor interrogate use `py.path.svnwc`. Not exploitable.
            cmd=[
                "uv",
                "run",
                "pip-audit",
                "--skip-editable",
                "--ignore-vuln",
                "PYSEC-2022-42969",
            ],
            summarize=_pip_audit_summary,
        ),
        Check(
            name="vulture (dead code)",
            cmd=["uv", "run", "vulture"],
            blocking=False,
            summarize=_vulture_summary,
        ),
        Check(
            name="xenon (complexity)",
            cmd=[
                "uv",
                "run",
                "xenon",
                "--max-absolute",
                "D",
                "--max-modules",
                "B",
                "--max-average",
                "B",
                "src/i3xua",
            ],
            blocking=False,
            summarize=_xenon_summary,
        ),
        Check(
            name="radon (complexity report)",
            cmd=["uv", "run", "radon", "cc", "src/i3xua", "-s", "-n", "B"],
            blocking=False,
            summarize=_radon_summary,
        ),
        Check(
            name="interrogate (docstrings)",
            cmd=["uv", "run", "interrogate", "-v", "src/i3xua"],
            summarize=_interrogate_summary,
        ),
        Check(
            # Coverage gate set at the current floor (~65%) so the andon board
            # starts green. Ratchet up as test gaps are filled — target 85%.
            name="pytest + coverage (≥65% floor)",
            cmd=[
                "uv",
                "run",
                "pytest",
                "--cov=i3xua",
                "--cov-report=term",
                "--cov-fail-under=65",
                "tests/unit",
                "tests/contract",
                "-q",
            ],
            summarize=_coverage_summary,
        ),
    ]
    if with_pydeps:
        checks.append(
            Check(
                name="pydeps (dep graph)",
                cmd=[
                    "uv",
                    "run",
                    "pydeps",
                    "src/i3xua",
                    "--max-bacon=3",
                    "--cluster",
                    "--noshow",
                    "-o",
                    "andon-deps.svg",
                ],
                blocking=False,
                summarize=_pydeps_summary,
            )
        )
    return checks


# ------------------------------------------------------------------ runner


def _run_check(check: Check) -> CheckResult:
    started = time.monotonic()
    proc = subprocess.run(
        check.cmd,
        cwd=check.cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    duration = time.monotonic() - started
    summarize = check.summarize or _generic_summary
    status, summary = summarize(proc)
    # Soft tools never go red — clamp to YELLOW.
    if not check.blocking and status is Status.RED:
        status = Status.YELLOW
        summary = f"{summary} (non-blocking)"
    result = CheckResult(
        name=check.name,
        command=" ".join(check.cmd),
        status=status,
        summary=summary,
        duration_s=duration,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
        blocking=check.blocking,
    )
    return result


# ------------------------------------------------------------------ HTML render


_CSS = """
:root { color-scheme: light dark;
        --bg: #f5f6f8; --panel: #fff; --fg: #111; --muted: #555;
        --border: #c8c8c8;
        --green: #1a7f37; --green-bg: #ddf4e0;
        --yellow: #b87900; --yellow-bg: #fff1cc;
        --red:    #c0252b; --red-bg:    #ffd9da; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0c0e12; --panel: #161a20; --fg: #f0f0f0; --muted: #aaa;
          --border: #333a44;
          --green-bg: #0f3b1c; --yellow-bg: #4a3300; --red-bg: #4a1416;
          --green: #46d871; --yellow: #ffce4d; --red: #ff7b80; }
}
* { box-sizing: border-box; }
body { font: 13px/1.4 ui-monospace, Menlo, Consolas, monospace;
       margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg); }
h1 { font-size: 18px; margin: 0 0 .25rem; }
.meta { color: var(--muted); margin-bottom: 1rem; font-size: 12px; }

.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: .75rem;
        margin-bottom: 1rem; }
.kpi { padding: .75rem 1rem; border-radius: 6px; border: 1px solid var(--border);
       background: var(--panel); }
.kpi b { display: block; font-size: 22px; font-weight: 800; }
.kpi span { color: var(--muted); font-size: 11px; text-transform: uppercase;
            letter-spacing: .05em; }
.kpi.green  b { color: var(--green); }
.kpi.yellow b { color: var(--yellow); }
.kpi.red    b { color: var(--red); }

.row { display: grid; grid-template-columns: 14px 1fr 18ch 9ch 4ch;
       gap: .75rem; align-items: center;
       padding: .55rem .75rem;
       border: 1px solid var(--border); border-radius: 6px;
       background: var(--panel); margin-bottom: .35rem; cursor: pointer;
       transition: background-color .1s; }
.row:hover { background: color-mix(in srgb, var(--panel) 92%, var(--fg) 8%); }
.dot { width: 14px; height: 14px; border-radius: 50%; }
.dot.green  { background: var(--green); box-shadow: 0 0 0 2px var(--green-bg); }
.dot.yellow { background: var(--yellow); box-shadow: 0 0 0 2px var(--yellow-bg); }
.dot.red    { background: var(--red); box-shadow: 0 0 0 2px var(--red-bg); }
.tool { font-weight: 700; }
.summary { color: var(--muted); }
.duration { color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; }
.flag { font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
        text-align: right; color: var(--muted); }
.flag.blocking { color: var(--red); }
.flag.advisory { color: var(--yellow); }

details { margin: 0 0 .75rem; border: 1px solid var(--border); border-radius: 6px;
          background: var(--panel); }
details > summary { padding: .55rem .75rem; cursor: pointer; font-weight: 600;
                    list-style: none; display: flex; gap: .75rem; align-items: center; }
details > summary::-webkit-details-marker { display: none; }
details[open] { padding-bottom: .5rem; }
details pre { margin: 0; padding: .5rem .75rem; background: transparent;
              white-space: pre-wrap; word-break: break-all; color: var(--fg);
              font-size: 12px; max-height: 360px; overflow-y: auto; }
details .cmd { color: var(--muted); padding: 0 .75rem; font-size: 11px; }
"""


def _classify_overall(results: list[CheckResult]) -> Status:
    if any(r.status is Status.RED and r.blocking for r in results):
        return Status.RED
    if any(r.status in (Status.RED, Status.YELLOW) for r in results):
        return Status.YELLOW
    return Status.GREEN


def _render(results: list[CheckResult]) -> str:
    overall = _classify_overall(results)
    n_green = sum(1 for r in results if r.status is Status.GREEN)
    n_yellow = sum(1 for r in results if r.status is Status.YELLOW)
    n_red = sum(1 for r in results if r.status is Status.RED)
    total_dur = sum(r.duration_s for r in results)
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows: list[str] = []
    details: list[str] = []
    for i, r in enumerate(results):
        anchor = f"check-{i}"
        r.anchor = anchor
        # Row policy flag: only render INFO on advisory rows. Gate-policy
        # rows (the majority) leave the chip empty so the eye lands on the
        # exceptions — informational checks where a yellow status doesn't
        # need to alarm anyone.
        flag_lbl = "" if r.blocking else "info"
        rows.append(
            f'<a href="#{anchor}" style="text-decoration: none; color: inherit;">'
            f'<div class="row">'
            f'<span class="dot {r.status.value}"></span>'
            f'<span class="tool">{html.escape(r.name)}</span>'
            f'<span class="summary">{html.escape(r.summary)}</span>'
            f'<span class="duration">{r.duration_s:.1f}s</span>'
            f'<span class="flag advisory">{flag_lbl}</span>'
            f"</div></a>"
        )
        body = (r.stdout + r.stderr).strip() or "(no output)"
        details.append(
            f'<details id="{anchor}">'
            f'<summary><span class="dot {r.status.value}"></span>'
            f"{html.escape(r.name)} — {html.escape(r.summary)}"
            f"</summary>"
            f'<div class="cmd">$ {html.escape(r.command)}'
            f"  &nbsp;(exit {r.returncode}, {r.duration_s:.1f}s)</div>"
            f"<pre>{html.escape(body)}</pre>"
            f"</details>"
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>i3xua — andon report</title>
<style>{_CSS}</style>
</head><body>
<h1>i3xua — static-analysis andon</h1>
<div class="meta">{timestamp} · total {total_dur:.1f}s · {len(results)} checks</div>

<div class="kpis">
  <div class="kpi {overall.value}"><b>{overall.value.upper()}</b><span>overall</span></div>
  <div class="kpi green"><b>{n_green}</b><span>passing</span></div>
  <div class="kpi yellow"><b>{n_yellow}</b><span>warnings</span></div>
  <div class="kpi red"><b>{n_red}</b><span>failures</span></div>
</div>

<h2 style="font-size:14px;margin:1rem 0 .5rem;">Board</h2>
{"".join(rows)}

<h2 style="font-size:14px;margin:1.5rem 0 .5rem;">Details</h2>
{"".join(details)}

</body></html>
"""


# ------------------------------------------------------------------ CLI


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="andon-report.html",
        type=Path,
        help="Output HTML file (default: andon-report.html)",
    )
    parser.add_argument(
        "--no-pydeps",
        action="store_true",
        help="Skip pydeps (e.g. when graphviz isn't installed)",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always exit 0 — useful for local runs that want the HTML "
        "report regardless of red findings.",
    )
    args = parser.parse_args()

    checks = _build_checks(with_pydeps=not args.no_pydeps)

    print(f"Running {len(checks)} checks...", file=sys.stderr)
    results: list[CheckResult] = []
    for i, check in enumerate(checks, 1):
        print(f"  [{i}/{len(checks)}] {check.name}...", file=sys.stderr, end="", flush=True)
        result = _run_check(check)
        results.append(result)
        print(
            f"  {result.status.value} ({result.duration_s:.1f}s) — {result.summary}",
            file=sys.stderr,
        )

    out_path = args.out if args.out.is_absolute() else REPO_ROOT / args.out
    out_path.write_text(_render(results), encoding="utf-8")
    overall = _classify_overall(results)
    print(f"\nReport: {out_path}  [overall: {overall.value.upper()}]", file=sys.stderr)

    if args.exit_zero:
        return 0
    return 1 if overall is Status.RED else 0


if __name__ == "__main__":
    raise SystemExit(main())
