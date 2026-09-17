# moodle-cloud-monitor

Watches the Cihan University e-learning system for course-file, deadline and
timetable changes — plus private grade alerts for the admin — and reports to
Telegram through a Cloudflare Worker relay.

Every ~10 minutes, GitHub Actions runs `goldilocks.py`, which diffs the current
state against its memory (a single Supabase row) and sends only what changed.

- **Operations, restarts, semester flips:** [RUNBOOK.md](RUNBOOK.md)
- **v2.5.0 change summary:** [CHANGES.md](CHANGES.md)
