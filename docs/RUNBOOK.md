# AGaura — Incident runbook

What to do when something goes wrong, before or during an assembly. Commands run on the server
from `/srv/agaura`, as the user who installed AGaura (docs/INSTALL.md). Room incidents are the
tribune's; server incidents are the operator's. In every case, **never break a tie or fix a
result by hand in the database**: cancel and vote again, and let the minutes say so.

First look, whatever the incident:

```sh
docker compose ps                                   # app "healthy", caddy "Up"
docker compose logs --since 15m app caddy | tail -50
docker compose exec app agaura stats                # sessions in session, polls, ballots
docker compose exec app agaura db backups           # latest automatic backup
```

## 1. The service does not answer (VPS unavailable)

Symptoms: `https://agaura.org` times out, or the external monitor alerts.

1. **In the room**: tell the tribune at once. Ballots confirmed on the phones are on disk: a
   restart loses none (SPEC §10). A poll whose timer ran out meanwhile closes by itself once the
   service is back. If the service cannot come back before the next vote, use the paper
   fallback of the session kit (P8).
2. Infomaniak Manager: is the VPS running? Any maintenance or incident announced? Restart it from
   there if it does not answer over SSH.
3. Once logged in:
   - `docker compose ps`: a stopped container comes back with `docker compose up -d`;
   - `df -h /`: a full disk stops the database; remove old Docker build cache
     (`docker builder prune --filter until=720h`) or old copies in `backups/` (never the most
     recent ones);
   - `docker compose logs app`: an application that restarts in a loop with `malformed` in its
     logs is §2.
4. After the incident: `docker compose exec app agaura audit verify`, then tell the tribunes of
   the sessions in progress what happened and when.

**VPS lost for good**: the backups are on the VPS too (SPEC §10, off-site copy still to come).
Install a new server (docs/INSTALL.md) with the same `AGAURA_SECRET_KEY` (kept outside the
server), then restore the most recent copy that survived (§2); the exports the tribunes
downloaded are the only record of what came after it.

## 2. Damaged database

Symptoms: `database disk image is malformed` in `docker compose logs app` or from an `agaura`
command; the application restarts in a loop; pages answer 500.

1. **During a vote**: the tribune stops relying on the service (paper fallback). Nothing written
   after the latest backup will come back: note the time.
2. Choose the copy: `docker compose run --rm --no-deps -T app agaura db backups` (works with the
   application stopped). The latest automatic backup was taken after the latest poll closing.
3. Restore it:

   ```sh
   deploy/restore.sh --force agaura-YYYYMMDD-HHMMSSZ.db
   ```

   The script checks the copy first (integrity, migrations, audit log), then stops the
   application, keeps the damaged database aside, puts the copy in place and starts again: about
   ten seconds without service. `--force` is needed when the damaged database cannot be read,
   or while a session is in session.
4. Damaged files that could not be copied go to `/data/damaged-<date>/` in the volume. They keep
   the arrival order of the ballots of an open poll (SPEC §9.2): once no longer needed, delete
   them (`docker compose exec app rm -r /data/damaged-<date>`).
5. Tell the tribunes concerned: polls closed after the backup, accounts and sessions created
   since, are gone. A poll open at the time of the backup is open again after the restore,
   without the ballots cast later: cancel it with a reason and vote again (§5).

Restoring a copy written by `deploy/update.sh` (`backups/agaura-<date>-<version>.db`) works the
same way, with its path instead of a name.

## 3. Mobile network outage in the room

Symptoms: several voters cannot load the page or send their ballot; the console still works on
the tribune's computer (another network).

1. Do not close the poll: ballots already confirmed are counted, and a phone that sends its ballot
   again once back online gets "vote déjà enregistré" if it had gone through (SPEC §10).
2. If the poll has a timer, it closes by itself: cancel it rather than let people be shut out
   (§5), or announce more time before the next vote.
3. Offer another way in: the venue Wi-Fi if there is one (on assembly days its address is exempted
   from fail2ban, deploy/README.md §4), or a phone shared as a hotspot.
4. If too many people cannot vote, cancel the poll with a reason and vote by a show of hands or on
   paper; the minutes record the cancellation.

## 4. Session password leaked outside the room

Symptoms: the console shows more devices than people present, or the headcount warning (FR-28).

1. **Lock entries** (FR-27): no new device can join; those already in are not affected. Locking
   also hides the password and the QR code from the projection screen.
2. Enter the headcount from the sign-in sheet (FR-28): the console warns when devices or ballots
   exceed it.
3. Replace the session password (FR-20) before unlocking for latecomers: devices that joined
   keep their access.
4. If a vote may have been distorted (more ballots than people), cancel it with a reason and run
   it again in codes mode (cards handed out at the door, FR-23) or by a show of hands. Codes cannot
   be generated while a poll is open.
5. Many refused passwords from one address: the console warns the tribune and the application
   refuses that address only (SPEC §9.3); nothing to do on the server.

## 5. Mistake in an open poll

Symptoms: a wrong option, title or rule in a poll already open.

1. An open poll cannot be edited: **cancel it** with the mandatory reason (FR-35). Its ballots are
   kept, no result is shown, and the minutes record the cancellation and its reason.
2. Duplicate it, correct the copy, and push it.
3. Say aloud why the vote is run again: on the phones, the cancelled poll simply disappears.

A mistake found after the closing follows the same steps: a closed or published poll can be
cancelled too.

## After any incident

- `docker compose exec app agaura audit verify` reports an intact chain.
- `docker compose exec app agaura db backups`: a new copy once no poll is open.
- `docker compose exec app agaura email failed`: emails given up during the incident; once the
  cause is fixed, `agaura email retry --all`.
- Write down what happened, when, and what was lost, for the tribune and the minutes.
