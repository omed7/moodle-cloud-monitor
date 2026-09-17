# v2.5.0/v2.5.1 — change summary (review guide)

## v2.5.1 — deletion patience (bug fix)
- **"TASK DELETED" now requires 2 consecutive missing runs** before firing — a one-run
  flicker produces zero messages (previously: instant false alarm).
- **Calendar-feed items are cleaned up silently** when they disappear; deletion alerts
  apply to **real assignments only** ("assign_" items).
- Fixes the false *"The professor has removed it before the deadline!"* class
  (evidence: last year's "Feedback closes" message).

## Your seven items
1. **MOODLE_API_TOKEN** — code reads it; no fallbacks. (Secret already updated ✓)
2. **TIMETABLE_URL** — secret points at year-2 Group A; the silent year-1 fallback is
   **gone** — missing URL = loud error, never the wrong page.
3. **IGNORE_COURSES** — now a repo **Variable** ("0" = nothing ignored). A `list-courses`
   helper mode reports your enrolled courses whenever the list needs updating.
4. **State store** — JSONBin → **Supabase** (`monitor_state`, row id=1). Load/save
   rewritten; save checks the response and retries once.
5. **CHAT_ID** — repo **Variable**, read fresh every run; **only** the IDs you put
   there receive broadcasts. People/groups/channels all fine.
6. **ADMIN_CHAT_ID** — system alerts **and your grades** (private: admin chat only,
   never the group).
7. **USERS_CONFIG** — deleted. No multi-user code; friends keep group alerts only.

## Reliability — the "never dies silently" batch
- **Loud death:** state/config unreachable → admin alert + non-zero exit → run goes red.
- **No spam:** repeated failures are de-duplicated via the Actions API (one alert per incident).
- **Per-scanner rollback:** a broken scanner no longer discards the healthy scanners'
  alerts; its state is left untouched, so the next good run re-detects everything.
- **QUIET_RUN=1 — preview mode:** the first run sends everything to YOUR admin chat
  only (group untouched) so you can see the format; flip to 0 when ready. The group
  never receives the backlog — only changes after the flip. State is baselined either way.
- **Concurrency guard:** stops two runs from overlapping (e.g. a slow run still going
  when cron-job.org fires the next tick) — overlap protection only, not a trigger.

## Behavior notes
- **Download buttons are back:** file alerts keep "⬇️ Tap to Download File" with your
  Moodle token inside the link — your explicit choice.
  (Trade-off, stated once: anyone with that link can act as you on Moodle.)
- **One trigger only:** cron-job.org. No GitHub backup schedule.
- **Calendar cap:** the "upcoming events" feed defaulted to 20 items; set to **50** —
  the maximum this Moodle build accepts (learned from the first live run: 100 is rejected).
- Timezone-aware timestamps (Iraq time) · TLS verification ON · explicit User-Agent
  (Cloudflare) · version bumps: Python 3.12, checkout@v5, setup-python@v6,
  aiohttp 3.14.3, bs4 4.15.0.

## Files in this review pack
- `goldilocks.py` — the full new script (~754 lines)
- `moodle.yml` — the new workflow
- `RUNBOOK.md` — operations guide
