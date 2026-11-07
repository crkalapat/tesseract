"""Spotify: sign-in, "play X by Y", the controls, Soloist, and the music stream next to Franklin's voice."""

import io
import json
import os
import stat
import tarfile
import urllib.parse

import httpx
import numpy as np
import pytest

from tesseract import config
from tesseract import dsp
from tesseract import spotify as sp
from tesseract.alarms import Alarms
from tesseract.commands import LocalCommands
from tesseract.playback import ArraySource, Mixer


class FakeSpotify:
    """Just enough of accounts.spotify.com and the Web API."""

    def __init__(self):
        self.calls = []
        self.tokens = 0

    def __call__(self, req: httpx.Request):
        path, q = req.url.path, dict(urllib.parse.parse_qsl(req.url.query.decode()))
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else None
        self.calls.append((req.method, path, q, body))
        if path == "/api/token":
            self.tokens += 1
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600,
                                             "refresh_token": "rt-" + form.get("grant_type", "")})
        if path == "/v1/search":
            if q["type"] == "track" and q["q"].startswith("artist:"):
                return httpx.Response(200, json={"tracks": {"items": [
                    {"name": "Love Story", "uri": "spotify:track:ls", "artists": [{"name": "Taylor Swift"}]},
                    {"name": "Blinding Lights", "uri": "spotify:track:bl", "artists": [{"name": "The Weeknd"}]},
                    {"name": "Shake It Off", "uri": "spotify:track:sio", "artists": [{"name": "Taylor Swift"}]}]}})
            if q["type"] == "track":
                return httpx.Response(200, json={"tracks": {"items": [None, {"name": "Blinding Lights",
                    "uri": "spotify:track:bl", "artists": [{"name": "The Weeknd"}]}]}})
            return httpx.Response(200, json={
                "artists": {"items": [{"name": "Taylor Swift", "uri": "spotify:artist:ts"}]},
                "tracks": {"items": [{"name": "Love Story", "uri": "spotify:track:ls", "artists": [{"name": "Taylor Swift"}]}]},
                "playlists": {"items": [None, {"name": "Lofi Beats", "uri": "spotify:playlist:lofi"}]}})
        if path == "/v1/me/playlists":
            return httpx.Response(200, json={"items": [{"name": "Road Trip", "uri": "spotify:playlist:rt"},
                                                       {"name": "Study Vibes", "uri": "spotify:playlist:sv"}], "next": None})
        if path == "/v1/me/tracks":
            return httpx.Response(200, json={"items": [{"track": {"uri": f"spotify:track:{i}"}} for i in range(5)]})
        if path == "/v1/me/player/currently-playing":
            return httpx.Response(200, json={"is_playing": True, "item": {"name": "Blinding Lights", "uri": "spotify:track:bl",
                                                                          "artists": [{"name": "The Weeknd"}]}})
        return httpx.Response(204)


class FakeRemote:
    """Soloist's WebSocket API, as far as Spotify can tell: it takes commands."""

    def __init__(self):
        self.sent = []

    def start(self):
        pass

    def command(self, name, **args):
        self.sent.append((name, args) if args else name)


@pytest.fixture
def fake(tmp_path):
    f = FakeSpotify()
    (tmp_path / "spotify.json").write_text(json.dumps({"client_id": "cid", "refresh_token": "rt0", "soloist_key": "k"}))
    cfg = config.load()
    api = sp.SpotifyAPI(tmp_path / "spotify.json", client=httpx.Client(transport=httpx.MockTransport(f)))
    return f, sp.Spotify(cfg, api, remote=FakeRemote())


def played(spot):
    """What Soloist was told to play, then queue."""
    return [(c[0], c[1]["uri"]) for c in spot.remote.sent if c[0] in ("play", "add_to_queue")]


def test_sign_in_with_pkce(tmp_path):
    url, verifier, state = sp.login_url("cid")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://127.0.0.1:8888/callback"
    assert "user-modify-playback-state" in q["scope"] and "client_secret" not in url
    f = FakeSpotify()
    client = httpx.Client(transport=httpx.MockTransport(f))
    with pytest.raises(sp.SpotifyError):
        sp.finish_login(tmp_path / "s.json", "cid", verifier, state, "http://127.0.0.1:8888/callback?code=x&state=WRONG", client)
    sp.SpotifyAPI(tmp_path / "s.json").set_soloist_key("solo")                  # setup takes this first
    sp.finish_login(tmp_path / "s.json", "cid", verifier, state, f"http://127.0.0.1:8888/callback?code=abc&state={state}", client)
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved == {"client_id": "cid", "refresh_token": "rt-authorization_code", "soloist_key": "solo"}
    assert stat.S_IMODE(os.stat(tmp_path / "s.json").st_mode) == 0o600          # it's as good as a password


