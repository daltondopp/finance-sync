-- Personal finance tables (SimpleFIN -> Supabase). Applied as migration "finance_simplefin".
-- RLS is on with no policies: only the service key (GitHub Action) and the Supabase dashboard/MCP can read.

create table if not exists public.finance_config (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.finance_accounts (
  id                text primary key,           -- SimpleFIN account id
  org_name          text,
  org_domain        text,
  name              text not null,
  nickname          text,                       -- optional, set by hand
  kind              text,                       -- optional: checking / savings / credit_card / loan ...
  currency          text default 'USD',
  balance           numeric(14,2),              -- credit cards come through negative (amount owed)
  available_balance numeric(14,2),
  balance_date      timestamptz,
  hidden            boolean not null default false,
  updated_at        timestamptz not null default now()
);

create table if not exists public.finance_balance_history (
  account_id        text not null references public.finance_accounts(id) on delete cascade,
  as_of_date        date not null,
  balance           numeric(14,2),
  available_balance numeric(14,2),
  primary key (account_id, as_of_date)
);

create table if not exists public.finance_transactions (
  account_id    text not null references public.finance_accounts(id) on delete cascade,
  id            text not null,                  -- SimpleFIN transaction id
  posted_at     timestamptz,
  posted_date   date,                           -- Mountain-time date
  transacted_at timestamptz,
  amount        numeric(14,2) not null,         -- negative = money out / charge
  description   text,
  payee         text,
  memo          text,
  pending       boolean not null default false,
  category      text,                           -- optional, for later tagging
  updated_at    timestamptz not null default now(),
  primary key (account_id, id)
);
create index if not exists finance_transactions_date_idx on public.finance_transactions (posted_date desc);

create table if not exists public.finance_sync_runs (
  id           bigserial primary key,
  ran_at       timestamptz not null default now(),
  accounts     int,
  transactions int,
  errors       jsonb
);

alter table public.finance_config          enable row level security;
alter table public.finance_accounts        enable row level security;
alter table public.finance_balance_history enable row level security;
alter table public.finance_transactions    enable row level security;
alter table public.finance_sync_runs       enable row level security;

-- Quick snapshot: one row per account with balance and last-30-day spend
create or replace view public.finance_overview with (security_invoker = true) as
select a.id, coalesce(a.nickname, a.name) as account, a.org_name, a.kind,
       a.balance, a.available_balance, a.balance_date,
       (select coalesce(sum(-t.amount), 0) from public.finance_transactions t
         where t.account_id = a.id and t.amount < 0
           and t.posted_date >= current_date - 30) as spent_30d
from public.finance_accounts a
where not a.hidden
order by a.org_name, a.name;
