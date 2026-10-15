#!/bin/bash
# Runs ON the backend server. GitHub Actions copies it there
# over SSH and runs:  bash /tmp/cinevote-deploy.sh <new-tag>
# Steps: pull new images -> up -d -> smoke test -> keep or roll back
set -euo pipefail

NEW_TAG="$1"
APP_DIR="${APP_DIR:-$HOME/voting-app/backend}"
cd "$APP_DIR"

# The tag running now, read from .env (e.g. IMAGE_TAG=initial)
if ! grep -q '^IMAGE_TAG=' .env; then
  echo "No IMAGE_TAG line in .env (CI/CD guide, Part 5.3)"
  exit 1
fi
PREV_TAG=$(grep '^IMAGE_TAG=' .env | cut -d= -f2)
echo "Running now: ${PREV_TAG:-none}    Deploying: $NEW_TAG"

set_tag() {
  sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$1/" .env
}

# Smoke test: wait up to 90 s for Docker to call flask and
# worker "healthy", then ask Flask's /health endpoint directly.
smoke_test() {
  for i in $(seq 1 18); do
    F=$(docker inspect -f '{{.State.Health.Status}}' flask || echo missing)
    W=$(docker inspect -f '{{.State.Health.Status}}' worker || echo missing)
    echo "  check $i/18: flask=$F worker=$W"
    if [ "$F" = healthy ] && [ "$W" = healthy ]; then
      curl -fsS localhost:5000/health && echo && return 0
    fi
    sleep 5
  done
  return 1
}

# 1. Pull first. If the images cannot be downloaded, stop here:
#    nothing has changed and the old containers keep running.
IMAGE_TAG="$NEW_TAG" docker compose pull flask worker

# 2. Switch .env to the new tag and recreate flask and worker
set_tag "$NEW_TAG"
docker compose up -d flask worker

# 3. Smoke test. Healthy -> keep it.
if smoke_test; then
  echo "$(date -u +%FT%TZ) deployed $NEW_TAG" >> deploy-history.log
  # Delete unused images older than 3 days to keep the disk free
  docker image prune -af --filter "until=72h" > /dev/null
  echo "DEPLOY OK: $NEW_TAG is live"
  exit 0
fi

# 4. Not healthy -> roll back to the previous tag
echo "SMOKE TEST FAILED for $NEW_TAG. Last Flask log lines:"
docker compose logs flask --tail 20 || true
if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$NEW_TAG" ]; then
  echo "Rolling back to $PREV_TAG"
  set_tag "$PREV_TAG"
  docker compose up -d flask worker
  if smoke_test; then
    echo "ROLLBACK OK: $PREV_TAG is live again"
  else
    echo "WARNING: $PREV_TAG is not healthy either. Check the server."
  fi
  echo "$(date -u +%FT%TZ) $NEW_TAG failed, rolled back to $PREV_TAG" \
    >> deploy-history.log
fi
exit 1
