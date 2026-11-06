#!/usr/bin/env bash
# Restore the AGaura database from a backup (docs/INSTALL.md §10, docs/RUNBOOK.md).
#
#   deploy/restore.sh [--yes] [--force] BACKUP
#
# BACKUP is the name of an automatic backup (agaura-YYYYMMDD-HHMMSSZ.db, listed by
# "docker compose exec app agaura db backups"), or the path of a copy on the server, such as
# backups/agaura-20261103-101500-v0.2.0.db left by deploy/update.sh.
#
# The copy is checked first (integrity, migrations, audit log) while the application keeps
# serving. Then the application stops (about ten seconds without service), the current
# database is backed up to backups/ (the safety copy), the copy takes its place, and the
# application starts again and verifies its audit log. Everything written since the backup was
# taken (accounts, sessions, votes) is lost.
#
# Refused while a session is in session (demo sessions aside). --force restores anyway, for a
# damaged database: the safety copy is then taken even while a poll is open, and if the
# database cannot be copied at all, its files are moved aside in the volume (/data/damaged-…).
#
# Run it from any directory as the user who installed AGaura (a member of the docker group).

# The "sh -c" scripts run in the container expand their own arguments ($1, $2), on purpose.
# shellcheck disable=SC2016

set -euo pipefail
# The copies written to backups/ hold the whole database: readable by their owner only.
umask 077

readonly HEALTH_TIMEOUT=120
readonly STAGED=/data/restore.db
# Version reported by /health to a client inside the container only (agaura/main.py).
readonly HEALTH_VERSION='import json, urllib.request
print(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3))["version"])'

assume_yes=0 force=0
stage=none # what the exit trap must undo: none, staged, stopped, swapped, finished
safety=''

say() { printf '==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
fail() {
	printf 'Error: %s\n' "$*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
Usage: deploy/restore.sh [--yes] [--force] BACKUP

Put a backup in place of the database: the name of an automatic backup
(docker compose exec app agaura db backups), or the path of a copy on the server.
Everything written since the backup was taken is lost. Details: docs/INSTALL.md §10.
EOF
}

confirm() {
	((assume_yes)) && return 0
	local answer=''
	read -r -p "$1 [y/N] " answer || true
	if [[ $answer != [yY]* ]]; then
		say "Cancelled: nothing changed."
		exit 0
	fi
}

in_app() { docker compose run --rm --no-deps -T app "$@"; }

check_no_session() {
	# agaura stats leaves demo sessions out of "in_session", like /health and deploy/update.sh.
	local stats count
	if ! stats=$(in_app agaura stats </dev/null); then
		((force)) || fail "could not read the current database (agaura stats);" \
			"if it is damaged, add --force."
		warn "the current database cannot be read: going on (--force)."
		return
	fi
	count=$(awk '$1 == "in_session:" { print $2 }' <<<"$stats")
	[[ $count =~ ^[0-9]+$ ]] || fail "no in_session line in the output of agaura stats."
	((count == 0)) && return
	((force)) || fail "$count session(s) in session: restore once the assembly is over," \
		"or add --force (docs/RUNBOOK.md)."
	warn "$count session(s) in session: restoring anyway (--force)."
}

stage_copy() { # $1: automatic backup name or host file; writes $STAGED in the volume
	if ! [[ $1 =~ ^agaura-[0-9]{8}-[0-9]{6}Z\.db$ ]]; then # a file of the host
		[[ -f $1 ]] || fail "no file $1 (an automatic backup is given by its name only)."
		[[ $(head -c 15 "$1") == "SQLite format 3" ]] || fail "$1 is not an SQLite database."
		in_app sh -c 'cat >"$1.partial" && mv "$1.partial" "$1"' sh "$STAGED" <"$1"
	else
		in_app sh -c 'test -f "/backups/$1" || { echo "No automatic backup $1." >&2; exit 1; }
			cp "/backups/$1" "$2.partial" && mv "$2.partial" "$2"' sh "$1" "$STAGED" </dev/null ||
			fail "could not read $1 (list: docker compose exec app agaura db backups)."
	fi
}

back_up_database() { # $1: file to write, $2: extra option of agaura db backup (--force or empty)
	local partial="$1.partial"
	# Through standard output: no file in the volume to clean up, whatever the owner of backups/.
	if ! in_app sh -c 'agaura db backup /tmp/safety.db $1 >&2 && cat /tmp/safety.db' sh "$2" \
		</dev/null >"$partial"; then
		rm -f "$partial"
		return 1
	fi
	if [[ $(head -c 15 "$partial") != "SQLite format 3" ]]; then
		rm -f "$partial"
		return 1
	fi
	mv "$partial" "$1"
}

move_damaged_aside() { # $1: directory name in the volume
	in_app sh -c 'mkdir -m 700 "/data/$1" && for f in /data/agaura.db /data/agaura.db-wal /data/agaura.db-shm; do
		if [ -e "$f" ]; then mv "$f" "/data/$1/"; fi; done' sh "$1" </dev/null
}

wait_for_health() {
	local deadline=$((SECONDS + HEALTH_TIMEOUT))
	while ((SECONDS < deadline)); do
		docker compose exec -T app python -c "$HEALTH_VERSION" </dev/null >/dev/null 2>&1 && return 0
		sleep 3
	done
	warn "no answer from the application within $HEALTH_TIMEOUT s: docker compose logs app"
	return 1
}

ensure_backup_dirs() {
	mkdir -p backups/auto
	chmod 700 backups # the backups hold the whole database
	local owner
	owner=$(stat -c %u backups/auto)
	if [[ $owner == "$(id -u)" ]]; then chmod 700 backups/auto; fi
	# The automatic backups (compose.yaml): the application's user in the container is uid 1000.
	[[ $owner == 1000 ]] || warn "backups/auto belongs to uid $owner, not 1000: the application" \
		"cannot write its backups there (sudo chown 1000:1000 backups/auto)."
}

# shellcheck disable=SC2329 # run by the EXIT trap
on_exit() {
	local status=$?
	set +e
	((status == 0)) && return
	case $stage in
	staged)
		in_app rm -f "$STAGED" </dev/null
		;;
	stopped)
		in_app rm -f "$STAGED" </dev/null
		docker compose up -d --no-build app && say "Application restarted with its database: nothing changed."
		;;
	swapped)
		cat >&2 <<EOF

