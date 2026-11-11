"""Entry point.

    python -m nudge.main auth                # one-time Google OAuth, opens a browser
    python -m nudge.main sheet               # create the sheet if needed, print its URL
    python -m nudge.main run                 # the Discord bot
    python -m nudge.main console             # the same engine on stdin/stdout
    python -m nudge.main status              # what's active
    python -m nudge.main size ~/hw1.pdf      # estimate an assignment's length
    python -m nudge.main debug gaps|rank|offers|capacity
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

from . import config
from . import timeutil as tu
from .engine import capacity
from .engine.gaps import CalendarGapSource, Gap
from .engine.loop import NagEngine
from .io import templates
from .io.commands import parse
from .sheet import Sheet
from .store import db
from .store.repo import Repo

log = logging.getLogger(__name__)


def build_repo() -> Repo:
    return Repo(db.connect())


def require_google() -> None:
    """Assignments live in the sheet, so nothing works without Google."""
    from .connectors import google_auth

    try:
        google_auth.get_credentials()
    except google_auth.AuthError as exc:
        raise SystemExit(str(exc)) from exc


def build_engine(send) -> NagEngine:
    require_google()
    repo = build_repo()
    return NagEngine(repo, Sheet(repo), CalendarGapSource(), send)


# --- commands ------------------------------------------------------------
def cmd_auth(args: argparse.Namespace) -> None:
    from .connectors import google_auth

    google_auth.authorize()
    print(
        "Authorised. If the Cloud Console app is still in 'Testing' publishing "
        "status the refresh token expires after 7 days — set it to 'Production'."
    )


def cmd_sheet(args: argparse.Namespace) -> None:
    require_google()
    sheet = Sheet(build_repo())
    print(sheet.client.url(sheet.spreadsheet_id()))


def cmd_run(args: argparse.Namespace) -> None:
    from .io import bot  # imported late so other commands need no discord.py

    require_google()
    bot.run(build_engine)


def cmd_status(args: argparse.Namespace) -> None:
    require_google()
    repo = build_repo()
    now = tu.now_utc()
    assignments = Sheet(repo).read(now)
    print(templates.status(assignments, now, capacity.read_snapshot(repo, now),
                           limit=args.limit))


def cmd_console(args: argparse.Namespace) -> None:
    """The whole nag loop without Discord."""

    async def send(text: str, *, offer: bool = False) -> None:
        print(f"\n\033[1mnudge:\033[0m {text}\n> ", end="", flush=True)

    engine = build_engine(send)

    async def ticker() -> None:
        while True:
            try:
                await engine.tick()
            except Exception:  # noqa: BLE001
                logging.exception("tick failed")
            await asyncio.sleep(args.tick_seconds)

    async def repl() -> None:
        loop = asyncio.get_running_loop()
        print(f"console mode — ticking every {args.tick_seconds}s. `help` for replies.")
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            command = parse(line)
            reply = templates.HELP if command is None else await engine.handle(command)
            if reply:
                print(f"\n\033[1mnudge:\033[0m {reply}")
            print("> ", end="", flush=True)

    async def main() -> None:
        task = asyncio.create_task(ticker())
        try:
            await repl()
        finally:
            task.cancel()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def cmd_size(args: argparse.Namespace) -> None:
    from .ingest import estimate, extract

    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"Nothing at {path}")
    documents = extract.read_documents(path)
    if not documents:
        raise SystemExit(f"No readable .pdf, .docx, .md or .txt at {path}")
    name = args.name or (path.name if path.is_dir() else path.stem)
    try:
        result = estimate.estimate(args.course, name, None, documents)
    except estimate.EstimateError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{name}: {tu.fmt_duration(result.expected_minutes)}")
    if result.kind:
        print(f"  type: {result.kind}")
    if result.due_date:
        print(f"  due:  {result.due_date}")
    print(f"  {result.reasoning}")
    print("\nPut the length in the sheet's Expected length column if it looks right.")


def cmd_debug(args: argparse.Namespace) -> None:
    require_google()
    repo = build_repo()
    sheet = Sheet(repo)
    gaps = CalendarGapSource()
    now = tu.now_utc()

    if args.what == "offers":
        for row in repo.recent_offers(args.limit):
            when = tu.to_local(tu.parse(row["offered_at"]))
            logged = "" if row["minutes_logged"] is None else f" did={row['minutes_logged']:.0f}m"
            print(f"{when:%m-%d %H:%M}  {(row['response'] or 'open'):<9} "
                  f"asked={row['session_min']:>3}m{logged}  {row['label'][:50]}")

    elif args.what == "gaps":
        from .engine import policy

        gaps.refresh(now)
        for gap in gaps.upcoming(now):
            start, end = tu.to_local(max(gap.start, now)), tu.to_local(gap.end)
            here = "  <- now" if gap.contains(now) else ""
            print(f"  {start:%a %-I:%M%p} - {end:%-I:%M%p}{here}")
        gap = gaps.current_gap(now)
        if gap is not None:
            verdict = policy.may_nag(repo, gap, now)
            print(f"\nnag gate: {'open' if verdict.ok else verdict.reason}")

    elif args.what == "rank":
        minutes = args.minutes
        gap = Gap(now, now + timedelta(minutes=minutes + config.TAIL_BUFFER))
        engine = NagEngine(repo, sheet, gaps, None)
        for scored in engine.rank(sheet.read(now), gap, now)[: args.limit]:
            print(f"pressure={scored.pressure:5.2f} fit={scored.fit:4.2f}  "
                  f"{scored.assignment.label[:50]}")

    elif args.what == "capacity":
        for a in sheet.read(now):
            if a.remaining_min <= 0 or a.due_at is None:
                continue
            usable = capacity.usable_minutes(
                now, a.due_at, available_min=capacity.read_snapshot(repo, now).get(a.sheet_id)
            )
            verdict = "OK" if a.remaining_min <= usable else "SHORT"
            print(f"  {a.label[:36]:<38} {tu.fmt_duration(a.remaining_min):>7} left, "
                  f"{tu.fmt_duration(usable):>8} available  {verdict}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nudge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth").set_defaults(func=cmd_auth)
    sub.add_parser("sheet").set_defaults(func=cmd_sheet)
    sub.add_parser("run").set_defaults(func=cmd_run)

    p = sub.add_parser("status")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("console")
    p.add_argument("--tick-seconds", type=int, default=60)
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("size", help="estimate an assignment's length from its documents")
    p.add_argument("path", help="a document, or a folder of them")
    p.add_argument("--name", help="assignment name (default: the file or folder name)")
    p.add_argument("--course")
    p.set_defaults(func=cmd_size)

    p = sub.add_parser("debug")
    p.add_argument("what", choices=["gaps", "rank", "offers", "capacity"])
    p.add_argument("minutes", type=int, nargs="?", default=60, help="gap length for rank")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_debug)

    return parser


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
