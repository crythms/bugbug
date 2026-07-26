# Hackbot Bugzilla Poller

Polls one or more Bugzilla searches for untriaged bugs and feeds them to the
**`frontend-triage`** hackbot agent, keeping a fixed number of runs in flight and
never triaging the same bug twice.

## How it works

1. On startup, parse every configured query and run it once, logging the hit
   count. A malformed URL exits non-zero; a Bugzilla hiccup is logged and
   retried on the next tick.
2. Every `POLL_INTERVAL_SECONDS`, count how many of **our** runs are still
   `pending`/`running` (`GET /runs`), and work out how many slots are free.
3. If there is room, sweep up every bug ID already dispatched, run the queries,
   interleave the results, and dispatch the oldest bugs not in that set —
   `POST /agents/frontend-triage/runs`.
4. Repeat. Slots refill as runs finish.

The agent records its triage comment rather than posting it, so nothing reaches
a real Bugzilla bug until someone clicks **Apply** in the Hackbot UI.

## Configuring queries

**You can have as many queries as you like — one env var each.** Build the search
in Bugzilla's advanced search, run it, copy the URL out of your browser's address
bar, and add a `BUGZILLA_QUERY_<LABEL>` line to the query block in `deploy.sh`.
Add another line for another query:

```bash
ENV_VARS="${ENV_VARS}|BUGZILLA_QUERY_NEW_TAB_PAGE=https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=New%20Tab%20Page&bug_status=UNCONFIRMED&bug_status=NEW&f1=assigned_to&o1=equals&v1=nobody%40mozilla.org&bug_type=defect&bug_severity=S1&bug_severity=S2"
ENV_VARS="${ENV_VARS}|BUGZILLA_QUERY_TABBED_BROWSER=https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=Tabbed%20Browser&..."
ENV_VARS="${ENV_VARS}|BUGZILLA_QUERY_ADDRESS_BAR=https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=Address%20Bar&..."
```

Env vars are built inline in `deploy.sh` and passed with `--set-env-vars`, the
same way `hackbot-pulse-listener` and `hackbot-ui` do it. The `^|^` prefix on the
string makes gcloud split on `|` rather than `,`, so a pasted URL containing a
comma cannot be mangled — `|` is not a legal URL character, `,` and `@` are.

Those particular filters are not a recommendation — they just happen to reproduce
one hand-picked list. Write whatever search you actually want.

Nothing needs escaping or rewriting. Percent-encoding is already correct because
the browser produced it, and display-only parameters (`list_id`, `query_format`,
`columnlist`, `order`) are stripped automatically. Unrecognised search fields are
passed through untouched, so unusual searches — including a _Bug Numbers_ list,
which arrives as `bug_id=1,2,3` — keep working without a code change here.

The label after the `BUGZILLA_QUERY_` prefix is yours to choose — it only names
the query in the logs, so you can see which search each dispatched bug came from:

```
INFO app.bugzilla: query new_tab_page: 24 bugs
INFO app.bugzilla: query tabbed_browser: 15 bugs
INFO app.bugzilla: query address_bar: 4 bugs
```

Each query is one request, so several cost nothing meaningful. Results are
**interleaved** rather than concatenated — with the counts above, the dispatch
order starts `new_tab_page, tabbed_browser, address_bar, new_tab_page, …`, so the
24-bug query cannot starve the 4-bug one. A query that runs dry drops out of the
rotation. A bug matching two queries is dispatched once.

To remove a query, delete its line. To pause one without losing the URL, comment
it out.

### Check the count before you deploy a query

Every bug a query matches will eventually get a run, and every run costs real
money. Which filters you want is your call — but Bugzilla search terms cover far
more than they look like they do, so it is worth knowing the number first.
Measured against live Bugzilla, all for `Firefox :: New Tab Page`:

| Query                                                          | Matches  |
| -------------------------------------------------------------- | -------- |
| open + unassigned                                              | **1363** |
| ...restricted to `UNCONFIRMED`/`NEW`                           | 1346     |
| ...and `type=defect`                                           | 758      |
| ...and `severity` `S1`/`S2`                                    | **24**   |
| for comparison: `Firefox :: Tabbed Browser`, open + unassigned | **2358** |

`__open__` includes `ASSIGNED` and `REOPENED` and reaches back to bugs filed in
2011, so it is two orders of magnitude away from "the bugs I actually want
triaged". Status, severity, type, keywords, a whiteboard tag, a creation-date
cutoff — all of them are just fields in Bugzilla's advanced search, and none of
them are applied for you.

Check a new query before deploying:

```bash
cd services/hackbot-bug-poller
MAX_IN_FLIGHT=0 uv run --package hackbot-bug-poller python -m app
```

`MAX_IN_FLIGHT=0` leaves no capacity, so the queries run and log their hit counts
but nothing is dispatched. Confirm the count matches what Bugzilla showed you.

