# Performance & Indexing

The most common scaling question about RLS is "what does the policy cost, and how do I
index for it?" This page answers it with measured `EXPLAIN` output rather than theory.
The plan excerpts below were captured on PostgreSQL 16 against a 1,000,000-row `orders` table
spread evenly over 200 tenants (5,000 rows each), with one `order_items` row per order,
running as a `NOSUPERUSER NOBYPASSRLS` role with the policy exactly as `RLSConstraint`
generates it. Timings are warm-cache and single-run; they are there to show the *shape* of
the difference, not to benchmark your hardware. Repeated InitPlan nodes and some buffer
statistics are omitted from the excerpts for readability.

The short version:

- The RLS policy on its own is a **post-scan `Filter`** -- it is evaluated on every row
  the scan returns and never becomes an index condition.
- The ORM predicate `WHERE tenant_id = X` that `RLSManager` adds under a tenant context is
  what reaches the index. That is why the library runs **both**.
- A composite index with `tenant_id` first turns per-tenant "hot" queries into a handful
  of index page reads.
- For partial indexes shared by all tenants, put `tenant_id` in the key columns and
  a fixed application condition, such as `status = 'open'`, in the index predicate.

## Two filters, one query

A queryset built through `RLSManager` inside a tenant context carries two tenant checks.
The manager adds one at queryset construction; PostgreSQL applies the other at execution.
Build and evaluate the queryset inside the context:

```python
with tenant_context(tenant_id=42):
    orders = list(Order.objects.all())
```

```sql
-- what Django sends
SELECT "orders"."id", "orders"."product", "orders"."amount", "orders"."tenant_id"
FROM "orders"
WHERE "orders"."tenant_id" = 42
```

```sql
-- what PostgreSQL adds from the policy (the USING clause RLSConstraint generated)
CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
     THEN true
     ELSE tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int
END
```

For this query, both checks enforce the same tenant match and complement each other for
performance. The policy is the guarantee: it applies to raw SQL, `dbshell`, migrations
and anything else that skips the ORM (see [Security Model](../advanced/security.md)). The ORM predicate is the fast path:
it is the only one of the two the planner can use to pick an index. The rest of this page
explains why, and what to build on top of it.

## What the planner sees

Run the same table through `EXPLAIN` with and without the ORM predicate. First the
policy-only case -- a raw `SELECT * FROM orders` with `rls.current_tenant = '42'` set:

```text
Seq Scan on orders  (cost=0.03..30517.03 rows=500000 width=32) (actual time=0.023..96.058 rows=5000 loops=1)
  Filter: CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END
  Rows Removed by Filter: 995000
  InitPlan 1 (returns $0)
    ->  Result  (cost=0.00..0.01 rows=1 width=32) (actual time=0.001..0.002 rows=1 loops=1)
  InitPlan 2 (returns $1)
    ->  Result  (cost=0.00..0.01 rows=1 width=32) (actual time=0.000..0.001 rows=1 loops=1)
Execution Time: 96.309 ms
```

Now the ORM form, `SELECT * FROM orders WHERE tenant_id = 42`, with the same policy still
in force:

```text
Bitmap Heap Scan on orders  (cost=58.48..7539.11 rows=2494 width=32) (actual time=1.013..4.480 rows=5000 loops=1)
  Recheck Cond: (tenant_id = 42)
  Filter: CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END
  Heap Blocks: exact=5000
  ->  Bitmap Index Scan on orders_tenant_id_idx  (cost=0.00..57.83 rows=4988 width=0) (actual time=0.544..0.544 rows=5000 loops=1)
        Index Cond: (tenant_id = 42)
Execution Time: 4.609 ms
```

Three things to read off these plans:

1. **The policy is always a `Filter:`.** In both plans the `CASE WHEN ... END` expression
   sits on the scan node as a filter, applied to each row *after* it is fetched. In the
   first plan that means reading all 1,000,000 rows and discarding 995,000 of them.
