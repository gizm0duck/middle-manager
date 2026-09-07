"""Brief lint: the shape a defect brief must have before a worker starts on it.

Across review cycle 4, 25 of 102 worker reflections reported a brief that
stated a cause, mechanism, or current-state fact that was wrong against the
code, and 13 reported scope items that contradicted each other (issue #59).
What made a wrong premise cheap instead of fatal was an expected-discrepancy
table and a plan-note gate; briefs without them lost 15 to 40 minutes per wrong
claim. This module checks that shape. It is a pure function over the brief's
text; ``mm brief lint`` and ``mm dispatch --brief`` print what it finds.

A *defect* brief (one with a Hypotheses/Cause section, or one that speaks of a
defect, a FAIL, or a symptom) gets every check:

1. a Symptom section, before any Hypothesis/Cause/Mechanism/Diagnosis section;
2. a probe on every hypothesis — a sub-bullet or trailing clause that starts with
   ``Probe:``, ``Measure:``, ``Reproduce:`` or ``Check:``;
3. an Expected discrepancies table with at least one row per hypothesis;
4. self-consistency: a scope rule saying never/no/must not and a later
   acceptance or test line naming the same backticked token are pointed at;
5. no absolute ``/tmp`` or ``/private/tmp`` path in an evidence section.

Any other brief gets only checks 4 and 5. The heuristics are deliberately
narrow: a false positive costs the manager's trust in the lint, a miss costs
one more reflection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$")
#: A list item at the left margin (a hypothesis, a scope rule, a discrepancy row).
_TOP_BULLET = re.compile(r"^\s{0,1}(?:[-*+]|\d+[.)])\s+(.*)$")
#: Any list item, however deep (a sub-bullet carrying a probe).
_ANY_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
#: "Probe:" (or the other three) at the start of a line or of a trailing clause.
_PROBE = re.compile(
    r"(?:^|[—–;(]\s*|\.\s+|\s-\s+)[*_]{0,2}(?:probe|measure|reproduce|check)[*_]{0,2}\s*:",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:never|no|must not)\b", re.IGNORECASE)
_BACKTICKED = re.compile(r"`([^`\s]{2,})`")
_TMP_PATH = re.compile(r"(?<![\w./-])(?:/private)?/tmp/[^\s`'\")\]]+")
_DEFECT_WORDS = re.compile(r"\bdefect\b|\bsymptom\b", re.IGNORECASE)
_FAIL_WORD = re.compile(r"\bFAIL\b")

_CAUSE_WORDS = ("hypothes", "cause", "mechanism", "diagnos")
_SCOPE_WORDS = ("scope", "rule", "constraint", "derivation")
_ACCEPTANCE_WORDS = ("acceptance", "test", "verif", "case")


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a brief, at one line (1-based; 0 means the whole file)."""

    line: int
    message: str

    def render(self, path: str | None = None) -> str:
        where = f"{path}:{self.line}" if path else f"line {self.line}"
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class _Line:
    number: int
    text: str
    #: Lowercased headings enclosing this line, outermost first.
    headings: tuple[str, ...]

    def under(self, words: tuple[str, ...]) -> bool:
        return any(word in heading for heading in self.headings for word in words)


def _classified_lines(text: str) -> list[_Line]:
    """Every line with the heading stack that encloses it.

    A ``###`` under a ``## Scope`` still counts as scope; the stack is what makes
    that so, and it resets when a heading of the same or a higher level arrives.
    """
    stack: list[tuple[int, str]] = []
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _HEADING.match(raw)
        if match is not None:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip().lower()))
        lines.append(_Line(number, raw, tuple(h for _, h in stack)))
    return lines


def _own_heading(line: _Line) -> str | None:
    match = _HEADING.match(line.text)
    return match.group(2).strip().lower() if match else None


def _first_heading_line(lines: list[_Line], words: tuple[str, ...]) -> _Line | None:
    for line in lines:
        heading = _own_heading(line)
        if heading is not None and any(w in heading for w in words):
            return line
    return None


def is_defect_brief(text: str) -> bool:
    """A brief about something broken: it names a cause section, a defect, a FAIL, or a symptom."""
    lines = _classified_lines(text)
    if _first_heading_line(lines, _CAUSE_WORDS) is not None:
        return True
    return bool(_DEFECT_WORDS.search(text) or _FAIL_WORD.search(text))


def _hypotheses(lines: list[_Line]) -> list[tuple[_Line, list[_Line]]]:
    """Each hypothesis with the lines that belong to it (sub-bullets, continuations).

    A hypothesis is a left-margin list item under a Hypotheses/Cause heading. A
    cause section written as paragraphs instead of a list counts each paragraph
    as one hypothesis, so prose does not slip past the probe check.
    """
    items: list[tuple[_Line, list[_Line]]] = []
    in_cause = [ln for ln in lines if ln.under(_CAUSE_WORDS) and _own_heading(ln) is None]
    has_bullets = any(_TOP_BULLET.match(ln.text) for ln in in_cause)
    current: list[_Line] | None = None
    previous_blank = True
    for line in in_cause:
        blank = not line.text.strip()
        starts = (
            bool(_TOP_BULLET.match(line.text))
            if has_bullets
            else (not blank and previous_blank and not _TABLE_ROW.match(line.text))
        )
        if starts:
            current = []
            items.append((line, current))
        elif current is not None and not blank:
            current.append(line)
        if has_bullets and blank:
            current = None
        previous_blank = blank
    return items


