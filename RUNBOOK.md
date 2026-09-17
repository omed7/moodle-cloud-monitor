# RUNBOOK — Moodle Cloud Monitor v2.5.0

**What it is:** every ~10 minutes, a GitHub Actions job checks Cihan University's
e-learning system (moodle.duhokcihan.edu.krd) for your course files, deadlines
and timetable changes — plus your grades — and messages Telegram.

**Repo:** github.com/omed7/moodle-cloud-monitor — **State:** Supabase (`monitor_state`, row id=1)

## The moving parts
| Piece | Role |
|---|---|
| cron-job.org | The trigger (~every 10 min) → calls GitHub |
| GitHub Actions | Runs `goldilocks.py` |
| Supabase | The memory — what the monitor has already seen |
| Cloudflare Worker | Relay that delivers messages to Telegram |

## Where the settings live
GitHub → repo → Settings → Secrets and variables → Actions

**Secrets:** `MOODLE_API_URL` · `MOODLE_API_TOKEN` · `TELEGRAM_BOT_TOKEN` ·
`ADMIN_CHAT_ID` · `TIMETABLE_URL` · `SUPABASE_URL` · `SUPABASE_KEY`
**Variables:** `CHAT_ID` (who gets alerts) · `IGNORE_COURSES` ("0" = nobody) ·
`QUIET_RUN` (1 = preview: admin-only) · `HEALTHCHECK_URL` (optional)

## System alerts (sent to ADMIN chat only) — what each one means
| Alert | When it fires | What it means · what to do |
|---|---|---|
| 🚨 MONITOR ABORTED | The run couldn't read its memory (Supabase) or a setting is missing | It refused to run blind — nothing was scanned, state untouched. Usually a missing/expired key: check the run log, fix, the next run proceeds. Won't re-DM for the same incident. |
| 🚨 SYSTEM ALERT | One of the four sources failed twice in a row | Usually a site hiccup (Moodle down, timetable page changed). Only that source pauses; the others keep alerting. Cause is listed; if it persists, read the run log. |
| ✅ CONNECTION RESTORED | After a SYSTEM ALERT, everything works again | Nothing to do — back to normal. |
| (log only) 🔇 Duplicate alert suppressed | Outage still ongoing but you already got one alert | Deliberate anti-spam; the run still goes red so you can see it. |

## When courses change — semester vs year (IMPORTANT)
Old courses are only **deleted by the university at a YEAR boundary** — inside a year,
previous-semester courses stay enrolled.

- **Semester flip (within a year, e.g. 3 → 4):** old courses remain → run the
  `list-courses` helper (Actions → Run workflow → mode: list-courses), then add the
  finished courses' IDs to `IGNORE_COURSES`. *(This is what the ignore list is for.)*
- **Year flip (e.g. year 2 → year 3):** the university deletes last year's courses
  automatically → check the helper; usually nothing to do.

To update the list: Variables → `IGNORE_COURSES` → paste IDs, comma-separated.
If the variable doesn't exist yet: New repository variable → name `IGNORE_COURSES` → paste.

## Restart after a long outage
1. Actions tab → open the latest run → read it. Most common: "MONITOR ABORTED — Cloud state unreachable".
2. **Token expired?** Moodle → Preferences → Web services → Manage tokens → copy the
   32-character value — paste ONLY that (if it arrives with a `wstoken: ` prefix, delete
   the prefix). → GitHub secret `MOODLE_API_TOKEN` → Update.
3. cron-job.org → job enabled? GitHub PAT stored there still valid? Failure-notifications ON.
4. First run: `QUIET_RUN`=1 → baseline messages arrive in YOUR admin chat (group untouched) → add the bot to the group → set `QUIET_RUN`=0.
5. Watch the next two runs: green + no unexpected messages = done.

## Soft-launch checklist (first time)
- [ ] Workflow pushed, secrets/variables in place
- [ ] Run 1: mode `scan`, `QUIET_RUN=1` → baseline messages arrive in your admin chat (format preview; group untouched)
- [ ] Add the bot to the group, then set `QUIET_RUN=0` → the group starts receiving only NEW changes from that moment on (never the baseline backlog)
- [ ] Run 2 → expect silence (baseline is current)
- [ ] cron-job.org re-enabled → first dispatch lands within ~10 min

## Troubleshooting
| Symptom | Likely cause | Fix |
|---|---|---|
| Runs red / MONITOR ABORTED | Supabase or config | Check the run log; fix missing/expired value |
| No runs at all | Trigger died | cron-job.org dashboard: job enabled? PAT valid? |
| "token rejected" in log | Token re-minted/revoked | Re-mint, update secret (see Restart #2) |
| No Telegram messages | Worker or bot token | Test the worker URL / re-check `TELEGRAM_BOT_TOKEN` |
| Duplicate broadcast | Overlapping runs | Concurrency guard is on; check for manual runs |

## Notes & quirks
- The university leaves **course dates junk** (some say 2020). We never rely on dates —
  watching = enrollment + `IGNORE_COURSES`.
- Cloudflare fronts the Moodle; the script sends an explicit User-Agent to stay unblocked.
- Supabase free tier pauses after ~7 days of **zero** activity — never matters while
  running; if parked for weeks, one dashboard tap wakes it.
- If alarms feel silent for a whole day: check Actions → latest runs. Green runs and
  no messages = genuinely quiet. (And if you ever set up Healthchecks, that covers it too.)