def test_play_a_song_by_an_artist(fake):
    f, spot = fake
    assert spot.play("blinding lights", "", "the weeknd") == "Playing Blinding Lights by The Weeknd."
    search = next(c for c in f.calls if c[1] == "/v1/search")
    assert search[2]["q"] == "track:blinding lights artist:the weeknd" and int(search[2]["limit"]) <= 10
    assert spot.remote.sent == ["activate", ("play", {"uri": "spotify:track:bl"})]   # on Tesseract, not the phone
    spot.active = True
    spot.play("blinding lights", "", "the weeknd")
    assert spot.remote.sent[-2:] == [("play", {"uri": "spotify:track:bl"})] * 2    # already the active device


def test_play_x_works_out_what_x_is(fake):
    f, spot = fake
    assert spot.play("taylor swift") == "Playing Taylor Swift."               # an artist: their popular songs
    assert played(spot) == [("play", "spotify:track:ls"), ("add_to_queue", "spotify:track:sio")]   # (not The Weeknd)
    assert spot.play("lofi beats") == "Playing Lofi Beats."                     # a mood: a playlist
    assert played(spot)[-1] == ("play", "spotify:playlist:lofi")


def test_playlists_yours_first_and_liked_songs(fake):
    f, spot = fake
    assert spot.play("study", "playlist") == "Playing your playlist Study Vibes."
    assert played(spot)[-1] == ("play", "spotify:playlist:sv")
    assert spot.play("my liked songs") == "Playing your liked songs."
    liked = played(spot)[-5:]
    assert liked[0][0] == "play" and {k for k, _ in liked[1:]} == {"add_to_queue"}
    assert sorted(u for _, u in liked) == [f"spotify:track:{i}" for i in range(5)]


def test_controls_and_like(fake):
    f, spot = fake
    spot.music.push(np.ones(4800, np.float32))
    assert spot.pause() == "" and not spot.music.ready()                       # stops now, not after the buffer
    spot.next()
    spot.previous()
    spot.shuffle(True)
    assert spot.remote.sent == ["pause", "skip_next", "skip_prev", ("set_shuffle", {"enabled": True})]
    assert spot.whats_playing() == "This is Blinding Lights by The Weeknd."    # nothing from Soloist yet: the Web API
    assert spot.like() == "Added Blinding Lights to your liked songs."
    assert f.calls[-1][:3] == ("PUT", "/v1/me/library", {"uris": "spotify:track:bl"})   # Feb 2026: was PUT /me/tracks


def test_resume_or_put_something_on(fake):
    f, spot = fake
    assert spot.resume() == "Playing your liked songs."                       # nothing loaded: something to play
    spot._event({"type": "track_changed", "item": {"uri": "spotify:track:1"}})
    spot.remote.sent.clear()
    assert spot.resume() == "" and spot.remote.sent == ["activate", "play"]


def test_tesseract_not_picked_yet(fake):
    f, spot = fake
    spot._event({"type": "auth_state", "logged_in": False, "is_active": False, "device_name": "Tesseract"})
    with pytest.raises(sp.SpotifyError, match="pick Tesseract"):
        spot.play("blinding lights", "", "the weeknd")
    assert spot.remote.sent == []


def test_says_why_theres_no_music(fake):
    f, spot = fake
    spot.player.problem = sp.UPDATE_FAILED
    with pytest.raises(sp.SpotifyError, match="expired"):
        spot.play("blinding lights", "", "the weeknd")


SONG = {"uri": "spotify:track:2JRo0gjbX4GrCqBYdRohoo", "entity_type": "track",
        "decorations": {"identity": {"name": "My Song"}, "playback": {"duration_ms": 210000},
                        "creators": [{"entity": {"uri": "spotify:artist:a", "entity_type": "artist",
                                                 "decorations": {"identity": {"name": "Artist Name"}}}}]}}


def test_now_playing_from_soloists_events(fake):
    """The shapes are the WebSocket API reference's own examples."""
    f, spot = fake
    shown = []
    spot.on_change = shown.append
    spot._event({"type": "auth_state", "logged_in": True, "is_active": True, "device_name": "Tesseract"})
    assert spot.logged_in and spot.active
    spot._event({"type": "playback_state", "status": "playing", "item": SONG, "is_active": True, "volume": 65})
    spot._event({"type": "playback_changed", "status": "playing"})               # no change: not shown twice
    spot._event({"type": "playback_changed", "status": "paused"})
    spot._event({"type": "track_changed", "item": {**SONG, "uri": "spotify:track:next"}})
    spot._event({"type": "playback_changed", "status": "playing"})
    assert shown == [{"title": "My Song", "artist": "Artist Name", "uri": SONG["uri"]}, None,
                     {"title": "My Song", "artist": "Artist Name", "uri": "spotify:track:next"}]
    assert spot.whats_playing() == "This is My Song by Artist Name."
    assert not any(c[1] == "/v1/me/player/currently-playing" for c in f.calls)   # Soloist said; no API call
    spot._event({"type": "disconnected"})
    assert shown[-1] is None and spot.logged_in is None


