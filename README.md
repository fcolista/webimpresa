# WebImpresa

WebImpresa is a small, server-rendered business management application built
independently of the WebPreventivi and WebRitenuta source trees. It combines
shared customer records, commercial estimates, and configurable occasional-work
receipts in one persistent workspace.

## What is supported

- One shared customer directory for all normal users, including individual or
  company identity, tax identifiers, address, PEC and recipient code. Companies
  use the legal name as their fiscal identity and can have multiple contacts
  with separate first and last names, role, email, phone and mobile number.
  Search covers company and contact data; type filters, sorting and editing
  are available.
- Commercial estimates with multiple rows, quantity, unit price, discount,
  per-row VAT and withholding flags, configurable withholding percentage,
  payment terms, validity, notes and terms.
- Per-calendar-year, transactionally allocated document numbers. SQLite write
  transactions and unique constraints prevent two requests from receiving the
  same number. Receipt numbers can be customized while the receipt is a
  draft, must match its issue year, and cannot duplicate another receipt;
  automatic numbering advances past manually assigned numbers.
- Draft editing, status progression, duplicate-as-new-draft, HTML details and
  A4 PDF download for estimates. Unit prices are explicitly net of VAT. Users
  can record acceptance and archive or download the returned signed PDF.
- Occasional-work receipts only: service rows, expense reimbursements,
  configurable withholding/contribution/stamp parameters, PDF, and explicit
  draft-to-issued confirmation. The operator can enter service compensation as
  gross or specify the desired net compensation; reimbursement rows are
  unaffected. Issued receipt text is immutable, but a receipt can be
  permanently deleted by an authorized operational user.
- Accepted estimate conversion creates a linked **receipt draft**, not a
  fiscal document. The user must review the customer and fiscal values before
  the separate issue action. A receipt can also be linked to any accepted
  estimate for the same customer. An estimate is never changed by conversion.
- PDF attachment email sending is explicit and uses the admin-configured SMTP
  server. Delivery outcomes are recorded without storing message bodies or SMTP
  credentials.
- Exactly two application roles: `admin` and `user`. Admins can access only
  the administrative UI; normal users can access shared customers and
  documents. The backend enforces role checks independently of navigation.
- Admin settings for app name, theme colors, provider data, user lifecycle and
  SMTP. Primary/accent/background colors are checked for WCAG AA contrast for
  their intended text pairings and previewed before saving. The admin can
  upload a PNG/JPEG brand logo and configure complete provider identity,
  certified email (PEC), payment account details, and a graphic provider
  signature for receipt PDFs.
- Estimate and receipt PDFs include the provider identity and logo. Bank
  details appear only on receipt PDFs; estimates include VAT and withholding
  information, not bank/payment account coordinates. Logo and provider data
  are snapshotted for new documents; older records use current branding where
  no snapshot exists.
- Estimate prices on withholding lines can be entered as **net** amounts: the
  server grosses them up so the net due after withholding equals the entered
  amount, and the PDF labels the amounts as gross ("Compenso lordo").
- The **Report** page summarizes issued receipts marked as paid (by payment date) per year: totals by month
  (for withholding payments) and by client (for certifications). Drafts are
  excluded. CSV exports (semicolon separated, decimal comma, UTF-8 with BOM,
  spreadsheet-formula safe) are available for the receipt list and for both
  withholding summaries.

## Explicit limits

- Supported fiscal output is an **occasional-work receipt**, not an ordinary
  invoice and not an Italian electronic invoice. There is no SDI XML, signing,
  transmission, receipt/rejection processing or electronic-invoice compliance
  claim.
- Tax rates and stamp/contribution settings are configurable examples, not tax
  advice. Verify their applicability for the specific provider, service and
  current rules before issuing a receipt.
- The simplified estimate tax model supports line discounts, VAT and a
  withholding base selected per line. It is not a complete accounting or tax
  return system.
- The first release uses one shared workspace for normal users, matching the
  requested central shared directory. It has no per-user ownership or
  multi-company tenancy.
- Signed estimate copies are PDF uploads (up to 1.5 MB) stored in the database.
  The company logo and provider signature accept PNG/JPEG up to 1 MB and are
  re-encoded before storage. Deleting estimates and receipts is permanent;
  linked receipts remain but are unlinked when their estimate is deleted.
