# Getting Support

The issue tracker is the maintainer's work list, so each kind of request has its own place.

| You want to... | Go to |
|----------------|-------|
| Ask how to do something, or whether a design fits your app | [Discussions: Q&A](https://github.com/dvoraj75/django-rls-tenants/discussions/categories/q-a) |
| Suggest a feature | [Discussions: Ideas](https://github.com/dvoraj75/django-rls-tenants/discussions/categories/ideas), or the [feature request template](https://github.com/dvoraj75/django-rls-tenants/issues/new?template=feature_request.yml) when the problem and the solution are already clear |
| Report a bug | The [bug report template](https://github.com/dvoraj75/django-rls-tenants/issues/new?template=bug_report.yml) |
| Report a vulnerability | [`SECURITY.md`](SECURITY.md): a private advisory, never a public issue |
| Tell us how you use the library | [Discussions: Show and tell](https://github.com/dvoraj75/django-rls-tenants/discussions/categories/show-and-tell) |

## Before you ask

Something wrong right now? Run these two commands and read their output first. Between
them they catch most misconfigurations and name the fix in the message:

```bash
python manage.py check
python manage.py check_rls --verbose
```

Then look up the symptom on the
[Troubleshooting page](https://dvoraj75.github.io/django-rls-tenants/troubleshooting/).
The first cause of "RLS does not filter" is a database role that is a superuser or has
`BYPASSRLS`: PostgreSQL does not apply row-level security to it.

## Filing a bug

The bug report template asks for the output of both commands above, your `RLS_TENANTS`
setting (secrets redacted), whether the database role bypasses RLS, and the shape of your
deployment (WSGI or ASGI, server, driver, connection pooling). A report with those facts
can usually be reproduced without a round-trip.

The full documentation is at <https://dvoraj75.github.io/django-rls-tenants/>.
