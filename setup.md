# Reelz Stream Resolver — Setup (Render)

Server-side resolver: takes a TMDB id, runs the right site adapter
(light HTTP or full Playwright render, depending on the source), returns
a clean JSON stream URL, and caches it in Postgres with real expiry read
from each source's own token — not a guessed TTL.

**This version targets Render as the deployment platform.** An earlier
version of this project targeted AWS Lambda specifically — that path
(container build → ECR → Lambda → IAM roles → service quota requests)
turned out to be a lot of ceremony for pre-traffic scale, and has been
removed from this codebase (`api/lambda_handler.py`,
`api/lambda_sweep_handler.py`, and the `mangum` dependency are gone).
Everything else — the resolver engine, adapters, caching, stats — is
unchanged and platform-agnostic; it runs under plain `uvicorn` regardless
of host.

---

## 1. Local development (do this first, before deploying anywhere)

Get the resolver working on your own machine before pushing to Render —
debugging scraping logic and deployment config at the same time is a bad
time.

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps          # Linux only; skip on Mac/Windows
```

### Local Postgres — two options

**Option A: Docker Compose (recommended)** — spins up Postgres +
the resolver together, matches production behavior closely:
```bash
docker compose up -d --build
docker compose logs -f resolver
```
This reads `POSTGRES_PASSWORD` from a `.env` file — copy `.env.example`
to `.env` first and set a real password.

**Option B: Just Postgres via Docker, run the app directly**
```bash
docker run --name reelz-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=reelz \
  -p 5432:5432 -d postgres:16

export DATABASE_URL="postgresql://postgres:devpass@localhost:5432/reelz"
export RESOLVER_DEPLOYMENT=ec2   # generic "long-running host" mode, see api/main.py

uvicorn api.main:app --reload --port 8000
```

### Test it

```bash
curl "http://localhost:8000/health"
curl "http://localhost:8000/resolve?tmdb_id=27205&media_type=movie"
```

If the second call returns a real JSON stream URL, the core logic works
and you're ready to deploy with confidence.

### Run the tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

**Before trusting any adapter beyond `vidsrc_to.py` in production**:
that adapter's detection logic was built directly against real DevTools
captures. `vidlink_pro.py` is explicitly marked in its own docstring as
an unverified structural template — capture real traffic against it the
same way before relying on it.

---

## 2. Get a Postgres database — Supabase (recommended)

1. Go to supabase.com, create a project (free tier is fine to start)
2. Wait for provisioning (~2 minutes)
3. Project Settings → Database → Connection string
4. Select the **"Transaction pooler"** tab, **not** "Direct connection" —
   short-lived pooled connections behave much better under a web app's
   normal request pattern than a direct connection, especially once you
   have concurrent traffic
5. Copy the URI (port **6543**), replace the `[YOUR-PASSWORD]`
   placeholder with your actual database password — this is the single
   most common mistake, don't leave the literal placeholder text in there
6. If your password contains special characters (`@ # % / : ?`), either
   URL-encode them or reset the password to alphanumeric-only to avoid
   the connection string parsing incorrectly

You do **not** need to create a database or any tables manually — the
app creates its own `resolved_streams` table automatically on first
successful connection (`core/cache.py`'s `StreamCache.start()`, using
`CREATE TABLE IF NOT EXISTS`).

---

## 3. Push your code to GitHub

If it isn't already:
```bash
cd reelz_resolver
git init
git add .
git commit -m "resolver: initial version"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Make sure `Dockerfile`, `requirements.txt`, `render.yaml`, `core/`,
`adapters/`, and `api/` end up at the **repo root** (or note the actual
path if nested — Render needs to know where the Dockerfile is, see
below).

---

## 4. Deploy on Render

### Option A: Blueprint (uses the included `render.yaml`, fewer manual clicks)

1. Push `render.yaml` to your repo (already included in this project)
2. Render Dashboard → **New** → **Blueprint**
3. Connect your GitHub repo, select it
4. Render reads `render.yaml` and shows you the `reelz-resolver` service
   it's about to create — confirm
5. It will prompt you to fill in the one secret marked `sync: false` —
   paste your **Supabase connection string** as `DATABASE_URL`
6. Click **Apply** / **Create**

### Option B: Manual web service (if you skip the Blueprint)

1. Render Dashboard → **New** → **Web Service**
2. Connect your GitHub repo
3. **Runtime**: Docker (Render auto-detects the `Dockerfile`)
4. **Region**: pick one close to you/your users — doesn't need to match
   anything specific about the streaming sources themselves
5. **Instance type**: **Starter** at minimum. Do NOT use the Free tier
   for this — free instances sleep after 15 minutes of inactivity, which
   reintroduces the exact Chromium cold-start problem a warm process is
   supposed to avoid. Starter (~$7/mo) keeps it running continuously.
6. **Environment Variables**, add:
   - `DATABASE_URL` = your Supabase connection string
   - `RESOLVER_DEPLOYMENT` = `ec2`
7. **Health Check Path**: `/health`
8. Click **Create Web Service**

### What happens next

Render builds your Dockerfile (this takes a few minutes — downloading
the base image, installing Python deps, downloading Chromium) and
deploys it. Watch the build logs in Render's dashboard. Once it shows
"Live," Render gives you a URL like `https://reelz-resolver.onrender.com`.

