# Screenshotting

Takes bestseller-rank proof screenshots of Amazon category pages and saves them to Google Drive. Runs on GitHub Actions.

## How it works
1. `dispatch` (every hour) reads the control sheet. A job runs when the current local time is inside its window and the hour is in `run_hours`.
2. Each due data sheet is split into chunks of `chunk_rows` rows; each chunk runs as its own job, up to 6 at a time.
3. Each row's page is opened; if the keyword (book title) is on it, the visible area is saved to the sheet's Drive folder, with the URL, category and time stamped on top.
4. The best (lowest) rank seen per URL is kept in the `<sheet title> filtered` folder.
5. No repeats within an hour: before each row, the job skips it if the sheet's folder already has that row's screenshot from this local hour (this also skips duplicate rows in a sheet). A chunk where every row worked leaves a marker in `_screenshot_run_markers` (inside the Drive folder), so later runs that hour skip it; a chunk with errors is retried by the next trigger, for the failed rows only. Run the workflow by hand with `force` ticked to redo an hour anyway.
6. Failures post to Slack when `SLACK_WEBHOOK_URL` is set.

## Control sheet
- Tab `Jobs`: A sheet URL, B start `YYYY-MM-DD HH:MM`, C stop `YYYY-MM-DD HH:MM`, D timezone (e.g. `Asia/Kolkata`; empty = UTC).
- Tab `Settings` (optional): `run_hours` = comma list of local hours 0-23 (24 = midnight), default `9,19`; `chunk_rows`, default `120`, max `200`.
- Data sheet: header row, then A category, B URL, C country, D keyword.
- Share the control sheet and every data sheet (view) and the Drive folder (edit) with the service account.

## Secrets (repo Settings > Secrets and variables > Actions)
- `GOOGLE_CREDENTIALS_JSON`: service account key, saved on ONE line (a multi-line value makes GitHub drop the job list as "may contain secret").
- `CONTROL_SHEET_ID`, `DRIVE_FOLDER_ID`.
- `SLACK_WEBHOOK_URL` (optional): alerts when a run fails, rows error, or dispatch hits a problem.

## Triggers
- GitHub schedule at minute 35 every hour (GitHub may delay or skip these).
- Weekly keepalive (Monday 03:50 UTC): re-enables the workflow so GitHub's 60-day inactivity switch-off for public repos never applies. An external caller can also send `PUT /repos/<owner>/<repo>/actions/workflows/screenshot.yml/enable` weekly as a backup.
- Optional external hourly trigger: `POST /repos/<owner>/<repo>/actions/workflows/screenshot.yml/dispatches` with `{"ref":"main"}`.

## Local test
```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```
