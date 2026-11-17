# Parallel deployment beside v1

Proposal. v1 facts come from the service files and scripts in the v1 repos.
Goal: v2 runs beside v1 and cannot disturb it.

## The host, inspected 2026-11-14

- One Linux host, shared with v1 in production.
- PostgreSQL 16.15, one cluster. PostGIS 3.4.2, `pg_trgm` and `pgcrypto` are
  available.
- GDAL 3.8.4 and Python 3.12.3 are installed. Port 8100 was free.
- Local socket logins are tied to the OS user; other roles log in with a
  password over `127.0.0.1`.
- Not checked: the reverse proxy and TLS setup, and when the v1 importer runs.

The same host is the development machine for now. A development database
`stiflyt2` (1.7 GB with three sources loaded), a template `stiflyt2_template`
holding the extensions, and the four `stiflyt2_*` roles exist on the cluster.
The checkout is in the developer's home directory, not yet the
`/opt/stiflyt2` layout below.
No vhost, OS user or service has been added.

The map viewer runs as the developer's user on `127.0.0.1:8100` (`deploy/viewer.sh`),
started at boot from the crontab, and reached through an SSH tunnel. It is not
behind the reverse proxy yet: see the next section.

One scheduled job exists, in that user's crontab since 2026-11-14:
`deploy/refresh-nightly.sh` at 04:30 UTC. It checks every loaded source and
imports what changed, logging to `var/log/refresh.log` and to
`import_control.check_log`. Original delivery files are kept for the newest
14 snapshots per source and scope; the database keeps every version. Nothing
alerts on a failed night yet: `stiflyt2 sources list` shows the latest check.

## Opening it under its own name, with sign-in (2026-11-16)

**Switched on 2026-11-16**: the public address (here
`https://stiflyt2.example.org/`) answers with a Let's Encrypt certificate, and nothing is given to anyone not signed in,
straight on the host included. What follows is how it was done, and how to go
back. The proxy on the host is Caddy,
which serves v1 under its own name and fetches
its Let's Encrypt certificates by itself. Sign-in is Google, as in v1
(Authlib and a signed session cookie), with a client of v2's own.

Before the steps below were done the service ran **local**: it answers only
requests made straight to `127.0.0.1:8100` (an SSH tunnel), as the one editor
named in `STIFLYT2_EDITOR`, and refuses anything a proxy has passed on. So
adding the Caddy block before the sign-in settings shows a refusal to the
world, not the map.

What only the person who runs the server can do, in this order:

1. **A Google OAuth client.** In Google Cloud Console, Credentials, "OAuth
   client ID", type Web application. Authorised redirect URI:
   `https://stiflyt2.example.org/api/v1/auth/callback`. (A client of its own,
   not v1's: v1's redirect URIs and secret are left alone.)
2. **The settings**, in `~/stiflytv2/.env` on the server (not in the
   repository):

   ```
   STIFLYT2_GOOGLE_CLIENT_ID="….apps.googleusercontent.com"
   STIFLYT2_GOOGLE_CLIENT_SECRET="…"
   STIFLYT2_SESSION_SECRET="…"        # .venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
   STIFLYT2_PUBLIC_URL="https://stiflyt2.example.org"
   ```

3. **Who may sign in**, before anyone tries:

   ```sh
   .venv/bin/stiflyt2 access add ola@… "Ola Nordmann" --role editor
   .venv/bin/stiflyt2 access add kari@… "Kari Nordmann" --role editor     # or reader
   .venv/bin/stiflyt2 access list
   ```

   The name is what changes are recorded under. For whoever was the editor
   named in `STIFLYT2_EDITOR`, use that name exactly, so that what was done
   before sign-in is still theirs, Undo included.
4. **Restart the viewer**: `deploy/viewer.sh restart`. From now on nobody
   gets anything without signing in, over the tunnel too.
5. **The proxy**, as root. Add to Caddy's configuration, beside v1's block
   and without touching it, then `sudo systemctl reload caddy`:

   ```
   # --------------------
   # Stiflyt v2
   # --------------------
   stiflyt2.example.org {
       reverse_proxy 127.0.0.1:8100
   }
   ```

   Caddy gets the certificate at the first request (the name already points
   here). Then open the public address.

