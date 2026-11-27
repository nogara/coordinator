#!/usr/bin/env python3
"""The fleet's terminals from a shell, on whichever runtime the user's config names.

  fleet_term.py ls                       {"terminals": [...]}
  fleet_term.py get ADDR                 the terminal as JSON; exit 3 when it is gone
  fleet_term.py send ADDR TEXT           type TEXT and Enter, tagged with the sender
  fleet_term.py read ADDR [--lines N]    the terminal's screen text
  fleet_term.py keys ADDR KEY...         press keys, such as escape
  fleet_term.py label ADDR k=v...        set labels; `k=` removes k
  fleet_term.py self                     {"address": ...} of the terminal this runs in; exit 1 outside one
"""

from __future__ import annotations

import argparse
import json
import sys

import terminals
from check_error import CheckError

NOT_FOUND = 3


def parse_labels(pairs: list[str]) -> dict[str, str]:
    labels = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not key or not separator:
            raise CheckError(f"{pair!r} is not k=v")
        labels[key] = value
    return labels


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fleet_term.py")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("ls")
    verbs.add_parser("self")
    verbs.add_parser("get").add_argument("address")
    send = verbs.add_parser("send")
    send.add_argument("address")
    send.add_argument("text")
    read = verbs.add_parser("read")
    read.add_argument("address")
    read.add_argument("--lines", type=int)
    keys = verbs.add_parser("keys")
    keys.add_argument("address")
    keys.add_argument("keys", nargs="+")
    label = verbs.add_parser("label")
    label.add_argument("address")
    label.add_argument("labels", nargs="+")
    args = parser.parse_args(argv)
    backend = terminals.current()
    try:
        if args.verb == "ls":
            print(json.dumps({"terminals": backend.ls()}))
        elif args.verb == "get":
            terminal = backend.get(args.address)
            if terminal is None:
                print(f"fleet_term: no terminal {args.address}", file=sys.stderr)
                return NOT_FOUND
            print(json.dumps(terminal))
        elif args.verb == "self":
            address = backend.self_address()
            if not address:
                print("fleet_term: not inside a fleet terminal", file=sys.stderr)
                return 1
            print(json.dumps({"address": address}))
        elif args.verb == "send":
            sent = backend.send(args.address, args.text, tagged=True)
            sys.stdout.write(sent.stdout or "")
            sys.stderr.write(sent.stderr or "")
            return sent.returncode
        elif args.verb == "read":
            sys.stdout.write(backend.read(args.address, args.lines, strict=True))
        elif args.verb == "keys":
            backend.keys(args.address, args.keys)
        elif args.verb == "label":
            backend.set_labels(args.address, parse_labels(args.labels))
    except CheckError as error:
        print(f"fleet_term: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
