#!/bin/sh
# Optional egress through a VPN gateway: when VIDHIVE_EGRESS_GATEWAY is set (the IP of a
# gateway container on this Docker network), the default route is moved there. Traffic to
# the Docker network itself (coordinator, other containers) keeps its direct, more specific
# route, so only internet traffic goes through the VPN. Empty = no change.
set -e
if [ -n "${VIDHIVE_EGRESS_GATEWAY:-}" ]; then
  GW="${VIDHIVE_EGRESS_GATEWAY}"
  # A service name (e.g. "vpn") is resolved to its IP; the gateway may start a bit later.
  case "$GW" in
    *[!0-9.]*) for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                 IP="$(getent hosts "$GW" | cut -d' ' -f1 | head -n1)"; [ -n "$IP" ] && break; sleep 2
               done; GW="${IP:-}" ;;
  esac
  if [ -n "$GW" ] && ip route replace default via "$GW"; then
    echo "egress: default route via $GW"
  else
    echo "egress: gateway ${VIDHIVE_EGRESS_GATEWAY} unavailable (or no NET_ADMIN); continuing without VPN" >&2
  fi
fi
exec "$@"
