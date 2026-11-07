"""Spotify: Tesseract plays it itself, and you drive it by voice.

    Spotify Soloist (Spotify's own headless Connect player) ─► a private PulseAudio: one null sink, no
        sound card ─► parec, 48 kHz mono ─► MusicSource ─► the mixer, next to Franklin's voice: ducked
        while you talk to Franklin, in the echo canceller's reference (so "Franklin" still works over
        loud music), under the volume buttons

    "play Blinding Lights by The Weeknd" ─► Web API search ─► Soloist's local WebSocket API: play it
    "pause" / "next" ─► the WebSocket API (commands.py, no LLM round trip)
    the screen's "now playing" ◄─ the WebSocket API's events

Why the detour through PulseAudio: Soloist only plays to PipeWire or PulseAudio, and Tesseract drives
the sound card itself through ALSA (the echo canceller needs exactly what went out). A PulseAudio
started with no default script never touches the card. Its null sink keeps the Pi's time rather than
the card's, so the two drift apart a little; MusicSource takes that up.

Why not librespot (what this used before): since late 2025 Spotify refuses it the keys to decrypt
songs on many accounts, so it skips every track (librespot-org/librespot#1649).

Needs Spotify Premium (Soloist's API key and, since February 2026, the developer API both do).
One-time setup is `tesseract spotify setup`: it downloads Soloist, takes its API key, signs in to a
Spotify developer app for the Web API with PKCE (no client secret on the device), and you pick
"Tesseract" once in the Spotify app on your phone so Soloist can keep its own sign-in.

Soloist builds expire 90 days after they're built. When one does (exit code 10) this downloads the
current one from Spotify, which asks that builds only ever come from there.

Web API notes (developer mode, February 2026): search returns at most 10 results per type,
saving to Liked Songs is PUT /me/library with URIs, playlist contents are /playlists/{id}/items.
"""

from __future__ import annotations

import base64
import collections
import difflib
import hashlib
import json
import logging
import os
import platform
import queue
import random
import re
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path

import numpy as np

from . import dsp
from .playback import Source

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
REDIRECT = "http://127.0.0.1:8888/callback"      # Spotify allows plain http only for loopback IPs
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read user-library-modify")
SOLOIST_URL = "https://soloist-builds.spotifycdn.com/soloist_release_{arch}.tar.gz"
SOLOIST_ARCH = {"aarch64": "arm64", "arm64": "arm64", "armv7l": "arm32", "x86_64": "x86_64"}
EXPIRED = 10                     # Soloist's exit code when its build is more than 90 days old
SINK = "tesseract_music"         # the null sink Soloist plays into
RATE = 48_000
LIKED = {"liked songs", "my liked songs", "my likes", "my favorites", "my favourites", "my music", "my library",
         "my saved songs", "saved songs", "my songs"}
LIKED_QUEUE = 25                 # liked songs to line up (Soloist plays URIs one at a time, plus a queue)
PICK_TESSERACT = ("Tesseract isn't signed in to Spotify yet. Open Spotify on your phone, tap the devices button "
                  "and pick Tesseract once.")
NOT_RUNNING = "Spotify's player on me isn't running yet. Try again in a moment."
NOT_SET_UP = "Spotify isn't set up on me yet. Run tesseract spotify setup on me."
UPDATE_FAILED = ("Spotify's player on me has expired, and I couldn't get the new one from Spotify yet. "
                 "I'll keep trying.")


class SpotifyError(Exception):
    """Something to say out loud."""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", "and")).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _join(names: list[str]) -> str:
    return " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else (names[0] if names else "")


def _artists(item: dict) -> str:
    """A Web API track or album's artists, said out loud."""
    return _join([a.get("name", "") for a in item.get("artists") or [] if a])


def _entity(item) -> dict | None:
    """Soloist's entity envelope (uri, decorations.identity.name, decorations.creators[].entity...)
    -> {title, artist, uri}."""
    if not isinstance(item, dict) or not item.get("uri"):
        return None
    deco = item.get("decorations") or {}
    names = [(((c or {}).get("entity") or {}).get("decorations") or {}).get("identity", {}).get("name", "")
             for c in deco.get("creators") or []]
    return {"title": (deco.get("identity") or {}).get("name", ""), "artist": _join([n for n in names if n]),
            "uri": item["uri"]}