def test_no_lonely_music_note(fake):
    """The screen shows a music note and the track: never just the note, never once the music's stopped."""
    f, spot = fake
    shown = []
    spot.on_change = shown.append
    spot._event({"type": "playback_state", "status": "playing", "item": SONG})
    spot._event({"type": "playback_changed", "status": "idle"})                  # a status that isn't "paused"
    assert shown[-1] is None
    spot._event({"type": "playback_state", "status": "playing", "item": {"uri": "spotify:track:x"}})   # no name
    assert shown[-1] is None
    spot._event({"type": "playback_state", "status": "playing", "item": SONG})
    assert shown[-1]["title"] == "My Song"
    spot._changed -= 6                                                          # Soloist still says "playing"...
    spot._show()                                                                # ...but no music's come out
    assert shown[-1] is None
    spot.music.push(np.full(9600, 0.3, np.float32))                             # and back when it does
    spot._show()
    assert shown[-1]["title"] == "My Song"


class FakeWS:
    def __init__(self, remote, answer):
        self.remote, self.answer, self.sent = remote, answer, []

    def send(self, text):
        self.sent.append(json.loads(text))
        self.remote._got(json.dumps(self.answer))          # Soloist answers on the receiving thread


def test_remote_commands_and_refusals():
    events = []
    r = sp.Remote("ws://127.0.0.1:1", events.append)
    with pytest.raises(sp.SpotifyError, match="isn't running"):
        r.command("pause")
    r.ws = FakeWS(r, {"type": "command_result", "command": "play"})
    r.command("play", uri="spotify:album:x")
    assert r.ws.sent == [{"type": "command", "command": "play", "uri": "spotify:album:x"}]
    r.ws.answer = {"type": "error", "message": "command requires authentication"}
    with pytest.raises(sp.SpotifyError, match="pick Tesseract"):
        r.command("pause")
    r._got('{"type": "volume_changed", "volume": 40}')
    r._got("not json")
    assert events == [{"type": "volume_changed", "volume": 40}]


def test_player_keeps_pulseaudio_off_the_sound_card(tmp_path):
    cfg = config.load()
    p = sp.Player(cfg, sp.MusicSource(1.0, 0.15), "key")
    pulse = p.pulse_args()
    assert "-n" in pulse and not any("alsa" in a or "udev" in a for a in pulse)     # no default.pa, no cards
    assert any(a.startswith("--load=module-null-sink") for a in pulse)
    rec = p.parec_args()
    assert f"--device={sp.SINK}.monitor" in rec and "--rate=48000" in rec and "--channels=1" in rec
    solo = p.soloist_args()
    assert solo[solo.index("--ws") + 1] == "127.0.0.1:47821" and solo[solo.index("--api-key") + 1] == "key"
    env = p.env()
    assert env["PULSE_SERVER"] == f"unix:{p.sock}" and env["XDG_RUNTIME_DIR"] == str(p.sock.parent)


