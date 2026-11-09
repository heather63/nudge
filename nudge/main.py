"""Entry point.

    python -m nudge.main auth      # one-time Google OAuth, opens a browser
    python -m nudge.main seed      # first run: build the sheet from Google Tasks
    python -m nudge.main run       # the Discord bot
    python -m nudge.main mirror    # one sheet <-> tasks reconcile (for cron)
    python -m nudge.main sheet     # print the spreadsheet URL
    python -m nudge.main console   # same engine, stdin/stdout, no Discord
    python -m nudge.main status    # what's active
    python -m nudge.main gaps      # today's free intervals, from freeBusy
    python -m nudge.main capacity  # does the remaining work still fit?
    python -m nudge.main rank 30   # score the pile against a 30-minute gap
    python -m nudge.main ingest "CS 4262/HW1"   # size one assignment folder
    python -m nudge.main offers    # the audit log, for tuning
    python -m nudge.main estimates # asked-for vs actually-done, to calibrate
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
from datetime import timedelta
from pathlib import Path

from . import config
from . import timeutil as tu
from .engine.gaps import CalendarGapSource, FakeGapSource, Gap
from .engine.loop import NagEngine
from .io.commands import parse
from .store import db
from .store.repo import Repo, remaining_minutes

log = logging.getLogger(__name__)


def build_repo() -> Repo:
    return Repo(db.connect())


def build_mirror(repo: Repo, *, required: bool = False):
    """None when Google isn't set up yet — the nag loop still runs offline."""
    from .connectors import google_auth
    from .mirror import Mirror

    try:
        google_auth.get_credentials()
    except google_auth.AuthError as exc:
        if required:
            raise SystemExit(str(exc)) from exc
        log.warning("sheet + tasks sync disabled: %s", exc)
        return None
    return Mirror(repo)


def build_gap_source():
    """Real calendar gaps when Google is authorised, synthetic ones otherwise."""
    if config.FAKE_GAPS:
        return FakeGapSource()
    from .connectors import google_auth

    try:
        google_auth.get_credentials()
    except google_auth.AuthError as exc:
        log.warning("no calendar access (%s) — falling back to synthetic gaps", exc)
        return FakeGapSource()
    return CalendarGapSource()


def build_engine(repo: Repo, send):
    return NagEngine(repo, build_gap_source(), send, mirror=build_mirror(repo))


async def _print(text: str) -> None:
    print(f"\n{text}\n")


# --- commands ------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> None:
    from .io import bot  # imported late so other commands need no discord.py

    repo = build_repo()
    bot.run(repo, build_gap_source(), mirror=build_mirror(repo))


def cmd_auth(args: argparse.Namespace) -> None:
    from .connectors import google_auth
    from .connectors.gtasks import TasksClient

    google_auth.authorize()
    print("Authorised. Task lists nudge can see:")
    for item in TasksClient().list_tasklists():
        marker = " (excluded)" if item["title"] in config.TASKLIST_EXCLUDE else ""
        print(f"  {item['title']}{marker}")
    print(
        "\nIf the Cloud Console app is still in 'Testing' publishing status the "
        "refresh token expires after 7 days — set it to 'Production'."
    )


def cmd_seed(args: argparse.Namespace) -> None:
    repo = build_repo()
    mirror = build_mirror(repo, required=True)
    created = mirror.seed_from_tasks()
    print(f"{created} assignments read out of Google Tasks")
    report = mirror.sync()
    print(report)
    print(f"\nSheet: {mirror.url}")


def cmd_mirror(args: argparse.Namespace) -> None:
    repo = build_repo()
    mirror = build_mirror(repo, required=True)
    print(mirror.sync(full=args.full))


def cmd_sheet(args: argparse.Namespace) -> None:
    repo = build_repo()
    mirror = build_mirror(repo, required=True)
    url = mirror.url or mirror.sheets.url(mirror.spreadsheet_id())
    print(url)