# ── sign-in (PKCE) ───────────────────────────────────────────────────────────

def login_url(client_id: str) -> tuple[str, str, str]:
    """-> (URL to open, code verifier, state)."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(12)
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
         "code_challenge_method": "S256", "code_challenge": challenge, "state": state}
    return f"{ACCOUNTS}/authorize?{urllib.parse.urlencode(q)}", verifier, state


def finish_login(path: Path, client_id: str, verifier: str, state: str, landed_on: str, client=None) -> None:
    """landed_on: the address the browser ended up at (http://127.0.0.1:8888/callback?code=...)."""
    import httpx
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(landed_on.strip()).query)
    if q.get("error"):
        raise SpotifyError(f"Spotify said: {q['error'][0]}")
    if q.get("state", [""])[0] != state or not q.get("code"):
        raise SpotifyError("That isn't the address from this sign-in; run setup again.")
    client = client or httpx.Client(timeout=10)
    r = client.post(f"{ACCOUNTS}/api/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier})
    if r.status_code != 200:
        raise SpotifyError(f"Spotify didn't accept the sign-in ({r.status_code}: {r.text[:120]})")
    data = _load(path)                    # keeps the Soloist key
    data.update(client_id=client_id, refresh_token=r.json()["refresh_token"])
    _save(path, data)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)                 # a refresh token is as good as being signed in
    tmp.replace(path)


# ── the Web API ──────────────────────────────────────────────────────────────

class SpotifyAPI:
    def __init__(self, path: Path, client=None):
        import httpx
        self.path = path
        self.client = client or httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0))
        self._data = _load(path)
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    @property
    def linked(self) -> bool:
        return bool(self._data.get("client_id") and self._data.get("refresh_token"))

    @property
    def soloist_key(self) -> str:
        return self._data.get("soloist_key", "")

    def set_soloist_key(self, key: str) -> None:
        self._data["soloist_key"] = key
        _save(self.path, self._data)

    def _access(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            if not self.linked:
                raise SpotifyError("Spotify isn't set up yet. Run tesseract spotify setup on me.")
            import httpx
            try:
                r = self.client.post(f"{ACCOUNTS}/api/token", data={
                    "grant_type": "refresh_token", "refresh_token": self._data["refresh_token"],
                    "client_id": self._data["client_id"]})
            except httpx.HTTPError as e:
                raise SpotifyError("I can't reach Spotify right now.") from e
            if r.status_code != 200:
                log.warning("spotify: token refresh failed: %s %s", r.status_code, r.text[:200])
                raise SpotifyError("My Spotify sign-in expired. Run tesseract spotify setup again.")
            j = r.json()
            self._token, self._expires = j["access_token"], time.time() + j.get("expires_in", 3600)
            if j.get("refresh_token") and j["refresh_token"] != self._data["refresh_token"]:
                self._data["refresh_token"] = j["refresh_token"]      # PKCE refresh tokens rotate
                _save(self.path, self._data)
            return self._token

    def call(self, method: str, path: str, params=None, body=None, retry: bool = True):
        import httpx
        try:
            r = self.client.request(method, API + path, params=params, json=body,
                                    headers={"Authorization": f"Bearer {self._access()}"})
        except httpx.HTTPError as e:
            raise SpotifyError("I can't reach Spotify right now.") from e
        if r.status_code == 401 and retry:
            self._access(force=True)
            return self.call(method, path, params, body, retry=False)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code >= 400:
            msg = (j.get("error") or {}).get("message", "") if isinstance(j.get("error"), dict) else str(j)
            log.warning("spotify: %s %s -> %s %s", method, path, r.status_code, msg)
            if r.status_code == 403 and "premium" in msg.lower():
                raise SpotifyError("Playing music on Tesseract needs Spotify Premium.")
            if r.status_code == 429:
                raise SpotifyError("Spotify asked me to slow down. Try again in a minute.")
            raise SpotifyError("Spotify didn't like that.")
        return j


# ── Soloist itself ───────────────────────────────────────────────────────────

def soloist_path(cfg) -> Path:
    """The Soloist that runs: [spotify] soloist, else the copy setup keeps in the state folder."""
    S = cfg.spotify
    return cfg.path(S.soloist) if S.soloist else cfg.path(cfg.system.state_dir) / "soloist" / "soloist"


def soloist_version(path: Path) -> str:
    """The first line of `soloist --version`, or SpotifyError if it won't run here."""
    try:
        r = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise SpotifyError(f"Spotify Soloist won't run here: {e}") from e
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise SpotifyError(f"Spotify Soloist won't run here ({r.returncode}): {out[-300:]}")
    return out.splitlines()[0] if out else "?"


def install_soloist(dest: Path, client=None) -> str:
    """Download Spotify's current Soloist build to dest (they expire, so this is also the update), check
    it runs, then swap it in. -> its version line."""
    import httpx
    arch = SOLOIST_ARCH.get(platform.machine())
    if arch is None:
        raise SpotifyError(f"Spotify Soloist has no build for {platform.machine()}.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive, new = dest.with_name("soloist.tar.gz"), dest.with_name("soloist.new")
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True)
    try:
        with client.stream("GET", SOLOIST_URL.format(arch=arch)) as r:
            r.raise_for_status()
            with archive.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        with tarfile.open(archive, "r:gz") as tar:      # read the one file out; nothing is extracted by name
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "soloist"), None)
            if member is None:
                raise SpotifyError("Spotify's Soloist download didn't have the player in it.")
            new.write_bytes(tar.extractfile(member).read())
    except (httpx.HTTPError, OSError, tarfile.TarError) as e:
        raise SpotifyError(f"Couldn't download Spotify Soloist: {e}") from e
    finally:
        archive.unlink(missing_ok=True)
    os.chmod(new, 0o755)
    try:
        version = soloist_version(new)
    except SpotifyError:
        new.unlink(missing_ok=True)
        raise
    new.replace(dest)
    return version


