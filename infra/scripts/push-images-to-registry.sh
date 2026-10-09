#!/bin/sh
# Tag and push the host-built sandbox/proxy images to the sidecar registry
# so DinD (and a future K8s cluster) can pull them by name.
# Idempotent — re-running with the same images is a no-op.
#
# Network shape: this script runs in a sidecar container with the host's
# docker.sock mounted. Both `tag` and `push` are forwarded to the host docker
# daemon, which performs the push from the host's network namespace — reaching
# the registry via its loopback-bound port (default 127.0.0.1:5001). DinD
# pulls the same images by their backend-network name (registry:5000).
#
# #523 adds two guards:
#   REGISTRY_AUTH_DIR  holds the `username` and `password` that registry-auth-init
#                      generated. The registry refuses anonymous pushes, so the
#                      script logs in first, into a throwaway docker config
#                      that is deleted on exit.
#   PINS_FILE          receives `<image>:<tag> <digest>` for every image pushed,
#                      the digest taken from the push itself. The orchestrator
#                      runs these images by digest, so a later push to the same
#                      tag, by anyone, is never used. Written only when every
#                      push succeeded, by an atomic rename.
set -eu

REGISTRY="${REGISTRY:-127.0.0.1:5001}"
IMAGES="${IMAGES:-embry0-proxy embry0-sandbox embry0-sandbox-dev-python embry0-sandbox-dev-jvm-python embry0-sandbox-qa}"
TAG="${TAG:-latest}"
REGISTRY_AUTH_DIR="${REGISTRY_AUTH_DIR:-}"
PINS_FILE="${PINS_FILE:-}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

if [ -n "$REGISTRY_AUTH_DIR" ]; then
    for f in username password; do
        if [ ! -s "$REGISTRY_AUTH_DIR/$f" ]; then
            echo "ERROR: registry $f missing at $REGISTRY_AUTH_DIR/$f." >&2
            echo "registry-auth-init writes it: docker compose logs registry-auth-init" >&2
            exit 1
        fi
    done
    export DOCKER_CONFIG="$workdir/docker-config"
    mkdir -m 700 "$DOCKER_CONFIG"
    docker login "$REGISTRY" --username "$(cat "$REGISTRY_AUTH_DIR/username")" \
        --password-stdin <"$REGISTRY_AUTH_DIR/password"
fi

pins="$workdir/pins"
: >"$pins"

for image in $IMAGES; do
    src="${image}:${TAG}"
    dst="${REGISTRY}/${image}:${TAG}"
    if ! docker image inspect "$src" >/dev/null 2>&1; then
        echo "ERROR: ${src} not found in host docker daemon." >&2
        echo "Build it first: docker compose --profile images build" >&2
        exit 1
    fi
    docker tag "$src" "$dst"
    if ! docker push "$dst" >"$workdir/push.out"; then
        cat "$workdir/push.out"
        exit 1
    fi
    cat "$workdir/push.out"
    # `<tag>: digest: sha256:<hex> size: <n>` names what was pushed. Not
    # RepoDigests: on the containerd image store that can name a multi-platform
    # index the push never sent.
    digest="$(sed -n 's/^.*digest: \(sha256:[0-9a-f]\{64\}\).*$/\1/p' "$workdir/push.out" | tail -n 1)"
    if [ -z "$digest" ]; then
        echo "ERROR: the push of ${dst} reported no digest, so it cannot be pinned." >&2
        exit 1
    fi
    printf '%s %s\n' "${image}:${TAG}" "$digest" >>"$pins"
    echo "pushed ${dst} ${digest}"
done

if [ -n "$PINS_FILE" ]; then
    tmp="${PINS_FILE}.tmp.$$"
    {
        echo "# Written by init-push-images: the digest each image was pushed as."
        cat "$pins"
    } >"$tmp"
    chmod 0444 "$tmp"
    mv -f "$tmp" "$PINS_FILE"
    echo "pinned $(grep -c . "$pins") images in ${PINS_FILE}"
fi
