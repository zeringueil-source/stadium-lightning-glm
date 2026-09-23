# GLM lightning pipeline for the Stadium Lightning Dashboard

A tiny scheduled job that pulls real satellite-detected lightning flashes
(NOAA's GOES-19 Geostationary Lightning Mapper — total lightning, in-cloud
and ground, not the ground-network-only feed the dashboard uses today) and
republishes them as a small JSON file the dashboard can fetch. No server to
run, no cost — it lives entirely in a free GitHub repo.

## What's in here

- `fetch_glm.py` — downloads the last 15 minutes of GLM flash data from
  NOAA's public S3 bucket and writes `data/lightning.json`.
- `.github/workflows/update-lightning.yml` — runs that script every 5
  minutes on GitHub's infrastructure and commits the result.
- `requirements.txt` — the two Python packages the script needs.
- `data/lightning.json` — a placeholder; the first Actions run replaces it.

## Setup (one-time)

1. **Create a new GitHub repository.** It needs to be **public** — a
   private repo's files aren't fetchable by URL without a login token,
   which the dashboard (running in your browser) can't provide.

2. **Push everything in this folder to it**, keeping the folder structure
   exactly as-is (the `.github/workflows/` path matters — that's how
   GitHub finds the workflow).

   ```
   git init
   git add .
   git commit -m "GLM lightning pipeline"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```

3. **Turn on write access for Actions.** In the repo: Settings → Actions →
   General → scroll to "Workflow permissions" → select **"Read and write
   permissions"** → Save. This is off by default on a new repo, and without
   it the job will run but fail silently at the last step (it can't push
   the updated JSON back).

4. **Run it once by hand** to check it actually works before waiting on the
   schedule: go to the **Actions** tab → "Update GLM lightning data" →
   **Run workflow**. After it finishes (a minute or so), check the run's
   log, and check that `data/lightning.json` in the repo now has a real
   `generated_at` timestamp and a non-empty `flashes` array instead of the
   placeholder.

   If it fails: the log will say why. The two likeliest issues are the
   permissions step above, or NOAA having changed something about the file
   format since this was written — I verified the variable names against
   two independent real sources, but couldn't actually run this against a
   live file before handing it off, so this first run is the true test.

5. **Once it's working**, your dashboard's data URL is:

   ```
   https://raw.githubusercontent.com/<your-username>/<your-repo>/main/data/lightning.json
   ```

   That's the value to paste into the dashboard once GLM support is wired
   up there (next step, separate from this pipeline).

## Notes

- Rebuilds fresh from S3 every run — it doesn't depend on previous runs, so
  one missed or failed run just means a slightly different flash list next
  time, never stale drift or duplicates piling up.
- Covers a generous CONUS-ish box (15–55°N, 130–60°W). Widen `BBOX` in
  `fetch_glm.py` if venues outside the continental US ever get added.
- GOES-19 is the current operational GOES-East satellite (confirmed via
  NOAA/NESDIS, 2025). If that ever changes, update `BUCKET` in
  `fetch_glm.py` to the new satellite's bucket name.
