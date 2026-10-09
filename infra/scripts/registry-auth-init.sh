#!/bin/sh
# Generate the in-stack registry's login, once (#523).
#
# The registry refuses anonymous clients. This runs before it, in the
# registry-auth-init compose service, and writes three files into the
# registry-auth volume:
#
#   username  the account the pushers and the orchestrator log in as
#   password  random, made on the first run and kept on every later one
#   htpasswd  the bcrypt of the two, which the registry reads at startup
#
# The service runs an httpd image for its `htpasswd`: the registry accepts
# only bcrypt, and nothing else in the stack can make one.
#
# Idempotent. A later run keeps the password and rewrites htpasswd from it, so
# the two cannot drift. To rotate: delete `password` from the volume, then
# recreate registry-auth-init, registry, init-push-images and orchestrator.
#
# The files are world-readable on purpose. The orchestrator reads `password`
# as a non-root user, and the boundary is the volume: only registry-auth-init,
# registry, init-push-images and orchestrator mount it.
set -eu

AUTH_DIR="${AUTH_DIR:-/auth}"
REGISTRY_USERNAME="${REGISTRY_USERNAME:-embry0}"

case "$REGISTRY_USERNAME" in
    *[!A-Za-z0-9._-]* | "")
        echo "ERROR: REGISTRY_USERNAME '${REGISTRY_USERNAME}' must be letters, digits, '.', '_' or '-'" >&2
        exit 1
        ;;
esac

mkdir -p "$AUTH_DIR"
cd "$AUTH_DIR"

if [ ! -s password ]; then
    # 40 characters from [A-Za-z0-9]: about 238 bits. A password this long makes
    # the bcrypt cost factor irrelevant to guessing, so htpasswd keeps its
    # default cost; the registry pays that cost on every request it serves.
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 >password.tmp
    if [ "$(wc -c <password.tmp)" -ne 40 ]; then
        echo "ERROR: could not generate a registry password" >&2
        rm -f password.tmp
        exit 1
    fi
    mv password.tmp password
    echo "generated a new registry password"
else
    echo "kept the existing registry password"
fi

printf '%s' "$REGISTRY_USERNAME" >username.tmp
mv username.tmp username

# -i reads the password from stdin, so it never appears in a process list.
htpasswd -niB "$REGISTRY_USERNAME" <password | sed '/^$/d' >htpasswd.tmp
case "$(cat htpasswd.tmp)" in
    "${REGISTRY_USERNAME}:\$2"[aby]"\$"*) ;;
    *)
        echo "ERROR: htpasswd did not produce a bcrypt entry" >&2
        rm -f htpasswd.tmp
        exit 1
        ;;
esac
mv htpasswd.tmp htpasswd

chmod 0444 username password htpasswd
echo "registry login ready for user '${REGISTRY_USERNAME}'"
