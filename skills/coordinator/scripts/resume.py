"""
What fleet_status.py --resume prints around the status table: the backlog
sections a coordinator reads again after compaction or a restart, and
whether the domain's fleet watcher runs.
"""

from __future__ import annotations

from pathlib import Path

import backlog
import fleet
from watch_lock import watcher_running

SECTIONS_READ_AGAIN = ("## Boundary", "## Standing yes", "## Open")


def backlog_to_read_again(domain: str) -> str:
    """The sections a coordinator reads again, or their sizes when the text outside Done is over its limit."""
    path = fleet.BACKLOG_DIR / f"{domain}-backlog.md"
    text = path.read_text()
    sections = backlog.sections(text)
    if backlog.bytes_outside_done(text) > backlog.LIMIT_OUTSIDE_DONE:
        sizes = "\n".join(f"{len(body.encode()) / backlog.BYTES_PER_KB:8.1f} KB  {heading}"
                          for heading, body in sections.items() if heading)
        return (f"{path} is over its limit, so it is not printed: {'; '.join(backlog.parts_over_their_limit(text))}.\n"
                f"{sizes}\nRead its Boundary and Standing yes sections yourself, and search Open for each delegate "
                f"and ask you handle. {backlog.what_to_do_when_over(text)}")
    return f"{path}\n\n" + "\n\n".join(f"{heading}\n{sections[heading].strip()}" for heading in SECTIONS_READ_AGAIN
                                       if heading in sections)


def watcher_line(domain: str) -> str:
    state = fleet.WATCH_DIR / f"{domain}.json"
    try:
        pid = watcher_running(state)
    except fleet.CheckError as error:
        return f"Whether a fleet watcher runs for {domain} could not be checked: {error}."
    if pid:
        return f"A fleet watcher runs for {domain}, pid {pid}."
    return (f"No fleet watcher runs for {domain} on {state}. Arm it as the Watch section of "
            f"{Path(fleet.SCRIPTS).parent / 'fleet.md'} says.")


def resume_text(domain: str, status: str) -> str:
    return f"{backlog_to_read_again(domain)}\n\n{status}\n\n{watcher_line(domain)}"