- Documents store a customer snapshot. Editing the directory never rewrites
  those snapshots. Issued receipts are immutable in the UI; a correction or
  cancellation workflow must be handled according to applicable law outside
  this release.
- Existing contact names are split at the first space during the database
  upgrade; review and correct first/last names in the customer record where
  that simple migration cannot infer the intended name parts.
- There is no password self-service recovery, customer portal, scheduled
  backups, or automated email delivery. An admin can reset another account's
  password; the operator must protect and test off-host backups.
- SMTP can be used only after an admin configures a server and external
  encryption key. TLS certificate verification is always enabled. Unencrypted
  SMTP is rejected unless the operator explicitly enables it for a trusted
  environment.

## Architecture and technology choices

- **Code layout:** `app/operations/` is a package with one module per area
  (`clients`, `quotes`, `receipts`, `reports`, `mailing`, `dashboard`) sharing
  one blueprint and common helpers in `common.py`. Line-level amount
  calculations live in `app/finance.py`.
- **Flask + Jinja + small vanilla JavaScript/CSS:** the source estimate app
  already uses Flask successfully. Server-rendered pages avoid a second API
  stack, SPA runtime, node build chain and extra client-side copies of fiscal
  calculations.
- **SQLite:** appropriate for a low-volume, single-instance office tool.
  Foreign keys, unique constraints, WAL, busy timeout and `BEGIN IMMEDIATE`
  writes are used. For multiple app replicas or sustained concurrent writes,
  move to PostgreSQL before scaling out.
- **Integer cents, basis points and Decimal:** server calculations use Decimal
  input conversion and integer cent arithmetic with half-up rounding.
- **ReportLab:** creates PDF documents on the server from escaped text and
  stored snapshots.
- **Werkzeug scrypt password hashes, signed HTTP-only sessions, CSRF tokens,
  login throttling and role checks:** chosen to keep auth dependencies small.
- **Fernet from cryptography:** protects the SMTP password at rest. Its key is
  supplied outside the database and must be backed up separately. Losing the
  key makes the saved SMTP password unrecoverable; database-only backups are
  insufficient for restoring SMTP operation.
- Dependencies are pinned in `requirements.txt`; run `pip-audit` as described
  below and update pins on a regular, tested schedule. No online CVE service is
  queried automatically during app runtime.

## Requirements and first deployment

Requirements: Docker Engine and Docker Compose v2. The default Compose
configuration joins the existing external Docker network named `web`, allowing
a reverse proxy on that network to reach the service by `webimpresa:8000`. If
there is no such network, create it once:

```sh
docker network create web
```

Create a local environment file and replace both placeholders with generated
random values:

