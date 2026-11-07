"""Moving Tesseract: location (+ time zone), Wi-Fi via nmcli, and the phone setup page."""

import shutil
import threading
import time
import tomllib
import urllib.parse
import urllib.request

import pytest

from tesseract import config, settings
from tesseract.portal import SetupPortal
from tesseract.state import Status


def test_set_config_value_keeps_comments(tmp_path):
    p = tmp_path / "config.toml"
    shutil.copy(config.CODE_DIR / "config.toml", p)
    settings.set_config_value(p, "assistant", "location", 'Chicago, "Illinois"')
    settings.set_config_value(p, "leds", "brightness", 0.5)      # same key exists in [display]: leave that one
    settings.set_config_value(p, "assistant", "new_key", True)
    text = p.read_text()
    assert 'location = "Chicago, \\"Illinois\\""                 # e.g. "Austin, Texas"' in text
    data = tomllib.loads(text)
    assert data["assistant"]["location"] == 'Chicago, "Illinois"' and data["assistant"]["new_key"] is True
    assert data["leds"]["brightness"] == 0.5 and data["display"]["brightness"] == 10
    assert text.count("\n") >= (config.CODE_DIR / "config.toml").read_text().count("\n")   # nothing dropped


def test_set_location(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.source = tmp_path / "config.toml"
    cfg.local = tmp_path / "data" / "local.toml"          # never the real data/ folder
    shutil.copy(config.CODE_DIR / "config.toml", cfg.source)
    zones = []
    monkeypatch.setattr(settings, "current_timezone", lambda: "America/New_York")
    monkeypatch.setattr(settings, "set_timezone", lambda tz: zones.append(tz) or True)

    class Weather:
        def lookup(self, place):
            assert place == "chicago"
            return {"label": "Chicago, Illinois", "timezone": "America/Chicago", "lat": 41.9, "lon": -87.6}

    msg = settings.set_location(cfg, "chicago", Weather())
    assert msg == "Okay, I'll use Chicago, Illinois for the weather. The clock is on Chicago time now."
    assert zones == ["America/Chicago"] and cfg.assistant.location == "Chicago, Illinois"
    assert config.load(cfg.source).assistant.location == "Chicago, Illinois"     # saved
    # ...on the device, not in config.toml: copying a fresh config.toml over keeps it
    assert 'location = ""' in cfg.source.read_text()
    shutil.copy(config.CODE_DIR / "config.toml", cfg.source)
    assert config.load(cfg.source).assistant.location == "Chicago, Illinois"


class FakeNmcli:
    def __init__(self, connected=False):
        self.calls, self.connected, self.profiles = [], connected, {"preconfigured": "Campus Wi-Fi"}
        self.fail_connect = False

    def __call__(self, *args, timeout=45):
        self.calls.append(args)
        a = list(args)
        if a == ["-t", "-f", "DEVICE,STATE,CONNECTION", "device"]:
            state = "connected" if self.connected else "disconnected"
            return f"wlan0:{state}:{'Campus Wi-Fi' if self.connected else ''}\nlo:connected (externally):lo\n"
        if a[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
            return "".join(f"{n.replace(':', chr(92) + ':')}:802-11-wireless\n" for n in self.profiles) + \
                "lo:loopback\n"
        if "-g" in a:
            return self.profiles.get(a[-1], "") + "\n"
        if "list" in a:
            return "Home\\: 5G:70:WPA2\nHome\\: 5G:40:WPA2\nCafe:55:--\n:20:WPA2\n"
        if a[:2] == ["connection", "add"]:
            self.profiles[a[a.index("con-name") + 1]] = a[a.index("ssid") + 1]
        if a[:2] == ["connection", "delete"]:
            self.profiles.pop(a[-1], None)
        if "up" in a and self.fail_connect and "tesseract-setup" not in a:
            raise RuntimeError("Secrets were required, but not provided")
        return ""


def test_wifi_nmcli():
    nm = FakeNmcli(connected=True)
    w = settings.Wifi(run=nm)
    assert w.status() == (True, "Campus Wi-Fi")
    scan = w.scan()
    assert [(n.ssid, n.signal, n.secure) for n in scan] == [("Home: 5G", 70, True), ("Cafe", 55, False)]
    w.add("Home: 5G", "hunter22")
    add = nm.calls[-1]
    assert add[:2] == ("connection", "add") and add[-4:] == ("wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "hunter22")
    w.add("eduroam", "pw", username="me@school.edu")
    assert ("802-1x.identity", "me@school.edu") == nm.calls[-1][nm.calls[-1].index("802-1x.identity"):][:2]
    w.add("Cafe")
    assert "wifi-sec.key-mgmt" not in nm.calls[-1]                  # open network
    w.add("Home: 5G", "newpass")                                     # re-adding replaces ours, once
    assert sum(1 for s in nm.profiles.values() if s == "Home: 5G") == 1
    assert dict(w.saved())["preconfigured"] == "Campus Wi-Fi"        # Imager's profile is left alone
    assert w.forget("Cafe") == 1 and "tesseract-Cafe" not in nm.profiles


def _get(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as r:
        return r.read().decode()


def test_setup_portal_flow():
    cfg = config.load()
    nm = FakeNmcli()
    status, joined, said = Status(), [], []
    p = SetupPortal(cfg, settings.Wifi(run=nm), status, on_joined=lambda s, loc: joined.append((s, loc)),
                    say=said.append, port=0)
    p.sleep = lambda s: None                                          # skip the radio-settling pauses
    p.start()
    assert p.active and status.setup["ssid"] == "Tesseract-Setup" and len(status.setup["password"]) == 8
    add = next(c for c in nm.calls if c[:2] == ("connection", "add") and "tesseract-setup" in c)
    assert status.setup["password"] in add and add[add.index("wifi-sec.pmf") + 1] == "disable"   # iPhones can join
    assert nm.calls[-1][:6] == ("--wait", "20", "connection", "up", "id", "tesseract-setup")
    assert said and "Scan the code" in said[0]
    url = f"http://127.0.0.1:{p.port}"
    page = _get(url + "/hotspot-detect.html")                        # any URL: that's what pops it up on phones
    assert "Home: 5G" in page and "Cafe (open)" in page and 'name="location"' in page
    # wrong password: setup comes back with the error
    nm.fail_connect = True
    assert "Connecting" in _get(url, {"ssid": "Home: 5G", "password": "nope", "location": "Chicago"})
    assert wait(lambda: p.active and status.setup and status.setup.get("error"))
    assert "password" in status.setup["error"] and status.setup["brief"] == "Wrong password?" and joined == []
    # right password
    nm.fail_connect = False
    url = f"http://127.0.0.1:{p.port}"
    _get(url, {"ssid": "", "other": "Hidden Net", "password": "right", "location": "Chicago"})
    assert wait(lambda: joined)
    assert joined == [("Hidden Net", "Chicago")] and not p.active and status.setup is None
    assert ("connection", "down", "id", "tesseract-setup") in nm.calls
    add = next(c for c in reversed(nm.calls) if c[:2] == ("connection", "add") and "Hidden Net" in c)
    assert "802-11-wireless.hidden" in add                           # never seen in a scan: asked for by name


def test_setup_waits_for_the_network_after_the_hotspot():
    """Right after the hotspot closes the scan list is empty; it must look again, not fail."""
    nm = FakeNmcli()
    scans = []
    real = nm.__call__

    def run(*args, timeout=45):
        if "list" in args:
            scans.append(1)
            if len(scans) < 3:                                       # the radio is still settling
                return ""
            return "Charlie\u2019s iPhone:70:WPA2\n"
        return real(*args, timeout=timeout)
    joined = []
    p = SetupPortal(config.load(), settings.Wifi(run=run), Status(),
                    on_joined=lambda s, loc: joined.append(s), port=0)
    p.sleep = lambda s: None
    p.apply("charlie's iphone", "hunter22")
    assert joined == ["Charlie\u2019s iPhone"] and len(scans) == 3   # the iPhone's real (curly) name
    add = next(c for c in reversed(nm.calls) if c[:2] == ("connection", "add"))
    assert "802-11-wireless.hidden" not in add


def test_setup_says_when_it_cant_see_the_network():
    cfg = config.load()
    status = Status()
    p = SetupPortal(cfg, settings.Wifi(run=FakeNmcli()), status, port=0)
    p.sleep = lambda s: None
    p.wifi.connect = lambda name: False
    try:
        p.apply("My iPhone", "pw")
        assert status.setup["brief"] == "Can't see that network" and "2.4 GHz" in status.setup["error"]
    finally:
        p.stop()


def wait(cond, timeout=5):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_portal_opens_when_no_known_wifi():
    cfg = config.load()
    cfg.network.setup_after_s = 0
    nm = FakeNmcli(connected=False)
    p = SetupPortal(cfg, settings.Wifi(run=nm), Status(), port=0)
    p.boot_grace_s = 0
    ticks = iter(range(3))

    def fake_sleep(s):
        if next(ticks, None) is None:
            raise SystemExit
    p.sleep = fake_sleep
    with pytest.raises(SystemExit):
        p.watch()
    assert p.active
    p.stop()


def test_portal_waits_longer_right_after_boot():
    """The speaker starts before Wi-Fi is up: slow networks get extra time before setup takes over."""
    cfg = config.load()
    cfg.network.setup_after_s = 0
    p = SetupPortal(cfg, settings.Wifi(run=FakeNmcli(connected=False)), Status(), port=0)
    ticks = iter(range(3))

    def fake_sleep(s):
        if next(ticks, None) is None:
            raise SystemExit
    p.sleep = fake_sleep
    with pytest.raises(SystemExit):
        p.watch()
    assert not p.active


def test_volume_buttons_together_open_setup():
    from tesseract.buttons import Buttons

    class B:
        is_pressed = False

    up, down, fired = B(), B(), []
    threading.Thread(target=Buttons._combo, args=([up, down], lambda: fired.append(1), 0.3), daemon=True).start()
    up.is_pressed = True
    time.sleep(0.5)
    assert fired == []                                   # one button alone is just volume
    down.is_pressed = True
    time.sleep(0.8)
    assert fired == [1]                                  # once per hold, not repeatedly
    up.is_pressed = down.is_pressed = False


def test_say_set_up_wifi(tmp_path):
    from tesseract.alarms import Alarms
    from tesseract.commands import LocalCommands

    class Hooks:
        started = False
        def set_volume(self, v): pass
        def volume(self): return 5
        def start_wifi_setup(self): Hooks.started = True

    local = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), Hooks())
    for said in ("set up wifi", "wifi setup", "connect to a new wifi network", "change the wifi"):
        r = local.handle(said)
        assert r and "Scan the code" in r.text and r.then is not None, said
    r.then()
    assert Hooks.started


def test_setup_screen_qr_matches_what_was_encoded():
    import datetime as dt

    import segno

    from tesseract.display import Display
    from tesseract.state import State
    cfg = config.load()
    cfg.display.rotation, cfg.display.mirror = 0, "none"   # the drawing itself, not how this panel is mounted
    s = Status()
    s.set(state=State.IDLE, setup={"ssid": "Tesseract-Setup", "password": "48312297", "url": "http://10.42.0.1"})
    img = Display(cfg, Status(), sim=True).render(s, dt.datetime(2026, 10, 20, 19, 5))
    m = segno.make("WIFI:T:WPA;S:Tesseract-Setup;P:48312297;;", error="l", boost_error=False).matrix
    n = len(m) + 4                                       # 2-module white border on each side
    scale = 100 // n
    x0, y0 = (176 - n * scale) // 2, 20
    for y, row in enumerate(m):
        for x, dark in enumerate(row):
            px = img.getpixel((x0 + (x + 2) * scale + scale // 2, y0 + (y + 2) * scale + scale // 2))
            assert (px == (0, 0, 0)) == bool(dark), (x, y)


def test_app_builds_like_on_the_pi(tmp_path, monkeypatch):
    """sim=False is the path the Pi takes (Wi-Fi setup, real drivers); the other tests all use sim=True."""
    import tesseract.app as app_mod
    from tesseract.audio import AudioIO

    cfg = config.load()
    cfg.system.state_dir = str(tmp_path)
    monkeypatch.setattr(app_mod, "AudioIO", lambda cfg, sim=False, wav_input=None: AudioIO(cfg, sim=True))
    class FakeWifi(settings.Wifi):          # NetworkManager "present"
        available = staticmethod(lambda: True)

        def __init__(self):
            super().__init__(run=FakeNmcli(connected=True))

    monkeypatch.setattr(settings, "Wifi", FakeWifi)
    app = app_mod.Tesseract(cfg, sim=False)          # LEDs/display/buttons fall back when the hardware isn't there
    assert app.portal is not None and app.status.volume == cfg.audio.volume


def test_wifi_name_match_and_real_mac():
    """Phones call their hotspot "Charlie’s iPhone" (curly apostrophe); nobody types that."""
    nm = FakeNmcli()
    real = nm.__call__

    def run(*args, timeout=45):
        if "list" in args:
            return "Charlie\u2019s iPhone:80:WPA2\nTAMU_IoT:60:--\n"
        return real(*args, timeout=timeout)
    w = settings.Wifi(run=run)
    assert w.match("charlie's iphone") == "Charlie\u2019s iPhone"
    assert w.match("TAMU_IoT") == "TAMU_IoT" and w.match("Elsewhere") == "Elsewhere"
    w.add("TAMU_IoT")
    add = nm.calls[-1]
    assert add[add.index("802-11-wireless.cloned-mac-address") + 1] == "permanent"   # what the IoT portal registered


def test_leftover_setup_hotspot_is_not_wifi():
    nm = FakeNmcli()
    real = nm.__call__

    def run(*args, timeout=45):
        if list(args) == ["-t", "-f", "DEVICE,STATE,CONNECTION", "device"]:
            return "wlan0:connected:tesseract-setup\n"
        return real(*args, timeout=timeout)
    w = settings.Wifi(run=run)
    assert w.status() == (False, "tesseract-setup")
    p = SetupPortal(config.load(), w, Status(), port=0)
    p.sleep = lambda s: (_ for _ in ()).throw(SystemExit)          # stop after the start-up check
    with pytest.raises(SystemExit):
        p.watch()
    assert ("connection", "down", "id", "tesseract-setup") in nm.calls


def test_offline_fixes_escalate_and_match_the_cause():
    from tesseract.settings import network_fix
    # Wi-Fi chip stalled (nothing gets through): re-join first, then reset the radio
    assert network_fix(0, ip_ok=False, dns_ok=False, has_tailscale=True) == [["nmcli", "device", "reconnect", "wlan0"]]
    assert network_fix(1, False, False, True)[0] == ["nmcli", "radio", "wifi", "off"]
    assert network_fix(5, False, False, True)[-1] == ["nmcli", "radio", "wifi", "on"]
    # the internet works but names don't: DNS (Tailscale's, then NetworkManager's)
    assert network_fix(0, ip_ok=True, dns_ok=False, has_tailscale=True) == [["systemctl", "restart", "tailscaled"]]
    assert network_fix(1, True, False, True) == [["systemctl", "restart", "NetworkManager"]]
    assert network_fix(0, True, False, has_tailscale=False) == [["systemctl", "restart", "NetworkManager"]]
