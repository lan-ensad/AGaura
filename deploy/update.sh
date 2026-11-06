#!/usr/bin/env bash
# Update AGaura on the server to a released version (docs/INSTALL.md §10).
#
#   deploy/update.sh [--yes] [vX.Y.Z]     update to this version, or to the latest one
#   deploy/update.sh [--yes] --rollback   go back to the version and database before the last
#                                         update (everything done since is lost)
#
# Versions are the git tags vX.Y.Z. Refused while a session is in session (demo sessions
# aside, SPEC §10). The new image is built while the current version keeps serving; then the
# application stops (about half a minute without service), the database is backed up to
# backups/, and the new version starts and migrates it. If it does not report the expected
# version within two minutes, or its audit log fails to verify, the previous image, code and
# database come back by themselves.
#
# Run it from any directory as the user who installed AGaura (a member of the docker group).

set -euo pipefail

readonly HEALTH_TIMEOUT=120
readonly STATE=backups/last-update
# Version reported by /health to a client inside the container only (agaura/main.py).
readonly HEALTH_VERSION='import json, urllib.request
print(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3))["version"])'

assume_yes=0
stage=none # what the exit trap must undo: none, checked_out, stopped, swapped, rolling_back, finished
app_image='' repository='' next_image=''
current_commit='' current_label='' current_version='' backup='' rollback_image=''

say() { printf '==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
fail() {
	printf 'Error: %s\n' "$*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
Usage: deploy/update.sh [--yes] [vX.Y.Z]
       deploy/update.sh [--yes] --rollback

Update AGaura to a released version (the latest one by default), or go back to the version
and database before the last update. Details: docs/INSTALL.md §10.
EOF
}

version_at() { # version number in agaura/__init__.py at a commit
	git show "$1:agaura/__init__.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p'
}

# Releases only: a pre-release tag (v1.0.0-rc1) sorts above its release and would be refused.
latest_tags() {
	git tag --list 'v[0-9]*' --sort=-version:refname |
		awk '/^v[0-9]+\.[0-9]+\.[0-9]+$/' | sed -n "1,${1}p"
}

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

confirm() {
	((assume_yes)) && return 0
	local answer=''
	read -r -p "$1 [y/N] " answer || true
	if [[ $answer != [yY]* ]]; then
		say "Cancelled: nothing changed."
		exit 0
	fi
}

check_no_session() {
	# agaura stats, with the image in service, whether the application runs or not: its
	# "in_session" line leaves demo sessions out, like /health.
	local stats count
	stats=$(docker compose run --rm --no-deps -T app agaura stats </dev/null) ||
		fail "could not read the platform statistics (agaura stats)."
	count=$(awk '$1 == "in_session:" { print $2 }' <<<"$stats")
	[[ $count =~ ^[0-9]+$ ]] || fail "no in_session line in the output of agaura stats."
	((count == 0)) || fail "$count session(s) in session: update once the assembly is over."
}

back_up_database() { # $1: file to write
	local partial="$1.partial"
	# Through standard output: no file in the volume to clean up, whatever the owner of backups/.
	# umask 077 there only (not for the checkout): the copy holds the whole database.
	if ! (umask 077 && docker compose run --rm --no-deps -T app \
		sh -c 'agaura db backup /tmp/update.db >&2 && cat /tmp/update.db' </dev/null >"$partial"); then
		rm -f "$partial"
		return 1
	fi
	if [[ $(head -c 15 "$partial") != "SQLite format 3" ]]; then
		rm -f "$partial"
		warn "the backup is not an SQLite database."
		return 1
	fi
	mv "$partial" "$1"
}

restore_database() { # $1: backup file; the application must be stopped
	# The WAL files belong to the database being replaced (docs/INSTALL.md, Backup and restore).
	docker compose run --rm --no-deps -T app sh -c \
		'cat >/data/agaura.db.restore && rm -f /data/agaura.db-wal /data/agaura.db-shm && mv /data/agaura.db.restore /data/agaura.db' \
		<"$1"
}

start_services() { # $1: the commit checked out before
	docker compose up -d --no-build --force-recreate app &&
		docker compose up -d --no-build || return 1
	# Caddy reads its configuration file at start only, and the file it mounts is the one that
	# existed then: restart it when the checkout replaced that file.
	if ! git diff --quiet "$1" HEAD -- deploy/Caddyfile; then
		say "The Caddy configuration changed: restarting Caddy"
		docker compose up -d --no-build --force-recreate caddy || return 1
	fi
}

wait_for_version() { # $1: version /health must report
	local deadline=$((SECONDS + HEALTH_TIMEOUT)) running
	while ((SECONDS < deadline)); do
		if running=$(docker compose exec -T app python -c "$HEALTH_VERSION" </dev/null 2>/dev/null); then
			[[ $running == "$1" ]] && return 0
			warn "the application reports version $running instead of $1."
			return 1
		fi
		sleep 3
	done
	warn "no answer from the application within $HEALTH_TIMEOUT s: docker compose logs app"
	return 1
}

verify_audit_log() { docker compose exec -T app agaura audit verify </dev/null; }

return_to() { # $1: commit, $2: its version, $3: its image, $4: backup of its database
	local from
	from=$(git rev-parse HEAD)
	docker compose stop app &&
		restore_database "$4" &&
		docker image tag "$3" "$app_image" &&
		git checkout --quiet "$1" &&
		start_services "$from" &&
		wait_for_version "$2" &&
		verify_audit_log
}

# shellcheck disable=SC2329 # run by on_exit
manual_recovery() { # $1: commit, $2: its image, $3: backup of its database
	cat >&2 <<EOF

The automatic return FAILED. Bring the previous version back by hand, from $PWD:
  docker compose stop app
  git checkout $1
  docker image tag $2 $app_image
  docker compose run --rm --no-deps -T app sh -c 'cat >/data/agaura.db.restore && rm -f /data/agaura.db-wal /data/agaura.db-shm && mv /data/agaura.db.restore /data/agaura.db' <$3
  docker compose up -d --no-build --force-recreate app
  docker compose exec app agaura audit verify
EOF
}

# shellcheck disable=SC2329 # run by the EXIT trap
on_exit() {
	local status=$?
	set +e
	((status == 0)) && return
	case $stage in
	checked_out)
		git checkout --quiet "$current_commit" && say "Back to $current_label: nothing else changed."
		;;
	stopped)
		git checkout --quiet "$current_commit"
		docker compose up -d --no-build app && say "$current_label restarted: nothing else changed."
		;;
	swapped)
		say "Returning to $current_label and its database"
		if return_to "$current_commit" "$current_version" "$rollback_image" "$backup"; then
			say "$current_label is back, with its database as before the update."
		else
			manual_recovery "$current_commit" "$rollback_image" "$backup"
		fi
		;;
	rolling_back)
		manual_recovery "$(state previous_commit)" "$(state previous_image)" "$(state backup)"
		;;
	esac
	[[ -n $next_image ]] && docker image rm "$next_image" >/dev/null 2>&1
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