(Run it from this directory, not the repo root. Every service in this workspace
ships a package named `app`, and they share one virtualenv, so `python -m app`
from the root can pick up a different service's.)

## Not running the same bug twice

Bugzilla keeps handing back bugs we have already triaged, and always will: the
agent only _records_ its comment, so the bug stays open and unassigned and never
leaves the query's result set. Dedupe therefore happens entirely on our side.

Once per tick the poller sweeps `GET /runs?agent=frontend-triage` into a set of
every bug ID ever dispatched, and filters the candidate list against it in
memory. That is one request per 100 runs in history, rather than one request per
candidate bug — a query matching 2000 bugs would otherwise mean up to 2000 round
trips every minute.

It is durable (the `runs` table, not an in-memory cache that a restart would
lose) and it counts runs started by anyone, so a bug someone triaged by hand in
the UI will not be picked up again.

The reverse is deliberately not true: manual runs carry no `source` field, so
they do **not** consume a concurrency slot. Manual work never starves the
pipeline, and the pipeline never starves manual work.

There is no meaningful double-dispatch window. `POST /agents/.../runs` commits
the `runs` row before it returns, so a dispatched bug is visible to the very next
check — seconds later, not when triage eventually finishes. And a single
always-on process means two ticks cannot overlap.

## Settings

| Variable                         | Default                        | Meaning                                                                                                |
| -------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------ |
| `HACKBOT_API_URL`                | —                              | set once in `deploy.sh`                                                                                |
| `HACKBOT_API_KEY`                | —                              | from Secret Manager (`external-api-key`)                                                               |
| `BUGZILLA_API_URL`               | `https://bugzilla.mozilla.org` |                                                                                                        |
| `BUGZILLA_API_KEY`               | unset                          | optional; **anonymous by default**, which is what keeps security-restricted bugs out of reach entirely |
| `BUGZILLA_QUERY_*`               | —                              | one pasted buglist URL per query                                                                       |
| `AGENT_NAME`                     | `frontend-triage`              |                                                                                                        |
| `POLL_INTERVAL_SECONDS`          | `60`                           |                                                                                                        |
| `MAX_IN_FLIGHT`                  | `1`                            | our runs only; `0` = inspect without dispatching                                                       |
| `STALE_RUN_MINUTES`              | `180`                          | when a non-terminal run stops holding a slot                                                           |
| `MODEL` / `MAX_TURNS` / `EFFORT` | unset                          | passed to the agent; unset means hackbot's own defaults                                                |

Defaults live in `app/config.py` and nowhere else — `deploy.sh` names only the
values that are environment-specific (the API URL and the queries), matching how
`hackbot-pulse-listener` keeps its own tuning knobs out of its deploy script. To
change one on a running service without a rebuild:

```bash
gcloud beta run worker-pools update hackbot-bug-poller \
  --region us-central1 --update-env-vars MAX_IN_FLIGHT=5
```

That holds until the next deploy, since `--set-env-vars` replaces the whole
environment. Add it to `ENV_VARS` in `deploy.sh` to make it permanent.

`MAX_IN_FLIGHT` starts at **1** on purpose. A failed run still counts as triaged
and is never retried, so a broken agent Job — bad credentials, bad image, no
quota — would otherwise work through the whole query producing nothing and
permanently retiring every bug in it. One at a time caps that at a single bug.
Watch a run reach `succeeded` end to end, then raise it. Throughput is
`MAX_IN_FLIGHT ÷ run duration`, so this is also the dial for how fast a backlog
drains.

`STALE_RUN_MINUTES` exists because run completion arrives as a Pub/Sub event — a
lost event would otherwise leave a run `running` forever, holding a slot for
good. It is deliberately well below the 8h job timeout: erring short only risks
briefly exceeding `MAX_IN_FLIGHT`, while erring long can stall the pipeline for
hours. Note it is a guess until you have timed a real run — if runs routinely
take longer than this, the poller will treat live runs as abandoned and exceed
the cap.

## Run locally

```bash
cd services/hackbot-bug-poller

export HACKBOT_API_URL=https://hackbot-api.../ HACKBOT_API_KEY=...
# One export per query -- add as many as you want.
export BUGZILLA_QUERY_NEW_TAB_PAGE='https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=New%20Tab%20Page&bug_status=__open__'
export BUGZILLA_QUERY_ADDRESS_BAR='https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=Address%20Bar&bug_status=__open__'
export MAX_IN_FLIGHT=0        # look without touching

uv run --package hackbot-bug-poller python -m app
```

Single-quote the URLs in a shell: `&` would otherwise background the command.

To run it in the container instead, `docker-compose.dev.yml` builds the image and
live-mounts `app/`, reading the same variables from the repo-root `.env`:

```bash
docker compose -f docker-compose.dev.yml up --build
```

## Test

```bash
uv run --package hackbot-bug-poller pytest services/hackbot-bug-poller/tests
```

## Deploy

Cloud Run worker pool (no HTTP). `PROJECT=my-proj ./deploy.sh`.

A worker pool rather than a Job + Cloud Scheduler: the poll interval is an env
var rather than a cron expression, there is no Scheduler job or IAM to manage,
and a single always-on process is what rules out overlapping ticks.
