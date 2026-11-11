# nudge

A personal agent that DMs me on Discord, offers assignment work sized to
whatever free time I actually have, and tracks progress in a Google Sheet.

Opportunistic, not planned: no weekly schedule, no slot assignment. When a gap
opens in my calendar, nudge offers the one assignment under the most deadline
pressure, time-boxed to the gap.

![tests](https://github.com/heather63/nudge/actions/workflows/tests.yml/badge.svg)

**At a glance**

- **Finds real free time.** Reads Google Calendar `freeBusy` across every
  calendar, respecting quiet hours, a cooldown and a daily budget.
- **Offers exactly one thing.** It picks whatever has the most work left
  relative to the realistic time before it's due.
- **One tap to answer.** Every offer has *Did it · Something else · Not now ·
  Snooze* buttons; typed replies like `did 40` or `busy until friday` work too.
- **One source of truth.** Assignments live only in a Google Sheet you can edit
  freely. nudge reads it fresh each time and writes back just the % done.
- **Warns before it's too late.** A daily check flags work that no longer fits
  in the free time left before its due date.
- **Sizes work from the documents.** `nudge size spec.pdf` asks Claude for an
  expected length. The LLM never decides *when* anything happens.

## How it fits together

```
  Google Sheet  ◀── the assignments: what exists, how long, how far along
       ▲ ▼
  nudge engine  ◀── SQLite: snoozes, declines, the offers log (nothing else)
       ▲
  Google Calendar ─▶ freeBusy ─▶ free gap ─▶ best assignment ─▶ Discord DM
```

The sheet has one row per assignment: **Assignment, Class, Due date, Expected
length, % done, Repeats, Not before, ID**. Add, edit, reorder or delete rows
whenever you like; extra columns of your own are left alone. nudge fills in a
blank ID and writes to three cells only: **% done** when you report progress,
**Due date** when a repeating row rolls over, and **ID**.

## Setup

Python 3.11+ (developed on 3.13).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                   # fill in the Discord values
.venv/bin/python -m nudge.main auth    # Google OAuth, opens a browser
.venv/bin/python -m nudge.main sheet   # creates the sheet, prints its link
```

Set `NUDGE_TIMEZONE` in `.env` if you are not on US Eastern time; it drives
quiet hours and what counts as "today".

### Discord

A bot can only DM someone it shares a server with.

1. Create a server with just you in it.
2. https://discord.com/developers/applications → New Application → Bot.
3. Enable **Message Content Intent** under Bot → Privileged Gateway Intents.
4. OAuth2 → URL Generator → scope `bot`, permission `Send Messages`. Open the
   URL and add it to your private server.
5. Bot token → `NUDGE_DISCORD_TOKEN`. Settings → Advanced → Developer Mode,
   right-click yourself → Copy User ID → `NUDGE_DISCORD_USER_ID`.

### Google

1. Cloud Console → new project → enable the **Sheets** and **Calendar** APIs.
2. OAuth consent screen → publishing status **Production**. In "Testing" the
   refresh token expires after 7 days, which is the most common way a project
   like this quietly dies.
3. Credentials → OAuth client ID → **Desktop app** → save the JSON as
   `credentials.json`.
4. `python -m nudge.main auth`.

Scopes: `spreadsheets`, `calendar.readonly`.

### Anthropic (optional)

`ANTHROPIC_API_KEY` in `.env`, used only by `nudge size`.

## Running

```bash
.venv/bin/python -m nudge.main run              # the bot
.venv/bin/python -m nudge.main console          # the same engine, in the terminal
.venv/bin/python -m nudge.main status           # what's active
.venv/bin/python -m nudge.main sheet            # the spreadsheet link
.venv/bin/python -m nudge.main size ~/hw1.pdf   # estimate an expected length
.venv/bin/python -m nudge.main debug gaps       # today's free time and the nag gate
.venv/bin/python -m nudge.main debug rank 60    # rank the pile against a 60-min gap
.venv/bin/python -m nudge.main debug offers     # the offers log
.venv/bin/python -m nudge.main debug capacity   # does each assignment still fit?
.venv/bin/python -m unittest discover -s tests
```

## Replying

Press a button under the offer, or type:

| reply | effect |
|---|---|
| **Did it** / `done` | log the sitting nudge asked for (or 100%, if that was all that was left) |
| `did 40` / `did 1h` | log time actually spent; converts to a percentage |
| `40%` | set completion directly (`100%` = finished) |
| **Not now** / `not now` | decline; nothing else this gap |
| **Something else** | the next-best assignment |
| **Snooze** / `snooze friday` | push this assignment out (default: tomorrow morning) |
| `busy until 5pm` / `busy for 3h` | nothing at all until then; `busy off` to undo |
| `status` | what's active, with percentages and slack |

Three ways to say no, each with a different scope: **not now** is this gap,
**snooze** is this assignment, **busy** is everything. Pass on the same
assignment four times in a row and nudge leaves it alone until tomorrow.

`did 30` means thirty **minutes**: bare numbers in a reply are minutes, while a
bare number in the sheet's Expected length column is hours.

## Repeating assignments

A weekly quiz is one row, not fifteen. Put a rule in the **Repeats** column and
the due date rolls forward on its own:

| Repeats | means |
|---|---|
| `weekly:fri` | every Friday |
| `sun,tue` | every Sunday and Tuesday |
| `every Monday` | every Monday |
| *(blank)* | one-off |

Once an occurrence is finished, or its due date passes (a weekly quiz you
missed is simply gone), the row resets to 0% and takes the next date. Finishing
early rolls from the date you completed, not from today, so doing Friday's on
Wednesday still gives you next Friday.

## "Not before": work that can't be started yet

Some work has an opening time, not just a deadline. A peer review due Friday
might only open at 10am that day; offering it on Tuesday is noise. The **Not
before** column handles that, resolved against each occurrence's due date:

| Not before | with a due date of | opens |
|---|---|---|
| `fri 10am` | Friday | that Friday, 10:00 |
| `fri 5pm` | Monday | the *previous* Friday, 17:00 |
| `10am` | any day | that morning |
| `2026-10-01` | any | a fixed date |

An assignment that has not opened is never offered, and the capacity check
measures its free time from the opening rather than from now.

## How it picks

```
available = real free time before the due date, quiet hours removed,
            each day capped at WORKABLE_HOURS_PER_DAY
pressure  = remaining work / (available * CAPACITY_REALISM)
```

The highest pressure wins. One number covers both urgency and size: a ten-hour
project due Friday outranks a one-hour reading due Wednesday. Ties go to
whatever you could finish in this sitting. Work with no due date has no
pressure, so it comes up when nothing else is waiting.

The sitting nudge asks for is the gap or what's left, whichever is smaller,
capped at `MAX_SESSION_MINUTES`; a free afternoon is not a five-hour session.

## The daily capacity check

Once a day, per assignment: does the remaining work still fit in the free time
before it's due? The per-day cap is what makes the answer mean anything. An
empty day reads as ~15 waking hours from freeBusy, so a flat discount would
make everything look comfortable forever. The calendar can only make a day
*worse* than the cap: it says when you're free, not that you'll work all of it.

Warnings fire once per assignment and re-arm if it fits again and later slips
back. The measured availability is cached and feeds the ranking, so the nag
and the warning always agree.

## Sizing assignments from their documents

```bash
.venv/bin/python -m nudge.main size "~/CS 4262/HW1"     # a folder of .pdf/.docx/.md/.txt
.venv/bin/python -m nudge.main size spec.pdf --course "CS 4262"
```

Claude reads the documents and prints a total expected length, a type, any
stated due date, and one line of reasoning. Nothing is written for you: a wrong
number silently distorts every ranking and capacity warning, so you copy it
into the sheet if it looks right.

## Layout

```
nudge/
  config.py         every tuning knob
  sheet.py          the Google Sheet: read rows, write % done, roll repeats
  recurrence.py     Repeats and Not before rules
  timeutil.py       UTC storage, local comparison, "until friday" parsing
  store/            SQLite: snoozes, declines, the offers log
  connectors/       Google OAuth with retry/backoff, Sheets, Calendar freeBusy
  engine/
    gaps.py         free time from freeBusy
    policy.py       quiet hours, busy, cooldown, daily budget
    scoring.py      pressure, then fit
    capacity.py     remaining work vs available time
    loop.py         the tick and every reply handler
  io/
    commands.py     typed reply parsing
    templates.py    everything nudge says
    bot.py          discord.py client and the offer buttons
  ingest/           `nudge size`: document text and the Claude estimate
```

## Notes

- Everything is timezone-aware: UTC stored, local compared.
- If Google is unreachable, that tick is skipped and nothing else changes; a
  broken token gets one DM every six hours rather than silent failure.
- `db.connect()` checkpoints the WAL on exit, since every CLI command is its
  own short-lived process.
- Row IDs always begin with letters (`id3f9a2c1b`). Sheets reads a bare hex id
  like `202e65` as scientific notation, mangling it.
- Earlier versions also mirrored assignments into Google Tasks and SQLite. On
  first run, an old `nudge.db` is cleaned up automatically and keeps its link
  to your existing sheet; Google Tasks is simply no longer touched.