2. **The ORM predicate becomes the `Index Cond:`.** `tenant_id = 42` is a plain
   "indexed column = constant" clause, so the planner feeds it to the B-tree on
   `tenant_id` (the index Django creates for every `ForeignKey`) and only touches the
   5,000 matching heap rows. The policy filter still runs, but on rows that already
   match it, so it rejects nothing.
3. **The policy hides selectivity from the planner.** Look at the estimates: the
   policy-only plan guesses `rows=500000` for a scan that returns 5,000, because the
   planner has no statistics for a `CASE` expression and falls back to a 50 % guess. The
   ORM predicate gets `rows=4988` from the column statistics. That matters beyond this
   one scan: bad row estimates feed bad join strategies (see [Joins](#joins) below).

!!! note "It is the policy's shape, not `current_setting()`"
    A common explanation -- repeated in older versions of these docs -- is that the
    planner cannot push the policy into an index because `current_setting()` is not
    `LEAKPROOF`. That is not what is happening. PostgreSQL's leakproof rule governs
    whether *your* query's clauses may be evaluated *before* the policy's, so that an
    untrusted function cannot see rows the policy would hide. It does not stop the policy
    clause itself from becoming an index condition. A bare
    `tenant_id = nullif((SELECT current_setting(...)), '')::int` policy *is* indexable --
    on the test table it plans as a bitmap index scan with
    `Index Cond: (tenant_id = (NULLIF($0, ''::text))::integer)`.

    What blocks the index is the **admin bypass**. `CASE WHEN <admin> THEN true ELSE
    <match> END` is not an "indexed column *operator* value" clause, so no index access
    method can use it; the planner can only apply it as a filter. Splitting it into two
    permissive policies does not help either, because PostgreSQL ORs permissive policies
    together and `(tenant_id = $1) OR ($0 = 'true')` cannot use an index on `tenant_id`
    any more than the `CASE` can (measured: a parallel sequential scan). The ORM
    predicate exists precisely to give the planner an indexable clause alongside the
    policy that carries the bypass semantics.

!!! warning "Measure as the application role"
    PostgreSQL superusers and `BYPASSRLS` roles skip the policy entirely, and their
    plans show no `Filter:` at all. An `EXPLAIN` run from a superuser `psql` session will
    look faster than production and tell you nothing about the policy. See
    [How to measure](#how-to-measure).

## Index design

### Start from what Django gives you

Every `ForeignKey` gets a B-tree index, so each `RLSProtectedModel` table already has
one on `tenant_id`. That is the `orders_tenant_id_idx` in the plan above, and it is enough
for "all rows of this tenant". It is not enough for the queries most apps actually run:
"the newest 20 open orders of this tenant", "this tenant's invoices for March". With only
the FK index the planner has to fetch *every* row of the tenant and then sort or filter
them:

```sql
SELECT * FROM orders
WHERE tenant_id = 42 AND status = 'open'
ORDER BY created_at DESC LIMIT 20;
```

```text
Limit  (cost=7573.74..7573.79 rows=20 width=32) (actual time=3.577..3.579 rows=20 loops=1)
  ->  Sort  (cost=7573.71..7575.83 rows=848 width=32) (actual time=3.576..3.577 rows=20 loops=1)
        Sort Key: orders.created_at DESC
        Sort Method: top-N heapsort  Memory: 27kB
        ->  Bitmap Heap Scan on orders  (cost=58.05..7551.15 rows=848 width=32) (actual time=0.820..3.476 rows=1666 loops=1)
              Recheck Cond: (tenant_id = 42)
              Filter: ((status = 'open'::text) AND CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END)
              Rows Removed by Filter: 3334
              Heap Blocks: exact=5000
              ->  Bitmap Index Scan on orders_tenant_id_idx  (cost=0.00..57.83 rows=4988 width=0) (actual time=0.399..0.399 rows=5000 loops=1)
                    Index Cond: (tenant_id = 42)
Execution Time: 3.617 ms
```

5,000 heap blocks read and a sort, to return 20 rows.

### Composite indexes: tenant first

Add a composite index whose **leading column is the tenant FK**, followed by the columns
the query filters or orders on:

```python
from django.db import models

from django_rls_tenants import RLSProtectedModel


class Order(RLSProtectedModel):
    status = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField()

    class Meta(RLSProtectedModel.Meta):
        db_table = "orders"
        indexes = [
            models.Index(
                fields=["tenant", "-created_at"],
                name="orders_tenant_created_idx",
            ),
        ]
```

The same query now walks the index in order and stops after 20 matches:

```text
Limit  (cost=0.45..367.65 rows=20 width=32) (actual time=0.038..0.166 rows=20 loops=1)
  ->  Index Scan using orders_tenant_created_idx on orders  (cost=0.42..15422.73 rows=840 width=32) (actual time=0.037..0.164 rows=20 loops=1)
        Index Cond: (tenant_id = 42)
        Filter: ((status = 'open'::text) AND CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END)
        Rows Removed by Filter: 22
Execution Time: 0.211 ms
```

No sort, 42 rows examined instead of 5,000. The ORM predicate `tenant_id = 42` is what
selects this index; the policy rides along as a filter that never rejects anything.

Rules of thumb:

- **Tenant first is a useful default for per-tenant queries.** Leading equality columns
  followed by a range column narrow a B-tree scan. With `(created_at, tenant_id)`, a date
  range can still narrow the scan, and the index can check the tenant before fetching
  heap rows, but the scanned date range spans all tenants. Choose the order for your
  actual filters and ordering; see PostgreSQL's
  [multicolumn index rules](https://www.postgresql.org/docs/16/indexes-multicolumn.html).
- **Then the columns your query filters on with equality, then the one it orders or
  ranges on.** `(tenant, status, -created_at)` would also absorb the `status = 'open'`
  filter above and remove the 22 discarded rows; whether that is worth a wider index
  depends on how many distinct queries share it.
- **A leading-tenant composite can replace the FK index.** PostgreSQL uses the leading
  column of a composite for a bare `tenant_id = X` lookup too, so once you have
  `(tenant, created_at)` the automatic single-column index is redundant for reads. It
  still costs a write per insert. If that matters, declare the tenant FK yourself with
  `db_index=False` (see [Custom ForeignKey](protecting-models.md#custom-foreignkey));
  the `class_prepared` handler keeps your declaration.
- **Unique constraints are composite indexes too.** A per-tenant uniqueness rule such as
  `UniqueConstraint(fields=["tenant", "number"])` is both the correct way to scope
  uniqueness (a global unique index spans every tenant -- see
  [Referential Integrity and Unique Indexes](../advanced/security.md#referential-integrity-and-unique-indexes))
  and a ready-made `(tenant_id, number)` index for lookups by number.

### Partial indexes

A partial index is usable when the planner can prove that the query's restrictions imply
the index predicate. RLS policy clauses can contribute to that proof, but this library's
admin-bypass `CASE` does not imply any fixed tenant value. For an index shared by all
tenants, keep `tenant_id` in the key columns and use a fixed application condition:

```python
class Meta(RLSProtectedModel.Meta):
    indexes = [
        models.Index(
            fields=["tenant", "-created_at"],
            name="orders_open_tenant_created_idx",
            condition=models.Q(status="open"),
        ),
    ]
```

```text
Limit  (cost=0.45..146.94 rows=20 width=32) (actual time=0.024..0.092 rows=20 loops=1)
  Buffers: shared hit=23
  ->  Index Scan using orders_open_tenant_created_idx on orders  (cost=0.42..6057.95 rows=827 width=32) (actual time=0.023..0.089 rows=20 loops=1)
        Index Cond: (tenant_id = 42)
        Filter: CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END
Execution Time: 0.125 ms
```

23 buffer hits; the `status = 'open'` condition has disappeared from the `Filter:` because
the index predicate already proves it.

A per-tenant partial index such as
`CREATE INDEX ... ON orders (created_at DESC) WHERE tenant_id = 42` can work when the
query explicitly contains `tenant_id = 42`. The library's policy alone cannot establish
that condition: it also allows other tenants in admin mode, and its GUC value is a runtime
parameter. In the measured policy-only query, PostgreSQL falls back to a full scan and sort --
measured at 44 ms versus 0.07 ms for the same statement with the explicit `WHERE`. The GUC
value is not a constant the planner can match against an index predicate. Prefer a
leading-tenant composite for a shared workload; one index can serve every tenant.

Prepared statements add another caveat: a generic plan for `status = $1` cannot assume
that `$1` is always `'open'`, so it cannot use the status partial index above. A custom
plan with the value available may use it. Check the plan with your production driver and
preparation settings; see PostgreSQL's
[partial index documentation](https://www.postgresql.org/docs/16/indexes-partial.html).

## The InitPlan: evaluating the GUC once per statement

Since v1.3.0 (#57) the policy reads each GUC through an uncorrelated scalar sub-SELECT,
`(SELECT current_setting('rls.current_tenant', true))`, instead of calling
`current_setting()` inline. PostgreSQL hoists such a sub-SELECT into an **InitPlan**: a
node evaluated at most once when its result is needed, exposing that result as a
parameter (`$0`, `$1`) to the filter. An unused branch may leave an InitPlan unexecuted.
That is what the `InitPlan 1 (returns $0)` lines in the plans represent, and why the filter
reads `$0 = 'true'` rather than `current_setting(...) = 'true'`.

The difference only shows when the policy filter actually runs on many rows -- a
policy-only scan, an admin query, or a raw statement without its own tenant predicate.
`SELECT count(*) FROM orders` under a tenant GUC, single worker, 1,000,000 rows:

```text
-- v1.3.0 policy (InitPlan form)
Aggregate  (cost=31767.03..31767.03 rows=1 width=8) (actual time=68.211..68.212 rows=1 loops=1)
  InitPlan 1 (returns $0)
    ->  Result  (cost=0.00..0.01 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=1)
  InitPlan 2 (returns $1)
    ->  Result  (cost=0.00..0.01 rows=1 width=32) (actual time=0.002..0.002 rows=1 loops=1)
  ->  Seq Scan on orders  (cost=0.00..30517.00 rows=500000 width=0) (actual time=0.037..67.966 rows=5000 loops=1)
        Filter: CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END
        Rows Removed by Filter: 995000
Execution Time: 68.294 ms
```

```text
-- pre-1.3.0 policy (inline current_setting)
Aggregate  (cost=36767.00..36767.01 rows=1 width=8) (actual time=148.212..148.214 rows=1 loops=1)
  ->  Seq Scan on orders_inline  (cost=0.00..35517.00 rows=500000 width=0) (actual time=0.019..147.894 rows=5000 loops=1)
        Filter: CASE WHEN (current_setting('rls.is_admin'::text, true) = 'true'::text) THEN true ELSE (tenant_id = (NULLIF(current_setting('rls.current_tenant'::text, true), ''::text))::integer) END
        Rows Removed by Filter: 995000
Execution Time: 148.241 ms
```

The inline form calls `current_setting()` up to twice per row -- about two million calls
for this scan -- and takes roughly twice as long. For an index-driven ORM query that
touches 20 rows the saving is unmeasurable, which is why the ORM predicate, not the
InitPlan, is the main performance feature.

Existing deployments keep their pre-1.3.0 policy until it is recreated; the
`Filter:` line tells you which form a table has (`$0` versus a literal
`current_setting(`). See
[Optional: adopt the InitPlan policy form](../advanced/migration-guide.md#optional-adopt-the-initplan-policy-form-57).

## Joins

### `select_related()`

The policy applies to every RLS-protected table in a join, and each one gets its own
post-scan filter. Join keys can already provide indexable clauses, such as a lookup by
primary key. Adding explicit tenant predicates also makes tenant indexes available when
choosing the join order. `TenantQuerySet.select_related()` adds a tenant predicate to the
RLS-protected model at the end of each explicitly named relation path:

```python
with tenant_context(tenant_id=42):
    OrderItem.objects.select_related("order")
```

```sql
SELECT ... FROM "order_items"
INNER JOIN "orders" ON ("order_items"."order_id" = "orders"."id")
WHERE ("order_items"."tenant_id" = 42 AND "orders"."tenant_id" = 42)
```

Measured on 1,000,000 items joined to 1,000,000 orders, with the two policies in force:

```text
-- ORM form: both tenant predicates present
Gather  (cost=1058.93..15478.35 rows=6 width=73) (actual time=1.164..27.211 rows=5000 loops=1)
  ->  Nested Loop  (cost=58.88..14477.70 rows=2 width=73) (actual time=0.322..21.252 rows=1667 loops=3)
        ->  Parallel Bitmap Heap Scan on orders  (cost=58.46..7466.35 rows=1039 width=32) (actual time=0.294..5.576 rows=1667 loops=3)
              Recheck Cond: (tenant_id = 42)
              Filter: CASE WHEN ($2 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($3, ''::text))::integer) END
              ->  Bitmap Index Scan on orders_tenant_id_idx  (cost=0.00..57.83 rows=4988 width=0) (actual time=0.383..0.384 rows=5000 loops=1)
                    Index Cond: (tenant_id = 42)
        ->  Index Scan using order_items_order_id_idx on order_items  (cost=0.42..6.74 rows=1 width=41) (actual time=0.009..0.009 rows=1 loops=5000)
              Index Cond: (order_id = orders.id)
              Filter: ((tenant_id = 42) AND CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END)
Execution Time: 27.401 ms
```

```text
-- policy only: the same join with no WHERE clause
Gather  (cost=22421.21..71778.09 rows=250000 width=73) (actual time=73.397..79.153 rows=5000 loops=1)
  ->  Parallel Hash Join  (cost=21421.16..45778.04 rows=104167 width=73) (actual time=70.036..70.665 rows=1667 loops=3)
        Hash Cond: (order_items.order_id = orders.id)
        ->  Parallel Seq Scan on order_items  (cost=0.00..18721.00 rows=208333 width=41) (actual time=0.018..33.788 rows=1667 loops=3)
              Filter: CASE WHEN ($0 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($1, ''::text))::integer) END
              Rows Removed by Filter: 331667
        ->  Parallel Hash  (cost=17392.00..17392.00 rows=208333 width=32) (actual time=33.843..33.844 rows=1667 loops=3)
              Buckets: 131072  Batches: 8  Memory Usage: 1120kB
              ->  Parallel Seq Scan on orders  (cost=0.00..17392.00 rows=208333 width=32) (actual time=0.018..33.020 rows=1667 loops=3)
                    Filter: CASE WHEN ($2 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($3, ''::text))::integer) END
                    Rows Removed by Filter: 331667
Execution Time: 79.338 ms
```

With the predicates, the planner enters one side through its tenant index and probes the
other by primary or foreign key -- a nested loop over 5,000 rows. Without them it
estimates 250,000 result rows (the 50 % guess, squared across two tables), sequentially
scans both tables and builds an eight-batch hash that spills to temp files. Which side
the planner starts from is its own choice; what the injected predicates buy is that
*either* side can be entered through a tenant index. Estimates can still be inaccurate:
the scoped plan estimates six joined rows but returns 5,000. The explicit predicates do
not teach the planner the cross-table correlation between the two tenant columns.

For nullable relations the injected predicate keeps the join a `LEFT OUTER JOIN`, so rows
whose FK is `NULL` are not dropped (fixed for nested paths in 1.3.2, #79):

```sql
-- OrderNote.order is nullable
SELECT ... FROM "order_notes"
LEFT OUTER JOIN "orders" ON ("order_notes"."order_id" = "orders"."id")
WHERE ("order_notes"."tenant_id" = 42
       AND ("orders"."tenant_id" = 42 OR "order_notes"."order_id" IS NULL))
```

Only explicitly named paths get the predicate: to scope both `order` and
`order__customer`, name both paths. `select_related()` with no arguments falls through
to Django's default and relies on the policy for tenant filtering on the joined tables;
primary-key join lookups can still use indexes.

### `prefetch_related()`

`prefetch_related()` fetches related objects in separate queries. Reverse foreign-key
and many-to-many prefetches normally use the related model's default manager. When that
is `RLSManager` and a tenant context is active, the prefetch receives a tenant predicate.
For example, prefetching an order's items produces:

```sql
SELECT ... FROM "orders" WHERE "orders"."tenant_id" = 42
SELECT ... FROM "order_items"
WHERE ("order_items"."tenant_id" = 42 AND "order_items"."order_id" IN (1, 2, 3, ...))
```

Forward foreign-key and one-to-one prefetches use Django's base manager by default,
which is unscoped on `RLSProtectedModel`. Those queries remain RLS-protected and can use
their primary-key `IN` lookup, but do not automatically add a tenant predicate. Supply
an explicit queryset when you want one:

```python
from django.db.models import Prefetch

with tenant_context(tenant_id=42):
    items = list(
        OrderItem.objects.prefetch_related(
            Prefetch("order", queryset=Order.objects.all()),
        )
    )
```

The queryset passed to `Prefetch` must also be built inside the tenant context. See
[Django's base manager behavior](https://docs.djangoproject.com/en/5.2/topics/db/managers/#base-managers).

Consider indexing the prefetched side as `(tenant, <fk>)` if the `IN` list gets long;
the FK's own single-column index is often sufficient when the `IN` on `order_id` is
already highly selective.

### M2M through tables

Auto-created through tables have no `tenant_id` column, so their policy is an `EXISTS` subquery
against each protected side (see
[M2M Policy Generation](../advanced/architecture.md#m2m-policy-generation)). The subquery
matches the parent primary key and checks its `tenant_id`. PostgreSQL may use repeated
PK probes or a hashed subplan; inspect the actual plan. Start with the FK indexes and
unique pair index Django creates, then measure any additional indexes against the
queries you run. Explicit through models can instead inherit `RLSProtectedModel` and
have their own tenant column and indexes.

## Raw SQL

RLS protects raw SQL, but the policy alone leaves you with the policy-only plans above.
To get the index you need an explicit tenant predicate in the statement, and its *form*
matters.

`safe_tenant_sql()` with the default `include_admin=True` reproduces the policy's
semantics, including the admin bypass -- and for the same reason as the policy, the
resulting `(tenant_id = ... OR <admin> = 'true')` fragment is **not indexable**. Measured
against the test table it plans as a parallel sequential scan, 38 ms for 5,000 rows.

Two forms do reach the index:

```python
from django.db import connection

from django_rls_tenants import safe_tenant_sql, tenant_context

with tenant_context(tenant.pk), connection.cursor() as cursor:
    # 1. Bare tenant match: the fragment is `tenant_id = nullif((SELECT ...), '')::int`,
    #    a stable expression the planner evaluates once and uses as the Index Cond.
    cursor.execute(
        f"SELECT * FROM orders WHERE {safe_tenant_sql(include_admin=False)}"
        " AND status = %s ORDER BY created_at DESC LIMIT 20",
        ["open"],
    )

    # 2. The tenant id as an ordinary bind parameter -- exactly what the ORM does.
    cursor.execute(
        "SELECT * FROM orders WHERE tenant_id = %s"
        " AND status = %s ORDER BY created_at DESC LIMIT 20",
        [tenant.pk, "open"],
    )
```

```text
-- form 1 on the composite partial index from above
Limit  (cost=0.47..146.80 rows=20 width=32) (actual time=0.011..0.025 rows=20 loops=1)
  ->  Index Scan using orders_open_tenant_created_idx on orders  (cost=0.43..6066.05 rows=829 width=32) (actual time=0.011..0.023 rows=20 loops=1)
        Index Cond: (tenant_id = (NULLIF($0, ''::text))::integer)
        Filter: CASE WHEN ($1 = 'true'::text) THEN true ELSE (tenant_id = (NULLIF($2, ''::text))::integer) END
Execution Time: 0.038 ms
```

`include_admin=False` matches nothing inside `admin_context()` (the tenant GUC is empty,
so the fragment is `tenant_id = NULL`), which is the right behaviour for a tenant-scoped
code path and the wrong one for an admin report. Admin reports want every tenant and
therefore no tenant predicate; the policy's admin branch lets them through. Other report
filters can still use indexes, while a report over every row may need a full scan. Keep
the default `include_admin=True` for statements that must behave identically in both
contexts and are not on a hot path. See
the [Raw SQL guide](raw-sql.md) for the helpers' full contract.

## The GUC affinity guard

*Added in 1.4.0.* Every statement on a PostgreSQL connection passes through the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard)'s execute wrapper; it
acts only on aliases in `RLS_TENANTS["DATABASES"]`, where it compares the active context
with what the connection holds. That costs about a
microsecond of Python per statement (0.8 to 1.4 µs measured with a stub execute and the real
wrapper, depending on SQL length and load). Measured on one idle
machine on `SELECT 1` over a local socket, the median per statement went from 29.6 to
31.1 µs with psycopg2 and from 31.6 to 33.3 µs with psycopg 3, so the share is smaller for
any real query. It adds no round trip while the
connection already holds the context's GUCs, as in the normal nested use of contexts on one
thread and connection. It sends one `set_config()` statement when they differ: on another
thread's connection, in a sibling task, after a rollback undid them, or when it cannot tell
(a transaction ended through the driver connection, a `RESET ALL`). The guard does not
change the SQL, so the plans in this guide are unaffected.

**With `USE_LOCAL_SET=True`** (*changed in 1.4.0*, when the guard became active in this
mode), the guard's Python cost per statement rises by about 0.1 to 0.15 µs outside a
transaction and by almost nothing inside one, which is negligible. The round trips differ,
because transaction-local values end with their transaction, and they depend on where the
context is entered:

- A context entered before a transaction opens (a task's context around an
  `atomic()` block, or the request's context on the middleware's async path with
  `ATOMIC_REQUESTS`): the guard sends one `set_config()` per transaction, plus one per
  context change inside it.
- A context entered inside `transaction.atomic()` (a `TestCase`, the WSGI request
  transaction): the block writes the GUCs itself, two `current_setting()` reads and two
  `SET LOCAL` statements on entry and two `SET LOCAL` statements on exit. The guard sends
  none for it while its tracker is clean (a test transaction, the WSGI request
  transaction); after an earlier context has ended on that connection it first clears the
  session's values, one more statement, and a loop that enters `atomic()` and a context on
  every iteration pays it every time.
- In autocommit, a statement run under a context gets a transaction of its own: `BEGIN`,
  the `set_config()` statement, the statement, `COMMIT`. That is **three extra round trips
  per statement**, each as long as your latency to the database. This is the cost of
  requests on the middleware's async path, Celery tasks and scripts that run without
  `transaction.atomic()`.
- Outside any context, a statement runs as it is, with no extra round trip. The next one
  after a context has ended, in autocommit or in a transaction, sends one `set_config()` to
  clear what the session may hold for itself (see
  [After a context ends](connection-pooling.md#after-a-context-ends)), for example the next
  request's session lookup on a persistent connection. The clear is for the session, so
  once its transaction commits it sticks.

Put batches in one `transaction.atomic()`: a task that runs a hundred statements under one
context then pays for one `set_config()` (the context outside, the `atomic()` inside) or six
small statements, plus one clear after an earlier context (the `atomic()` outside), instead
of three hundred extra round trips, and its writes commit together. See
[Code Outside a Request Transaction](connection-pooling.md#code-outside-a-request-transaction).

## How to measure

1. **Connect as the application role.** `EXPLAIN` from a superuser session shows no
   policy at all. If `python manage.py check` reports `W005`, fix the role before
   trusting any plan. `W005` (`E001` with `DEBUG = False`) checks every alias in
   `RLS_TENANTS["DATABASES"]` and skips a database it cannot connect to, so its absence
   alone is not proof (a failed catalogue query on a reachable database gives `W013`).
   On the connection being measured, verify that both role flags are false. In `psql`:

    ```sql
    SELECT current_user, rolsuper, rolbypassrls
    FROM pg_roles WHERE rolname = current_user;
    SELECT set_config('rls.current_tenant', '42', false);
    SELECT set_config('rls.is_admin', '', false);
    EXPLAIN (ANALYZE, BUFFERS) SELECT ... ;
    ```

    Or from Django, inside the context that sets the GUCs for you:

    ```python
    from django.db import connection

    from django_rls_tenants import tenant_context

    with tenant_context(tenant_id=42):
        qs = Order.objects.filter(status="open").order_by("-created_at")[:20]
        print(qs.explain(analyze=True, buffers=True))
    ```

2. **Read the access path and the full filter.** For the tenant-index examples above,
   `Index Cond:` contains `tenant_id = <value>`. Its absence is not automatically a
   problem: the planner may prefer a selective primary-key lookup, a partial index that
   already implies the tenant, or a sequential scan for a large result set. Check the
   generated SQL to confirm scoping, then compare rows examined and buffer usage.
   `Rows Removed by Filter:` counts rejections by the entire filter, including status
   and other application conditions, not just RLS. An InitPlan reading the GUC and
   referenced by the policy confirms the v1.3.0 form.
3. **Check the live policy body** with `python manage.py check_rls --verbose`, which
   prints each table's `USING` / `WITH CHECK` from `pg_policies`. A policy that still
   calls `current_setting()` inline predates v1.3.0 (see
   [Management Commands](management-commands.md#verbose-mode)).
4. **Use realistic data.** With a few hundred rows PostgreSQL may prefer a sequential scan
   over an index. Match production row counts, tenant skew, selectivity and statistics;
   the million-row dataset here is an example, not a minimum requirement.

## Checklist

- [ ] The measured connection uses a verified `NOSUPERUSER NOBYPASSRLS` role, so
      the plans you measure are the plans production runs.
- [ ] Tenant-scoped querysets are built and evaluated inside the tenant context, or
      explicitly scoped with `for_user()`, so the ORM adds `WHERE tenant_id = X`.
      `STRICT_MODE=True` rejects evaluation without a context; it does not add a missing
      tenant predicate to a queryset built earlier.
- [ ] Hot per-tenant queries have a composite index with `tenant` first, then the
      equality columns, then the sort column.
- [ ] Partial indexes shared by tenants put `tenant` in the key columns and use a
      condition the planner can prove for the production query.
- [ ] Per-tenant unique rules are `UniqueConstraint(fields=["tenant", ...])`.
- [ ] `select_related()` names its relations; the joined RLS tables have their own
      `tenant` index.
- [ ] Raw SQL on a hot path uses `safe_tenant_sql(include_admin=False)` or a bind
      parameter for the tenant id, not the admin-inclusive fragment.
- [ ] Tables created before v1.3.0 have had their policy recreated in the InitPlan form
      if they see admin or raw-SQL scans.