state() { sed -n "s/^$1=//p" "$STATE"; }

update() { # $1: tag, or empty for the latest one
	local target=$1 target_commit target_version migrations old_rollback_image=''
	git fetch --quiet --tags origin || fail "could not fetch the versions (git fetch)."
	if [[ -z $target ]]; then
		target=$(latest_tags 1)
		[[ -n $target ]] || fail "no version (tag vX.Y.Z) published yet."
	fi
	target_commit=$(git rev-parse --quiet --verify "refs/tags/$target^{commit}") ||
		fail "no version $target. Latest ones: $(latest_tags 5 | paste -sd ' ')"
	target_version=$(version_at "$target_commit")
	if [[ $target_commit == "$current_commit" ]]; then
		say "$target is already installed."
		return
	fi
	if git merge-base --is-ancestor "$target_commit" "$current_commit"; then
		fail "$target is older than the installed $current_label, and migrations do not go back." \
			"To undo the last update: deploy/update.sh --rollback"
	fi
	[[ "v$target_version" == "$target" ]] ||
		fail "tag $target holds version $target_version (agaura/__init__.py): not a release."

	migrations=$(git diff --name-only --diff-filter=A "$current_commit" "$target_commit" \
		-- agaura/migrations/versions | awk '/\.py$/ { n++ } END { print n + 0 }')
	printf '\nInstalled: %s (%s)\nTarget:    %s (%s)\nNew database migrations: %s\n\n' \
		"$current_label" "${current_commit:0:7}" "$target" "${target_commit:0:7}" "$migrations"
	git --no-pager log --oneline --no-decorate -n 25 "$current_commit..$target_commit"
	echo
	if ! git diff --quiet "$current_commit" "$target_commit" -- .env.example; then
		warn ".env.example changed: compare it with .env before going on" \
			"(git diff $current_label $target -- .env.example)."
	fi
	check_no_session
	confirm "Update to $target? The site stops for about half a minute, after the build."

	stage=checked_out
	git checkout --quiet "$target_commit"
	say "Building $target while $current_label keeps running"
	docker build --pull --tag "$next_image" .
	check_no_session # the build took minutes

	# The image in service, kept under the name of its version for the return.
	if [[ -f $STATE ]]; then old_rollback_image=$(state previous_image); fi
	rollback_image="$repository:$current_label"
	docker image tag "$app_image" "$rollback_image"
	stage=stopped
	say "Stopping the application"
	docker compose stop app
	backup="backups/agaura-$(date +%Y%m%d-%H%M%S)-$current_label.db"
	say "Backing up the database to $backup"
	back_up_database "$backup" || fail "the backup failed (refused while a poll is open)."
	docker image tag "$next_image" "$app_image"
	stage=swapped
	say "Starting $target (database migrations apply now)"
	start_services "$current_commit" || fail "could not start $target."
	wait_for_version "$target_version" || fail "$target did not start."
	verify_audit_log || fail "the audit log does not verify under $target."
	stage=finished

	cat >"$STATE" <<EOF
date=$(date '+%Y-%m-%d %H:%M')
label=$target
commit=$target_commit
previous_label=$current_label
previous_commit=$current_commit
previous_version=$current_version
previous_image=$rollback_image
backup=$backup
EOF
	docker image rm "$next_image" >/dev/null || true
	next_image=
	if [[ -n $old_rollback_image && $old_rollback_image != "$rollback_image" ]]; then
		docker image rm "$old_rollback_image" >/dev/null 2>&1 || true
	fi
	say "AGaura $target installed. To go back: deploy/update.sh --rollback"
}