The restored database is in place but the application did not come back (docker compose logs app).
EOF
		if [[ -n $safety ]]; then
			# Absolute paths: the script moved to the repository, wherever it was started from.
			printf 'To put the previous database back:\n  %s %s\n' "$PWD/deploy/restore.sh" \
				"$PWD/$safety" >&2
		fi
		;;
	esac
}

main() {
	local backup='' started name
	while (($#)); do
		case $1 in
		--yes | -y) assume_yes=1 ;;
		--force) force=1 ;;
		-h | --help)
			usage
			return 0
			;;
		-*)
			usage >&2
			return 2
			;;
		*)
			[[ -z $backup ]] || {
				usage >&2
				return 2
			}
			backup=$1
			;;
		esac
		shift
	done
	[[ -n $backup ]] || {
		usage >&2
		return 2
	}
	# A host path relative to where the script was started, before moving to the repository.
	if [[ -f $backup ]]; then backup=$(readlink -f "$backup"); fi

	# Run as root, the lock and the copies would belong to root and stop the next runs.
	((EUID != 0)) || fail "run as the user who installed AGaura, not as root (no sudo)."
	cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
	local tool
	for tool in docker flock awk head; do
		command -v "$tool" >/dev/null || fail "$tool is not installed."
	done
	docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is missing."
	docker info >/dev/null 2>&1 || fail "Docker unreachable: run as a member of the docker group."
	[[ -f .env ]] || fail ".env is missing: install first (docs/INSTALL.md §5)."
	ensure_backup_dirs
	exec 9>backups/.update.lock # the lock of deploy/update.sh: never both at once
	flock --nonblock 9 || fail "an update or a restore is running."
	trap on_exit EXIT

	say "Checking $backup (the application keeps serving)"
	stage=staged
	stage_copy "$backup"
	in_app agaura db check "$STAGED" </dev/null || fail "this backup cannot be restored."
	check_no_session
	echo
	echo "Everything written since this backup was taken (accounts, sessions, votes) is lost;"
	echo "a copy of the current database is kept in backups/."
	confirm "Restore $backup? The site stops for about ten seconds."
	((assume_yes)) || check_no_session # a session may have opened while the question waited

	started=$SECONDS
	stage=stopped
	say "Stopping the application"
	docker compose stop app
	safety="backups/agaura-$(date +%Y%m%d-%H%M%S)-before-restore.db"
	say "Backing up the current database to $safety"
	if ! back_up_database "$safety" ''; then
		((force)) || fail "the current database could not be backed up (a poll is open?)."
		if back_up_database "$safety" --force; then
			warn "the safety copy holds an open poll (--force)."
		else
			name="damaged-$(date +%Y%m%d-%H%M%S)"
			warn "the current database cannot be copied: its files go to /data/$name in the" \
				"volume. They keep the arrival order of ballots (SPEC §9.2): delete them once" \
				"no longer needed (docs/RUNBOOK.md)."
			move_damaged_aside "$name" || fail "could not move the damaged database aside."
			safety=''
		fi
	fi
	stage=swapped
	say "Putting the backup in place"
	# The WAL files belong to the database being replaced: never leave them beside the copy.
	in_app sh -c 'rm -f /data/agaura.db-wal /data/agaura.db-shm && mv -f "$1" /data/agaura.db' \
		sh "$STAGED" </dev/null
	say "Starting the application (migrations apply now if the backup is older)"
	docker compose up -d --no-build app
	wait_for_health || fail "the application did not start."
	docker compose exec -T app agaura audit verify </dev/null ||
		fail "the audit log does not verify after the restore."
	stage=finished
	say "Database restored from $backup in $((SECONDS - started)) s of downtime."
	[[ -z $safety ]] || say "The database replaced: $safety"
}

# On one line, like deploy/update.sh: bash reads the whole file before running it.
main "$@"; exit
