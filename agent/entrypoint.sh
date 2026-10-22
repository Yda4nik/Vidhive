#!/bin/sh
# Optional egress through a VPN gateway: when VIDHIVE_EGRESS_GATEWAY is set (the IP of a
# gateway container on this Docker network), the default route is moved there. Traffic to
# the Docker network itself (coordinator, other containers) keeps its direct, more specific
# route, so only internet traffic goes through the VPN. Empty = no change.
set -e
if [ -n "${VIDHIVE_EGRESS_GATEWAY:-}" ]; then
  ip route replace default via "${VIDHIVE_EGRESS_GATEWAY}"     && echo "egress: default route via ${VIDHIVE_EGRESS_GATEWAY}"     || echo "egress: could not set the route (needs NET_ADMIN); continuing without VPN" >&2
fi
exec "$@"