def ws_url(cfg) -> str:
    return f"ws://127.0.0.1:{cfg.spotify.ws_port}"


def ask_soloist(cfg, timeout: float = 3.0) -> dict | None:
    """Soloist's auth_state ({logged_in, is_active, device_name}), or None if it isn't answering.
    It sends one to every new connection."""
    from websockets.sync.client import connect
    try:
        with connect(ws_url(cfg), open_timeout=timeout) as ws:
            end = time.monotonic() + timeout
            while (left := end - time.monotonic()) > 0:
                msg = json.loads(ws.recv(timeout=left))
                if isinstance(msg, dict) and msg.get("type") == "auth_state":
                    return msg
    except Exception:                    # not running, refused, timed out, garbled
        return None
    return None


# ── the music itself ─────────────────────────────────────────────────────────

class MusicSource(Source):
    """Soloist's audio for the mixer. Never finishes; ducks under Franklin's voice.

    It arrives on PulseAudio's clock and leaves on the sound card's, which drift apart by up to a few
    hundredths of a percent, so it plays a hair faster or slower (0.1%: inaudible) to keep the queue
    between LOW and HIGH instead of running dry or piling up."""
    background = True          # the mixer doesn't count it as "busy" and stop_all() leaves it alone
    LOW, TARGET, HIGH = 0.04, 0.12, 0.25          # seconds queued
    SILENT = 1e-4              # Soloist's sink sends this (well, zeros) while it's paused

    def __init__(self, volume: float, duck_level: float, duck=lambda: False, max_s: float = 0.5,
                 prime_s: float = 0.1):
        self.gain = volume
        self.duck_level, self.duck = duck_level, duck
        self.max, self.prime = int(RATE * max_s), int(RATE * prime_s)
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._n = 0
        self._lock = threading.Lock()
        self._level = 1.0
        self._drop_until = 0.0
        self._primed = False
        self._speed = dsp.Varispeed()
        self._step = 1.0
        self.heard = 0.0           # when it last had sound in it
        self.finished = False

    @property
    def queued(self) -> float:
        return self._n / RATE

    def push(self, x: np.ndarray) -> None:
        if time.monotonic() < self._drop_until or not len(x):
            return
        if float(np.abs(x).max()) > self.SILENT:
            self.heard = time.monotonic()
        elif not self.playing:
            return                 # paused: don't keep the mixer busy with silence
        level = self.queued
        if level > self.HIGH:
            self._step = 1.001
        elif level < self.LOW:
            self._step = 0.999
        elif (self._step > 1 and level <= self.TARGET) or (self._step < 1 and level >= self.TARGET):
            self._step = 1.0
        x = self._speed.process(x, self._step)
        with self._lock:
            if self._n + len(x) > self.max:
                return             # the mixer isn't taking it (closed?): don't pile up
            self._chunks.append(x)
            self._n += len(x)

    def flush(self, drop_s: float = 0.0) -> None:
        """Drop what's queued, and what arrives in the next drop_s (still on its way from Soloist), so a
        pause stops the music now rather than half a second later."""
        self._drop_until = time.monotonic() + drop_s
        with self._lock:
            self._chunks.clear()
            self._n = 0
            self._primed = False

    def ready(self) -> bool:
        return self._n >= (1 if self._primed else self.prime)

    @property
    def playing(self) -> bool:
        return time.monotonic() - self.heard < 1.5

    def read(self, n):
        out, need = [], n
        with self._lock:
            if not self._primed:
                if self._n < self.prime:          # (re)starting: a little in hand first, so it doesn't stutter
                    return np.zeros(0, dtype=np.float32)
                self._primed = True
            while need and self._chunks:
                c = self._chunks[0]
                take = c[:need]
                out.append(take)
                need -= len(take)
                if len(take) == len(c):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = c[len(take):]
            self._n -= n - need
            if self._n == 0:
                self._primed = False
        if not out:
            return np.zeros(0, dtype=np.float32)
        x = np.concatenate(out)
        target = self.duck_level if self.duck() else 1.0
        if abs(target - self._level) > 1e-3 or self._level != 1.0:
            step = (target - self._level) * min(1.0, len(x) / 3600)   # a ~75 ms glide: quick, but no clicks
            ramp = np.linspace(self._level, self._level + step, len(x), dtype=np.float32)
            self._level += step
            x = x * ramp
        return x