```sh
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Put the first value in `SECRET_KEY` and the second in `SMTP_FERNET_KEY`. Keep
both out of source control and store a protected copy of the Fernet key apart
from the database. Build and start:

```sh
docker compose up -d --build
docker compose ps
```

### Releases and versioned Docker images

Application releases are identified by semantic-version Git tags in the
`vMAJOR.MINOR.PATCH` format. The Git tag is the source of truth for the
application and its Docker image. To publish a release:

```sh
docker build --target test -t webimpresa-tests .
docker run --rm webimpresa-tests
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin main
git push origin v1.0.0
```

Pushing a `v*` tag runs the release workflow. It validates the semantic-version
tag, runs the test suite, then builds and publishes
`ghcr.io/fcolista/webimpresa:<tag>` with the same version in its OCI image
metadata. A failed test or invalid tag prevents publication. The package uses
GitHub Actions' `GITHUB_TOKEN`; no registry credentials are stored in the
repository.

To deploy a published release, set `WEBIMPRESA_VERSION` in `.env` to the exact
Git tag (for example `v1.0.0`), then pull and start that image:

```sh
docker compose pull
docker compose up -d --no-build
```

For a fresh GHCR package, GitHub may keep its visibility private by default.
Grant the deployment host access to private packages or change the package
visibility in GitHub before pulling anonymously.

The app binds its optional host port to `127.0.0.1:8183` only. The container
port is available to reverse proxies on the Docker `web` network. Example Caddy
site:

```caddyfile
impresa.example.com {
    reverse_proxy webimpresa:8000
}
```

Terminate HTTPS at the trusted reverse proxy. Keep `SESSION_COOKIE_SECURE=true`
for HTTPS deployments. For a local-only HTTP test, set it to `false` in `.env`
and do not expose the service publicly. Configure the reverse proxy to forward
the original host/protocol and set appropriate request-size and rate limits.

Create the first administrator interactively (no built-in/default password):

```sh
docker compose exec webimpresa flask --app wsgi create-admin
```

The command prompts for a username and a password of at least 14 characters.
Sign in as that admin, create the ordinary user account(s), configure provider
details and SMTP, then use an ordinary user account for operational work.
There is deliberately no public registration or setup URL.

### SMTP setup

In **Admin → Configurazione**, set SMTP host, port and STARTTLS or implicit TLS,
sender identity and optionally the login username/password. Enter the SMTP
password only when setting/changing it. The form never reads it back; leaving
the password field empty preserves the encrypted value. Use **Invia email di
prova** to verify credentials, network access and TLS certificate validation.
The password is encrypted before it enters SQLite and is never included in
read responses or logs.

## Database, backups and updates

The named volume `webimpresa_data` contains `/data/webimpresa.db`. Versioned,
repeatable migrations run before Gunicorn starts; the schema version is stored
in SQLite `user_version`. Company contacts, uploaded logos and signed estimate
copies are stored in this database and are included in its SQLite backups.

Create a consistent online backup:

```sh
docker compose exec webimpresa flask --app wsgi backup-db
```

Backups are written to `/data/backups` in the persistent volume. Copy them to
protected off-host storage and retain the matching `SMTP_FERNET_KEY` separately.
The admin panel also offers **Backup e ripristino** to create and download a
backup, browse and delete server-side copies, and restore a compatible backup.
Deletion is permanent, so keep a protected off-host copy before removing a
server backup. The
panel validates SQLite integrity and schema version, verifies that the current
admin account is present in the uploaded database, and creates a safety backup
before replacing the database. Uploads are limited to 64 MiB.

For command-line restores, use a maintenance window and stop the application:

```sh
docker compose stop webimpresa
docker compose run --rm --no-deps webimpresa flask --app wsgi restore-db webimpresa-YYYYMMDD-HHMMSS.db
docker compose up -d
```

The CLI restore validates the database and schema, accepts only a file directly
under `/data/backups`, and creates a safety backup before replacing the
database. Test backups periodically; a backup on the same volume is not a
disaster-recovery copy.

Update source and pinned dependencies, run tests and dependency audit, then:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 webimpresa
```

Take a backup before applying updates. Do not downgrade application code past a
database migration without a tested restore; migrations are forward-only.

## Development

Python 3.12 is used in production. For a local environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export SMTP_FERNET_KEY="$(python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
export DATABASE_PATH="$PWD/data/webimpresa.db"
export INSTANCE_PATH="$PWD/data"
export SESSION_COOKIE_SECURE=false
flask --app wsgi init-db
flask --app wsgi create-admin
flask --app wsgi run
```

### Tests and dependency audit

The isolated test target installs development dependencies and includes the test
suite, but not in the production image:

```sh
docker build --target test -t webimpresa-tests .
docker run --rm webimpresa-tests
```

Or run locally with `pytest -q`. Check pinned dependencies with:

```sh
pip-audit -r requirements.txt
```

The release workflow runs the same audit and stops publication if a runtime
dependency has a known advisory. Review advisories, update pins deliberately and
repeat the full test/build cycle.
An audit result is a point-in-time check, not a guarantee that the application
is vulnerability-free.

## Security controls and remaining operational duties

Implemented controls include parameterized SQL, backend field validation,
autoescaped HTML templates, escaped PDF paragraph content, CSP with per-response
nonce for the minimal inline scripts/styles, clickjacking/MIME/referrer/
permissions headers, CSRF on state-changing forms, password scrypt hashing,
15-minute per-source login throttling (10 failed attempts), 12-hour signed
sessions, secure-cookie deployment default, admin/user backend authorization,
SMTP password encryption with an external key, TLS certificate validation,
bounded request and text sizes, non-root container execution, read-only
container filesystem, dropped Linux capabilities, health check and log
rotation.

Security depends on the deployment too: use HTTPS, set a unique `SECRET_KEY`
and external `SMTP_FERNET_KEY`, restrict access to the Docker network and host,
protect/off-site database backups, patch pinned dependencies and monitor
application/host logs. SQLite and per-process application behavior are sized
for a single app instance, not horizontal scaling. The admin status endpoint
returns only database/schema diagnostics; it does not expose customer or
document contents.