roll_back() {
	local label previous_label previous_commit previous_version previous_image saved safety
	[[ -f $STATE ]] || fail "no update to undo ($STATE is missing)."
	label=$(state label)
	previous_label=$(state previous_label)
	previous_commit=$(state previous_commit)
	previous_version=$(state previous_version)
	previous_image=$(state previous_image)
	saved=$(state backup)
	[[ $current_commit == "$(state commit)" ]] ||
		fail "the installed code ($current_label) is not the one of the last update ($label)."
	[[ -f $saved ]] || fail "the backup $saved is missing."
	image_exists "$previous_image" || fail "the image $previous_image is missing."

	printf '\nInstalled: %s\nBack to:   %s, with its database as saved on %s\n\n' \
		"$label" "$previous_label" "$(state date)"
	echo "Everything done since that update (accounts, sessions, votes) is lost;"
	echo "a copy of the current database stays in backups/."
	echo
	check_no_session
	confirm "Go back to $previous_label?"
	((assume_yes)) || check_no_session # a session may have opened while the question waited

	stage=stopped
	say "Stopping the application"
	docker compose stop app
	safety="backups/agaura-$(date +%Y%m%d-%H%M%S)-$label-before-rollback.db"
	say "Backing up the current database to $safety"
	back_up_database "$safety" || fail "the backup failed (refused while a poll is open)."
	stage=rolling_back
	return_to "$previous_commit" "$previous_version" "$previous_image" "$saved" ||
		fail "could not go back to $previous_label."
	stage=finished
	rm -f "$STATE"
	say "AGaura $previous_label is back. The database of $label: $safety"
}

main() {
	local target='' rollback=0
	while (($#)); do
		case $1 in
		--yes | -y) assume_yes=1 ;;
		--rollback) rollback=1 ;;
		-h | --help)
			usage
			return 0
			;;
		v[0-9]*) target=$1 ;;
		[0-9]*) target=v$1 ;;
		*)
			usage >&2
			return 2
			;;
		esac
		shift
	done
	((rollback)) && [[ -n $target ]] && fail "--rollback takes no version."
	# Run as root, the lock and the copies would belong to root and stop the next runs.
	((EUID != 0)) || fail "run as the user who installed AGaura, not as root (no sudo)."

	cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
	local tool
	for tool in git docker flock awk sed; do
		command -v "$tool" >/dev/null || fail "$tool is not installed."
	done
	docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is missing."
	docker info >/dev/null 2>&1 || fail "Docker unreachable: run as a member of the docker group."
	[[ -f .env ]] || fail ".env is missing: install first (docs/INSTALL.md §5)."
	ensure_backup_dirs
	exec 9>backups/.update.lock
	flock --nonblock 9 || fail "another update is running."

	git diff --quiet HEAD -- ||
		fail "tracked files were changed on the server (git status): settings belong in .env."
	current_commit=$(git rev-parse HEAD)
	current_label=$(git describe --tags --always)
	current_version=$(version_at HEAD)
	app_image=$(docker compose config --images app)
	[[ $app_image == *:* && $app_image != *$'\n'* ]] ||
		fail "unexpected image name for the app service: $app_image"
	repository=${app_image%:*}
	next_image="$repository:next"

	trap on_exit EXIT
	if ((rollback)); then roll_back; else update "$target"; fi
}

# On one line: the checkout replaces this file while it runs, so bash must have read it all.
main "$@"; exit
