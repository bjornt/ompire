#!/usr/bin/env bash
# qa-auth-tunnel.sh — forward the pi-auth-gateway so a QA VM's workshop
# containers can reach it. The gateway listens on THIS host's localhost:4000;
# the VM needs it on ITS localhost:4000 (workshop.yaml's tunnel plug).
#
# Run on the gateway side (this container):
#     ./qa-auth-tunnel.sh ubuntu@<qa-host>
#
# The loop re-establishes on disconnect; Ctrl-C to stop.
set -euo pipefail

TARGET=${1:?usage: qa-auth-tunnel.sh <user@host> [port]}
PORT=${2:-4000}
KEY=${QA_AGENT_KEY:-/project/.qa-agent/id_ed25519}

while true; do
	echo "qa-auth-tunnel: $TARGET:$PORT -> localhost:$PORT"
	ssh -N -T \
		-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
		-o ExitOnForwardFailure=yes \
		-o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
		-R "$PORT:localhost:$PORT" \
		"$TARGET" && true
	echo "qa-auth-tunnel: disconnected; retrying in 3s" >&2
	sleep 3
done
