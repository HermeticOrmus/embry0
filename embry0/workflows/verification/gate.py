"""Base-vs-head comparison for the deterministic test gate (#299).

Pure: takes the runs ``runner.py`` produced and decides. No I/O, no LLM.

Granularity, stated plainly: the unit of comparison is the *test target*
(one configured command). When a well-known runner's failure lines can be
read from both sides (pytest, Maven Surefire, vitest/jest, ``go test``) the
comparison is by test name — so a new failure is caught even when the base
is already red. Otherwise it falls back to exit codes per target, where a
base that is red too (a flapping baseline, or a base that does not build)
cannot be told apart from head and is recorded, not blamed on the change.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Side = Literal["base", "head"]
RunOutcome = Literal["passed", "failed", "timed_out", "errored"]
TargetStatus = Literal["passed", "new_failures", "preexisting_failures", "flaky", "error"]
# What a verdict was decided from. "tests" is the only CONCLUSIVE one for
# "nothing new": "files" can only see a whole file turn red, and "exit_code"
# not even that.
Comparison = Literal["none", "tests", "files", "exit_code"]
Granularity = Literal["test", "file"]
GateVerdict = Literal["passed", "new_failures", "unstable", "error", "not_configured", "no_matching_targets"]

SCHEMA_VERSION = 1
# Names kept per list in the stored report — prose for humans, safe to cap.
MAX_NAMES = 50

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# (regex, granularity). A capture is only worth comparing when it names a
# TEST; a pattern that can only name a file says so, and the comparison is
# downgraded rather than trusted (review of #341).
_FAILURE_LINE_RES: tuple[tuple[re.Pattern[str], Granularity], ...] = (
    # pytest short test summary: "FAILED tests/x.py::test_a[case one] - Error".
    # The id runs to the first " - " (pytest's separator before the message),
    # never to the first space: "test_a[case one]" and "test_a[case two]" are
    # different tests and must not collide. An id containing " - " itself is
    # cut at it — rare, and it errs toward a coarser name, never a wrong one.
    (re.compile(r"^(?:FAILED|ERROR) (\S+::.+?)(?: - .*)?$"), "test"),
    # Maven Surefire result block: "[ERROR]   FooTest.bar:42 expected ..."
    (re.compile(r"^\[ERROR\] {2,}((?:[\w$]+\.)+[\w$\[\]]+?)(?::\d+)?(?: |$)"), "test"),
    # go test: "--- FAIL: TestName (0.01s)"
    (re.compile(r"^\s*--- FAIL: (\S+)"), "test"),
    # vitest: " FAIL  src/x.test.tsx > suite > name" — file AND test path.
    (re.compile(r"^\s*FAIL\s+(\S+\.[A-Za-z0-9]+ *>.*\S)\s*$"), "test"),
    # jest and vitest's file-level line: " FAIL  src/x.test.tsx". A whole
    # file, so it cannot prove that nothing new broke inside it.
    (re.compile(r"^\s*FAIL\s+(\S+\.[A-Za-z0-9]+)\s*$"), "file"),
)
_SUMMARY_RES = (
    re.compile(r"^=+ (.*\b(?:passed|failed|errors?)\b.* in [\d.]+s.*?) =+$"),
    re.compile(r"(Tests run: \d+, Failures: \d+, Errors: \d+, Skipped: \d+)\s*$"),
    re.compile(r"^\s*(Tests:?\s+\d+ .*)$"),
)


@dataclass(frozen=True)
class ParsedOutput:
    """What could be read out of one run's output."""

    failed_tests: list[str] | None
    summary: str | None
    granularity: Granularity | None


def parse_test_output(output: str) -> ParsedOutput:
    """Failed names, the summary line, and how precise the names are.

    ``failed_tests is None`` means no known failure line was seen — the
    caller then compares exit codes. ``granularity`` is ``"file"`` when any
    matched line could only name a file: one such line makes the whole set
    file-granular, because a set that mixes the two cannot be compared as
    tests either. Best-effort by design, but never optimistic: a pattern
    that cannot see individual tests must not be read as if it could.
    """
    names: set[str] = set()
    summary: str | None = None
    granularity: Granularity | None = None
    for raw in output.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        for rx, kind in _FAILURE_LINE_RES:
            found = rx.match(line)
            if found:
                names.add(found.group(1).strip())
                if kind == "file" or granularity == "file":
                    granularity = "file"
                else:
                    granularity = "test"
                break
        for rx in _SUMMARY_RES:
            found = rx.search(line)
            if found:
                summary = found.group(1).strip()
                break
    if not names:
        return ParsedOutput(None, summary, None)
    return ParsedOutput(sorted(names), summary, granularity)


@dataclass(frozen=True)
class TargetRun:
    """One execution of one target's command on one side."""

    target: str
    side: Side
    attempt: int
    outcome: RunOutcome
    exit_code: int | None
    duration_ms: int
    failed_tests: list[str] | None
    summary: str | None
    output_tail: str
    # How precise ``failed_tests`` is. Defaulted so a report stored before
    # the field existed still loads, and defaulted to the precise value
    # because every parser that predates it named tests.
    name_granularity: Granularity = "test"

    @property
    def red(self) -> bool:
        return self.outcome in ("failed", "timed_out")

    @property
    def names(self) -> set[str] | None:
        """Failed test names usable for a by-name comparison.

        An empty list on a red run (a head that no longer compiles prints no
        FAILED lines) is NOT "no failures" — it is unusable.
        """
        if self.outcome != "failed" or not self.failed_tests:
            return None
        return set(self.failed_tests)

    @property
    def file_granular(self) -> bool:
        """The names name files, so they cannot prove nothing new broke."""
        return self.name_granularity == "file"