def cmd_status(args: argparse.Namespace) -> None:
    from .engine import capacity
    from .io import templates

    repo = build_repo()
    now = tu.now_utc()
    print(templates.status(repo.summary(), now, capacity.read_snapshot(repo, now),
                           limit=args.limit))


def cmd_tick(args: argparse.Namespace) -> None:
    asyncio.run(build_engine(build_repo(), _print).tick())


def cmd_rank(args: argparse.Namespace) -> None:
    repo = build_repo()
    now = tu.now_utc()
    gap = Gap(now, now + timedelta(minutes=args.minutes + config.TAIL_BUFFER))
    engine = NagEngine(repo, build_gap_source(), _print)
    ranking = engine.rank_for(gap, now)
    if not ranking:
        print("nothing worth starting")
        return
    print(f"usable gap: {gap.usable_min(now):.0f} min")
    for scored in ranking[: args.limit]:
        parts = "  ".join(f"{k}={v:.2f}" for k, v in scored.parts.items())
        print(f"{scored.score:+.3f}  {scored.candidate.label[:38]:<40} {parts}")


def cmd_gaps(args: argparse.Namespace) -> None:
    from .engine import policy

    source = build_gap_source()
    now = tu.now_utc()
    source.refresh(now)
    gaps = getattr(source, "_gaps", None)
    if gaps is None:
        gap = source.current_gap(now)
        gaps = [gap] if gap else []
    if not gaps:
        print("no free intervals in the window")
        return
    print(f"free intervals over the next {config.FREEBUSY_WINDOW_HOURS}h:")
    for gap in gaps:
        if gap.end <= now:
            continue
        start, end = tu.to_local(gap.start), tu.to_local(gap.end)
        if gap.contains(now):
            verdict = policy.gap_is_usable(gap, now)
            note = "usable now" if verdict.ok else verdict.reason
            note += f", {tu.fmt_duration(gap.usable_min(now))} usable"
            mark = "  <- you are here"
        else:
            note, mark = "upcoming", ""
        print(
            f"  {start:%a %-I:%M%p} - {end:%-I:%M%p}"
            f"  ({tu.fmt_duration(gap.length_min)}, {note}){mark}"
        )
    allowed = policy.may_nag(build_repo(), now)
    print(f"\nnag gate: {'open' if allowed.ok else allowed.reason}")


def cmd_capacity(args: argparse.Namespace) -> None:
    from .engine import capacity

    repo = build_repo()
    source = build_gap_source()
    now = tu.now_utc()
    warnings = capacity.daily_check(repo, now, getattr(source, "free_intervals", None))
    snapshot = capacity.read_snapshot(repo, now)
    for row in repo.summary():
        remaining = remaining_minutes(row["expected_min"], row["percent_done"])
        if remaining <= 0:
            continue
        due = tu.parse(row["due_at"])
        usable = capacity.usable_minutes(
            now, due, available_min=snapshot.get(int(row["id"]))
        )
        name = f"{row['course']} · {row['name']}" if row["course"] else row["name"]
        verdict = "" if usable == float("inf") else (
            "  OK" if remaining <= usable else "  SHORT"
        )
        budget = "no due date" if usable == float("inf") else tu.fmt_duration(usable)
        print(
            f"  {name[:36]:<38} {tu.fmt_duration(remaining):>7} left, "
            f"{budget:>8} available{verdict}"
        )
    if warnings:
        print(f"\n{len(warnings)} new warning(s) would be sent.")


