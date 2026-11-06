# AGaura — Installation on the server

Putting AGaura online on an Infomaniak VPS, from an empty server to the first invited account,
then keeping it running. Local development and testing with phones: [README](../README.md) and
[LOCAL_TESTING.md](LOCAL_TESTING.md). Server hardening details: [deploy/README.md](../deploy/README.md).

The application runs in two containers (`compose.yaml`): `app` (uvicorn, SQLite database in the
`agaura-data` volume, automatic backups in `backups/auto/` on the server) behind `caddy` (HTTPS
with automatic Let's Encrypt certificates). Only Caddy's ports 80 and 443 are published.

## 1. Before starting

- A VPS: 2 vCPU and 2 GB RAM are enough (SPEC §12.5); Debian or Ubuntu, with an SSH key.
- The domain (`agaura.org`) and access to its DNS zone.
- An Infomaniak mailbox for `no-reply@agaura.org` (sending address of invitations and resets).
- The repository address (GitHub remote, P0-online).

## 2. DNS

| Record | Name | Value |
|---|---|---|
| A | `agaura.org` | IPv4 address of the VPS |
| AAAA | `agaura.org` | IPv6 address of the VPS |
| CNAME | `www` | `agaura.org` (Caddy redirects it to the apex) |
| TXT (SPF), DKIM, DMARC | as given by Infomaniak for the mailbox | |

Wait until `dig +short agaura.org` returns the VPS address: Caddy needs it to obtain the
certificate. The `AAAA` record requires Docker 27 or later, so that visitors keep their own IPv6
address behind Docker; §9 checks it. Leave it out until that check passes.

## 3. Prepare the server

A sudo user with your SSH key. The Debian images of Infomaniak come with one, `debian` (usually uid 1000, `id -u`:
the uid of the application's user in the container, so `backups/auto` needs no `chown`): keep
it and skip the commands below, writing `debian` wherever this guide says `agaura-admin`.
Otherwise create one, as root (`sudo -i` first when logged in as another user: `adduser` lives
in `/usr/sbin`, outside a plain user's `PATH`, hence "adduser: command not found"), then log in
as that user from a second terminal before closing the first:

```sh
adduser agaura-admin && usermod -aG sudo agaura-admin
mkdir -p /home/agaura-admin/.ssh && cp ~/.ssh/authorized_keys /home/agaura-admin/.ssh/
chown -R agaura-admin: /home/agaura-admin/.ssh && chmod 700 /home/agaura-admin/.ssh
```

Then follow [deploy/README.md](../deploy/README.md) §1: firewall (22, 80, 443 on the system and
in the Infomaniak Manager), SSH by key only without root login, automatic security updates.

Install Docker Engine with the Compose plugin (https://docs.docker.com/engine/install/), then
let your user run it: `sudo usermod -aG docker $USER` and log in again. Check with
`docker compose version`, and `docker version` for an Engine 27 or later (IPv6, §2).

## 4. Get the code

The server runs published versions only (git tags `vX.Y.Z`, §10), never the tip of `main`.

```sh
sudo mkdir -p /srv/agaura && sudo chown $USER: /srv/agaura
git clone <repository address> /srv/agaura
cd /srv/agaura
git checkout "$(git tag --list 'v[0-9]*' --sort=-version:refname | head -n 1)"   # latest version
```

Until the repository is published, copy the working tree from the development machine over SSH,
in the repository's directory (`sudo apt install rsync` on the server first). The files listed
in `.gitignore` (`.env`, `data/`, `backups/`, `logs/`) are neither sent nor deleted on the
server (rsync does not read the `!.env.example` exception of `.gitignore`, hence the
`--include`):

```sh
rsync -az --delete --include=.env.example --exclude-from=.gitignore --exclude=.git --exclude=.claude ./ USER@SERVER:/srv/agaura/
```

Such a copy has no version tag, so `deploy/update.sh` cannot update it: with no session in
session (`agaura stats`), take a copy of the database by hand (§10), send the code again the
same way, then `docker compose up -d --build`. Switch to a `git clone` once versions are
published.

The automatic backups (§10) go to a directory of the server, outside the Docker volumes, that
the application's user in the container (uid 1000) writes to. The application does not start
while it is missing:

```sh
mkdir -p -m 700 backups backups/auto    # both private: the backups hold the whole database
[ "$(id -u)" = 1000 ] || sudo chown 1000:1000 backups/auto
```

## 5. Configure

```sh
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"    # the master secret
```

In `.env`:

| Variable | Production value |
|---|---|
| `AGAURA_ENVIRONMENT` | `production` (HTTPS-only cookies, HSTS, no `/docs`; refuses to start without a real secret). `development` is refused with a public `AGAURA_BASE_URL` or `AGAURA_DOMAIN` |
| `AGAURA_SECRET_KEY` | the generated secret, at least 32 characters |
| `AGAURA_BASE_URL` | `https://agaura.org` (links in emails and QR codes of code cards): in production on a public address, refused unless `https://` followed by `AGAURA_DOMAIN` |
| `AGAURA_DOMAIN` | `agaura.org` (served by Caddy, `www` redirected) |
| `AGAURA_EMAIL_BACKEND` | `smtp` (`console`, which sends nothing, is refused in production on a public address) |
| `AGAURA_SMTP_HOST`, `AGAURA_SMTP_PORT` | `mail.infomaniak.com`, `465` |
| `AGAURA_SMTP_USERNAME`, `AGAURA_SMTP_PASSWORD` | the mailbox and its password |
| `AGAURA_EMAIL_FROM` | `"AGaura <no-reply@agaura.org>"` (quoted: it contains spaces) |
| `AGAURA_SIGNUP` | `closed`: the super-admin invites each account |
| `AGAURA_CONTACT_EMAIL` | `contact@agaura.org` (shown on the home, login and `/signup` pages to ask for an account) |
| `AGAURA_DEMO`, `AGAURA_DEMO_MAX` | `true` (the default; `false` turns it off), `50`: "Essayer la démo" opens a temporary account of one's own, deleted with its sessions after 2 h without activity, 24 h at most (FR-09a) |
| `AGAURA_SOURCE_URL` | address of the public repository (link to the source code, AGPL section 13) |
| `CADDY_LOG_DIR` | `/srv/agaura/logs/caddy` (access log read by fail2ban) |
| `BACKUP_DIR` | `/srv/agaura/backups/auto` (the default `./backups/auto` is the same; automatic backups, §10) |
| `AGAURA_BACKUP_KEEP_DAYS` | `30` (the default): days an automatic backup is kept, the latest one always |

**Keep a copy of `AGAURA_SECRET_KEY` outside the server** (password manager). Session passwords
are stored encrypted and individual codes as digests derived from it, and the audit log is
hash-chained with a key derived from it: a lost or changed secret makes passwords and codes
unusable, and every existing log entry then fails `agaura audit verify`.

## 6. Start

```sh
docker compose up -d --build
docker compose ps                                  # app "healthy", caddy "Up"
curl https://agaura.org/health                     # {"status":"ok",…}
curl -I https://www.agaura.org                     # 301 to https://agaura.org/
```

The database is created and migrated when the application starts; the certificate is obtained
on the first request (ports 80 and 443 must be reachable). If `/health` does not answer:
`docker compose logs caddy` (DNS or firewall) and `docker compose logs app` (configuration).

## 7. Create the super-admin

Once `/health` answers 200 (before, the tables do not exist yet):

```sh
docker compose exec app agaura user create admin@agaura.org --superadmin    # asks the password twice
```

The super-admin logs in at `https://agaura.org/login`, lands on `/superadmin` and invites the
first account by email; it never sees the sessions. Check that the invitation reaches the inbox,
not the spam folder (SPF, DKIM and DMARC passing).

## 8. fail2ban

Follow [deploy/README.md](../deploy/README.md) §2 and §3: install fail2ban, copy the AGaura
filter and jail, check the filter with `fail2ban-regex` on the real log, restart, then
`sudo fail2ban-client status agaura-probes`.

## 9. Final check

- [ ] `https://agaura.org/health` answers 200 with a valid certificate; `www` redirects.
- [ ] `curl -sI https://agaura.org/login` shows `Strict-Transport-Security` and
      `Content-Security-Policy`.
- [ ] The super-admin invites an account; the email arrives; the invited person sets a password.
- [ ] That account creates a session, opens it, a phone joins with the QR code and votes.
- [ ] `docker compose exec app agaura audit verify` reports an intact chain.
- [ ] `docker compose exec app agaura db backups` lists a first backup, written at the start.
- [ ] `sudo fail2ban-client status` lists `sshd` and `agaura-probes`.
- [ ] With the `AAAA` record: a request over IPv6 is logged with the visitor's own address, not
      `172.x.0.1` ([deploy/README.md](../deploy/README.md) §2).

## 10. Running it

### Publish a version

Versions are git tags `vX.Y.Z` on a commit of `main` whose CI passed. On the development
machine:

```sh
# The version number in agaura/__init__.py and pyproject.toml (a test checks they match), then:
uv lock
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "AGaura 0.2.0"
git push origin main v0.2.0
```

### Update

```sh
tmux new -s update                       # (sudo apt install tmux) a lost SSH link stops nothing
/srv/agaura/deploy/update.sh             # the latest version, after a summary and a question
/srv/agaura/deploy/update.sh v0.2.0      # a given version; --yes skips the question
```

The script fetches the tags and shows the commits since the installed version and the number of
new migrations, with a warning when `.env.example` changed (a new setting may belong in `.env`).
It refuses while a session is in session (demo sessions aside), then:

1. builds the new image while the current version keeps serving;
2. checks the sessions again, stops the application (the site is down for about half a minute)
   and backs up the database to `backups/agaura-<date>-<installed version>.db`;
3. starts the new version, which applies the migrations, and waits up to two minutes for
   `/health` to report the new version number; Caddy restarts too when `deploy/Caddyfile`
   changed, and Docker pulls and starts the Caddy version that `compose.yaml` names when a
   release raised it;
4. checks the audit log (`agaura audit verify`).

If step 3 or 4 fails, the previous image, code and database come back by themselves;
`docker compose logs app` tells why. A version older than the installed one is refused:
migrations do not go back. Settings belong in `.env` only: the script refuses to run when a
tracked file was changed on the server (`git status`). It creates `backups/auto/` when missing
and warns when that directory does not belong to uid 1000 (§4).

To undo an update that went through but turned out wrong:

```sh
/srv/agaura/deploy/update.sh --rollback
```

It restores the previous version with the database saved just before the update: everything
done since (accounts, sessions, votes) is lost, once a copy of the current database is kept in
`backups/`. Only the last update can be undone this way.

Each update leaves Docker build cache behind; `docker builder prune --filter until=720h` clears
what is more than a month old.

**Never `docker compose down -v`**: `-v` deletes the volumes, that is the database
(`agaura-data`) and the certificates (`caddy-data`). Certificates asked for again and again hit
Let's Encrypt's limits (5 identical certificates a week), leaving the site without HTTPS for
days. `docker compose down` (without `-v`) keeps both.

### Logs

```sh
docker compose logs -f app caddy    # no ballot content, no password, no code; secrets in paths redacted
docker compose logs app | grep -E 'backup|retention|ERROR'
```

In production the application writes one JSON object per line (time, level, logger, message);
Caddy's access log is JSON too. Docker keeps three files of 10 MB per service.

### Backup and restore

**Automatic backups.** The application copies its database by itself, once a day and within a
minute of each poll closing, into `backups/auto/` (`agaura-YYYYMMDD-HHMMSSZ.db`, UTC). It never
copies while a poll of a real session is open: copies taken during a vote, compared with each
other, show which ballots and which participations arrived in between (SPEC §9.2). Copies are
compacted (`VACUUM INTO`: every table rebuilt in key order) and kept 30 days, the latest one
always. A copy is skipped, with an error in the logs, while the disk has less free space than
twice the database plus 256 MiB.

```sh
docker compose exec app agaura db backups      # the copies, and the age of the latest one
```

A backup holds the whole database; session passwords stay encrypted with `AGAURA_SECRET_KEY`.
Keep backups and the secret key in separate places: a backup whose log was altered without the
key fails `agaura audit verify` once restored. For now backups stay on the server (SPEC §10):
losing the VPS loses them too.

**A copy by hand**, for instance before an intervention, through standard output (no file to
clean up in the volume):

```sh
( umask 077 && docker compose exec -T app sh -c 'agaura db backup /tmp/copy.db >&2 && cat /tmp/copy.db; s=$?; rm -f /tmp/copy.db; exit $s' \
  > backups/by-hand.partial ) && mv backups/by-hand.partial backups/agaura-$(date +%F)-by-hand.db
```

`umask 077` keeps the copy readable by its owner only, wherever it is moved later. The copies
that `deploy/update.sh` and `deploy/restore.sh` leave in `backups/` (and those made by hand)
are never deleted by the application: they still hold the accounts deleted since (D12). Remove
them once no longer needed, keeping the latest few.

`agaura db backup` refuses while a poll is open (exit status 1); `--force` overrides it, for an
emergency copy only. Never copy the database file page by page (`cp`, `sqlite3 .backup`,
SQLite's backup API) while the application runs: during a vote, its pages keep the arrival order
of ballots (SPEC §9.2).

**Restore**, with no session in session:

```sh
deploy/restore.sh agaura-20261105-143000Z.db              # an automatic backup, by its name
deploy/restore.sh backups/agaura-20261103-101500-v0.2.0.db  # or a copy on the server
```

The script checks the copy while the site keeps serving (`agaura db check`: integrity, migrations
applied to a temporary copy, audit log keyed with the current secret) and shows what it holds.
After a question (`--yes` skips it), it stops the application, backs the current database up to
`backups/agaura-<date>-before-restore.db`, puts the copy in place and starts again, then
verifies the audit log: about ten seconds without service. Everything written since the copy
was taken is lost. `--force` restores while a session is in session or when the current
database is damaged (docs/RUNBOOK.md §2). Restoring and updating share a lock: never both at
once.

**Restore test** (P7 exit criterion), every few months and after a change of server, without
touching the service: check the latest automatic copy, which migrates a temporary copy of it
and verifies its audit log with the secret in use.

```sh
docker compose exec app agaura db backups                                  # the latest name
docker compose exec app agaura db check /backups/agaura-YYYYMMDD-HHMMSSZ.db
```

The full drill runs on a second checkout of the same version, with its own `.env` copied from
the real one but `COMPOSE_PROJECT_NAME=agaura-drill`, `AGAURA_EMAIL_BACKEND=console` (no
email may leave a drill), `AGAURA_BASE_URL=https://localhost` and `AGAURA_DOMAIN=localhost`
(the console backend is refused on a public address) and `BACKUP_DIR=./backups/auto` (the real
value, an absolute path, would let the drill write and prune the real backups), its own
`backups/auto/` holding a copy of the latest backup, and the
application only (`docker compose up -d app`, no Caddy, no published port): `deploy/restore.sh`
there. Once done, remove that project with its name in the command, so that it can never reach
the real one: `docker compose -p agaura-drill down -v`. Write down how long it took.

### Operator commands

Run in the container: `docker compose exec app agaura …`.

| Command | Use |
|---|---|
| `agaura stats` | Counts only: accounts, sessions, polls. Demo sessions are counted apart: `in_session` is the update guard |
| `agaura audit verify [--anchor HASH]` | Check the audit log's hash chain, keyed with `AGAURA_SECRET_KEY` (a changed key fails from the first entry); `--anchor` also checks that the log still holds the entry whose hash PDF minutes print ("Empreinte du journal de la session") |
| `agaura user create EMAIL [--superadmin] [--verified]` | Create an account (asks the password) |
| `agaura user disable EMAIL` / `enable EMAIL` | Suspend or restore an account: disabled, it cannot log in, its delegate links stop, and its sessions look unknown from outside (projection screen, public results, voter pages, joining and voting), until `enable` brings everything back; nothing is deleted |
| `agaura user quota EMAIL [--open-assemblies N] [--polls-per-assembly N] [--codes-per-assembly N] [--reset]` | Show or change the quotas of an account |
| `agaura user delete EMAIL` | Delete an account and its sessions (refused while in session) |
| `agaura session delete SESSION_ID [--force]` | Delete an abusive session and all its data (`--force` even while in session) |
| `agaura db upgrade` | Apply migrations (done at each start anyway) |
| `agaura db backup PATH [--force]` | Compacted copy of the database (refused while a poll is open) |
| `agaura db backups` | The automatic backups and the age of the latest one |
| `agaura db check PATH` | Check a database copy before restoring it (the file is only read) |
| `agaura email failed` | Emails given up (8 failed attempts, or a recipient refused for good), with the error |
| `agaura email retry ID… \| --all` | Send given-up emails again, once the cause is fixed |

### A legitimate visitor is refused

The application refuses an address with "Trop de tentatives" for up to 10 minutes after
repeated failures (15 minutes for logins on one account: only the address that failed is
refused, so the account holder can log in from another network, e.g. mobile data); fail2ban
never bans for that. fail2ban only bans addresses probing for other software; to lift such a ban
at once: `sudo fail2ban-client set agaura-probes unbanip ADDRESS`. On an assembly day, exempt
the venue address beforehand (deploy/README.md §4). During an assembly, the console warns the
tribune when passwords are refused in numbers.

After a few failed logins on an account from anywhere, its login form asks for the anti-robot
check (JavaScript needed, under a second): someone else may be guessing its password.

A PDF (minutes, code cards) refused with "Serveur occupé" waited 3 s behind another document:
one is rendered at a time. "Trop de documents" means more than 10 PDFs asked by the account in
10 minutes. A login or password change refused with "Serveur occupé" came while many passwords
were being checked at once (two at a time); retry a few seconds later. All lift by themselves
(SPEC §9.3).