@dataclass(frozen=True)
class TargetVerdict:
    target: str
    status: TargetStatus
    comparison: Comparison = "none"
    new_failures: list[str] = field(default_factory=list)
    preexisting: list[str] = field(default_factory=list)
    flaky: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "new_failures"


def _capped(names: set[str] | list[str]) -> list[str]:
    return sorted(names)[:MAX_NAMES]


def compare_target(head_runs: list[TargetRun], base: TargetRun | None) -> TargetVerdict:
    """Decide one target from its head run(s) (first run, then re-runs) and
    its base run (``None`` when the base was not run)."""
    first = head_runs[0]
    target = first.target
    if first.outcome == "errored":
        return TargetVerdict(target, "error", note="the command could not be run on head")
    if first.outcome == "passed":
        fixed = _capped(base.names or set()) if base is not None and base.red else []
        note = "fails on base, passes on head" if base is not None and base.red else ""
        return TargetVerdict(target, "passed", fixed=fixed, note=note)

    # Head is red. A re-run that goes green means the failure is not stable.
    reruns = head_runs[1:]
    if any(r.outcome == "passed" for r in reruns):
        return TargetVerdict(
            target,
            "flaky",
            flaky=_capped(first.names or set()),
            note="failed once on head and passed when re-run",
        )
    if base is None or base.outcome == "errored":
        return TargetVerdict(target, "error", note="head fails and there is no usable base run to compare with")

    red_heads = [r for r in head_runs if r.red]
    head_name_sets = [r.names for r in red_heads]
    by_name = all(names is not None for names in head_name_sets)
    stable: set[str] = set.intersection(*[n for n in head_name_sets if n is not None]) if by_name else set()
    seen: set[str] = set.union(*[n for n in head_name_sets if n is not None]) if by_name else set()
    unstable = _capped(seen - stable)

    if base.outcome == "passed":
        if by_name and not stable:
            return TargetVerdict(
                target, "flaky", comparison="tests", flaky=unstable, note="no test failed on every head attempt"
            )
        return TargetVerdict(
            target,
            "new_failures",
            comparison="tests" if by_name else "exit_code",
            new_failures=_capped(stable),
            flaky=unstable,
        )

    # Base is red too.
    base_names = base.names
    if by_name and base_names is not None:
        new = stable - base_names
        # A file-granular set can still show a WHOLE FILE newly failing, but
        # it can never show that nothing new broke inside a file that was
        # already red — so the absence of `new` proves nothing here, and the
        # verdict says which question it actually answered (review of #341).
        coarse = base.file_granular or any(r.file_granular for r in red_heads)
        comparison: Comparison = "files" if coarse else "tests"
        status: TargetStatus = "new_failures" if new else "preexisting_failures"
        note = ""
        if coarse and not new:
            note = (
                "base fails too and the test runner named only files, so a new failure inside "
                "an already-failing file cannot be ruled out"
            )
        return TargetVerdict(
            target,
            status,
            comparison=comparison,
            new_failures=_capped(new),
            preexisting=_capped(stable & base_names),
            flaky=unstable,
            note=note,
        )
    return TargetVerdict(
        target,
        "preexisting_failures",
        comparison="exit_code",
        note="base fails too and test names could not be read, so new failures cannot be told from old ones",
    )


@dataclass(frozen=True)
class GateReport:
    """The gate's machine-readable result — the shape auto-merge (#310) reads."""

    verdict: GateVerdict
    targets: list[TargetVerdict] = field(default_factory=list)
    runs: list[TargetRun] = field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    base_ref: str = ""
    note: str = ""

    @property
    def blocking(self) -> bool:
        return self.verdict == "new_failures"

    @classmethod
    def without_runs(cls, verdict: GateVerdict, *, note: str = "") -> GateReport:
        return cls(verdict=verdict, note=note)

    def runs_for(self, side: Side) -> list[TargetRun]:
        return [r for r in self.runs if r.side == side]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": self.verdict,
            "blocking": self.blocking,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "note": self.note,
            "targets": [{**asdict(t), "blocking": t.blocking} for t in self.targets],
            "runs": [asdict(r) for r in self.runs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateReport:
        targets = [TargetVerdict(**{k: v for k, v in t.items() if k != "blocking"}) for t in data.get("targets") or []]
        runs = [TargetRun(**r) for r in data.get("runs") or []]
        return cls(
            verdict=data["verdict"],
            targets=targets,
            runs=runs,
            base_sha=data.get("base_sha", ""),
            head_sha=data.get("head_sha", ""),
            base_ref=data.get("base_ref", ""),
            note=data.get("note", ""),
        )


def build_report(
    *,
    targets: list[TargetVerdict],
    runs: list[TargetRun],
    base_sha: str,
    head_sha: str,
    base_ref: str,
    note: str = "",
) -> GateReport:
    """Fold per-target verdicts: new failures > error > unstable > passed."""
    statuses = {t.status for t in targets}
    verdict: GateVerdict
    if "new_failures" in statuses:
        verdict = "new_failures"
    elif "error" in statuses:
        verdict = "error"
    elif statuses & {"preexisting_failures", "flaky"}:
        verdict = "unstable"
    else:
        verdict = "passed"
    return GateReport(
        verdict=verdict,
        targets=targets,
        runs=runs,
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=base_ref,
        note=note,
    )
