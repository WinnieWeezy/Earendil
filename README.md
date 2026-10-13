# Earendil

Earendil tracks stock trades disclosed by members of Congress and flags the ones worth a closer look:
late filings, trades in sectors a member's committee oversees, trades near that committee's meetings,
and unusually well-timed trades. It runs itself on GitHub every four hours, publishes a report you can
install on your phone like an app, and sends a push notification when something gets flagged.

A flag is a pattern worth examining, not evidence of wrongdoing. The report says so on every page.

## How it fits together
- **GitHub Actions** runs `earendil.py` on a schedule, free.
- **GitHub Pages** publishes the report from the `docs` folder.
- **Your phone** opens the report from its home screen and gets alerts through the free ntfy app.

Do the one-time setup on a computer (about 15 minutes). After that, everything happens on your phone.

## One-time setup

**1. Create the repository.** Sign up at github.com, then create a new repository named `earendil`.
Make it **public**: free accounts can only publish Pages sites from public repositories. Everything in it
is public record anyway, as long as you keep your keys in secrets (step 3).

**2. Upload the files.** Unzip `earendil.zip`, then on the repository page choose *Add file > Upload files*
and drag in everything, including the `.github` folder. On a Mac, press Cmd+Shift+Period in Finder to show
that hidden folder. If it won't upload, choose *Add file > Create new file*, type
`.github/workflows/earendil.yml` as the name, and paste in that file's contents.

**3. Add your keys as secrets.** *Settings > Secrets and variables > Actions > New repository secret*:

| Secret | What it's for | Required |
|---|---|---|
| `FMP_API_KEY` | Trade data, sectors, prices (financialmodelingprep.com) | Yes |
| `NTFY_TOPIC` | Phone alerts. Make up a long, hard-to-guess name like `earendil-k7q2-pocatello` | Recommended |
| `CONGRESS_GOV_API_KEY` | Committee meeting timing (free at api.congress.gov/sign-up) | Optional |
| `WEBHOOK_URL` | Discord or Slack alerts | Optional |

Check that your FMP plan includes the Senate and House trading endpoints.

**4. Turn on the website.** *Settings > Pages*, then under *Build and deployment* choose *Deploy from a branch*,
branch `main`, folder `/docs`. Save. Your address will be `https://YOUR-USERNAME.github.io/earendil/`.

**5. Run it once.** *Actions* tab > *Earendil* > *Run workflow*. A green check means it worked. A red X means
something failed, and GitHub emails you the log.

**6. Tell Earendil its address.** Open `settings.html` in a browser, enter your Pages address under
*Your report's web address*, adjust anything else, save `scraper_config.json`, and upload it to the
repository, replacing the existing one. Leave every key field blank; secrets handle those.

## On your phone
- **Install the app:** open your Pages address. On iPhone, tap Share > *Add to Home Screen*. On Android,
  open Chrome's menu > *Install app* or *Add to Home screen*. It opens full-screen with the evening-star icon
  and keeps the last report for offline viewing.
- **Get alerts:** install **ntfy** from the App Store or Google Play and subscribe to your `NTFY_TOPIC`.
  Tapping an alert opens your report.
- **Run it now:** the GitHub mobile app can trigger *Run workflow* from the Actions tab.

## Running it on a computer instead
    pip install -r requirements.txt
    python earendil.py --demo      # fictional data, writes docs/demo.html
    python earendil.py             # run once (keys can live in scraper_config.json locally)
    python earendil.py --loop 60   # every hour

## Good to know
- **What's public:** the report, `docs/trade_log.csv`, and `data/trades.json`. All of it is derived from
  public disclosures. Never put keys in `scraper_config.json` on GitHub.
- **Schedules:** GitHub's scheduled runs can start a few minutes late. GitHub also pauses schedules in
  repositories with no activity for 60 days; Earendil's own commits normally prevent that, but if it ever
  stops, re-enable it from the Actions tab.
- **Committee timing** builds its meeting cache gradually over the first several runs.
- **Editorial choices** like which committees oversee which sectors are in `COMMITTEE_SECTORS` in
  `earendil.py`. Adjust them, and keep the methodology section honest about it.
- **FMP renames fields occasionally.** If runs succeed but no trades appear, compare the endpoint URLs in
  `scraper_config.json` with FMP's current documentation.
- **Your v2 settings** still load. Open them in `settings.html` to add the new options.
- **Well-timed flags wait for the full window.** A trade younger than the performance window
  (30 days by default) is rechecked on later runs and only flagged once the window has closed. You get
  an alert when an earlier trade earns the flag.
- **Amended filings** are merged into the original trade instead of showing up twice. The report shows
  "amended" with the later date, and the late-filing check uses the original disclosure date.
- **Flags that need a missing key switch themselves off** with a warning in the run log, for example
  committee timing without `CONGRESS_GOV_API_KEY`.
