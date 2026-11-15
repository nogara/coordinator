#!/usr/bin/env python3
"""
Check a domain's backlog: its size, and what a trim must not lose.

  backlog_check.py <domain>
  backlog_check.py <domain> --before <file> [--ledger <file>]
  backlog_check.py <domain> --since <commit> [--ledger <file>]
  backlog_check.py <domain> --cut-done

The first form prints the size of each section against the limits in the
config. The second compares the backlog with a copy saved before a trim.
The third does the same with the backlog's text at <commit>, when Git
tracks the file. A LOST line is something the trim must put back:

  - an entry of Standing yes
  - the session id of a live delegate of this domain, which the cap check
    and the status report find in the Open section
  - an open pull request from your GitHub account that the old text named
  - the user's words that the old text quoted outside Done and the new
    text quotes nowhere. The check finds a quote after a date, as in
    `(03-14: "Drop it")`, a quoted answer after a quoted question, and a
    quote after a name in the config's user_names
  - a sentence outside Done that held an open ask and is gone: one that
    starts `Ask:`, says "open before restart", or matches a pattern in
    the config's open_ask_patterns

The ledger is the trim's record of what it dropped on purpose and why. A
quote is not LOST for each time the ledger quotes exactly those words, and
a sentence for each time the ledger repeats it. The ledger clears no other
kind of LOST line.

A CHECK line is something to put back unless it is finished or answered:

  - the session id of a delegate this domain ended with delegate_end.py
  - a repo the backlog no longer links or names

--cut-done deletes the oldest lines of Done until Done is under its limit.
When Git tracks the backlog with no uncommitted change, Git keeps the lines
it deletes. Otherwise it first saves them to <backlog_dir>/trims/.

Exit status: 0 within the limits with no LOST line, 1 over a limit or a
LOST line, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog  # noqa: E402
import fleet  # noqa: E402
from fleet import CONFIG, CheckError  # noqa: E402

QUOTED = r"[\"“]([^\"“”]{2,})[\"”]"
QUOTE_AFTER_A_DATE = re.compile(r"(?<![\d-])(?:\d{4}-)?\d{1,2}-\d{1,2}(?![\d-])[^\"“”\n.]{0,20}?[:,]\s*" + QUOTED)
ANSWER_AFTER_A_QUESTION = re.compile(r"\?[\"”][:,]\s*" + QUOTED)
WORDS_BETWEEN_A_NAME_AND_ITS_QUOTE = r"(?:[^\"“”\n.]|(?<=\d)\.(?=\d)){0,40}?[:,]?\s*"
ANY_QUOTED_TEXT = re.compile(QUOTED)
STARTS_AN_ASK = re.compile(r"^(?:[-*+]|\d+[.)])?\s*(?:\*\*)?Ask:")
OPEN_BEFORE_RESTART = re.compile(r"\bopen before restart\b", re.IGNORECASE)
END_OF_SENTENCE = re.compile(r"(?<=[.?!])[\"”)*]*\s+(?=[-*`(\"“A-Z0-9])")
A_DATE_NEAR_THE_START = re.compile(r"(?<![\d#-])(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?![\d-])")
CHARACTERS_SEARCHED_FOR_A_DATE = 120
DATE_OF_A_DONE_LINE = re.compile(r"^- (?:\*\*)?(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\b")
TRIMS_DIR = fleet.BACKLOG_DIR / "trims"


def configured_patterns(key: str, wrap) -> list[re.Pattern]:
    try:
        return [re.compile(wrap(pattern), re.IGNORECASE) for pattern in CONFIG[key]]
    except (re.error, TypeError) as error:
        raise CheckError(f"{key} in {fleet.CONFIG_FILE} holds a pattern that is not a regular expression: {error}")


def quote_patterns() -> list[re.Pattern]:
    names = configured_patterns("user_names", lambda name: rf"(?<!\w)(?:{name})(?!\w)" + WORDS_BETWEEN_A_NAME_AND_ITS_QUOTE + QUOTED)
    return [QUOTE_AFTER_A_DATE, ANSWER_AFTER_A_QUESTION, *names]


def collapsed(text: str) -> str:
    return " ".join(text.split())


def quotes_of_the_user(text: str, patterns: list[re.Pattern]) -> list[str]:
    found = {(match.start(1), collapsed(match.group(1))) for pattern in patterns for match in pattern.finditer(text)}
    return [quote for _, quote in sorted(found)]


def sentences(text: str) -> list[str]:
    """Each sentence of each list item or paragraph, so that one changed sentence in a long item reads as one loss."""
    blocks: list[list[str]] = [[]]
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("- ", "* ", "#")):
            blocks.append([])
        blocks[-1].append(line)
    return [s for block in blocks for s in END_OF_SENTENCE.split(collapsed(" ".join(block))) if s]


def holds_an_open_ask(sentence: str, extra: list[re.Pattern]) -> bool:
    return bool(STARTS_AN_ASK.match(sentence) or OPEN_BEFORE_RESTART.search(sentence)
                or any(pattern.search(sentence) for pattern in extra))


def done_cut_to_its_limit(text: str, today: dt.date) -> tuple[str, list[str]]:
    """The backlog with the oldest lines of Done deleted until Done is under its limit, and the lines deleted.

    A date with no year is in the twelve months up to tomorrow. A line with no date
    near its start takes the date of the nearest dated line above it.
    """
    tomorrow = today + dt.timedelta(days=1)
    done = fleet.section_text(text, "## Done")
    lines = done.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("- ")]
    items = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        dated = DATE_OF_A_DONE_LINE.match(lines[start]) or A_DATE_NEAR_THE_START.search(lines[start][:CHARACTERS_SEARCHED_FOR_A_DATE])
        written = None
        if dated:
            year, month, day = dated.groups()
            month, day = int(month), int(day)
            if 1 <= month <= 12 and 1 <= day <= 31:
                # A line written late in the day can carry tomorrow's date, in UTC.
                year = int(year) if year else tomorrow.year - ((month, day) > (tomorrow.month, tomorrow.day))
                written = (year, month, day)
        items.append(dict(written=written, start=start, end=end))
    for position, item in enumerate(items):
        if item["written"] is None:
            neighbours = [other["written"] for other in items[:position][::-1] + items[position + 1:] if other["written"]]
            item["written"] = neighbours[0] if neighbours else (0, 0, 0)
    size = len(done.encode())
    deleted: list[int] = []
    for item in sorted(items, key=lambda item: item["written"]):
        if size <= backlog.LIMIT_OF_DONE:
            break
        size -= len("\n".join(lines[item["start"]:item["end"]]).encode()) + 1
        deleted += range(item["start"], item["end"])
    if not deleted:
        return text, []
    gone = set(deleted)
    kept = "\n".join(line for i, line in enumerate(lines) if i not in gone).rstrip("\n") + "\n"
    return text.replace(done, kept, 1), [lines[i] for i in sorted(deleted)]


def git_keeps(path: Path) -> bool:
    """Git tracks the file and it has no uncommitted change."""
    folder = str(path.parent)
    tracked = fleet.git(folder, "ls-files", "--error-unmatch", path.name) is not None
    return tracked and fleet.git(folder, "diff", "--quiet", "HEAD", "--", path.name) is not None


def text_at_commit(path: Path, commit: str) -> str:
    top = fleet.git(str(path.parent), "rev-parse", "--show-toplevel")
    if top is None:
        raise CheckError(f"{path} is in no Git repo, so --since cannot read its earlier text. "
                         "Pass --before <file> with a copy saved before the trim")
    relative = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    text = fleet.git(top, "show", f"{commit}:{relative}")
    if text is None:
        raise CheckError(f"no {relative} at commit {commit} in {top}")
    return text


def entries(section: str) -> list[str]:
    """Each top-level list item or paragraph of a section, with its whitespace collapsed."""
    found: list[list[str]] = []
    for line in section.splitlines():
        starts_one = line.startswith("- ") or not line.strip()
        if starts_one or not found:
            found.append([])
        found[-1].append(line)
    return [text for text in (collapsed(" ".join(lines)) for lines in found) if text]


def compared_with(herd: fleet.Fleet, domain: str, before: str, after: str, ledger: str = "") -> tuple[list[str], list[str]]:
    """What the trim lost, and what to check that it meant to drop."""
    lost, check = [], []
    listed = collapsed(ledger)
    quoted_in_ledger = [collapsed(quote) for quote in ANY_QUOTED_TEXT.findall(ledger)]
    standing_yes_after = collapsed(fleet.section_text(after, "## Standing yes"))
    for entry in entries(fleet.section_text(before, "## Standing yes")):
        if entry not in standing_yes_after:
            lost.append(f"Standing yes entry: {entry[:120]}")

    outside_done_before = before.replace(fleet.section_text(before, "## Done"), "")
    open_after = fleet.section_text(after, "## Open")

    def dropped_from_open(session: str) -> bool:
        short = session[:fleet.SHORT_SESSION_ID]
        return len(short) == fleet.SHORT_SESSION_ID and short in outside_done_before and short not in open_after

    for member in herd.members_of(domain):
        session = fleet.session_of(member["agent"])
        if dropped_from_open(session):
            lost.append(f"session of the live delegate {member['agent']['name']}, missing from Open: {session}")
    for record in fleet.ended_records(domain):
        if dropped_from_open(record["session"]):
            check.append(f"session of the ended delegate {record['name']}, missing from Open: {record['session']}. "
                         "Keep its handle unless its task finished")

    # A copy that survives elsewhere, or one ledger line, must not excuse every dropped copy of the same words.
    patterns = quote_patterns()
    quoted_before = quotes_of_the_user(outside_done_before, patterns)
    quoted_after = quotes_of_the_user(after, patterns)
    for quote in dict.fromkeys(quoted_before):
        gone = quoted_before.count(quote) - quoted_after.count(quote) - quoted_in_ledger.count(quote)
        if gone > 0:
            lost.append(f"the user's words, {gone} of {quoted_before.count(quote)} places: \"{quote}\"")
    extra = configured_patterns("open_ask_patterns", lambda pattern: pattern)
    asks_before = [s for s in sentences(outside_done_before) if holds_an_open_ask(s, extra)]
    kept = collapsed(after)
    for sentence in dict.fromkeys(asks_before):
        gone = asks_before.count(sentence) - kept.count(sentence) - listed.count(sentence)
        if gone > 0:
            lost.append(f"sentence that held an open ask: {sentence}")

    repos = sorted(fleet.backlog_repos(before, herd.checkouts))
    open_numbers = {slug: fleet.open_pull_request_numbers(slug) for slug in repos}
    for slug, number in sorted(backlog.pull_requests_named(before, open_numbers) - backlog.pull_requests_named(after, open_numbers)):
        lost.append(f"open pull request: {slug}#{number}")
    for slug in sorted(set(repos) - fleet.backlog_repos(after, herd.checkouts)):
        check.append(f"repo the backlog no longer links or names: {slug}. When the domain owns it, name its checkout "
                     "by its path in Boundary, or the fleet scripts stop counting its pull requests")
    return lost, check


def cut_done(path: Path, domain: str, text: str) -> str:
    after, deleted = done_cut_to_its_limit(text, dt.date.today())
    if not deleted:
        print("deleted no line of Done")
        return text
    kept_by = "Git keeps them at HEAD"
    if not git_keeps(path):
        TRIMS_DIR.mkdir(parents=True, exist_ok=True)
        saved = TRIMS_DIR / f"{domain}-done-cut-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        saved.write_text("\n".join(deleted) + "\n")
        kept_by = f"saved them to {saved}"
    path.write_text(after)
    print(f"deleted the {sum(line.startswith('- ') for line in deleted)} oldest lines of Done; {kept_by}")
    return after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain")
    earlier = parser.add_mutually_exclusive_group()
    earlier.add_argument("--before", metavar="FILE", help="a copy of the backlog saved before the trim")
    earlier.add_argument("--since", metavar="COMMIT", help="the commit made just before the trim")
    parser.add_argument("--ledger", metavar="FILE", help="the trim's record of what it dropped on purpose")
    parser.add_argument("--cut-done", action="store_true", help="delete the oldest lines of Done until it is under its limit")
    parser.add_argument("--terminals", metavar="FILE", help="read a saved JSON list of `hc get` output instead of running hc")
    args = parser.parse_args()
    if args.ledger and not (args.before or args.since):
        parser.error("--ledger needs --before <file> or --since <commit>")
    try:
        path = fleet.BACKLOG_DIR / f"{args.domain}-backlog.md"
        if not path.is_file():
            raise CheckError(f"no backlog at {path}")
        after = path.read_text()
        if args.cut_done:
            after = cut_done(path, args.domain, after)
        for heading, text in backlog.sections(after).items():
            if heading or text.strip():
                print(f"{len(text.encode()) / backlog.BYTES_PER_KB:8.1f} KB  {heading or '(before the first heading)'}")
        over = backlog.parts_over_their_limit(after)
        for part in over:
            print(f"OVER  {part}")
        lost = []
        if args.before or args.since:
            before = Path(args.before).expanduser().read_text() if args.before else text_at_commit(path, args.since)
            ledger = Path(args.ledger).expanduser().read_text() if args.ledger else ""
            lost, check = compared_with(fleet.Fleet(args.terminals), args.domain, before, after, ledger)
            for item in lost:
                print(f"LOST  {item}")
            for item in check:
                print(f"CHECK  {item}")
            print(f"compared with {args.before or args.since}: {len(lost)} lost, {len(check)} to check")
    except (CheckError, OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"backlog_check: could not run: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 1 if over or lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
