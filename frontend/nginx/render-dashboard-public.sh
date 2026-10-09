#!/bin/sh
# Render a template for the public dashboard hostname, DASHBOARD_PUBLIC_HOSTNAME.
#
# One script holds the hostname rules for every consumer of the variable:
#
#   frontend image   installed as /docker-entrypoint.d/40-dashboard-public-vhost.sh;
#                    the stock nginx entrypoint runs it with no arguments before
#                    nginx starts, rendering the public vhost.
#   dashboard tunnel the cloudflared-dashboard-config compose service runs it
#                    with --require to render the tunnel's ingress.
#   provision.sh     runs it with --check before touching Cloudflare.
#
# Usage:
#   render-dashboard-public.sh [TEMPLATE OUTPUT]          unset: renders nothing, exit 0
#   render-dashboard-public.sh --require TEMPLATE OUTPUT  unset: refused, exit 1
#   render-dashboard-public.sh --check                    validate only; unset: refused
#
# Without --require, unset or empty means "no public vhost": every Host is
# then served by the LAN vhost in default.conf. With --require, an unset
# value is refused: a tunnel with no hostname rule would have to route
# everything somewhere, and the only vhost left is the permissive LAN one.
#
# A set value must be one DNS name. Anything else is refused with exit 1: a
# URL, a port, a wildcard, "_", several names. A public origin that falls
# through to the LAN vhost loses its hardening silently, which is the failure
# this script exists to prevent. A refusal stops whatever runs the script:
# the nginx container, or the tunnel, which waits for its render.
#
# OUTPUT is removed before anything else, so it exists only after a
# successful render. A container restarted later can never pick up a file
# left over from an earlier value.
#
# Not the stock /etc/nginx/templates mechanism: that renders every template
# unconditionally, and an empty value would become `server_name ;`, which
# fails nginx's config test and takes the LAN vhost down with it.
set -eu

me=$(basename "$0")
mode=optional
case "${1:-}" in
    --require) mode=require; shift ;;
    --check) mode=check; shift ;;
esac
template="${1:-/etc/nginx/dashboard-public/dashboard-public.conf.template}"
output="${2:-/etc/nginx/conf.d/dashboard-public.conf}"
hostname="${DASHBOARD_PUBLIC_HOSTNAME:-}"

[ "$mode" = check ] || rm -f "$output"

if [ -z "$hostname" ]; then
    if [ "$mode" = optional ]; then
        echo "$me: DASHBOARD_PUBLIC_HOSTNAME is not set; no public dashboard vhost, every Host is served by the LAN vhost"
        exit 0
    fi
    echo "$me: DASHBOARD_PUBLIC_HOSTNAME is not set; refusing to continue (set it in infra/.env to the public dashboard's hostname)" >&2
    exit 1
fi

# One DNS name: letters, digits and hyphens in dot-separated labels of 1-63
# characters, at least two labels. The case rejects every other character,
# newlines included, before grep (which matches line by line) sees the value.
label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
case "$hostname" in
    *[!A-Za-z0-9.-]*) valid=no ;;
    *) valid=yes ;;
esac
if [ "$valid" = yes ] && [ "${#hostname}" -le 253 ] \
    && printf '%s\n' "$hostname" | grep -Eqx "$label(\\.$label)+"; then
    :
else
    echo "$me: DASHBOARD_PUBLIC_HOSTNAME='$hostname' is not a single hostname (no scheme, port, path, wildcard or list); refusing to start" >&2
    exit 1
fi

[ "$mode" = check ] && exit 0

# The value is validated above, so it holds no sed metacharacter.
tmp="$output.tmp.$$"
sed "s/[\$]{DASHBOARD_PUBLIC_HOSTNAME}/$hostname/g" "$template" > "$tmp"
mv "$tmp" "$output"
echo "$me: rendered $output for $hostname"
