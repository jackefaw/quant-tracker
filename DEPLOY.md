# Running & sharing the web app

## A. Run locally first (2 minutes)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Your browser opens at `http://localhost:8501`. Paste your FMP key in the sidebar, press
**Score stocks**. That `localhost` link only works on your machine — Part B makes it public.

## B. Put it online with a public link (free)

Streamlit Community Cloud hosts it for free and gives you a URL like
`https://yourname-quant-tracker.streamlit.app`. You need a free GitHub account (that's where
the code lives). One-time, ~10 minutes.

### 1. GitHub account
Sign up at github.com.

### 2. Create a repository with the files
- **+** (top right) → **New repository** → name it `quant-tracker` → **Private** → **Create**.
- Click **uploading an existing file**.
- Drag in every file: `app.py`, `tracker.py`, `pipeline.py`, `providers.py`,
  `scoring.py`, `market_regime.py`, `backtest.py`, `storage.py`, `requirements.txt`,
  `watchlist.txt`, and the `.streamlit` folder.
- **Do not upload `.env`** — your key never goes in the repo. Click **Commit changes**.

### 3. Deploy
- Go to **share.streamlit.io** → sign in with GitHub → **Create app** → **Deploy from GitHub**.
- Repository `quant-tracker`, branch `main`, main file `app.py` → **Deploy**.
- First build takes a couple minutes.

### 4. Add your API key as a secret
- In the app page: **⋮** → **Settings** → **Secrets**, paste and save:
  ```
  FMP_API_KEY = "your_actual_key_here"
  ```
Copy the `.streamlit.app` URL and share it.

## C. (Optional) Make revision history survive on the cloud

Streamlit Cloud wipes its local disk when the app sleeps or redeploys, so the SQLite
snapshot file doesn't persist there. To keep the revisions radar working online, point it
at a free hosted Postgres — 5 minutes:

1. Make a free database at **neon.tech** (or supabase.com). Copy its connection string
   (looks like `postgresql://user:pass@host/dbname`).
2. In your Streamlit app's **Secrets**, add a second line:
   ```
   DATABASE_URL = "postgresql://user:pass@host/dbname"
   ```
That's it — the storage layer auto-detects it and writes snapshots there instead of SQLite.
Locally, with no `DATABASE_URL` set, it keeps using the SQLite file. Same code, both places.

## Decide before you share widely

- **Whose key pays?** With your key in Secrets, every visitor's lookups burn *your* free
  quota (250 calls/day). Fine for a few friends; for a public post, leave Secrets empty so
  each person pastes their own key, or upgrade your FMP plan.
- **Data licensing.** FMP's free tier is personal-use. Publishing their data on a public
  app generally wants a paid/commercial license — worth a 2-minute terms check first.
- **The "advice" line.** Once people act on grades you publish, you're brushing up against
  investment advice. The app already shows a "research only, not advice" line; keep it, and
  be deliberate here once you're licensed.

## Keep the daily quota sane

The provider caches every call to disk per day, so re-runs and slider moves are free. Per
fresh run it's roughly: 5-6 calls per ticker, +3 for SPY, +2 macro, +11 if breadth is on,
plus peers if sector mode is on. A 7-name watchlist with breadth is ~55 calls — comfortably
under the free 250/day. Turn off breadth or sector mode to stretch it further.
