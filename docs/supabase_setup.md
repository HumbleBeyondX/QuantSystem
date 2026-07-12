# Supabase Cloud Data Setup

QuantSystem uses Supabase (free tier: 500 MB PostgreSQL + 1 GB Storage)
to persist backtest results and factor-research artifacts. Setup is one-time
and takes about 10 minutes.

## 1. Create the project

1. Go to https://supabase.com → **Start your project** → sign in with GitHub
2. **New project**:
   - Name: `quantsystem` (or anything)
   - Database password: **generate a strong one and save it** — you'll need it
   - Region: pick the one closest to you (e.g. `Northeast Asia (Tokyo)` if in Asia)
   - Pricing plan: **Free** is fine to start
3. Wait ~2 minutes for the project to provision.

## 2. Copy credentials into `.env`

Open `%APPDATA%\QuantSystem\.env` (or use the Desktop app's tray menu:
**Edit .env**). Fill in:

```
# Project Settings → API
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_ANON_KEY=<long-anon-jwt>
SUPABASE_SERVICE_KEY=<long-service-jwt>     # Settings → API → Project API keys

# Project Settings → Database → Connection string → URI
# Replace [YOUR-PASSWORD] with the DB password you saved in step 1.
SUPABASE_DB_URL=postgresql://postgres.<ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:6543/postgres

SUPABASE_BUCKET=quantsystem-artifacts
```

> **Use the `postgresql://` (psycopg) form, not `postgres://`, and use the
> pooler host/port (6543).** QuantSystem's cloud sync uses asyncpg with a
> connection pool.

## 3. Create the storage bucket

1. In the Supabase dashboard → **Storage** → **New bucket**
2. Name: `quantsystem-artifacts`
3. Public bucket: **off** (keep it private — the service key handles access)
4. Click **Create**

## 4. Create the schema

QuantSystem ships with Alembic migrations that work on Supabase PostgreSQL.

From the project root, with your `.env` populated:

```bash
# Point alembic at Supabase for a one-shot migration
QS_DATABASE_URL="$SUPABASE_DB_URL" \
  python -m alembic -c core/database/alembic.ini upgrade head
```

This creates all tables: `backtest_results`, `factor_evaluations`, `orders`,
`fills`, `positions`, `portfolio_snapshots`, `strategy_state`, `risk_events`,
`audit_log`, `sync_cursors`.

Verify in the Supabase dashboard → **Table Editor** — you should see ~10 tables.

## 5. Health check

Restart the QuantSystem desktop app. In the logs you should see:

```
supabase_client_ready
cloud_sync_scheduler_started
```

Or from the CLI:

```bash
python -c "
import asyncio
from core.cloud import get_supabase_client

async def main():
    c = await get_supabase_client()
    print('connected' if c else 'NOT configured')
    if c:
        print('health:', await c.health_check())

asyncio.run(main())
"
```

## 6. Free tier limits

| Resource | Free tier | Estimated usage (1 year) |
|----------|-----------|--------------------------|
| Database size | 500 MB | ~50 MB (all trading tables) |
| Storage | 1 GB | ~200 MB (50 backtest artifacts) |
| Egress | 2 GB / month | ~50 MB / month |
| Concurrent connections | 60 (pooler) | <10 |

For factor-research workloads with many backtest runs:
- Each backtest artifact (equity + trades Parquet) ≈ 2-5 MB
- 100 runs ≈ 300 MB → well within free tier
- When you approach limits, upgrade to Pro ($25/mo) or migrate to self-hosted.

## 7. Troubleshooting

**"SupabaseClient not connected"** — `SUPABASE_URL` or `SUPABASE_ANON_KEY` is missing
from `.env`. All cloud features gracefully disable themselves in this case.

**Connection timeout on `alembic upgrade`** — check the pooler host/port is correct
(should be `aws-0-<region>.pooler.supabase.com:6543`, not the direct `:5432`).

**"permission denied for table"** — you used the anon key when you needed the
service key. Edit the `.env` to include `SUPABASE_SERVICE_KEY`.
