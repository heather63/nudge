# nudge

A personal agent that DMs me on Discord, offers assignment work sized to
whatever free time I actually have, and tracks progress in a Google Sheet.

Opportunistic, not planned: no weekly schedule, no slot assignment. A ranked
pile of assignments, and the best-fitting one gets offered — time-boxed to the
gap — whenever a gap opens.

![tests](https://github.com/heather63/nudge/actions/workflows/tests.yml/badge.svg)

**At a glance**

- **Finds real free time.** Reads Google Calendar `freeBusy` across every
  calendar and inverts it into gaps, respecting quiet hours and a daily budget.
- **Offers exactly one thing.** A deterministic weighted score (urgency,
  workload pressure, fit, staleness, continuity, declines) picks the single best
  assignment for the gap you have right now.
- **Talks in plain replies.** `did 40`, `snooze tomorrow`, `busy until friday`:
  a tolerant parser turns DM replies into progress and scheduling decisions.
- **Three-way sync without data loss.** A Google Sheet, a local SQLite mirror
  and Google Tasks stay reconciled under explicit conflict rules.
- **Sizes work from the documents.** Drop an assignment PDF in a folder and
  Claude proposes an expected length for you to approve. The LLM never decides
  *when* anything happens.
- **Warns before it's too late.** A daily capacity check flags work that no
  longer fits in the free time left before its due date.
- **184 unit tests**, no network access needed.

## How it fits together

```
  Google Sheet  ◀── source of truth: what exists, how far along it is
       ▲ ▼
     SQLite  ◀── mirror + everything scheduling-related (offers, cooldowns)
       ▲ ▼
  Google Tasks  ◀── second mirror; ticking something off flows back
       ▲
  Google Calendar ─▶ freeBusy ─▶ gaps ─▶ nag engine ─▶ Discord DM
```

The sheet has one row per assignment: **Assignment, Class, Due date, Type,
Expected length, % done, Repeats, Not before, Relevant files, ID**. Both you and nudge write to it.
Google Tasks carries the same assignments as tasks in per-class lists, so you
can tick things off in the app you already use; those completions come back.
A task you create there yourself becomes a new row in the sheet on the next
reconcile, so either side can be where work gets added.

When nudge offers work and you reply `did 40` or `done`, it converts that into a
percentage against the expected length and writes it to both.

## Setup

Python 3.11+ (developed on 3.13).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in the Discord values and the Anthropic key
.venv/bin/python -m nudge.main auth    # Google OAuth, opens a browser
.venv/bin/python -m nudge.main seed    # build the sheet from your existing Tasks
```

Set `NUDGE_TIMEZONE` in `.env` if you are not on US Eastern time; it drives
quiet hours and what counts as "today".

`seed` reads every task already in your account, guesses a type from the title,
creates the spreadsheet and fills it in. Expected lengths start blank — set them
by hand, or drop the assignment documents in a folder and let nudge size them.

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

1. Cloud Console → new project → enable the **Sheets**, **Tasks** and
   **Calendar** APIs.
2. OAuth consent screen → publishing status **Production**. In "Testing" the
   refresh token expires after 7 days, which is the most common way a project
   like this quietly dies.
3. Credentials → OAuth client ID → **Desktop app** → save the JSON as
   `credentials.json`.
4. `python -m nudge.main auth`.

Scopes: `spreadsheets`, `tasks`, `calendar.readonly`. Adding a scope invalidates
the saved token — nudge detects that and tells you to re-run `auth` rather than
failing with a raw 403.

### Anthropic

`ANTHROPIC_API_KEY` in `.env`, used only to size assignments from their
documents. The LLM never decides *when* anything happens.

## Running

```bash
.venv/bin/python -m nudge.main run              # the bot
.venv/bin/python -m nudge.main mirror           # one sheet <-> tasks reconcile
.venv/bin/python -m nudge.main console          # the real engine, no Discord
.venv/bin/python -m nudge.main sheet            # print the spreadsheet URL
.venv/bin/python -m nudge.main status           # what's active
.venv/bin/python -m nudge.main gaps             # today's free intervals
.venv/bin/python -m nudge.main capacity         # does the remaining work fit?
.venv/bin/python -m nudge.main rank 60          # score the pile against a gap
.venv/bin/python -m nudge.main ingest "CS 4262/HW1"
.venv/bin/python -m nudge.main offers           # the audit log, for tuning
.venv/bin/python -m nudge.main estimates        # asked-for vs actually-done
.venv/bin/python -m unittest discover -s tests
```

`run` reconciles on every tick, so a cron job is optional. If you want the sheet
and Tasks kept in step even when the bot is not running:

```cron
*/10 * * * * cd /path/to/nudge && .venv/bin/python -m nudge.main mirror >> /tmp/nudge-mirror.log 2>&1
```

## Commands (DM replies)

| reply | effect |
|---|---|
| `done` | mark the offered assignment 100% |
| `did 40` / `did 1h` | log time actually spent; converts to a percentage |
| `40%` | set completion directly |
| `not now` | decline; nothing else this gap |
| `snooze 2h` / `snooze tomorrow` | push it out |
| `skip` | stop offering it — the sheet row is untouched |
| `undo` | reverse the last skip |
| `something else` | next-ranked assignment |
| `busy until friday` / `busy for 3h` | not a decline — stop nagging until then |
| `mute 3h` / `unmute` | global silence |
| `status` | what's active, with percentages and slack |
| `sync` | force a sheet + Tasks reconcile |
| `capacity` | does the remaining work still fit? |
| `sheet` | link to the spreadsheet |
| `approve` / `reject` | answer a proposed estimate |
| `ingest CS 4262/HW1` | re-read that assignment folder |
| `gap 30` / `why` | debug helpers |

Parsing is tolerant: "ok done, finally" and "ehh not now" both land. `did 30`
means thirty **minutes** — bare numbers in a reply are minutes, while a bare
number in the sheet's Expected length column is hours.

## Layout

```
nudge/
  config.py         all tuning knobs; nothing else hardcodes a number
  timeutil.py       UTC storage, local comparison, duration parsing
  errors.py         AuthError, in a leaf module so the engine stays light
  mirror.py         sheet <-> SQLite <-> Tasks reconciliation
  store/            schema.sql, connection + migrations, typed accessors
  connectors/
    google_auth.py  OAuth, token refresh, scope check, retry/backoff
    gsheets.py      the spreadsheet: cell parsing and per-cell writes
    gtasks.py       Tasks client
    gcal.py         freeBusy, with per-calendar error tolerance
  engine/
    gaps.py         Gap, busy->free inversion, calendar and fake sources
    scoring.py      the weighted sum
    capacity.py     remaining work vs available time
    policy.py       quiet hours, cooldown, budget, escalation ladder
    loop.py         the tick and every command handler
  io/
    commands.py     DM reply parsing
    templates.py    deterministic phrasing (+ optional LLM phraser hook)
    bot.py          discord.py client
  ingest/
    extract.py      pdf/docx/md/txt -> plain text
    estimate.py     LLM -> expected length, type, due date
    pipeline.py     folder -> proposal -> approval
    watcher.py      watchdog + per-folder debounce
```

## Repeating assignments

A weekly quiz is one row, not fifteen. Put a rule in the **Repeats** column and
the due date rolls forward on its own:

| Repeats | means |
|---|---|
| `weekly:fri` | every Friday |
| `sun,tue` | every Sunday and Tuesday |
| `every Monday` | every Monday |
| *(blank)* | one-off |

Once an occurrence is finished — or once its due date passes, because a weekly
quiz you missed is simply gone — the row resets to 0% and takes the next date.
Finishing early rolls from the date you completed, not from today, so doing
Friday's on Wednesday still gives you next Friday rather than the same one. The
history stays in the offers log.

## "Not before": work that can't be started yet

Some work has an opening time, not just a deadline. `RFTH` is due Friday, but
student submissions only land at 10am that day — offering it on Tuesday is
noise. The **Not before** column fixes that, resolved against each occurrence's
due date:

| Not before | with a due date of | opens |
|---|---|---|
| `fri 10am` | Friday | that Friday, 10:00 |
| `fri 5pm` | Monday | the *previous* Friday, 17:00 |
| `10am` | any day | that morning |
| `2026-10-01` | any | a fixed date |

An assignment that has not opened is never offered, however urgent it looks, and
the capacity check measures its free time from the opening rather than from now
— a task you can only start Friday morning does not have all week to be done in.
For a repeating row the opening rolls forward with the due date.

## Conflict rules

Three writable copies of the same facts need one rule each, and both are the
same shape: **a local change that has not been pushed yet is never overwritten
by what the other side still shows.** `sheet_synced_at` and `synced_at` are the
dirty flags; the push in the same pass resolves them.

- The sheet is truth for *existence*. Delete a row and the assignment is gone —
  but only rows that actually reached the sheet can be deleted from it, so a
  locally created assignment waiting for its first push is safe.
- Tasks is truth for *nothing that already exists*, but a task nudge has never
  seen is new information, so it becomes a row (`ADOPT_NEW_TASKS`). Completing
  a task there is new information too, so it sets the row to 100%.
- Adoption links the row to the task that spawned it rather than pushing a
  second copy, and a hand-made task matching a row that is still waiting for
  its first push is linked, not duplicated. A row you then delete from the
  sheet stays deleted: the lingering task is still *known*, so it is never
  re-adopted.
- Reopening a completed task in the Tasks app leaves it open. nudge tells you
  it was reopened and asks for a percentage rather than inventing one, and
  until you answer neither the sheet's 100% nor the next sync closes it again.
  "Active at 100%" is the marker; it is only reachable that way, so no extra
  state is needed.
- A row whose ID cell you clear is reattached by name rather than duplicated,
  and a row you copy-paste gets a fresh ID instead of stealing the original's.
- `skip` never touches the sheet or Tasks. It is a scheduling decision — "stop
  offering me this" — implemented as a very long snooze.

Two guards exist because both failures are silent and total:

- **An empty read is never treated as deletion.** If the sheet comes back with
  no rows while the mirror holds rows that have been there before, the sync
  aborts. Otherwise one bad response marks every assignment dropped. The
  consequence to know about: clearing *every* row at once will not delete
  anything — remove them a few at a time.
- **A header that matches nothing is never overwritten.** Stamping a fresh
  header onto a sheet whose header simply failed to parse shifts every column
  by one, which silently moves each row's ID into the wrong cell.

Row IDs always begin with letters (`id3f9a2c1b`). A bare hex id is a trap:
Sheets reads `202e65…` as scientific notation and eats the leading zero from
`01191655`, so the id comes back mangled and the row looks brand new.

## Scoring

A plain weighted sum over every active assignment, recomputed each tick:

```
score = W_URGENCY * urgency + W_PRESSURE * pressure + W_FIT * fit
      + W_STALENESS * staleness + W_CONTINUITY * continuity
      - W_DECLINE * decline_penalty
```

**pressure** is the term that makes size count rather than just proximity: a
ten-hour project due Friday outranks a one-hour reading due Wednesday.

```
available = real free time before the due date, quiet hours removed,
            each day capped at WORKABLE_HOURS_PER_DAY
pressure  = min(remaining / (available * CAPACITY_REALISM), 1.0)
```

**fit** rewards being able to finish something in one sitting. Because work is
time-boxed to the gap rather than pre-cut, nothing is ever disqualified — you
can always make some progress — so `fit` discriminates less than it did under a
chunk model, and `W_FIT` is lower to match.

**Offer exactly one thing.** A menu turns a nag into a decision, and decisions
in a twenty-minute gap get deferred. `something else` pops the next-ranked one.

Escalation is keyed on **consecutive declines**, not offers: a twenty-hour
project is legitimately offered many times, and any progress resets the count.
Six passes in a row and nudge stops bringing it up.

## Gap detection

`freeBusy` across every calendar, inverted into free intervals, refreshed at
most every `FREEBUSY_CACHE_SECONDS` and always on a worker thread. A calendar
that errors is logged once and dropped for the run — a subscribed holiday
calendar returns `notFound` and would otherwise fail every tick.

Two details that are easy to get wrong, both caught against a real calendar:

- **The window starts in the past** (`FREEBUSY_LOOKBACK_MINUTES`). Inverting
  from `now` gives every current gap a start time of `now`, so `LEAD_IN_DELAY`
  never elapses and the agent never nags at all. The lookback preserves the
  gap's true start, so the lead-in only delays a gap that genuinely just opened.
- **The usable gap is capped** (`MAX_USABLE_GAP_MINUTES`). A free afternoon is
  not a five-hour work session; the cap is also the longest single sitting
  nudge will ask for.

## The daily capacity check

Once a day, per assignment: does the remaining work still fit in the free time
before it is due? The per-day cap is what makes the number mean anything — an
empty day reads as ~15 waking hours from freeBusy, so a flat discount would make
everything look comfortable forever. The calendar can only make a day *worse*
than the cap: it says when you are free, not that you will work all of it.

Warnings fire once per assignment and re-arm if it fits again and later slips
back. The measured availability is cached and feeds `pressure`, so the nag and
the warning always agree.

## Sizing assignments from their documents

The sheet needs an expected length for every row, and typing forty of them is
the kind of chore that kills a system before it starts. Drop the documents in:

```
~/assignments/
  CS 4262/          <- matches the Class column and the Tasks list
    HW1/            <- matches the Assignment column (fuzzy: "hw 1" == "HW1")
      spec.pdf
```

On save (debounced), nudge hashes the files, skips if nothing changed, extracts
the text, asks Claude for a total expected length plus type and any stated due
date, and DMs it for `approve` / `reject`. Nothing reaches the sheet until you
answer — a wrong number silently distorts every ranking and capacity warning.

## Notes

- `offers_today()` and the offers log are derived from the table rather than a
  counter, so they survive restarts and DST.
- `db.connect()` checkpoints the WAL on exit. Every CLI command is its own
  short-lived process, and an orphaned WAL takes the whole mirror with it.
- Everything is timezone-aware: UTC stored, local compared.
- Every external dependency degrades quietly. No Google means the sheet sync is
  off and gaps fall back to a synthetic schedule; no Anthropic key means sizing
  DMs you an error instead of a proposal; no Discord token means the CLI and
  `console` still work.
