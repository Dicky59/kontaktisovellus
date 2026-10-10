# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Lead-generation and cold-calling toolkit for Finnish accounting firms (tilitoimisto). A three-step CSV pipeline of standalone scripts at the repo root builds a call list, and `soittolista-app/` is a local web app for working through it. Code comments, CLI help, CSV column names and UI strings are in Finnish; keep that convention.

There is no test suite, linter or build step.

## Purpose and scope

- Goal: B2B phone sales to Helsinki accounting firms with at least 3 employees. Contact is by phone only, so a usable phone number is the key field.
- Current scope is Helsinki only. Espoo and Vantaa may be added later (the scripts take `--location` / `--city`).
- Pipeline in short: PRH/YTJ open data → base list → Google Places for phone numbers → import into the call-list app.
- Chains (Talenom, Accountor, Azets, Administer) are wanted too. For them only a head office / main switchboard number is needed; the call goal is to reach the IT lead or a decision maker.
- Firms with a mobile number (04x, 050) are likely sole proprietors, so they are called last. The app's size estimate `1-2` removes a firm from the queue.
- Tooling must stay free or cheap. No paid services beyond the Google Places free tier.

## Constraints and safety

- **Public repo.** Never commit `.env`, `data/` (company contact data), or `soittolista-app/data/` (call history). All are gitignored; run `git status` before committing.
- **Google Places costs money.** Free tier is 1,000 Text Search calls per month. Always `--dry-run` or `--limit N` before a new run, never raise `--max-queries` casually, and never delete `data/places_cache.json` (re-runs are free only because of it). The API key lives in `.env` only; never print or paste it.
- **Call history is irreplaceable.** `soittolista-app/data/soittolista.db` holds call logs, callbacks and do-not-call flags. Do not delete it or change the schema without a migration path and a backup copy first (daily backups are in `data/backups/`).
- Do-not-call (`Ei markkinointisoittoja`) must always be respected: such companies never enter the queue and are never overwritten by imports.

## Working conventions

- Make changes on a feature branch, not directly on `main`.
- Use plan mode before larger changes (new DB columns, import logic, queue rules).
- Keep the app a single `app.py` plus a single `static/index.html` with no build step or frontend framework.
- To check a change to the app, start it with `python run.py` in `soittolista-app/` and test in the browser; to check pipeline changes, use `--dry-run` or the cache rather than live API calls.

## Pipeline (repo root, run from the root with the venv active)

Root `requirements.txt` only needs `requests` and `python-dotenv`. Data files live in `data/` (gitignored, with `.env`).

1. `python 01_fetch_prh.py` — queries the PRH/YTJ open API (v3, no key) by industry code and by name, dedupes by y-tunnus, filters inactive firms (`endDate`, bankruptcy/liquidation situations). Writes `data/prh_pohja.csv`. Useful flags: `--forms 16`, `--only-lines 69201`, `--location`, `--all`. The usual run is `python 01_fetch_prh.py --forms 16 --only-lines 69201` (limited companies, TOL 69201 = bookkeeping; 69202 is auditing and not wanted).
2. `python 02_enrich_places.py` — adds phone numbers via Google Places Text Search (New). Needs `GOOGLE_PLACES_KEY` in `.env`. **Every query costs money**: use `--dry-run` / `--limit N` first; `--max-queries` (default 250) is a hard safety cap. Results are cached in `data/places_cache.json` keyed by y-tunnus (chain searches keyed `ketju:<query>`), so re-running or tuning match logic is free. By default only firms with a website and a Helsinki address are processed (`--all` includes the rest, `--city` changes the city). Outputs `data/prh_rikastettu.csv` (confident matches with phone) and `data/prh_tarkista.csv` (uncertain/no-phone, reason in `syy`). `--chains` is a separate mode that searches big chains' offices directly from Google (PRH has one row per chain), writing `data/ketjut_rikastettu.csv`.
3. `python 03_clean_leads.py <input.csv> [output.csv]` — cleans an Apify Google Maps scraper export (closed removal, dedupe by placeId/phone/domain, chain flag, A/B/C priority). This is an alternative source to steps 1–2, not a continuation of them.

Match quality in step 2 (`evaluate`): name similarity via difflib over distinguishing words (generic words/legal suffixes stripped), plus same website domain and same postcode → `varma` / `todennäköinen` / `epävarma` / `ei`.

## Call-list app (`soittolista-app/`)

FastAPI + SQLite, single file `app.py`, static single-page frontend in `static/index.html`. Own `requirements.txt` and `.venv`.

- Start: `soittolista-app/start.bat` (creates venv on first run, then `run.py`), or `python run.py` in that directory. Serves on `127.0.0.1:8765` and opens the browser; if the port is taken it just opens the browser.
- DB: `soittolista-app/data/soittolista.db`, created on import of `app` (`init_db()` runs at module load), with a daily backup in `data/backups/` (keeps 14).
- Import (`POST /api/import`) accepts the pipeline CSVs and raw Apify exports (`FIELDS` maps alternative column names), auto-detects delimiter/encoding. Companies are deduped across imports by place_id → maps_url → phone → domain; chains skip domain matching because branches share a website. A match only fills in missing fields and links the company to the new list.
- Call queue is defined by `QUEUE_WHERE`: not do-not-call, has phone, size ≠ `1-2`, and status is `Ei soitettu`, due `Soita uudelleen`, or `Ei vastaa` from a previous day. Logging outcome `Ei markkinointisoittoja` sets `do_not_call`.
- `CHAIN_KEYWORDS`, `norm_phone`, `domain_of`, `is_real_website` and the priority rules are duplicated between `03_clean_leads.py` and `app.py` (and `domain_of`/`norm_phone` also in `02_enrich_places.py`); keep them in sync when changing one.
