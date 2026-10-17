#!/bin/bash
# Renew the Tailscale TLS cert and restart the organizer only if it changed.
# `tailscale cert` is a no-op until the cert is near expiry, so this is safe
# to run daily. Let's Encrypt certs last 90 days.
set -u
DIR="$HOME/.config/ableton-organizer"
HOSTNAME_TS="${1:?usage: renew-cert.sh <machine>.<tailnet>.ts.net}"
LABEL="com.ekkolabs.ableton-organizer"
TS=/usr/local/bin/tailscale

before=$(shasum -a 256 "$DIR/ts-cert.pem" 2>/dev/null | cut -d' ' -f1)
"$TS" cert --cert-file "$DIR/ts-cert.pem" --key-file "$DIR/ts-key.pem" "$HOSTNAME_TS" >/dev/null 2>&1 || {
  echo "$(date '+%F %T') cert renewal failed" >&2; exit 1; }
after=$(shasum -a 256 "$DIR/ts-cert.pem" 2>/dev/null | cut -d' ' -f1)

if [ "$before" != "$after" ]; then
  echo "$(date '+%F %T') cert renewed, restarting $LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
fi
