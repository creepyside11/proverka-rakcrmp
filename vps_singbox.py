"""Install a verified sing-box bridge runtime without restarting the bot."""

BRIDGE_SCRIPT = r'''set -eu
umask 077
[ "$(id -u)" -eq 0 ] || exit 21
root=/opt/rakcrmp-black
runtime=/opt/rakcrmp-black-runtime
[ -d /etc/rakcrmp-black ] && [ -f "$root/logic/vpn_bridge.py" ] && [ -x "$root/.venv/bin/python" ] && [ -d "$runtime" ] || exit 22
grep -Fxq 'SING_BOX_BIN=/opt/rakcrmp-black-runtime/sing-box' /etc/rakcrmp-black/bot.env || exit 43
exec 9>/etc/rakcrmp-black/manage.lock
flock -n 9 || exit 24
case "$(uname -m)" in
  x86_64|amd64) arch=amd64; sha=a1ec76e2b6b139eb747a1b1ebee7d14b8d4be5a833596cad8070a31ef960301f ;;
  aarch64|arm64) arch=arm64; sha=719b76196c8b31efa636b2d8f669e314547e0da0a5ab38a75e1882d307bbd154 ;;
  armv7l|armv7) arch=armv7; sha=9eedd8eb2d3ea66a48794359d1cc1e32ce618b6e1b105c9cd314da6408c9fc87 ;;
  *) exit 44 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
archive="sing-box-1.12.25-linux-${arch}.tar.gz"
curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --connect-timeout 15 --max-time 180 -o "$tmp/$archive" "https://github.com/SagerNet/sing-box/releases/download/v1.12.25/$archive" >/dev/null 2>&1 || exit 45
printf '%s  %s\n' "$sha" "$tmp/$archive" | sha256sum -c --status || exit 45
tar -xOzf "$tmp/$archive" "sing-box-1.12.25-linux-${arch}/sing-box" > "$tmp/sing-box" || exit 45
chmod 0755 "$tmp/sing-box"
chown rakcrmp-black:rakcrmp-black "$tmp" "$tmp/sing-box"
# Verify the real program's generated config and a local SOCKS handshake,
# under its own user, without connecting to any third-party VPN server.
cd "$root"
runuser -u rakcrmp-black -- "$root/.venv/bin/python" - "$tmp/sing-box" "$tmp/config.json" >/dev/null 2>&1 <<'CHECK' || exit 46
import json, socket, subprocess, sys, time
from logic.vpn_bridge import build_config, parse_link
binary, filename = sys.argv[1:]
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
port = sock.getsockname()[1]
sock.close()
node = parse_link('vless://00000000-0000-4000-8000-000000000000@192.0.2.1:443?security=none')
if not node: raise SystemExit(1)
with open(filename, 'w', encoding='utf-8') as f:
    json.dump(build_config('local-test', node, port), f)
if subprocess.run([binary, 'check', '-c', filename], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode:
    raise SystemExit(1)
p = subprocess.Popen([binary, 'run', '-c', filename], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if p.poll() is not None: raise SystemExit(1)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5) as s:
                s.sendall(b'\x05\x01\x00')
                if s.recv(2) != b'\x05\x00': raise SystemExit(1)
                break
        except OSError:
            time.sleep(.2)
    else: raise SystemExit(1)
finally:
    if p.poll() is None:
        p.terminate()
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=3)
CHECK
had_old=0
[ ! -x "$runtime/sing-box" ] || had_old=1
# Install next to the old binary then atomically rename; running bridges keep
# their existing executable and are never terminated here.
install -m 0755 "$tmp/sing-box" "$runtime/.sing-box-new-$$" || exit 47
mv -f -- "$runtime/.sing-box-new-$$" "$runtime/sing-box" || exit 47
if ! systemctl is-active --quiet rakcrmp-black.service; then exit 12; fi
if [ "$had_old" = 0 ]; then exit 13; fi
# Ask the running bot to refresh its saved subscription nodes. The existing
# pool owns bridge lifetimes and protects nodes in active use.
runuser -u rakcrmp-black -- "$root/.venv/bin/python" - >/dev/null 2>&1 <<'REFRESH' || exit 14
from logic.web_bridge import submit
reply = submit('vpn_refresh', {}, timeout=12)
if not reply.get('ok'): raise SystemExit(1)
REFRESH
'''
