"""
What a Claude Code session still runs after its turn ended, as herdr-core
shows it.
"""
import re

TASK_KINDS = r"(?:background )?(?:shell|monitor|task|agent|subagent|teammate)s?"
TURN_OVER_WITH_TASKS_RUNNING = re.compile(rf"^\d+ {TASK_KINDS}(?:,? (?:and )?\d+ {TASK_KINDS})* still running$")
TASK_TITLE_CHARS = 60


def still_running(terminal: dict) -> str:
    """What still runs after the agent's turn ended, from a terminal as `hc get` prints it, or "" mid-turn.

    Claude Code keeps reporting the status working while a background shell,
    monitor or subagent it started runs, and sets the status message to a
    count such as "1 shell still running". Each task it still runs is a
    record of the terminal, titled with its command or description.
    """
    status = terminal.get("status") or {}
    message = status.get("msg") or ""
    if status.get("state") != "working" or not TURN_OVER_WITH_TASKS_RUNNING.match(message):
        return ""
    titles = [record["title"][:TASK_TITLE_CHARS] for record in (terminal.get("records") or {}).values()
              if record.get("state") == "working" and record.get("title")]
    return f"{message}: {'; '.join(titles)}" if titles else message