Test it:
```bash
curl "https://reelz-resolver.onrender.com/health"
curl "https://reelz-resolver.onrender.com/resolve?tmdb_id=27205&media_type=movie"
```

### Automatic redeploys

This is the actual advantage over the AWS path: once connected, **every
`git push` to your main branch automatically triggers a new Render
build and deploy.** No manual Docker build, no ECR push, no Lambda
update command. This is the "like Render" experience from earlier in
this project's setup — you have it now, for real.

---

## 5. Android integration

Your existing `StreamEngine.kt` can call this resolver as an additional
source ahead of (or instead of) the on-device DirectScanner/WebViewScanner
path:

```kotlin
suspend fun resolveViaServer(tmdbId: Int, mediaType: MediaType, season: Int?, episode: Int?): StreamResult? {
    val url = buildString {
        append("https://reelz-resolver.onrender.com/resolve?tmdb_id=$tmdbId&media_type=${mediaType.name.lowercase()}")
        if (season != null) append("&season=$season")
        if (episode != null) append("&episode=$episode")
    }
    // plain OkHttp GET, parse JSON response into StreamResult
    // matching ResolveResponse's shape in api/routes.py
}
```

Suggested strategy: try the server resolver first (shared cache across
all users, often instant for popular titles), fall back to the existing
on-device WebViewScanner only if the server call fails or times out —
that way a resolver outage doesn't break playback entirely.

---

## 6. Security (do this before shipping to real users)

Currently `/resolve` is open to anyone who finds the URL. Before going
live:
- Add an API key check — a shared secret header your Android app sends,
  checked in `api/routes.py`. Simple to add; not included here since it
  should match whatever auth pattern you're already using elsewhere in
  your stack.
- Consider Render's built-in features for restricting access if you want
  defense in depth beyond the API key.
- Never commit `DATABASE_URL` or any secrets into the repo — Render's
  Environment Variables panel is where these belong, exactly what
  `render.yaml`'s `sync: false` marker is telling Render to prompt for
  rather than accepting a committed value.

---

## 7. Cost reality — read this before assuming Render is "free"

Render's **Starter** tier (~$7/mo as of writing, verify current pricing
on Render's site) is what keeps this warm and avoids Chromium
cold-starts. This is a flat monthly cost regardless of traffic — the
opposite tradeoff from Lambda's pay-per-use model discussed earlier in
this project. At your current pre-traffic scale, a small predictable
flat fee is very likely both cheaper AND simpler than fighting Lambda's
setup ceremony was turning out to be — but it's not literally free, be
aware of that ongoing cost.

---

## 8. The honest scope of "works with any source"

No system can promise 100% compatibility with every current and future
streaming site's security — including this one. Two real things are
provided instead of that promise:

- **`adapters/vidsrc_to.py`** — a real, working adapter built from
  actual captured DevTools traffic, demonstrating "verify actual
  content-type, never trust file extension" — the technique that
  defeats vidsrc.to's tokenized-HTML-segment scheme specifically.
- **`adapters/generic.py`** — a best-effort fallback usable against ANY
  embed URL with zero site-specific code, using the same verification
  technique generalized. Its own module docstring lists exactly what it
  can and cannot be expected to defeat (CAPTCHAs, automation
  fingerprinting, gesture-gated reveals — none of these are solved by
  this or any similar tool).
- **`POST /resolve-generic`** — the API surface for trying an ad-hoc
  source without registering a full adapter first.

When a source doesn't work with either of these, that's the honest
signal to either write a dedicated adapter (following `vidsrc_to.py`'s
pattern) or accept that source isn't worth the engineering cost right
now.

---

## 9. What this does NOT solve

- **No proxies included, by design.** If a source starts rate-limiting
  or Cloudflare-challenging your resolver's fixed IP under real traffic,
  that's a distinct problem from anything in this codebase — add a
  small rotating proxy pool only once you actually observe IP-based
  blocking in your logs, not preemptively.
- **Adapters need real maintenance.** When a source changes its
  obfuscation, its adapter needs updating — that arms race doesn't go
  away, it's just centralized server-side now instead of duplicated
  across every user's device.
- **`vidlink_pro.py` is an unverified template** — verify against real
  captured traffic before trusting it in production.
