"""What you change when you take Tesseract somewhere new: where it is, and its Wi-Fi.

  location  say "Franklin, I'm in Chicago", or run `tesseract location "Chicago, Illinois"`,
            or type it on the setup page. Updates [assistant] location in config.toml
            and switches the clock to that place's time zone.
  Wi-Fi     `tesseract wifi add "Home" "password"` saves a network before you leave,
            even out of range, and it joins by itself when it gets there. Or use the setup
            page: when no known Wi-Fi is around, it opens a hotspot (portal.py).

Wi-Fi goes through NetworkManager (nmcli), so passwords live in its root-only
store, never in config.toml (which ends up in git).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "tesseract-"          # NetworkManager profiles we create; ones from Raspberry Pi Imager are left alone
HOTSPOT = "tesseract-setup"


# ── config.toml ─────────────────────────────────────────────────────────────

def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)   # a JSON string is a valid TOML basic string


def set_config_value(path: Path, section: str, key: str, value) -> None:
    """Change one `key = value` in config.toml, keeping every comment and the layout."""
    lines = path.read_text().splitlines(keepends=True)
    head = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{section}]"), None)
    if head is None:
        lines += [f"\n[{section}]\n"]
        head = len(lines) - 1
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*)("(?:[^"\\]|\\.)*"|[^#\n]*?)(\s*(?:#.*)?)$')
    new = toml_value(value)
    for i in range(head + 1, end):
        m = pat.match(lines[i].rstrip("\n"))
        if m:
            lines[i] = m.group(1) + new + m.group(3) + "\n"
            break
    else:
        lines.insert(head + 1, f"{key} = {new}\n")
    text = "".join(lines)
    tomllib.loads(text)                               # never leave a config that doesn't parse
    st = path.stat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, st.st_mode)
    if os.geteuid() == 0:                             # run as root (`tesseract location`): keep it the user's file
        os.chown(tmp, st.st_uid, st.st_gid)
    tmp.replace(path)


# ── location and time zone ──────────────────────────────────────────────────

def current_timezone() -> str | None:
    try:
        return subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def set_timezone(tz: str) -> bool:
    try:
        subprocess.run(["timedatectl", "set-timezone", tz], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("settings: couldn't set the time zone to %s: %s", tz, e)
        return False
    time.tzset()                                      # this process's clock follows right away
    return True


def _ensure_local(cfg) -> None:
    if not cfg.local.exists():
        cfg.local.parent.mkdir(parents=True, exist_ok=True)
        cfg.local.write_text("# Settings changed on this device (by voice, the setup page or `tesseract ...`).\n"
                             "# They override config.toml, and copying a new config.toml over leaves them alone.\n")


def set_local_value(cfg, section: str, key: str, value) -> None:
    """Save one setting to data/local.toml (on top of config.toml)."""
    _ensure_local(cfg)
    set_config_value(cfg.local, section, key, value)


def set_local_agent(cfg, name: str, agent: dict | None) -> None:
    """Add or replace (or with None, remove) an [[agents]] block called `name` in data/local.toml."""
    _ensure_local(cfg)
    text = cfg.local.read_text()
    parts = re.split(r"(?m)^(?=\[)", text)              # chunks, each starting at a [section] line
    keep = [c for c in parts if not (c.startswith("[[agents]]") and
                                     tomllib.loads(c.replace("[[agents]]", "", 1)).get("name") == name)]
    text = "".join(keep).rstrip("\n") + "\n"
    if agent is not None:
        text += "\n[[agents]]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in agent.items())
    tomllib.loads(text)
    cfg.local.write_text(text)


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in .env (secrets live there, never in config.toml), readable only by its owner."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not re.match(rf"\s*{re.escape(key)}\s*=", ln)] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    os.environ[key] = value


def set_location(cfg, place: str, weather) -> str:
    """Look the place up, save it, fix the clock's time zone. Returns what to say."""
    hit = weather.lookup(place)
    label = hit["label"]
    set_local_value(cfg, "assistant", "location", label)
    cfg.assistant.location = label
    msg = f"Okay, I'll use {label} for the weather."
    tz = hit.get("timezone")
    if tz and tz != current_timezone() and set_timezone(tz):
        msg += f" The clock is on {tz.rsplit('/', 1)[-1].replace('_', ' ')} time now."
    log.info("settings: location -> %s (%s)", label, tz)
    return msg


# ── Wi-Fi (NetworkManager) ──────────────────────────────────────────────────

def _fields(line: str) -> list[str]:
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    return out + [cur]