def cmd_offers(args: argparse.Namespace) -> None:
    repo = build_repo()
    rows = repo.conn.execute(
        "SELECT o.offered_at, o.response, o.session_min, o.minutes_logged, "
        "       a.name, a.course, o.gap_start, o.gap_end "
        "FROM offers o JOIN assignments a ON a.id = o.assignment_id "
        "ORDER BY o.offered_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in rows:
        when = tu.to_local(tu.parse(row["offered_at"]))
        gap_min = tu.minutes_between(tu.parse(row["gap_start"]), tu.parse(row["gap_end"]))
        logged = "" if row["minutes_logged"] is None else f" did={row['minutes_logged']:.0f}m"
        name = f"{row['course']} · {row['name']}" if row["course"] else row["name"]
        print(
            f"{when:%m-%d %H:%M}  {(row['response'] or 'open'):<9} "
            f"gap={gap_min:>3.0f}m asked={row['session_min']:>3}m{logged}  {name[:40]}"
        )


def cmd_estimates(args: argparse.Namespace) -> None:
    rows = build_repo().logged_sessions(args.limit)
    if not rows:
        print("No sessions logged yet.")
        return
    print(f"{'asked':>7} {'did':>7} {'ratio':>6}  assignment")
    ratios = []
    for name, asked, did in rows:
        ratio = did / asked if asked else 0.0
        ratios.append(ratio)
        print(
            f"{tu.fmt_duration(asked):>7} {tu.fmt_duration(did):>7} "
            f"{ratio:>6.2f}  {name[:44]}"
        )
    median = statistics.median(ratios)
    verdict = "less" if median < 1 else "more"
    print(
        f"\nmedian {median:.2f} over {len(ratios)} sessions — you do {verdict} "
        "than I ask for."
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    from .ingest import pipeline

    repo = build_repo()
    folder = Path(args.folder).expanduser()
    if not folder.is_absolute():
        folder = pipeline.watch_root() / args.folder
    if not folder.is_dir():
        raise SystemExit(f"No folder at {folder}")
    asyncio.run(build_engine(repo, _print).ingest(folder))
    if repo.open_proposal_count():
        print("Reply `approve` in Discord, or run: nudge console")


def cmd_console(args: argparse.Namespace) -> None:
    """The whole nag loop without Discord — for tuning the policy."""
    repo = build_repo()

    async def send(text: str) -> None:
        print(f"\n\033[1mnudge:\033[0m {text}\n> ", end="", flush=True)

    engine = build_engine(repo, send)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(args.tick_seconds)
            try:
                await engine.tick()
            except Exception:
                logging.exception("tick failed")

    async def repl() -> None:
        loop = asyncio.get_running_loop()
        print(f"console mode — ticking every {args.tick_seconds}s. `help` for commands.")
        print("> ", end="", flush=True)
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            command = parse(line)
            if command is None:
                print("? unparsed\n> ", end="", flush=True)
                continue
            reply = await engine.handle(command)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nudge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("auth").set_defaults(func=cmd_auth)
    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("sheet").set_defaults(func=cmd_sheet)
    sub.add_parser("tick").set_defaults(func=cmd_tick)
    sub.add_parser("gaps").set_defaults(func=cmd_gaps)
    sub.add_parser("capacity").set_defaults(func=cmd_capacity)

    p_mirror = sub.add_parser("mirror")
    p_mirror.add_argument("--full", action="store_true", help="ignore the delta cursor")
    p_mirror.set_defaults(func=cmd_mirror)

    p_status = sub.add_parser("status")
    p_status.add_argument("--limit", type=int, default=50)
    p_status.set_defaults(func=cmd_status)

    p_rank = sub.add_parser("rank")
    p_rank.add_argument("minutes", type=int, nargs="?", default=60)
    p_rank.add_argument("--limit", type=int, default=15)
    p_rank.set_defaults(func=cmd_rank)

    p_offers = sub.add_parser("offers")
    p_offers.add_argument("--limit", type=int, default=50)
    p_offers.set_defaults(func=cmd_offers)

    p_estimates = sub.add_parser("estimates")
    p_estimates.add_argument("--limit", type=int, default=100)
    p_estimates.set_defaults(func=cmd_estimates)

    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("folder", help='e.g. "CS 4262/HW1", relative to the watch folder')
    p_ingest.set_defaults(func=cmd_ingest)

    p_console = sub.add_parser("console")
    p_console.add_argument("--tick-seconds", type=int, default=30)
    p_console.set_defaults(func=cmd_console)

    return parser


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
