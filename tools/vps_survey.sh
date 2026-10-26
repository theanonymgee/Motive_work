#!/usr/bin/env bash
# workmcp VPS survey (design M5 remote 9.1, approval step A). READ-ONLY: it changes nothing on the host.
#
# Run it on the VPS and keep the output (the output is the evidence for the branch table in the
# deployment guide):
#   ssh <vps> 'bash -s' < tools/vps_survey.sh > survey-before.txt
#
# Rules this script keeps:
#   - only commands that read state; no install, no write, no restart, no reload, no file output
#   - no network access of its own (no curl, no dig, no package index update)
#   - sudo is used only as `sudo -n` (never asks for a password) and only for read commands
#   - every command reads no stdin, and its output is passed through scrub() so a secret-looking
#     assignment is replaced by <redacted>; web server configs are filtered to a few directives
#   - process list shows names only (no command lines, which can carry secrets)
#
# Missing tools are reported as "(not available)" and are not errors.

export LC_ALL=C
export PAGER=cat SYSTEMD_PAGER=

scrub() {
    sed -E \
        -e 's/((pass(word)?|passwd|secret|token|api[_-]?key|apikey|bearer|authorization|credential|private[_-]?key)[A-Za-z0-9_]*[[:space:]]*[=:][[:space:]]*)((Bearer|Basic)[[:space:]]+)?[^[:space:]]+/\1<redacted>/Ig' \
        -e 's/(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+\/=-]{8,}/\1 <redacted>/g'
}

section() { printf '\n== %s ==\n' "$1"; }

# run a command, show its output (scrubbed), never fail the script
try() {
    "$@" </dev/null 2>&1 | scrub
    local rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] || echo "(exit $rc: $1)"
}

has() { command -v "$1" >/dev/null 2>&1; }

# `sudo -n` only when sudo works without a password; otherwise run plain (may show less)
if has sudo && sudo -n true </dev/null >/dev/null 2>&1; then SUDO="sudo -n"; SUDO_OK=yes; else SUDO=""; SUDO_OK=no; fi

echo "workmcp vps_survey (read-only) $(date -u +%Y-%m-%dT%H:%M:%SZ)"

section "host"
try hostnamectl
try uname -srm
try uptime
echo "nproc: $(nproc 2>/dev/null)"
try timedatectl show -p Timezone -p NTPSynchronized

section "memory (MiB) and swap"
try free -m
try swapon --show
echo "-- top 15 processes by RSS (name only, no command line)"
ps -eo pid,user,rss,comm --sort=-rss </dev/null 2>/dev/null | head -16
echo "-- cgroup memory per unit (one sample)"
has systemd-cgtop && systemd-cgtop -m -n 1 -b --depth=1 </dev/null 2>&1 | head -25 | scrub || echo "(not available)"

section "disk"
try df -h / /home /tmp
try lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
echo "-- inodes"
try df -i /

section "login and privileges of the account that runs this"
try id
echo "sudo without password: $SUDO_OK"
echo "user workmcp: $(getent passwd workmcp </dev/null || echo none)"
echo "home /home/workmcp exists: $([ -d /home/workmcp ] && echo yes || echo no)"

section "listening ports (tcp/udp; owner process when visible)"
echo "-- ss -tulpn ($([ "$SUDO_OK" = yes ] && echo 'with sudo' || echo 'without sudo: other users processes are hidden'))"
$SUDO ss -tulpn </dev/null 2>&1 | scrub
echo "-- port 80 / 443 / 8765 owners"
for p in 80 443 8765; do
    echo "port $p:"
    $SUDO ss -tlnp "( sport = :$p )" </dev/null 2>&1 | tail -n +2 | scrub
done
echo "-- sorted listen snapshot (diff-friendly: address:port only)"
ss -tuln </dev/null 2>/dev/null | awk 'NR>1 {print $1, $5}' | sort -u

section "running services and timers"
try systemctl list-units --type=service --state=running --no-pager --plain
try systemctl list-timers --no-pager --plain
echo "-- failed units"
try systemctl --failed --no-pager --plain
echo "-- systemd version (IPAddressDeny needs cgroup BPF)"
try systemctl --version

