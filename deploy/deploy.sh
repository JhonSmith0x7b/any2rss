#!/usr/bin/env bash
# Root-owned forced command. Install independently of the writable checkout.
set -euo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DOCKER_CONFIG=/root/.docker
unset DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROFILES BASH_ENV ENV

[[ $# == 1 && $1 =~ ^deploy\ ([0-9a-f]{40})$ ]] || { echo 'Expected: deploy <40-character commit SHA>' >&2; exit 64; }
revision=${BASH_REMATCH[1]}
root=/opt/any2rss
exec 9>"$root/deploy.lock"
flock -w 600 9 || { echo 'Another deployment is running' >&2; exit 75; }
cd "$root/repository"
git fetch --quiet origin main
[[ $(git rev-parse origin/main) == "$revision" ]] || { echo 'Only current origin/main can be deployed' >&2; exit 65; }
[[ -s "$root/runtime.env" ]] || { echo 'Missing runtime.env' >&2; exit 66; }

release="$root/releases/$revision"
mkdir -p "$release" "$root/data" "$root/backups"
git archive "$revision" | tar -x -C "$release"
image="any2rss:$revision"
docker build --quiet -t "$image" "$release"

# SQLite's online backup API captures WAL consistently without stopping readers.
if [[ -f "$root/data/any2rss.sqlite3" ]]; then
  backup="any2rss-$(date -u +%Y%m%dT%H%M%SZ)-$revision.sqlite3"
  docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true \
    -v "$root/data:/data" -v "$root/backups:/backups" "$image" \
    python -c 'import sqlite3,sys; src=sqlite3.connect("file:/data/any2rss.sqlite3?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[1]); src.backup(dst); dst.close(); src.close()' "/backups/$backup"
  chmod 600 "$root/backups/$backup"
fi

previous=''
if [[ -f "$root/current-revision" ]]; then previous=$(cat "$root/current-revision"); fi
run_compose() {
  ANY2RSS_IMAGE="any2rss:$1" docker compose -f "$root/releases/$1/deploy/compose.yaml" "${@:2}"
}
if ! run_compose "$revision" up -d --wait --wait-timeout 180; then
  echo 'Deployment unhealthy; restoring previous container.' >&2
  if [[ $previous =~ ^[0-9a-f]{40}$ && -f "$root/releases/$previous/deploy/compose.yaml" ]]; then
    run_compose "$previous" up -d --wait --wait-timeout 180 || echo 'Rollback also failed; operator intervention required.' >&2
  else
    run_compose "$revision" down || true
  fi
  echo 'Database was not rolled back. A consistent pre-deployment backup is retained.' >&2
  exit 1
fi
printf '%s\n' "$revision" > "$root/current-revision.next"
mv "$root/current-revision.next" "$root/current-revision"
echo "Deployed $revision"