class Player:
    """Runs Spotify Soloist as Tesseract's Spotify Connect speaker and brings its audio into the mixer:
    a private PulseAudio (a null sink, never the sound card) for Soloist to play into, and parec
    recording that sink as 48 kHz mono float for the MusicSource. Restarts them if they stop."""

    def __init__(self, cfg, music: MusicSource, key: str):
        self.cfg, self.music, self.key = cfg, music, key
        self.dir = cfg.path(cfg.system.state_dir) / "soloist"
        self.sock = self.dir / "run" / "pulse.sock"
        self.binary = soloist_path(cfg)
        self.running = False
        self.problem = ""          # why there's no music, said when someone asks for some
        self._procs: list[subprocess.Popen] = []
        self._next_update = 0.0

    def env(self) -> dict:
        """Soloist tries PipeWire first: an XDG_RUNTIME_DIR without a PipeWire socket in it sends it to
        PulseAudio, and PULSE_SERVER to this one."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPEWIRE_", "PULSE_"))}
        env.update(XDG_RUNTIME_DIR=str(self.sock.parent), PULSE_SERVER=f"unix:{self.sock}")
        return env

    def pulse_args(self) -> list[str]:
        # -n: no default.pa, so nothing that looks for sound cards; just a socket and the null sink
        return ["pulseaudio", "-n", "--daemonize=no", "--use-pid-file=no", "--exit-idle-time=-1",
                "--disallow-exit", "--log-target=stderr", "--realtime=no", "--high-priority=no",
                f"--load=module-native-protocol-unix auth-anonymous=1 socket={self.sock}",
                f"--load=module-null-sink sink_name={SINK} rate={RATE} channels=2 "
                "sink_properties=device.description=Tesseract"]

    def parec_args(self) -> list[str]:
        return ["parec", f"--server=unix:{self.sock}", f"--device={SINK}.monitor", "--raw",
                "--format=float32le", f"--rate={RATE}", "--channels=1", "--latency-msec=20",
                "--client-name=Tesseract", "--stream-name=music"]

    def soloist_args(self) -> list[str]:
        S = self.cfg.spotify
        return [str(self.binary), "--device-name", S.device_name, "--api-key", self.key,
                "--data-dir", str(self.dir / "data"), "--cache-dir", str(self.dir / "cache"),
                "--cache-size", "100", "--initial-volume", "100", "--ws", f"127.0.0.1:{S.ws_port}"]

    def start(self) -> None:
        missing = [b for b in ("pulseaudio", "parec") if not shutil.which(b)]
        if missing or not self.key or not self.binary.exists():
            log.warning("spotify: not set up (%s); run `tesseract spotify setup`",
                        ", ".join(missing + ([] if self.key else ["no Soloist key"]) +
                                  ([] if self.binary.exists() else [f"no {self.binary}"])))
            self.problem = NOT_SET_UP
            return
        self.running = True
        threading.Thread(target=self._run, name="soloist", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        for p in self._procs:
            p.terminate()

    def _run(self) -> None:
        backoff = 2
        while self.running:
            t0 = time.monotonic()
            code = self._once()
            if not self.running:
                return
            if code == EXPIRED:
                self.problem = UPDATE_FAILED
                if self._update():
                    continue
            else:
                log.warning("spotify: the player stopped (%s); restarting", code)
            backoff = 2 if time.monotonic() - t0 > 60 else min(60, backoff * 2)
            time.sleep(backoff)

    def _once(self) -> int | None:
        """PulseAudio, then parec and Soloist, until one of them stops. -> Soloist's exit code, if it was Soloist."""
        self.sock.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sock.parent, 0o700)           # PulseAudio insists on that for its runtime dir
        self.sock.unlink(missing_ok=True)           # left over if PulseAudio was killed
        self._procs = []
        try:
            pulse = self._spawn("pulseaudio", self.pulse_args())
            end = time.monotonic() + 10
            while not self.sock.exists():
                if pulse.poll() is not None or time.monotonic() > end:
                    log.error("spotify: PulseAudio didn't start")
                    return None
                time.sleep(0.05)
            parec = self._spawn("parec", self.parec_args(), audio=True)
            threading.Thread(target=self._pump, args=(parec.stdout,), name="parec", daemon=True).start()
            soloist = self._spawn("soloist", self.soloist_args())
            t0 = time.monotonic()
            while self.running and all(p.poll() is None for p in self._procs):
                if self.problem and time.monotonic() - t0 > 10:
                    self.problem = ""               # it's been up a while: whatever it was is over
                time.sleep(0.5)
            return soloist.poll()
        except OSError as e:
            log.error("spotify: couldn't start the player: %s", e)
            return None
        finally:
            for p in self._procs:
                if p.poll() is None:
                    p.terminate()
            for p in self._procs:
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

    def _spawn(self, name: str, args: list[str], audio: bool = False) -> subprocess.Popen:
        p = subprocess.Popen(args, env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE if audio else subprocess.STDOUT, bufsize=0)
        self._procs.append(p)
        threading.Thread(target=self._log, args=(name, p.stderr if audio else p.stdout), daemon=True).start()
        return p

    def _update(self) -> bool:
        """The build expired: get the current one. At most every 6 hours (Spotify may not have a newer
        one up yet)."""
        if time.monotonic() < self._next_update:
            return False
        self._next_update = time.monotonic() + 6 * 3600
        log.info("spotify: Soloist's build expired; downloading the current one")
        try:
            log.info("spotify: updated Soloist: %s", install_soloist(self.binary))
            return True
        except SpotifyError as e:
            log.error("spotify: couldn't update Soloist: %s", e)
            return False

    def _pump(self, out) -> None:
        rest = b""
        while self.running:
            data = out.read(4096)
            if not data:
                return
            data, cut = rest + data, (len(rest) + len(data)) // 4 * 4
            data, rest = data[:cut], data[cut:]           # a read can end mid-sample: keep the rest
            if data:
                self.music.push(np.frombuffer(data, dtype="<f4"))

    @staticmethod
    def _log(name: str, stream) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if not line or "not intended to be run as root" in line:
                continue
            if name == "soloist":                         # the one to read when something's wrong
                log.info("soloist: %s", line[:300])
            else:                                         # PulseAudio's "E: ..." / "W: ..."
                (log.warning if re.match(r"[EW]: ", line) else log.debug)("%s: %s", name, line[:200])