def test_install_soloist(tmp_path):
    exe = b'#!/bin/sh\necho "Spotify Soloist 1.4.2 (built 2026-10-20)"\n'
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, data in (("soloist_release/LICENSE", b"..."), ("soloist_release/soloist", exe)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    urls = []

    def cdn(req):
        urls.append(str(req.url))
        return httpx.Response(200, content=blob.getvalue())
    dest = tmp_path / "soloist" / "soloist"
    version = sp.install_soloist(dest, client=httpx.Client(transport=httpx.MockTransport(cdn)))
    assert version == "Spotify Soloist 1.4.2 (built 2026-10-20)"
    assert urls[0].startswith("https://soloist-builds.spotifycdn.com/soloist_release_")   # only ever from Spotify
    assert os.access(dest, os.X_OK) and sorted(p.name for p in dest.parent.iterdir()) == ["soloist"]

    def broken(req):
        return httpx.Response(200, content=b"not a tarball")
    with pytest.raises(sp.SpotifyError):
        sp.install_soloist(dest, client=httpx.Client(transport=httpx.MockTransport(broken)))
    assert sp.soloist_version(dest) == version                                   # the old one is still there


def test_music_ducks_and_stays_out_of_the_way():
    ducked = [False]
    music = sp.MusicSource(0.6, 0.15, duck=lambda: ducked[0], max_s=2)
    music.push(np.ones(48_000, np.float32))
    assert np.allclose(music.read(960), 1.0)
    ducked[0] = True
    levels = [float(music.read(960)[-1]) for _ in range(20)]
    assert levels[0] > 0.5 and abs(levels[-1] - 0.15) < 0.01                  # glides down, no click

    class Audio:
        def output(self): raise RuntimeError("no card in tests")
    mixer = Mixer(Audio(), lambda: 1.0)
    mixer.play(music)
    assert not mixer.busy                                                       # music isn't "Franklin is talking"
    reply = mixer.play(ArraySource(np.zeros(10, np.float32)))
    assert mixer.busy
    mixer.stop_all()                                                            # "Franklin" interrupts the reply...
    assert reply.finished and not music.finished                                # ...not the music


def test_paused_music_lets_the_mixer_rest():
    music = sp.MusicSource(1.0, 0.15)
    music.push(np.zeros(9600, np.float32))                                      # Soloist's sink while paused
    assert not music.ready() and not music.playing
    music.push(np.full(9600, 0.3, np.float32))
    assert music.ready() and music.playing


@pytest.mark.parametrize("drift", [1.0005, 0.9995])
def test_music_keeps_in_step_with_the_sound_card(drift):
    """PulseAudio's clock runs 0.05% fast or slow against the card's: ten minutes of it, no gaps."""
    music = sp.MusicSource(1.0, 0.15)
    tone = (0.3 * np.sin(np.arange(48_000 * 2) * 0.05)).astype(np.float32)
    made = used = 0.0
    short = 0
    for _ in range(30_000):                                                     # 20 ms reads: 10 minutes
        want = int((used + 960) * drift) - int(made)
        if want > 0:
            music.push(tone[:want])
            made += want
        got = len(music.read(960))
        used += 960
        if got and got < 960:
            short += 1
    assert short <= 1 and music.LOW / 2 < music.queued < music.HIGH * 1.5


def test_varispeed_is_seamless():
    v = dsp.Varispeed()
    x = np.arange(1, 10_001, dtype=np.float32)
    out = np.concatenate([v.process(c) for c in np.split(x, 10)])
    assert np.array_equal(out, x)                                               # in step: untouched
    faster = np.concatenate([v.process(c, 1.001) for c in np.split(x + 10_000, 10)])
    assert abs(len(faster) - 10_000 / 1.001) < 2 and np.all(np.diff(faster) > 0)   # no jumps back


def test_soloist_audio_survives_odd_reads(tmp_path):
    cfg = config.load()
    music = sp.MusicSource(1.0, 0.15, max_s=100)
    player = sp.Player(cfg, music, "key")
    player.running = True
    tone = (np.sin(np.arange(48_000) / 10) * 0.3).astype("<f4")
    raw = tone.tobytes()

    class Odd(io.RawIOBase):          # a pipe that hands over 4093 bytes at a time: mid-sample cuts
        pos = 0
        def read(self, n):
            chunk = raw[self.pos:self.pos + min(n, 4093)]
            self.pos += len(chunk)
            return chunk
    player._pump(Odd())
    out = np.concatenate(list(music._chunks))
    assert abs(len(out) - 48_000) < 100
    assert np.abs(out).max() < 0.35                                             # no garbage from misaligned bytes


class Hooks:
    def __init__(self, spotify):
        self.spotify = spotify
    def set_volume(self, v): pass
    def volume(self): return 5


@pytest.mark.parametrize("said,call", [
    ("pause", "pause"), ("next song", "next"), ("previous song", "previous"), ("whats playing", "whats_playing"),
    ("play blinding lights by the weeknd", ("blinding lights", "", "the weeknd")),
    ("play my study playlist", ("study", "playlist")), ("play my liked songs", ("liked songs", "liked")),
    ("play some songs by taylor swift", ("taylor swift", "artist")), ("play", "resume")])
def test_voice_commands(tmp_path, said, call):
    got = []

    class Spot:
        def __getattr__(self, name):
            return lambda *a: got.append(a if name == "play" else name) or "ok"
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), Hooks(Spot())).handle(said)
    assert r is not None and got == [call]


def test_not_set_up(tmp_path):
    c = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), Hooks(None))
    assert "tesseract spotify setup" in c.handle("play blinding lights on spotify").text
    assert c.handle("play blinding lights") is None                            # left to the fast model