@dataclass
class Network:
    ssid: str
    signal: int = 0
    secure: bool = True


# ── when the Wi-Fi says "connected" but nothing gets through ────────────────
# The Zero 2 W's Wi-Fi chip (brcmfmac) can stall while staying associated, and Tailscale's DNS
# (100.100.100.100) can wedge; either leaves Tesseract "Offline" until a reboot. The app calls these
# after a couple of minutes offline: log what's wrong, then try the next fix.

def _sh(*cmd: str, timeout: float = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"({type(e).__name__}: {e})"


def _reachable(host: str, port: int = 443, timeout: float = 4) -> bool:
    import socket
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def diagnose_network(ifname: str = "wlan0") -> dict:
    import socket
    route = _sh("ip", "route", "show", "default")
    gateway = route.split()[2] if route.startswith("default via") else ""
    try:
        socket.getaddrinfo("openrouter.ai", 443)
        dns_ok = True
    except OSError:
        dns_ok = False
    try:
        resolv = " ".join(ln.split()[1] for ln in Path("/etc/resolv.conf").read_text().splitlines()
                          if ln.startswith("nameserver"))
    except OSError:
        resolv = "?"
    link = _sh("iw", "dev", ifname, "link").splitlines()
    return {
        "nm": _sh("nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device").replace("\n", " "),
        "link": " / ".join(ln.strip() for ln in link if ln.strip().startswith(("Connected", "Not connected",
                                                                                "signal", "SSID")))[:160],
        "gateway": gateway,
        "gateway_ping": bool(gateway) and "1 received" in _sh("ping", "-c", "1", "-W", "2", gateway),
        "ip_ok": _reachable("1.1.1.1"),
        "dns_ok": dns_ok,
        "nameservers": resolv,
    }


def network_fix(step: int, ip_ok: bool, dns_ok: bool, has_tailscale: bool, ifname: str = "wlan0") -> list[list[str]]:
    """The commands for fix number `step` (0, 1, 2, ...), given what diagnose_network() found."""
    if ip_ok and not dns_ok:              # the internet works, names don't: DNS is stuck
        if has_tailscale and step % 2 == 0:
            return [["systemctl", "restart", "tailscaled"]]
        return [["systemctl", "restart", "NetworkManager"]]
    if step == 0:                         # re-join the network
        return [["nmcli", "device", "reconnect", ifname]]
    return [["nmcli", "radio", "wifi", "off"], ["sleep", "3"], ["nmcli", "radio", "wifi", "on"]]   # reset the chip


def heal_network(step: int, ifname: str = "wlan0") -> None:
    d = diagnose_network(ifname)
    log.warning("network: offline. %s", " ".join(f"{k}={v}" for k, v in d.items()))
    for cmd in network_fix(step, d["ip_ok"], d["dns_ok"], shutil.which("tailscale") is not None, ifname):
        log.warning("network: trying %s", " ".join(cmd))
        out = _sh(*cmd, timeout=60)
        if out:
            log.info("network: %s", out[:200])


class Wifi:
    def __init__(self, ifname: str = "wlan0", run=None):
        self.ifname = ifname
        self._run = run or self._nmcli

    @staticmethod
    def available() -> bool:
        return shutil.which("nmcli") is not None

    @staticmethod
    def _nmcli(*args: str, timeout: float = 45) -> str:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"nmcli exited with {r.returncode}")
        return r.stdout

    def status(self) -> tuple[bool, str]:
        """(connected?, the active connection's name)"""
        for line in self._run("-t", "-f", "DEVICE,STATE,CONNECTION", "device").splitlines():
            f = _fields(line)
            if f[0] == self.ifname:
                conn = f[2] if len(f) > 2 else ""
                # our own setup hotspot also shows as "connected", but it's not being on Wi-Fi
                return f[1].startswith("connected") and conn != HOTSPOT, conn
        return False, ""

    def saved(self) -> list[tuple[str, str]]:
        """[(profile name, SSID)] of every saved Wi-Fi network."""
        out = []
        for line in self._run("-t", "-f", "NAME,TYPE", "connection", "show").splitlines():
            name, kind = (_fields(line) + [""])[:2]
            if kind == "802-11-wireless" and name != HOTSPOT:
                ssid = self._run("-t", "-g", "802-11-wireless.ssid", "connection", "show", "id", name).strip()
                out.append((name, ssid or name))
        return out

    def scan(self) -> list[Network]:
        best: dict[str, Network] = {}
        for line in self._run("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes").splitlines():
            ssid, signal, sec = (_fields(line) + ["", "", ""])[:3]
            if ssid and (ssid not in best or int(signal or 0) > best[ssid].signal):
                best[ssid] = Network(ssid, int(signal or 0), bool(sec and sec != "--"))
        return sorted(best.values(), key=lambda n: -n.signal)

    def find(self, ssid: str) -> str | None:
        """The exact name of the network in range that `ssid` was meant to be, or None if it isn't
        in range. Phones name their hotspot "Charlie’s iPhone" with a curly apostrophe, which nobody types."""
        key = lambda s: s.replace("\u2019", "'").replace("\u2018", "'").casefold().strip()
        try:
            found = [n.ssid for n in self.scan() if key(n.ssid) == key(ssid)]
        except (RuntimeError, subprocess.SubprocessError):
            return None
        return ssid if ssid in found else (found[0] if found else None)

    def match(self, ssid: str) -> str:
        return self.find(ssid) or ssid

    def wait_for(self, ssid: str, tries: int = 5, sleep=time.sleep) -> str | None:
        """Right after the setup hotspot closes, the radio hasn't scanned anything yet, and connecting
        then fails with "network not found" whatever the password. Scan until `ssid` shows up."""
        for i in range(tries):
            seen = self.find(ssid)
            if seen:
                return seen
            if i < tries - 1:
                sleep(2)
        return None

    def mac(self) -> str:
        """The Wi-Fi chip's own MAC address (what campus IoT networks ask you to register)."""
        try:
            r = subprocess.run(["ethtool", "-P", self.ifname], capture_output=True, text=True, timeout=5)
            if m := re.search(r"([0-9a-f]{2}(:[0-9a-f]{2}){5})", r.stdout):
                return m.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            return Path(f"/sys/class/net/{self.ifname}/address").read_text().strip()
        except OSError:
            return ""

    def add(self, ssid: str, password: str = "", username: str = "", hidden: bool = False) -> str:
        """Save a network (it needn't be in range). username = school/work Wi-Fi like eduroam."""
        name = PREFIX + ssid
        for prof, s in self.saved():
            if prof == name or (s == ssid and prof.startswith(PREFIX)):
                self._run("connection", "delete", "id", prof)
        args = ["connection", "add", "type", "wifi", "ifname", self.ifname, "con-name", name, "ssid", ssid,
                "connection.autoconnect", "yes",
                # always the chip's real address: networks that admit registered devices (TAMU_IoT) check it
                "802-11-wireless.cloned-mac-address", "permanent"]
        if username:
            args += ["wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
                     "802-1x.identity", username, "802-1x.password", password]
        elif password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        if hidden:                       # not seen in a scan: ask for it by name instead
            args += ["802-11-wireless.hidden", "yes"]
        self._run(*args)
        return name

    def connect(self, name: str) -> bool:
        """Bring a saved network up. On failure, last_error has NetworkManager's reason."""
        try:
            self._run("--wait", "40", "connection", "up", "id", name, "ifname", self.ifname, timeout=50)
            self.last_error = ""
            return True
        except (RuntimeError, subprocess.SubprocessError) as e:
            self.last_error = str(e)
            log.warning("wifi: couldn't join %s: %s", name, e)
            return False

    last_error = ""

    def forget(self, ssid: str) -> int:
        gone = 0
        for prof, s in self.saved():
            if ssid in (s, prof):
                self._run("connection", "delete", "id", prof)
                gone += 1
        return gone

    def hotspot_on(self, ssid: str, password: str) -> None:
        """The setup hotspot, spelled out rather than `nmcli device wifi hotspot`: that one turns on
        protected management frames, which the Pi's Wi-Fi chip (brcmfmac) mishandles as an access
        point, so iPhones fail to join ("Unable to join the network") while Macs get in."""
        self.hotspot_off()
        self._run("connection", "add", "type", "wifi", "ifname", self.ifname, "con-name", HOTSPOT, "ssid", ssid,
                  "connection.autoconnect", "no", "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                  "802-11-wireless.channel", "6", "ipv4.method", "shared", "ipv6.method", "disabled",
                  "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password, "wifi-sec.proto", "rsn",
                  "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable")
        self._run("--wait", "20", "connection", "up", "id", HOTSPOT, "ifname", self.ifname, timeout=30)

    def hotspot_off(self) -> None:
        for args in (("connection", "down", "id", HOTSPOT), ("connection", "delete", "id", HOTSPOT)):
            try:
                self._run(*args)
            except (RuntimeError, subprocess.SubprocessError):
                pass
