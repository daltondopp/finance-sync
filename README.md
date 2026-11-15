# finance-sync

Daily read-only sync of bank and credit card accounts from
[SimpleFIN Bridge](https://beta-bridge.simplefin.org) into Supabase (`finance_*` tables,
schema in `finance_schema.sql`), so Claude can query balances and transactions.

## Setup
1. Repo secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SIMPLEFIN_SETUP_TOKEN`.
2. Actions → **Daily finance sync** → Run workflow with `sync_days` = 90.

The setup token is claimed once; the access URL is stored in `finance_config` and reused.
Runs daily at 6:47 AM Mountain.