To go back: remove the Caddy block and reload; remove the four settings and
restart, and the service is local again.

A token for an assistant or a script, acting as a listed person:
`.venv/bin/stiflyt2 access token ola@… "Claude Code" --through mcp`. It is
shown once. The program sends it as `Authorization: Bearer …`.

Starts by itself: the crontab has `@reboot … deploy/viewer.sh start`
and, at 03:50 UTC, `deploy/backup-nightly.sh` (a copy of `core` and
`history`, and one of `access` and `feedback`, the newest 14 of each kept in
`var/backup/`, on the same disk as the database: it guards against mistakes,
not against losing the disk; for that the same script pushes the newest
copies to a private git repository, see the README, "At night"). The page tells an editor when the nightly
check or copy did not run or failed. Nothing is sent anywhere: no mail.

## Layout

v1 keeps its own database, database roles, OS user, directories, service,
hostname, deploy timer and backup. None of them is shared with v2:

| Piece | v2 |
|---|---|
| Database | new database `stiflyt2`, same PostgreSQL cluster |
| DB roles | `stiflyt2_owner`, `stiflyt2_migrate`, `stiflyt2_import`, `stiflyt2_api`; no rights on v1's database |
| OS user, code | `stiflyt2`, `/opt/stiflyt2` |
| Raw source files | `/var/lib/stiflyt2` |
| Services | `stiflyt2-api.service` on `127.0.0.1:8100`; `stiflyt2-worker.service` |
| Hostname | a name of its own, pointing at the same host, as a separate vhost |
| Deploy | manual `deploy.sh` at first |
| Backup | nightly dump of `core` and `history`, separate files |

## Why

- **Separate database, not new schemas in v1's.** The v1 loader drops and
  recreates schemas by name prefix and manages grants by prefix. A separate database is out of its reach and can be dropped and
  recreated freely. Cost: no joins to v1's teig and stedsnavn; v2 imports its
  own stedsnavn.
- **Separate hostname, not a path.** Own session cookie, own Google OAuth
  redirect URI, no edits to v1's proxy rules.
- **No second PostgreSQL or container on the server.** It would compete with
  v1 for memory. Docker Compose is for laptops.

## Protecting v1 on shared resources

- **PostgreSQL:** `ALTER ROLE ... CONNECTION LIMIT` and `statement_timeout`
  on the v2 login roles. Connecting to v1's database is already revoked
  from PUBLIC; verify it.
- **CPU and memory:** `MemoryMax=` and a low `CPUWeight=` on both v2 units.
- **Schedule:** v2 imports run outside v1's nightly refresh and deploy.
- **Disk:** v2 retains snapshots. Check free space first; index only the
  pilot area; keep a retention setting for old raw artifacts.
- **Server packages:** `pg_trgm` and `postgis` are enabled per database. No
  PostgreSQL or PostGIS upgrade for v2's sake. The route engine runs
  in-process, so no pgRouting package is needed.
- **Python:** own interpreter (3.12+) and venv under `/opt/stiflyt2`; the
  system Python is left alone.
- **Secrets:** own `.env`, own session secret, own OAuth client.

## Check on the host before installing

1. PostgreSQL and PostGIS versions; whether `pg_trgm` is available.
2. Free disk and RAM; current size of v1's database.
3. Which reverse proxy fronts v1 and how TLS certificates are issued.
4. When the v1 importer's cron job runs.
5. Whether Python 3.12+ is installed or needs adding.
6. That port 8100 is free.

## Install steps (each reversible)

1. Vhost and TLS certificate for the new name, proxying to
   `127.0.0.1:8100`. The DNS record exists. Until the vhost is added, the name
   reaches whatever the proxy serves by default, which may be v1.
2. Create OS user `stiflyt2`, `/opt/stiflyt2`, `/var/lib/stiflyt2`.
3. Run `bootstrap.sql` as superuser: database, roles, schemas, extensions.
4. Clone, create venv, run both Alembic tracks as `stiflyt2_migrate`.
5. Install and start the two systemd units.
6. Add the v2 backup timer.

## Removal

Stop and disable the two units, drop database `stiflyt2` and its roles, remove
the vhost, `/opt/stiflyt2` and `/var/lib/stiflyt2`. Nothing of v1 is touched.
