"""Moves a worktree's ignored files to a parked folder, leaving out build output.

sweep.py removes worktrees with these; nothing here deletes a parked folder.
"""
import os
import re
import shutil
import time

PARKED_AT = ".parked-at"
PARKED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BUILD_OUTPUT = {
    "node_modules", ".derived-data", "dist", "build", ".next", "target", ".turbo", ".build",
    "coverage", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".swiftpm", "Pods", ".expo", "out", ".cache", ".vercel", "test-results", "playwright-report",
    ".svelte-kit", ".parcel-cache", "storybook-static", ".source", "generated", ".DS_Store",
    ".eslintcache", "DerivedData",
}
BUILD_FILE = re.compile(r"(\.log|\.tsbuildinfo|next-env\.d\.ts|\.pyc)$")


def is_build_output(rel):
    """Files a build makes again, which removing a worktree drops instead of parking."""
    return any(part in BUILD_OUTPUT for part in rel.split("/")) or bool(BUILD_FILE.search(rel))


def mark_parked(folder):
    with open(os.path.join(folder, PARKED_AT), "w") as f:
        f.write(time.strftime(PARKED_AT_FORMAT, time.gmtime()) + "\n")


def split_build_output(worktree, rel, park, drop):
    """git lists a folder whose contents are all ignored as one entry, and such
    a folder can hold build output, as pkg/ holds pkg/node_modules/. A .scratch
    folder moves whole: the build-output names include `out` and `.log`."""
    if ".scratch" in rel.split("/"):
        park.append(rel)
        return
    if is_build_output(rel):
        drop.append(rel)
        return
    full = os.path.join(worktree, rel)
    holds_build_output = os.path.isdir(full) and not os.path.islink(full) and any(
        is_build_output(name)
        for _, subdirs, files in os.walk(full) for name in subdirs + files
    )
    if not holds_build_output:
        park.append(rel)
        return
    for name in sorted(os.listdir(full)):
        split_build_output(worktree, f"{rel}/{name}", park, drop)


def park_ignored(worktree, repo, paths):
    """Moves each path to the same place under parked/<worktree name>, and
    returns the folder and the moves, or moves them back and raises."""
    parked = os.path.join(repo, ".scratch", "parked")
    os.makedirs(parked, exist_ok=True)
    destination = os.path.join(parked, os.path.basename(worktree))
    if os.path.exists(destination):
        destination += time.strftime("-%Y%m%d-%H%M%S")
    moves = []
    try:
        for rel in paths:
            source = os.path.join(worktree, rel)
            target = os.path.join(destination, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
            moves.append((source, target))
        mark_parked(destination)
    except OSError as error:
        try:
            unpark(destination, moves)
        except OSError as rollback_error:
            raise OSError(f"{error}; moving the files back also failed, so some are still in {destination}: {rollback_error}")
        raise OSError(f"{error}; the files moved back, so nothing changed")
    return destination, moves


def unpark(destination, moves):
    """Moves the files back, then deletes the parking record and the folders
    the moves left empty. rmdir refuses a folder that still holds a file."""
    for source, target in reversed(moves):
        shutil.move(target, source)
    record = os.path.join(destination, PARKED_AT)
    if os.path.exists(record):
        os.remove(record)
    for root, _, _ in os.walk(destination, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass
