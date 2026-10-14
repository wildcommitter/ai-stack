#!/usr/bin/env bash
#
# Launch the Kali sandbox container with controlled network egress.
#
# The container is given its OWN network namespace (rootless slirp4netns), and
# all outbound traffic is pinned to a single host interface/IP or forced through
# a VPN tunnel. This is the "tunnel traffic through a network interface" piece:
# nothing the tools do can leak out of the interface you choose.
#
# Modes (set NET_MODE):
#   iface   (default) - bind all container egress to a host interface or IP.
#                        Set TUNNEL_IFACE to an interface name (e.g. wg0, tun0,
#                        eth0) or a host IP address. slirp4netns routes the
#                        namespace's traffic out via that address only.
#   vpn              - start a WireGuard or OpenVPN tunnel INSIDE the container
#                        and route everything through it. Mount the config and
#                        set VPN_CONFIG to its path inside the container.
#   none             - no network at all (fully offline analysis).
#   host             - share the host network namespace (NO sandboxing; opt-in).
#
set -euo pipefail

IMAGE="${IMAGE:-kali-sandbox:latest}"
NAME="${NAME:-kali-sandbox}"
NET_MODE="${NET_MODE:-iface}"
TUNNEL_IFACE="${TUNNEL_IFACE:-}"
VPN_CONFIG="${VPN_CONFIG:-}"
WORKDIR_HOST="${WORKDIR_HOST:-$PWD/work}"

mkdir -p "$WORKDIR_HOST"

# CAP_NET_RAW/ADMIN let scanners craft raw packets and bring up tun/wg devices
# without running the whole container as a privileged root box.
COMMON_ARGS=(
  --name "$NAME"
  --hostname kali-sandbox
  --replace
  --detach
  --cap-add=NET_RAW,NET_ADMIN
  --security-opt label=disable
  # Rootless podman maps the host user to container root by default, which makes
  # the bind-mounted /work unwritable by the non-root `operator` user. keep-id
  # maps the host user onto operator (uid 1000, gid 37 in our image) so files in
  # /work are owned by operator inside and by you on the host.
  --userns=keep-id:uid=1000,gid=37
  -v "$WORKDIR_HOST:/work:Z"
)

resolve_outbound() {
  local target="$1"
  # If it's already an IP, use it directly; otherwise resolve interface -> IP.
  if [[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "$target"
    return
  fi
  local ip
  ip="$(ip -4 -o addr show dev "$target" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  if [[ -z "$ip" ]]; then
    echo "ERROR: could not find an IPv4 address on interface '$target'." >&2
    echo "       Available interfaces:" >&2
    ip -o -4 addr show | awk '{print "         " $2 " " $4}' >&2
    exit 1
  fi
  echo "$ip"
}

case "$NET_MODE" in
  iface)
    if [[ -z "$TUNNEL_IFACE" ]]; then
      # Auto-detect the host's default-route interface so a bare run just works.
      TUNNEL_IFACE="$(ip -4 route show default | awk '{print $5; exit}')"
      if [[ -z "$TUNNEL_IFACE" ]]; then
        echo "ERROR: NET_MODE=iface and no default route found; set TUNNEL_IFACE explicitly." >&2
        exit 1
      fi
      echo ">> TUNNEL_IFACE not set; auto-detected default-route interface: $TUNNEL_IFACE"
    fi
    # Podman 5+/6 use pasta (slirp4netns was removed). pasta pins egress to a
    # device with --outbound-if4/6 (forces the interface, SO_BINDTODEVICE-style)
    # or to a source address with -o. We branch on whether TUNNEL_IFACE is an IP.
    if [[ "$TUNNEL_IFACE" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo ">> Pinning all container egress to source address $TUNNEL_IFACE"
      NET_SPEC="pasta:-o,$TUNNEL_IFACE"
    else
      OUTBOUND="$(resolve_outbound "$TUNNEL_IFACE")"   # validates the iface exists
      echo ">> Pinning all container egress to interface $TUNNEL_IFACE ($OUTBOUND)"
      NET_SPEC="pasta:--outbound-if4,$TUNNEL_IFACE"
      # Add IPv6 binding only if the interface actually has a global v6 address.
      if ip -6 -o addr show dev "$TUNNEL_IFACE" scope global 2>/dev/null | grep -q .; then
        NET_SPEC="$NET_SPEC,--outbound-if6,$TUNNEL_IFACE"
      fi
    fi
    podman run "${COMMON_ARGS[@]}" --network "$NET_SPEC" "$IMAGE"
    ;;

  vpn)
    if [[ -z "$VPN_CONFIG" ]]; then
      echo "ERROR: NET_MODE=vpn requires VPN_CONFIG (path to .conf inside container)." >&2
      echo "       Mount it, e.g.: VPN_CONFIG=/work/vpn/wg0.conf" >&2
      exit 1
    fi
    echo ">> Starting container; VPN tunnel will be brought up inside it."
    podman run "${COMMON_ARGS[@]}" \
      --device /dev/net/tun \
      --network pasta \
      "$IMAGE"
    echo ">> Bringing up tunnel from $VPN_CONFIG ..."
    if [[ "$VPN_CONFIG" == *.conf && "$(basename "$VPN_CONFIG")" == wg* ]]; then
      podman exec "$NAME" wg-quick up "$VPN_CONFIG"
    else
      podman exec -d "$NAME" openvpn --config "$VPN_CONFIG"
    fi
    echo ">> Verify the tunnel is the only route:"
    echo "   podman exec $NAME curl -s https://ifconfig.me"
    ;;

  none)
    echo ">> No network (offline mode)."
    podman run "${COMMON_ARGS[@]}" --network none "$IMAGE"
    ;;

  host)
    echo ">> WARNING: host networking - NO network sandboxing."
    podman run "${COMMON_ARGS[@]}" --network host "$IMAGE"
    ;;

  *)
    echo "ERROR: unknown NET_MODE '$NET_MODE' (use iface|vpn|none|host)." >&2
    exit 1
    ;;
esac

echo ">> Container '$NAME' is running."
echo ">> Egress check: podman exec $NAME curl -s https://ifconfig.me; echo"
