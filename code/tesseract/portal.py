"""Wi-Fi setup with just a phone, for when Tesseract is somewhere it knows no Wi-Fi.

    no known Wi-Fi for 45 s (or hold both volume buttons for 3 s, or "Franklin, set up Wi-Fi")
      -> it opens its own hotspot, "Tesseract-Setup", with a fresh password
      -> the screen shows a QR code: point your phone's camera at it to join
      -> the phone pops up the setup page by itself (like hotel Wi-Fi), or go to 10.42.0.1
      -> pick your network, type the password (and optionally where you are), Connect
      -> it drops the hotspot, joins your network and says "Connected to ..."
    Wrong password? Setup comes back with the error. Nobody shows up for 5 minutes?
    It closes the hotspot and tries the networks it knows again, in case the router was
    just slow, then reopens setup if there's still nothing.

The page pops up by itself because install.sh points all DNS on the hotspot at
Tesseract (/etc/NetworkManager/dnsmasq-shared.d/), so the phone's "is there
internet?" check lands here, sees a page instead of the expected answer, and
shows it as a sign-in screen.
"""

from __future__ import annotations

import html
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

ADDRESS = "10.42.0.1"            # NetworkManager's address for a shared (hotspot) connection
IDLE_TIMEOUT_S = 300


class SetupPortal:
    def __init__(self, cfg, wifi, status, on_joined=lambda ssid, location: None, say=lambda text: None,
                 port: int = 80):
        """on_joined(ssid, location) once it's on the new network; say(text) for spoken prompts."""
        self.cfg, self.wifi, self.status = cfg, wifi, status
        self.on_joined, self.say, self.port = on_joined, say, port
        self.active = False
        self.networks = []
        self.password = ""
        self.error = ""
        self._server: ThreadingHTTPServer | None = None
        self._last_seen = 0.0            # last request from a phone
        self._busy = False               # applying a submitted network
        self._lock = threading.Lock()
        self._resume_at = 0.0            # after a timeout, give known networks a chance first
        self.sleep = time.sleep          # (tests swap this out)

    # ── turning it on and off ───────────────────────────────────────────────
    def start(self, error: str = "", brief: str = "") -> None:
        with self._lock:
            if self.active:
                return
            self.active = True
        self.error = error
        try:
            self.networks = self.wifi.scan()      # before the hotspot takes the radio
        except Exception as e:
            log.warning("portal: scan failed: %s", e)
            self.networks = []
        self.password = "".join(secrets.choice("23456789") for _ in range(8))
        try:
            self.wifi.hotspot_on(self.cfg.network.hotspot_name, self.password)
        except Exception as e:
            log.error("portal: couldn't start the hotspot: %s", e)
            self.active = False
            return
        self._serve()
        self._last_seen = time.monotonic()
        self.status.set(setup={"ssid": self.cfg.network.hotspot_name, "password": self.password,
                               "url": f"http://{ADDRESS}", "error": error,
                               "brief": brief or ("Couldn't join: try again" if error else "")})
        log.info("portal: setup hotspot %r is up (password %s)", self.cfg.network.hotspot_name, self.password)
        if not error:
            self.say("I can't find a Wi-Fi network I know. Scan the code on my screen to set one up.")

    def stop(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
        if self._server is not None:
            # close the socket too, or reopening setup (after a wrong password) can't get port 80 back
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.wifi.hotspot_off()
        self.status.set(setup=None)
        log.info("portal: setup hotspot closed")

    def toggle(self) -> None:
        (self.stop if self.active else self.start)()

    boot_grace_s = 45.0      # just started (likely just booted): Wi-Fi, school Wi-Fi especially, may still be joining

    def watch(self) -> None:
        """Background thread: open setup when there's been no Wi-Fi for a while."""
        offline_since = None
        grace = self.boot_grace_s
        try:
            if not self.active and self.wifi.status()[1] == "tesseract-setup":
                log.info("portal: closing a setup hotspot left over from before")   # so known networks get a try
                self.wifi.hotspot_off()
        except Exception:
            log.exception("portal: checking for a leftover hotspot failed")
        while True:
            self.sleep(5)
            now = time.monotonic()
            try:
                if self.active:
                    if not self._busy and now - self._last_seen > IDLE_TIMEOUT_S:
                        log.info("portal: nobody came; trying known networks again")
                        self.stop()
                        self._resume_at = now + 60
                    continue
                if not self.cfg.network.setup_hotspot or self._busy or now < self._resume_at:
                    continue
                if self.wifi.status()[0]:
                    offline_since, grace = None, 0.0
                    continue
                offline_since = offline_since or now
                if now - offline_since > self.cfg.network.setup_after_s + grace:
                    offline_since, grace = None, 0.0
                    self.start()
            except Exception:
                log.exception("portal: watch failed")

    # ── the page ────────────────────────────────────────────────────────────
    def _serve(self) -> None:
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: str, code: int = 200):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):      # every URL is the form: that's what makes phones pop it up
                portal._last_seen = time.monotonic()
                self._send(portal.form())

            def do_POST(self):
                portal._last_seen = time.monotonic()
                n = int(self.headers.get("Content-Length") or 0)
                q = {k: v[0].strip() for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                ssid = q.get("other") or q.get("ssid", "")
                if not ssid:
                    self._send(portal.form("Pick a network first."))
                    return
                self._send(portal.page("Connecting…", f"Tesseract is joining <b>{html.escape(ssid)}</b>. This page "
                                       "will stop working when it switches over. Watch its screen: it'll say when "
                                       "it's online, or show setup again if the password was wrong."))
                threading.Thread(target=portal.apply, daemon=True,
                                 args=(ssid, q.get("password", ""), q.get("username", ""), q.get("location", ""))).start()

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="setup-page", daemon=True).start()

    def apply(self, ssid: str, password: str, username: str = "", location: str = "") -> None:
        """Save the network the phone sent, leave the hotspot and join it."""
        self._busy = True
        try:
            self.sleep(1.5)                       # let the "Connecting..." page reach the phone
            self.stop()
            self.status.set(setup={"connecting": ssid})
            # the radio was a hotspot until now, so it hasn't seen any networks yet: look first
            seen = self.wifi.wait_for(ssid, sleep=self.sleep)
            log.info("portal: %s is %s", ssid, f"in range as {seen!r}" if seen else "not in range (trying as hidden)")
            name = self.wifi.add(seen or ssid, password, username, hidden=seen is None)
            if self.wifi.connect(name):
                log.info("portal: joined %s", seen or ssid)
                self.status.set(setup=None)
                self.on_joined(seen or ssid, location)
            else:
                why = getattr(self.wifi, "last_error", "").lower()
                if seen is None:
                    brief, msg = "Can't see that network", (
                        f"Couldn't find {ssid}. Is it on and nearby? Tesseract only sees 2.4 GHz Wi-Fi: "
                        "on an iPhone, turn on Personal Hotspot → Maximize Compatibility and keep that screen open.")
                elif any(w in why for w in ("secret", "password", "psk", "802-1x", "auth")):
                    brief, msg = "Wrong password?", f"{seen} didn't accept the password. Check it and try again."
                else:
                    brief, msg = "Couldn't join: try again", f"Couldn't join {seen}: {why[:120] or 'no reason given'}."
                self.start(error=msg, brief=brief)
        except Exception as e:
            log.exception("portal: applying %s failed", ssid)
            self.start(error=f"Something went wrong: {e}"[:80])
        finally:
            self._busy = False

    def page(self, title: str, body: str) -> str:
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
body{{font:17px -apple-system,system-ui,sans-serif;margin:0;padding:24px 18px;background:#111;color:#eee}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:#aaa;margin:4px 0 18px}} label{{display:block;margin:16px 0 6px;color:#ccc}}
select,input{{width:100%;box-sizing:border-box;font-size:17px;padding:12px;border-radius:10px;border:1px solid #444;
background:#1c1c1e;color:#eee}} button{{width:100%;margin-top:24px;padding:14px;font-size:18px;border:0;
border-radius:12px;background:#0a84ff;color:#fff}} .err{{color:#ff6b5e}} details{{margin-top:14px;color:#aaa}}
</style></head><body><h1>{title}</h1>{body}</body></html>"""

    def form(self, message: str = "") -> str:
        err = message or self.error
        opts = "".join(f'<option value="{html.escape(n.ssid)}">{html.escape(n.ssid)}{"" if n.secure else " (open)"}'
                       f'</option>' for n in self.networks)
        loc = html.escape(self.cfg.assistant.location)
        return self.page("Set up Tesseract", f"""
<p>Pick the Wi-Fi it should use{": " if err else "."}<span class="err">{html.escape(err)}</span></p>
<form method="post">
<label>Network</label>
<select name="ssid"><option value="">Choose…</option>{opts}</select>
<input name="other" placeholder="…or type a network name" autocapitalize="off" autocorrect="off" style="margin-top:8px">
<label>Password</label>
<input name="password" type="password" autocomplete="off" autocapitalize="off" autocorrect="off">
<details><summary>School or work Wi-Fi (like eduroam)?</summary>
<label>Username</label><input name="username" autocapitalize="off" autocorrect="off" placeholder="you@school.edu">
</details>
<label>Where is it? <span style="color:#777">(for the weather and the clock)</span></label>
<input name="location" value="{loc}" placeholder="City, State">
<button>Connect</button>
</form>""")