class Remote:
    """Soloist's local WebSocket API (on 127.0.0.1 only: it has no authentication of its own). Events go
    to on_event; command() waits for Soloist to accept or refuse."""

    def __init__(self, url: str, on_event):
        self.url, self.on_event = url, on_event
        self.ws = None
        self.running = False
        self._replies: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._run, name="soloist-ws", daemon=True).start()

    def _run(self) -> None:
        from websockets.sync.client import connect
        while self.running:
            try:
                with connect(self.url, open_timeout=3, max_size=2 ** 22) as ws:
                    self.ws = ws
                    for raw in ws:
                        self._got(raw)
            except Exception as e:           # not up yet, or it restarted
                log.debug("spotify: Soloist's WebSocket: %s", e)
            if self.ws is not None:
                self.ws = None
                self.on_event({"type": "disconnected"})
            time.sleep(1)

    def _got(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") in ("command_result", "error"):
            self._replies.put(msg)
            return
        try:
            self.on_event(msg)
        except Exception:
            log.exception("spotify: event %s", msg.get("type"))

    def command(self, name: str, **args) -> None:
        with self._lock:
            ws = self.ws
            if ws is None:
                raise SpotifyError(NOT_RUNNING)
            while not self._replies.empty():          # nobody waited for these
                self._replies.get_nowait()
            try:
                ws.send(json.dumps({"type": "command", "command": name, **args}))
            except Exception as e:
                raise SpotifyError(NOT_RUNNING) from e
            try:
                reply = self._replies.get(timeout=3)
            except queue.Empty:
                log.warning("spotify: Soloist didn't answer %s", name)
                return
        if reply.get("type") == "error":
            msg = reply.get("message", "")
            log.warning("spotify: Soloist refused %s: %s", name, msg)
            raise SpotifyError(PICK_TESSERACT if "auth" in msg.lower() else "Spotify didn't like that.")


class Spotify:
    """What the voice commands and the fast model's tools call. Methods return what to say."""

    def __init__(self, cfg, api: SpotifyAPI | None = None, duck=lambda: False, on_change=lambda now: None,
                 remote=None):
        S = cfg.spotify
        self.cfg = cfg
        self.api = api or SpotifyAPI(cfg.path(cfg.system.state_dir) / "spotify.json")
        self.music = MusicSource(S.volume, S.duck, duck)
        self.player = Player(cfg, self.music, self.api.soloist_key)
        self.remote = remote or Remote(ws_url(cfg), self._event)
        self.on_change = on_change
        self.now: dict | None = None
        self.logged_in: bool | None = None    # None: Soloist hasn't said
        self.active = False                   # Tesseract is the account's active Connect device
        self.status = ""                      # "playing", "paused", ...
        self._item: dict | None = None        # what Soloist has loaded (its entity envelope)
        self._shown = None
        self._changed = 0.0                   # when Soloist last said something about playback
        self._show_lock = threading.Lock()

    def start(self, mixer) -> None:
        mixer.play(self.music)
        self.player.start()
        if self.player.running:           # else it isn't set up: player.problem says so when asked
            self.remote.start()
            threading.Thread(target=self._watch, name="spotify", daemon=True).start()

    @property
    def playing(self) -> bool:
        return self.music.playing

    # ── Soloist's side ──────────────────────────────────────────────────────
    def _event(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "auth_state":
            logged_in = bool(msg.get("logged_in"))
            if logged_in != self.logged_in:
                log.info("spotify: %s", "Soloist is signed in" if logged_in else
                         f"Soloist is waiting to be picked in the Spotify app as {self.cfg.spotify.device_name}")
            self.logged_in, self.active = logged_in, bool(msg.get("is_active"))
        elif kind == "playback_state":
            self.active = bool(msg.get("is_active", self.active))
            self._item, self.status = msg.get("item") or None, msg.get("status", "")
        elif kind == "track_changed":
            self._item = msg.get("item") or None
        elif kind == "playback_changed":
            self.status = msg.get("status", "")
        elif kind == "disconnected":
            self.logged_in, self.status = None, ""
        else:
            return
        self._changed = time.monotonic()
        self._show()

    def _show(self) -> None:
        """The screen's "now playing": a named track, while Soloist says it's playing and music is actually
        coming out (Soloist's word alone can outlast the music: the queue ran out, a status it has that
        isn't "paused"). A few seconds' grace after it says so, for the music to arrive."""
        now = _entity(self._item) if self.status == "playing" else None
        if now and not now["title"] and not now["artist"]:
            now = None                    # nothing to say but a music note
        if now and not self.music.playing and time.monotonic() - self._changed > 5:
            now = None
        with self._show_lock:
            key = (now or {}).get("uri")
            if key != self._shown:
                self._shown, self.now = key, now
                self.on_change(now)

    def _watch(self) -> None:
        """Music can stop without Soloist saying anything: look again every couple of seconds."""
        while True:
            time.sleep(2)
            self._show()

    def _ready(self) -> None:
        if self.player.problem:
            raise SpotifyError(self.player.problem)
        if self.logged_in is False:
            raise SpotifyError(PICK_TESSERACT)

    def _start(self, uri: str, then: list[str] = ()) -> None:
        """Play uri (a track, album or playlist) on Tesseract, then the tracks in `then`."""
        self._ready()
        if not self.active:
            self.remote.command("activate")
        self.remote.command("play", uri=uri)
        for u in then:
            self.remote.command("add_to_queue", uri=u)

    # ── playing things ──────────────────────────────────────────────────────
    def play(self, query: str, kind: str = "", artist: str = "") -> str:
        query, artist, kind = query.strip(), artist.strip(), (kind or "").lower()
        if kind == "liked" or _norm(query) in LIKED:
            return self._play_liked()
        if kind == "playlist":
            return self._play_playlist(query)
        if kind == "album":
            items = self._search(f"album:{query}" + (f" artist:{artist}" if artist else ""), "album")
            if not items:
                raise SpotifyError(f"I couldn't find the album {query}.")
            a = items[0]
            self._start(a["uri"])
            return self._said(a["name"], _artists(a))
        if kind == "artist":
            return self._play_artist(query)
        if artist:                        # "X by Y": a song by that artist ("Stand by Me": the whole phrase is the song)
            items = (self._search(f"track:{query} artist:{artist}", "track")
                     or self._search(f"{query} by {artist}", "track"))
            if not items:
                raise SpotifyError(f"I couldn't find {query} by {artist}.")
            return self._play_track(items[0])
        if kind == "track":
            items = self._search(query, "track")
            if not items:
                raise SpotifyError(f"I couldn't find {query}.")
            return self._play_track(items[0])
        # "play X": the artist if that's who X is, the song if one's called that, else a playlist
        found = self.api.call("GET", "/search", params={"q": query, "type": "artist,track,playlist", "limit": 5})
        artists = [a for a in (found.get("artists") or {}).get("items") or [] if a]
        tracks = [t for t in (found.get("tracks") or {}).get("items") or [] if t]
        lists = [p for p in (found.get("playlists") or {}).get("items") or [] if p]
        if artists and _similar(artists[0]["name"], query) > 0.85:
            return self._play_artists_songs(artists[0])
        if tracks and _similar(tracks[0]["name"], query) > 0.8:
            return self._play_track(tracks[0])
        if lists and (re.search(r"\b(music|songs|playlist|mix|radio|vibes|beats|hits|chill|study|workout|"
                                r"party|sleep|focus|jazz|lofi|lo fi|classical)\b", _norm(query)) or not tracks):
            self._start(lists[0]["uri"])
            return f"Playing {lists[0]['name']}."
        if tracks:
            return self._play_track(tracks[0])
        raise SpotifyError(f"I couldn't find anything called {query} on Spotify.")

    def _search(self, q: str, kind: str, limit: int = 5) -> list[dict]:
        found = self.api.call("GET", "/search", params={"q": q, "type": kind, "limit": limit})
        return [i for i in (found.get(kind + "s") or {}).get("items") or [] if i]

    def _play_track(self, t: dict) -> str:
        self._start(t["uri"])             # Spotify's autoplay carries on with similar songs afterwards
        return self._said(t["name"], _artists(t))

    def _play_artist(self, name: str) -> str:
        items = self._search(name, "artist")
        if not items:
            raise SpotifyError(f"I couldn't find {name} on Spotify.")
        return self._play_artists_songs(items[0])

    def _play_artists_songs(self, a: dict) -> str:
        """Soloist plays tracks, albums and playlists, not artists: their most popular songs, queued."""
        tracks = [t for t in self._search(f"artist:{a['name']}", "track", limit=10)
                  if any(_norm(x.get("name", "")) == _norm(a["name"]) for x in t.get("artists") or [] if x)]
        if not tracks:
            raise SpotifyError(f"I couldn't find any songs by {a['name']}.")
        self._start(tracks[0]["uri"], then=[t["uri"] for t in tracks[1:]])
        return f"Playing {a['name']}."

    def _play_playlist(self, name: str) -> str:
        """Your own playlists first (by closest name), then anyone's."""
        mine, url = [], "/me/playlists"
        params = {"limit": 50}
        for _ in range(4):
            page = self.api.call("GET", url, params=params)
            mine += [p for p in page.get("items") or [] if p]
            if not page.get("next"):
                break
            params = {"limit": 50, "offset": len(mine)}
        best = max(mine, key=lambda p: _similar(p.get("name", ""), name), default=None)
        if best is not None and (_similar(best["name"], name) > 0.6 or _norm(name) in _norm(best["name"])):
            self._start(best["uri"])
            return f"Playing your playlist {best['name']}."
        items = self._search(name, "playlist")
        if not items:
            raise SpotifyError(f"I couldn't find a playlist called {name}.")
        self._start(items[0]["uri"])
        return f"Playing {items[0]['name']}."

    def _play_liked(self) -> str:
        items = self.api.call("GET", "/me/tracks", params={"limit": 50}).get("items") or []
        uris = [i["track"]["uri"] for i in items if i and i.get("track")]
        if not uris:
            raise SpotifyError("You don't have any liked songs yet.")
        random.shuffle(uris)
        self._start(uris[0], then=uris[1:LIKED_QUEUE])
        return "Playing your liked songs."

    @staticmethod
    def _said(name: str, by: str) -> str:
        return f"Playing {name} by {by}." if by else f"Playing {name}."

    # ── the controls ────────────────────────────────────────────────────────
    def pause(self) -> str:
        self.remote.command("pause")
        self.music.flush(0.6)             # stop now, not after what's already queued
        return ""

    def resume(self) -> str:
        self._ready()
        if self._item is None:
            return self._play_liked()     # nothing to resume: put something on
        if not self.active:
            self.remote.command("activate")
        self.remote.command("play")
        return ""

    def next(self) -> str:
        self.remote.command("skip_next")
        self.music.flush(0.3)
        return ""

    def previous(self) -> str:
        self.remote.command("skip_prev")
        self.music.flush(0.3)
        return ""

    def shuffle(self, on: bool) -> str:
        self.remote.command("set_shuffle", enabled=on)
        return "Shuffle's on." if on else "Shuffle's off."

    def current(self) -> dict | None:
        """What's on: Soloist's own word if it has something loaded, else the Web API's."""
        now = _entity(self._item)
        if now:
            return {**now, "playing": self.status == "playing"}
        j = self.api.call("GET", "/me/player/currently-playing")
        item = j.get("item") if j else None
        if not item:
            return None
        return {"title": item.get("name", ""), "artist": _artists(item), "uri": item.get("uri", ""),
                "playing": bool(j.get("is_playing"))}

    def whats_playing(self) -> str:
        now = self.current()
        if not now:
            return "Nothing's playing."
        return f"This is {now['title']} by {now['artist']}." if now["artist"] else f"This is {now['title']}."

    def like(self) -> str:
        now = self.current()
        if not now or not now["uri"].startswith("spotify:track:"):
            return "Nothing's playing to like."
        self.api.call("PUT", "/me/library", params={"uris": now["uri"]})
        return f"Added {now['title']} to your liked songs."