section "web servers and proxies (binaries)"
for b in nginx caddy apache2 httpd haproxy traefik docker podman certbot ufw nft iptables fail2ban sqlite3 python3 python3.12 uv; do
    printf '%-10s %s\n' "$b" "$(command -v "$b" 2>/dev/null || echo '-')"
done
echo "-- running state of common web/proxy/container units"
for u in nginx caddy apache2 httpd haproxy traefik docker podman fail2ban ufw; do
    printf '%-10s %s\n' "$u" "$(systemctl is-active "$u" </dev/null 2>/dev/null || true)"
done

section "nginx (only server_name / listen / location / ssl_certificate / include / proxy_pass lines)"
if has nginx; then
    $SUDO nginx -T </dev/null 2>/dev/null \
        | grep -E '^[[:space:]]*(server_name|listen|location|ssl_certificate |include|proxy_pass|# configuration file)' \
        | head -150 | scrub
    echo "-- nginx -v"; nginx -v </dev/null 2>&1
else
    echo "(not available)"
fi

section "caddy (site addresses and handlers only; credential-looking lines dropped)"
if has caddy || [ -d /etc/caddy ]; then
    has caddy && caddy version </dev/null 2>&1 | head -2
    if [ -r /etc/caddy/Caddyfile ] || [ -n "$SUDO" ]; then
        $SUDO sed -n '1,120p' /etc/caddy/Caddyfile </dev/null 2>&1 \
            | grep -viE 'basic_?auth|\$2[aby]\$|\$argon2|\$apr1|api_?key|token|secret|password' | scrub
    else
        echo "(Caddyfile not readable without sudo)"
    fi
else
    echo "(not available)"
fi

section "apache"
if has apache2ctl; then $SUDO apache2ctl -S </dev/null 2>&1 | head -40 | scrub; else echo "(not available)"; fi

section "containers (name, image, published ports; no environment)"
if has docker; then
    out=$(docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' </dev/null 2>&1) ||
        out=$($SUDO docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' </dev/null 2>&1)
    printf '%s\n' "$out" | head -40
elif has podman; then
    podman ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' </dev/null 2>&1 | head -40
else
    echo "(not available)"
fi

section "certificates (names and expiry only)"
if [ -n "$SUDO" ] || [ -r /etc/letsencrypt/live ]; then
    $SUDO ls /etc/letsencrypt/live </dev/null 2>&1
    has certbot && $SUDO certbot certificates </dev/null 2>&1 | grep -E 'Certificate Name|Domains|Expiry Date' | scrub
else
    echo "(/etc/letsencrypt not readable without sudo)"
fi

section "firewall (rules only)"
if has ufw; then $SUDO ufw status verbose </dev/null 2>&1 | scrub; else echo "ufw: (not available)"; fi
if has nft; then $SUDO nft list ruleset </dev/null 2>&1 | head -80 | scrub; else echo "nft: (not available)"; fi
echo "-- note: the Hostinger panel firewall (not visible from inside the VPS) is checked separately"

section "python and sqlite (this runs python in memory only; no file is written)"
try python3 --version
for py in python3.12 python3; do
    if has "$py"; then
        echo "-- $py"
        "$py" - </dev/null <<'PY' 2>&1
import sqlite3, sys
print(sys.version.split()[0], "sqlite", sqlite3.sqlite_version)
db = sqlite3.connect(":memory:")
try:
    db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    print("fts5 trigram: yes")
except Exception as exc:
    print("fts5 trigram: NO", exc)
try:
    import venv  # noqa: F401
    print("venv module: yes")
except Exception:
    print("venv module: NO")
PY
    fi
done
echo "-- python3.12-venv package policy (apt cache only; no update)"
if has apt-cache; then apt-cache policy python3.12 python3.12-venv </dev/null 2>&1 | head -12; else echo "(not available)"; fi

section "scheduled jobs (names only)"
ls /etc/cron.d </dev/null 2>&1 | head -30
echo "-- current user crontab"
crontab -l </dev/null 2>&1 | head -20 | scrub

echo
echo "== end of survey (nothing was changed) =="