def _has_probe(head: _Line, body: list[_Line]) -> bool:
    candidates = [head.text, *[ln.text for ln in body]]
    for text in candidates:
        bullet = _ANY_BULLET.match(text)
        stripped = bullet.group(1) if bullet else text.strip()
        if _PROBE.search(stripped):
            return True
    return False


def _discrepancy_rows(lines: list[_Line]) -> tuple[_Line | None, int]:
    heading = _first_heading_line(lines, ("discrepanc",))
    if heading is None:
        return None, 0
    rows = 0
    header_seen = False
    for line in lines:
        if line.number <= heading.number or not line.under(("discrepanc",)):
            continue
        if _own_heading(line) is not None:
            continue
        if _TABLE_RULE.match(line.text):
            continue
        if _TABLE_ROW.match(line.text):
            if header_seen:
                rows += 1
            header_seen = True
            continue
        if _TOP_BULLET.match(line.text):
            rows += 1
    return heading, rows


def _check_symptom_first(lines: list[_Line]) -> list[Finding]:
    symptom = _first_heading_line(lines, ("symptom",))
    cause = _first_heading_line(lines, _CAUSE_WORDS)
    if symptom is None:
        at = cause.number if cause is not None else 1
        return [
            Finding(
                at,
                "no Symptom section — open a defect brief with `## Symptom`: the observed "
                "behaviour and the persisted evidence to read, before any cause",
            )
        ]
    if cause is not None and cause.number < symptom.number:
        return [
            Finding(
                cause.number,
                f"`{_own_heading(cause)}` comes before `Symptom` (line {symptom.number}) — "
                "state what was observed before what might explain it",
            )
        ]
    return []


def _check_probes(hypotheses: list[tuple[_Line, list[_Line]]]) -> list[Finding]:
    return [
        Finding(
            head.number,
            "hypothesis has no probe — add a sub-bullet or trailing clause starting "
            '"Probe:", "Measure:", "Reproduce:" or "Check:" naming the query, log filter, '
            "or test that would confirm or refute it",
        )
        for head, body in hypotheses
        if not _has_probe(head, body)
    ]


def _check_discrepancies(
    lines: list[_Line], hypotheses: list[tuple[_Line, list[_Line]]]
) -> list[Finding]:
    heading, rows = _discrepancy_rows(lines)
    wanted = len(hypotheses)
    if heading is None:
        cause = _first_heading_line(lines, _CAUSE_WORDS)
        return [
            Finding(
                cause.number if cause is not None else 1,
                "no Expected discrepancies table — add `## Expected discrepancies` with one "
                f"row per hypothesis ({wanted} here): the claim, and what to do if the probe "
                "comes back negative",
            )
        ]
    if rows < wanted:
        return [
            Finding(
                heading.number,
                f"Expected discrepancies has {rows} row(s) for {wanted} hypothesis(es) — "
                "every hypothesis needs a row saying what to do when it is wrong",
            )
        ]
    return []


def _check_self_consistency(lines: list[_Line]) -> list[Finding]:
    rules: dict[str, _Line] = {}
    for line in lines:
        if not line.under(_SCOPE_WORDS) or _own_heading(line) is not None:
            continue
        if not _NEGATION.search(line.text):
            continue
        for token in _BACKTICKED.findall(line.text):
            rules.setdefault(token, line)
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for line in lines:
        if not line.under(_ACCEPTANCE_WORDS) or _own_heading(line) is not None:
            continue
        for token in _BACKTICKED.findall(line.text):
            rule = rules.get(token)
            if rule is None or rule.number >= line.number or (token, line.number) in reported:
                continue
            reported.add((token, line.number))
            findings.append(
                Finding(
                    line.number,
                    f"rule (line {rule.number}) and required case name the same token "
                    f"`{token}`; check they agree",
                )
            )
    return findings


def _check_evidence_paths(lines: list[_Line]) -> list[Finding]:
    findings: list[Finding] = []
    for line in lines:
        if not line.under(("evidence",)):
            continue
        for path in _TMP_PATH.findall(line.text):
            findings.append(
                Finding(
                    line.number,
                    f"evidence points at {path} — a temp directory does not survive the "
                    "session; put evidence inside the worktree and name that path",
                )
            )
    return findings


def lint_brief(text: str) -> list[Finding]:
    """Every finding for ``text``, in line order.

    Empty when the brief has the shape a worker can be right about.
    """
    lines = _classified_lines(text)
    findings: list[Finding] = []
    if is_defect_brief(text):
        hypotheses = _hypotheses(lines)
        findings += _check_symptom_first(lines)
        findings += _check_probes(hypotheses)
        if hypotheses:
            findings += _check_discrepancies(lines, hypotheses)
    findings += _check_self_consistency(lines)
    findings += _check_evidence_paths(lines)
    return sorted(findings, key=lambda f: f.line)


def render(findings: list[Finding], path: str | None = None) -> str:
    """The findings as one line each, ready to print."""
    return "\n".join(f.render(path) for f in findings)
