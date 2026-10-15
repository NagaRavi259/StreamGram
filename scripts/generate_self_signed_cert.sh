#!/usr/bin/env bash
# Generates a self-signed TLS certificate for serving this project directly over HTTPS
# from uvicorn, without a reverse proxy or a real domain name in front of it.
#
# Browsers require an IP-based cert to carry the IP in subjectAltName (a bare CN= is not
# enough anymore), so this always sets SAN explicitly. The result is still self-signed:
# browsers will show a "not trusted" warning once, which visitors have to click through --
# that's unavoidable without owning a domain and getting a CA (e.g. Let's Encrypt) to sign it.
#
# Usage: scripts/generate_self_signed_cert.sh <ip-or-hostname> [output-dir]
set -euo pipefail

HOST="${1:?Usage: $0 <ip-or-hostname> [output-dir]}"
OUT_DIR="${2:-certs}"
DAYS="${DAYS:-3650}"

mkdir -p "$OUT_DIR"

if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="IP:$HOST"
else
    SAN="DNS:$HOST"
fi

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$OUT_DIR/key.pem" \
    -out "$OUT_DIR/cert.pem" \
    -days "$DAYS" \
    -subj "/CN=$HOST" \
    -addext "subjectAltName=$SAN"

chmod 600 "$OUT_DIR/key.pem"

echo
echo "Wrote $OUT_DIR/cert.pem and $OUT_DIR/key.pem (valid $DAYS days, SAN=$SAN)."
echo "Set in config.env:"
echo "  HAS_SSL=True"
echo "  SSL_CERTFILE=$OUT_DIR/cert.pem"
echo "  SSL_KEYFILE=$OUT_DIR/key.pem"
echo "  PORT=443   # or whatever port you forward HTTPS to; needs root/CAP_NET_BIND_SERVICE below 1024"
echo
echo "Browsers will warn that this certificate isn't trusted (it's self-signed, there's no CA"
echo "behind it) -- that's expected for a bare IP with no domain. Visitors click through once."
