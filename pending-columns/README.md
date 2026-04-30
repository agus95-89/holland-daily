# Pending Columns (review queue)

This directory holds **weekly column drafts awaiting human review** before
publication on the HARRO LIFE site.

## How the flow works

1. **Thursday 8-12 UTC** — `weekly-column.yml` runs, generates a draft, writes
   it here (`pending-columns/auto-{ISO-week}-{category}.md`), and emails the
   editorial team a preview via Resend.
2. **Editorial review** — team reviews the email; if changes are needed they
   edit the attached `.md` and reply to `suga@harrojp.com`.
3. **Publish or skip** — in a follow-up Claude Code session, the user says:
   - `{week} のコラム公開して` → Claude moves the file from
     `pending-columns/` to `harro-life-site/src/content/columns/`, commits to
     both repos, pushes. Cloudflare Pages auto-deploys.
   - `{week} のコラム見送り` → Claude moves the file from
     `pending-columns/` to `pending-columns/_skipped/` so the idempotent
     check still treats the week as "handled" (no re-generation on the next
     cron fire).

## Why a separate directory (and not an under-`_archive/` subdir)?

Pending columns are **not yet published** — keeping them outside
`harro-life-site/` avoids any risk of Astro accidentally building them. The
idempotency guard in `column_generator.py` checks both:
- `pending-columns/auto-{week}-*.md` (already drafted, awaiting review)
- `harro-life-site/src/content/columns/auto-{week}-*.md` (already published)

so a second cron fire in the same week won't generate a duplicate.

## Manual operations

```bash
# Generate a draft locally (uses real Anthropic + Unsplash APIs):
python -m src.column_generator

# Render the email preview HTML without sending (no API keys needed):
python -m scripts.smoke_column_html_dump

# Send a smoke-test review email to the editorial team:
REVIEW_TO=suga@harrojp.com python -m scripts.smoke_column_email
```
