# Reelz Stream Resolver — Setup

Server-side resolver: takes a TMDB id, runs the right site adapter (light
HTTP or full Playwright render, depending on the source), returns a
clean JSON stream URL, and caches it in Postgres with real expiry read
from each source's own token — not a guessed TTL.

**Read this whole file once before deploying.** The EC2 and Lambda paths
diverge in a few places that matter (see "Cold starts" and "Expiry
sweeping" below) — skipping straight to commands without understanding
why will bite you later.

---

## 1. Local development (do this first, before any cloud deployment)

Get the resolver working on your own machine before wrapping it in
Lambda or EC2 constraints — debugging scraping logic and infrastructure
config at the same time is a bad time, as discussed earlier.

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium     # downloads the actual browser binary
playwright install-deps          # OS-level deps Chromium needs (Linux)
```

### Local Postgres

Easiest via Docker:
```bash
docker run --name reelz-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=reelz \
  -p 5432:5432 -d postgres:16
```

```bash
export DATABASE_URL="postgresql://postgres:devpass@localhost:5432/reelz"
export RESOLVER_DEPLOYMENT=ec2   # runs the in-process sweeper locally too
```

### Run it

```bash
uvicorn api.main:app --reload --port 8000
```

Test:
```bash
curl "http://localhost:8000/health"
curl "http://localhost:8000/resolve?tmdb_id=27205&media_type=movie"
```

### Run the tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

**Before trusting any adapter beyond vidsrc_to.py in production**: the
vidsrc_to adapter's `extract_from_page` logic was written directly
against real DevTools captures you provided earlier in this
conversation — it's grounded in actual observed behavior. The
vidlink_pro adapter is explicitly marked in its own docstring as an
unverified structural template — capture real DevTools traffic against
it (same method: Network tab, filter Fetch/XHR, watch what happens when
the embed loads) and adjust `try_light_resolve`'s parsing to match what
you actually see, the same way we did for vidsrc.to. Don't ship it
as-is without doing that.

---

## 2. Deployment option A: Oracle Cloud Always Free / EC2 (simpler, recommended to start)

### ARM architecture — read this if deploying on Oracle's Always Free tier

Oracle's Always Free Compute is **Ampere ARM (arm64)**, not x86_64. Two
things this affects, both already handled in this repo:

1. **`Dockerfile`** uses `playwright install --with-deps chromium`
   (not a hardcoded x86 Chromium download) — this detects the host CPU
   architecture automatically and pulls the correct ARM64 build. You
   don't need to change anything for this to work on Oracle's ARM VM.
2. **Base image**: `python:3.12-slim` has official arm64 builds, so no
   change needed there either.

**What you DO need to check yourself**: if you ever add other
dependencies later, confirm they have arm64 wheels/builds available —
most mainstream Python packages do, but it's not universal. If a `pip
install` fails specifically on the Oracle VM but worked locally on an
x86 machine, this architecture mismatch is the first thing to suspect.

### Single-VM deployment via Docker Compose (recommended starting point)

For startup-scale traffic, don't split into separate VMs yet — run
everything as separate containers on ONE Oracle Always Free VM via the
included `docker-compose.yml`. This gets you process isolation (one
container crashing doesn't corrupt another) without the operational
overhead of managing multiple VMs before you actually need to.

```bash
# On the Oracle VM (Ubuntu, ARM):
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER   # log out/in after this

git clone <your-repo> reelz_resolver
cd reelz_resolver
cp .env.example .env
nano .env   # set a real POSTGRES_PASSWORD

docker compose up -d --build
docker compose logs -f resolver   # watch it come up
```

Test from the VM itself first (before opening any firewall ports):
```bash
curl "http://localhost:8000/health"
```

### Opening the port (Oracle-specific gotcha)

Oracle Cloud has BOTH a Security List/Network Security Group (cloud
firewall) AND the VM's own `iptables`/`ufw` — you need to open port 8000
(or whatever you front it with) in **both places**, not just one. This
trips up a lot of first-time Oracle users: the cloud console firewall
rule alone isn't enough if the VM's own OS firewall is also blocking it.

```bash
# On the VM itself:
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save   # if using iptables-persistent
```
Then separately, in the Oracle Cloud Console: your VCN's Security List
→ Add Ingress Rule → TCP, port 8000, source `0.0.0.0/0` (or narrower,
if you're fronting this with Cloudflare and want to restrict to
Cloudflare's IP ranges only — recommended once you add the API-key auth
mentioned in the Security section below).

### Plain EC2 (if not using Oracle / Docker Compose)

Why EC2 first, even though you said Lambda: a long-running EC2 process
avoids the cold-start-with-Chromium problem entirely (browser stays
warm across requests), and the in-process expiry sweeper just works as
a background asyncio task — no EventBridge, no second Lambda, no
Lambda-specific packaging quirks. You can migrate to Lambda later once
this is proven; the FastAPI app code doesn't change either way.

### Instance sizing
- `t3.small` (2GB RAM) is the realistic minimum for one Chromium
  instance handling occasional requests. Bump to `t3.medium` (4GB) if
  you expect concurrent resolutions — Chromium is not lightweight.

### Setup on the instance (Ubuntu 22.04/24.04)

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip

git clone <your-repo-or-scp-this-folder> reelz_resolver
cd reelz_resolver

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps
```

### Postgres

Either run Postgres on the same box (`sudo apt install postgresql`) for
simplicity at small scale, or point `DATABASE_URL` at RDS/managed
Postgres once you want backups/multi-instance. Since you're already
running a backend on Render per your remote config
(`backend.backend_url`), you could also just add this table to whatever
Postgres that backend already uses — one less thing to provision.

### Run as a systemd service (so it survives reboots/crashes)

`/etc/systemd/system/reelz-resolver.service`:
```ini
[Unit]
Description=Reelz Stream Resolver
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/reelz_resolver
Environment="DATABASE_URL=postgresql://user:pass@host:5432/reelz"
Environment="RESOLVER_DEPLOYMENT=ec2"
ExecStart=/home/ubuntu/reelz_resolver/venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`--workers 1` is deliberate, not a mistake**: the Playwright browser
instance lives in `app.state`, one per process. Multiple uvicorn workers
would each launch their own Chromium, multiplying memory use for no
benefit at this scale. If you need more throughput later, scale by
running multiple EC2 instances behind a load balancer instead of
multiple workers on one box — cleaner isolation.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reelz-resolver
sudo systemctl status reelz-resolver
```

Put this behind an ALB or Nginx with TLS termination — never expose port
8000 directly to the internet.

---

## 3. Deployment option B: AWS Lambda (once EC2 is proven working)

### Cold starts — read this before deploying

A Lambda invocation that has to launch Chromium from scratch takes
**2-5 seconds** before it even starts navigating. This is the single
biggest thing that can make "serverless" feel slower than what you have
now. Mitigations, roughly in order of effort:
1. **Provisioned Concurrency** on the resolver Lambda — keeps N warm
   instances ready, eliminates cold starts for those, costs money even
   when idle (you're paying to keep Chromium warm).
2. Accept the cold start for now and rely on the Postgres cache — most
   requests should hit cache, not the resolver, once titles are being
   resolved repeatedly. Cold starts only hurt for genuinely new/rare
   titles.
3. A scheduled "warmer" EventBridge rule that pings the Lambda every
   few minutes to keep at least one instance warm — cheap, imperfect
   (doesn't help under real concurrent load, only keeps ~1 instance hot).

Start with (2) — don't pay for Provisioned Concurrency until you've
confirmed cache hit rate makes cold starts rare enough not to matter.

### Package as a container image (not a zip)

Chromium + Playwright is too large for Lambda's zip-based 250MB limit.
Container images go up to 10GB.

`Dockerfile`:
```dockerfile
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install -r requirements.txt

# Playwright's own recommended way to get Chromium + its OS deps into
# a Lambda-compatible image
RUN playwright install --with-deps chromium

COPY . .

CMD ["api.lambda_handler.handler"]
```

Build and push:
```bash
aws ecr create-repository --repository-name reelz-resolver

docker build -t reelz-resolver .
docker tag reelz-resolver:latest <account-id>.dkr.ecr.<region>.amazonaws.com/reelz-resolver:latest

aws ecr get-login-password --region <region> | docker login --username AWS \
  --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com

docker push <account-id>.dkr.ecr.<region>.amazonaws.com/reelz-resolver:latest
```

### Create the Lambda function

```bash
aws lambda create-function \
  --function-name reelz-resolver \
  --package-type Image \
  --code ImageUri=<account-id>.dkr.ecr.<region>.amazonaws.com/reelz-resolver:latest \
  --role <your-lambda-execution-role-arn> \
  --memory-size 2048 \
  --timeout 30 \
  --environment "Variables={DATABASE_URL=postgresql://...,RESOLVER_DEPLOYMENT=lambda}"
```

Memory note: 2048MB is a reasonable starting point for one Chromium
instance doing one resolution at a time. Lambda also scales CPU with
memory, so under-provisioning memory makes Chromium slower too, not
just risking OOM.

### Expose it via a Function URL (simplest) or API Gateway

```bash
aws lambda create-function-url-config \
  --function-name reelz-resolver \
  --auth-type NONE   # put real auth in front of this before going live — see Security section
```

### Scheduled expiry sweep (separate, tiny Lambda)

This is NOT the same function — see `api/lambda_sweep_handler.py`'s
docstring for why. Deploy it as a second, much smaller Lambda (128-256MB
memory is plenty, it only touches Postgres):

```bash
# Package this one as a plain zip - no Chromium needed
pip install -r requirements.txt -t package/
cp core/cache.py core/expiry_sweeper.py api/lambda_sweep_handler.py package/
cd package && zip -r ../sweep.zip . && cd ..

aws lambda create-function \
  --function-name reelz-sweep-expired \
  --runtime python3.12 \
  --handler api.lambda_sweep_handler.handler \
  --zip-file fileb://sweep.zip \
  --role <your-lambda-execution-role-arn> \
  --memory-size 256 \
  --timeout 30 \
  --environment "Variables={DATABASE_URL=postgresql://...}"
```

Schedule it via EventBridge:
```bash
aws scheduler create-schedule \
  --name reelz-sweep-schedule \
  --schedule-expression "rate(15 minutes)" \
  --target "Arn=<sweep-lambda-arn>,RoleArn=<eventbridge-invoke-role-arn>" \
  --flexible-time-window "Mode=OFF"
```

Remember: this sweep is housekeeping, not correctness — `StreamCache.get()`
already filters `expires_at > now()` on every read regardless of
whether the sweep has run recently.

---

## 4. Postgres

Any Postgres works — RDS, your existing Render Postgres, Supabase,
whatever you already have. The resolver creates its own table
(`resolved_streams`) automatically on startup via `StreamCache.start()`
— no manual migration step needed for a first deploy.

Minimum IAM/network requirement: the EC2 instance or Lambda needs
network access to reach the Postgres host on port 5432 (security group /
VPC config — if using RDS + Lambda, put the Lambda in the same VPC as
the RDS instance).

---

## 5. Android integration

Your existing `StreamEngine.kt` can call this resolver as an additional
source ahead of (or instead of) the on-device DirectScanner/WebViewScanner
path. Rough shape of the change, not a full diff since it depends on how
you want to prioritize server vs on-device fallback:

```kotlin
suspend fun resolveViaServer(tmdbId: Int, mediaType: MediaType, season: Int?, episode: Int?): StreamResult? {
    val url = buildString {
        append("https://your-resolver-host/resolve?tmdb_id=$tmdbId&media_type=${mediaType.name.lowercase()}")
        if (season != null) append("&season=$season")
        if (episode != null) append("&episode=$episode")
    }
    // plain OkHttp GET, parse JSON response into StreamResult
    // matching ResolveResponse's shape in api/routes.py
}
```

I'd suggest: try the server resolver first (it has the shared cache, so
it's often instant for popular titles), fall back to the existing
on-device WebViewScanner only if the server call fails or times out —
that way a resolver outage doesn't break playback entirely, it just
reverts to the slower path you already have working.

---

## 6. Security (do this before any public deployment)

This is currently unauthenticated (`/resolve` is open to anyone who
finds the URL). Before shipping:
- Put an API key check in front of it (a shared secret header your
  Android app sends, checked in `api/routes.py`) — trivial to add, not
  included here since it depends on how you're already handling auth for
  your existing Render backend, and duplicating that pattern is better
  than inventing a second one.
- Rate-limit per API key / per IP — a resolver that spins up Chromium
  per uncached request is a real cost if someone hammers it.
- Never commit `DATABASE_URL` or any secrets into the repo — use environment
  variables / AWS Secrets Manager, as shown above.

---

## 6.5. Lambda cost control (read this before flipping the switch)

"Pay as you go" does not mean cheap for this workload — Lambda bills
duration × memory, and a Chromium resolve is long-duration AND
high-memory, the two things that multiply your bill. Three real
controls are built in, not just claimed:

1. **Postgres cache** (`core/cache.py`) — the majority of requests
   should never reach the engine at all once titles are being resolved
   repeatedly across your user base.
2. **Request coalescing** (`core/coalescer.py`) — N simultaneous users
   resolving the same trending title trigger exactly ONE Chromium
   launch, not N.
3. **`GLOBAL_RESOLVE_CEILING_SECONDS`** in `core/engine.py` — a hard
   35-second wall-clock backstop independent of any adapter's own
   timeout, so a hung page/challenge script can't silently run up
   Lambda duration billing past a sane ceiling even if an adapter's
   timeout logic has a bug.

**Watch your actual cost, don't guess it.** Every full-render (Chromium)
resolve logs its real duration with the literal string `FULL_RENDER` —
grep your CloudWatch logs for this to see exactly what's driving your
bill, rather than estimating. Light-path resolves (no Chromium) log
`LIGHT path` and cost essentially nothing by comparison — if a source
you use heavily is hitting FULL_RENDER every time, that's a concrete
signal it might be worth writing a lighter adapter for, if the site
actually supports one.

**Set a AWS Billing Alarm.** Genuinely do this before going live —
CloudWatch Billing Alarms cost nothing to set up and will tell you
immediately if something (a bug, a traffic spike, a misbehaving source
causing repeated full-renders) is running up cost faster than expected,
rather than finding out at the end of the month.

## 6.6. The honest scope of "works with any source"

No system can promise 100% compatibility with every current and future
streaming site's security — including this one. Two real, concrete
things are provided instead of that promise:

- **`adapters/vidsrc_to.py`** — a real, working adapter built directly
  from actual captured DevTools traffic (not guessed), demonstrating the
  "verify actual content-type, never trust file extension" technique
  that defeats vidsrc.to's specific tokenized-HTML-segment scheme.
- **`adapters/generic.py`** — a best-effort fallback usable against ANY
  embed URL with zero site-specific code, using the same verification
  technique generalized. Its own module docstring lists exactly what it
  can and cannot be expected to defeat (CAPTCHAs, automation
  fingerprinting, gesture-gated reveals — none of these are solved by
  this or any similar tool). Use it to quickly test whether a new
  source is worth writing a dedicated adapter for.
- **`POST /resolve-generic`** — the API surface for trying an ad-hoc
  source without registering a full adapter first.

When a source doesn't work with either of these, that's the honest
signal to either write a dedicated adapter (following vidsrc_to.py's
pattern: capture real traffic, verify real content-types, extract a
real expiry) or accept that source isn't worth the engineering cost
right now — not a sign the framework failed at some achievable "100%."

## 7. What this does NOT solve (be aware, not surprised later)

- **No proxies included, as requested.** If a source starts rate-limiting
  or Cloudflare-challenging your resolver's fixed IP under real traffic,
  that's a distinct, separate problem from anything in this codebase —
  see the earlier conversation about when proxies actually become
  necessary (volume-based blocking, not the JS-challenge problem this
  resolver already handles).
- **Adapters need real maintenance.** When a source changes its
  obfuscation, its adapter needs updating — same arms race as before,
  just centralized server-side now instead of duplicated across every
  user's device.
- **vidlink_pro.py is an unverified template**, explicitly marked as
  such in its own docstring — verify against real traffic before trusting
  it in production, the same way vidsrc_to.py was built from real
  DevTools captures, not guesswork.
