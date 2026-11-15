"""
A backlog's sections, its size limits, and the open pull requests it names.

The coordinator reads everything outside Done at each start and after each
compaction, so that part has a limit, backlog_limit_kb in the config, and
Done has its own, done_limit_kb. A backlog whose text outside Done is over
trim_by_delegate_over_kb is too large for its coordinator to read whole, so
one delegate trims it.
"""

from __future__ import annotations

from pathlib import Path

import fleet
from fleet import CONFIG

BYTES_PER_KB = 1000
LIMIT_OUTSIDE_DONE = CONFIG["backlog_limit_kb"] * BYTES_PER_KB
LIMIT_OF_DONE = CONFIG["done_limit_kb"] * BYTES_PER_KB
TRIMMED_BY_A_DELEGATE_OVER = CONFIG["trim_by_delegate_over_kb"] * BYTES_PER_KB
TRIM_STEPS = Path(fleet.SCRIPTS).parent / "backlog-trim.md"


def sections(text: str) -> dict[str, str]:
    """Each `## ` section's text by its heading, in file order. Text before the first heading is under ""."""
    found: dict[str, list[str]] = {"": []}
    heading = ""
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if not in_fence and fleet.SECTION_HEADING.match(line):
            heading = line.rstrip()
            found.setdefault(heading, [])
        else:
            found[heading].append(line)
    return {heading: "\n".join(lines) for heading, lines in found.items()}


def bytes_outside_done(text: str) -> int:
    return len(text.encode()) - len(sections(text).get("## Done", "").encode())


def parts_over_their_limit(text: str) -> list[str]:
    """One phrase for each part of a backlog that is over its size limit."""
    done_bytes = len(sections(text).get("## Done", "").encode())
    outside_done = bytes_outside_done(text)
    over = []
    if outside_done > LIMIT_OUTSIDE_DONE:
        over.append(f"everything outside Done is {outside_done / BYTES_PER_KB:.1f} KB, "
                    f"limit {LIMIT_OUTSIDE_DONE // BYTES_PER_KB}")
    if done_bytes > LIMIT_OF_DONE:
        over.append(f"Done is {done_bytes / BYTES_PER_KB:.1f} KB, limit {LIMIT_OF_DONE // BYTES_PER_KB}")
    return over


def what_to_do_when_over(text: str) -> str:
    """The instruction for a backlog over a limit: trim it, or have a delegate trim one too large to read."""
    if bytes_outside_done(text) > TRIMMED_BY_A_DELEGATE_OVER:
        return ("Do not trim it yourself: reading it would fill your context. Start one delegate to trim it, "
                f"as {TRIM_STEPS} says; read that file first.")
    return f"Trim it as {TRIM_STEPS} says; read that file first."


def pull_requests_named(text: str, open_numbers: dict[str, list[int]]) -> set[tuple[str, int]]:
    """The open pull requests the text names, by fleet_status.py's rule: a bare #<number> counts only
    when no other of these repos has that number open."""
    every_number = [number for numbers in open_numbers.values() for number in numbers]
    return {(slug, number) for slug, numbers in open_numbers.items() for number in numbers
            if fleet.names_pull_request(text, slug, number, every_number.count(number) > 1)}
