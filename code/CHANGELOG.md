# Tesseract firmware changelog

What changed in the firmware (`code/`), one entry per day, newest first. This file is written and kept
up to date by the AI assistants working on the code. Charlie's own build journal is `../JOURNAL.md`;
this is the technical record next to it.

## Instructions for the AI keeping this log

Update this file in every session that changes the firmware, before you finish. If you're unsure whether
a change belongs here, it does.

1. **One entry per day, newest at the top.** Find today's date (`YYYY-MM-DD`) under "Entries". If it's
   there, add to it. If not, copy the template below in above the previous day.
2. **Write for Charlie first, then for the next AI.** Say what changed in terms of what the speaker does
   ("listening ends ~1.5 s after you stop, not 5 s+"), then name the files, settings or commands
   involved. Skip internal refactors nobody would notice, unless the next AI needs to know about them.
3. **Say how you know it works.** Mark each item **Verified on the Pi** (Charlie ran it, or pasted
   output showing it) or **Not yet tried on the Pi** (tests or simulations only). Give real numbers
   when you have them (dB, %, seconds, false wakes per hour) and say what they were measured on.
4. **Keep "Open issues" honest.** Add what's still broken, unverified or waiting on hardware. Remove
   items when they're resolved, and say so in that day's entry.
5. **Include "To update the Pi"** whenever deploying needs more than rsync + restart, for example
   re-running `install.sh`, new downloads, or a wiring change.
6. **Don't rewrite past entries.** If one was wrong, add a correction to today's entry instead.
7. If another session or agent worked on something you can't see (say, in a separate window), mention
   it and point there rather than guessing.

### Template

```markdown
## YYYY-MM-DD

**In short:** one or two sentences on the day's main changes.

### New
- **Feature name.** What it does and how to use it (voice command, `tesseract ...` command,
  `config.toml` setting). Files: `tesseract/...`. *Verified on the Pi* / *Not yet tried on the Pi*.

### Changed
- **What.** Before → after, and why. *Verified / Not yet tried.*

### Fixed
- **Symptom Charlie saw.** The cause, and the fix. Measured result if there is one. *Verified / Not yet tried.*

### Hardware notes
- Anything physical: wiring, power, parts, enclosure. Leave this section out if there's nothing.

### To update the Pi
rsync from the Mac (BRINGUP.md step 5), then `sudo systemctl restart tesseract`. Plus any extra steps.

### Open issues
- What's still broken, unverified or waiting on someone.
```

---

## Entries

## 2026-11-06

**In short:** a documentation check before pushing to GitHub. No behaviour changes.

### Changed
- **Docs checked against the code.**
  - Root `README.md`: fixed a broken sentence about routing and a typo in the feature list.
  - `README.md`: the Hermes voice examples now use things its voice toolset can do (no terminal or
    files), and the files table lists `__main__.py`, `state.py`, `routing_test.py`, `models/`,
    `tools/wakeword/` and `tests/`.
  - `BRINGUP.md`: `doctor`'s two wake-word lines, no green pulse after a result, tuning for both wake
    detectors, and re-running `install.sh` when it changes.
- **The Tailscale network name is no longer in the repo:** `<tailnet>` in this log,
  `franklin.example.ts.net` in `tests/test_local_llm.py`. A scan found no API keys or passwords in the
  files to be pushed. `.env`, `data/` and the downloaded models stay gitignored.

## 2026-11-05

**In short:** Routing fixes from a day of real use. Anything about Charlie's own stuff now goes straight
to Hermes. The fast model is told to search rather than guess, and to never say "I can't". Muting
works by voice, and there's a new quiet mode. Hermes is asked for faster replies, logs how long it
takes, and is nudged to use its memory. Tests: 240 pass (up from 226).

### New
- **Quiet mode.** "Turn on quiet mode" / "quiet mode off" (on the device), or any phrasing via the fast
  model's `quiet_mode` tool. While it's on, a finished job isn't read out. The LEDs glow green and the
  screen says *Ready* until you tap the action button or ask "what did you find?" / "read it" /
  "what's the answer?" (`READ_HELD` in `app.py`, plus a `read_waiting_results` tool). Questions from
  jobs wait too, shown by the amber light. Turning quiet mode off reads anything waiting. It's saved
  in `data/settings.json`. Files: `app.py`, `commands.py`, `assistant.py`, `state.py`, `leds.py`,
  `display.py`. *Not yet tried on the Pi* (end-to-end test in sim).
- **Mute by voice.** "Mute", "stop listening" and "turn off your mic" mute the mic silently: no words,
  just the mute sound (Charlie: "the user isn't an idiot"), and the button turns it back on. "Mute the sound" / "unmute" set the speaker to 0 and back. Handled on the device, with a
  `mute` tool as the fallback. Before, the fast model said it couldn't. *Not yet tried on the Pi.*
- **Personal requests go straight to Hermes.** If the transcript mentions email, inbox, calendar,
  meetings, appointments, schedule, "my week/day", docs, sheets, Drive, budget, notes, the to-do list,
  contacts and the like (`PERSONAL` in `assistant.py`), it skips the fast model. That saves the ~1 s
  LLM round and stops answers like "I can't see your email". Timers, alarms and music are excluded.
  The last exchange with Franklin goes along for context ("add that to my calendar"). Only applies
  when an external agent is connected. *Not yet tried on the Pi.*
- **`tesseract routing-test [MODEL ...]`** runs 30 realistic requests through the real prompt and tools
  and scores each model's first move (answer / which tool), with time to the first token. It's for
  comparing fast models. Not run yet: the Mac's `.env` has no OpenRouter key. File:
  `tesseract/routing_test.py`.

### Changed
- **The fast model's routing rules** (`assistant.py` system prompt). It answers only stable knowledge
  it's certain of. Any other fact goes to `web_search`: people, companies, prices, sports, releases,
  statistics, local places, anything from 2024 on. It's told never to guess a name, number or date,
  and never to say it can't do something (use `start_task` instead). This targets gpt-oss
  hallucinations Charlie hit several times. *Not yet tried on the Pi.*
- **Handoff phrase "Okay, working on it." → "Okay, I'm on it."** The voice paused in the middle
  ("working… on it"). Not measured: the pause script needs an OpenRouter key on the Mac.
- **Hermes speed and memory** (`agents.py`). Each request asks for `model_options:
  {"reasoning_effort": "low"}`, a per-request override in Hermes's API docs. If franklin rejects it,
  the request is retried without it and a warning is logged. Options: `reasoning_effort` and
  `service_tier` in the agent block. Every reply logs `agents: Hermes replied in X s (first sign of
  life after Y s, N tool steps)`. The voice note now asks Hermes to save lasting facts to memory
  right away and to search past conversations before saying it doesn't know. *Not yet tried on the Pi.*

### Fixed
- **Couldn't interrupt Hermes's answers by voice.** Any reply containing "Franklin" ignored the wake
  word until it had finished (`_hearing_itself`), so the speaker couldn't wake itself by saying its own
  name. Hermes's answers often said "Franklin", partly because the context attached to straight-to-
  Hermes requests called the conversation "the conversation with Franklin". Now a wake during such a
  reply counts if the echo canceller hears a person over it (`someone_talked`, the same check as
  `_own_voice`). Its own voice alone is still ignored, as is the case where the canceller can't tell
  yet. The context label no longer names the assistant, and ignored wakes are logged ("the reply itself
  said ..."). The action button always interrupted. *Not yet tried on the Pi.*

- **The job progress bar sat nearly full for a minute.** Each progress event closed a third of the gap
  to 90%, and Hermes sends two per tool call (started, finished), so a few tool calls took it to ~90%
  and it stopped there. Now the bar grows with elapsed time, each *new* step counts as 8 s more
  of it, and it never passes 80% until the job is done, when it fills. That's about half full after
  30 s and three-quarters at 2 min (`Task.fraction` in `agents.py`, `PROGRESS_CAP`, `PROGRESS_TAU_S`,
  `STEP_S`). *Not yet tried on the Pi.*

- **A green "done" flash after every job, even outside quiet mode.** Finished jobs pulsed green while
  read out and for 8 s after. Now green means one thing: quiet mode is holding a result (*Ready*).
  Outside quiet mode a result is simply read out. A failure still shows red, and a question amber.
  Files: `app.py`. *Not yet tried on the Pi.*

### New (later the same day)
- **The screen shows what Hermes is doing** in the status row, instead of "Working": "Checking
  calendar", "Updating a sheet", "Searching the web", "Using the browser"... (`step_words` and
  `STEP_WORDS` in `agents.py` map Hermes's tool names; unknown tools show their name in words). The
  built-in agent's labels were already sent but never displayed; they show too now. *Not yet tried
  on the Pi.*
- **A bell with a slash in the top-left corner while quiet mode is on** (idle screen, shifting with the
  burn-in orbit, and the header of the other screens). Checked in a rendered preview.

- **Cut off mid-sentence when talking for a while.** Two causes. First, a hard 15 s cap
  (`[listen] max_utterance_s`), now 60 s. Second, a thinking pause in a long request ended it: 0.75 s
  after a complete-sounding sentence, or 1.2 s otherwise. Now, once you've talked for 6 s
  (`long_talk_after_s`), only a 1.6 s pause (`long_pause_ms`) ends it. Quick commands are as snappy as
  before. Measured on synthetic speech (10 s, a 1.3 s pause, 2 s more): before, listening stopped at
  the pause (10.8 s kept); now it keeps all of it (14.1 s). File: `vad.py`. *Not yet tried on the Pi.*

- **Closed captions while it speaks.** Before, the speaking screen showed a "Speaking" label with level
  bars, then the reply text as the model wrote it, often a sentence or two ahead of the voice. Now it
  shows only the sentence you're hearing, centred, and switches when playback reaches the next one.
  Each sentence's audio carries a marker (`StreamSource.mark` / `on_mark` in `playback.py`, placed by
  `SpeechSession._sequence`), and the caption changes when the mixer actually reads that sample. Long
  sentences scroll at about 14 characters a second. The "Speaking" label and bars are gone. Files:
  `playback.py`, `speech.py`, `display.py`, `state.py`, `app.py`. *Not yet tried on the Pi* (rendered
  previews and tests).

- **"On it. On it." and then nothing** (Charlie: "how much money did I spend at the movie theater").
  The log showed `heard ...` and no `tools:` line: gpt-oss *wrote* "On it." as its answer and never
  called `start_task`, so no job started. My routing prompt had quoted the phrase ("the speaker says a
  short 'On it.' by itself"), and the model imitated it. Three fixes:
  1. The prompt no longer quotes the phrase, and says never to claim to be working on something
     without calling `start_task` in the same reply.
  2. If the reply is still only an acknowledgement with no tool call (`EMPTY_ACK` in `assistant.py`:
     "on it", "let me check", "give me a sec", "I'll get back to you"), the job is started anyway, with
     a warning in the log.
  3. Money questions ("how much did I spend", "did I pay", my bank/bills/purchases/receipts/spending)
     now go straight to Hermes, and "my" may take a qualifier ("my recent purchases").
  `routing-test` gained two such cases. *Not yet tried on the Pi.*

### Findings (no code change)
- **"No email connection" is accurate.** The email set up on franklin is Hermes's email *gateway*:
  a way to chat with Hermes by email, like Discord. It isn't a tool for reading an inbox, and Hermes
  has no built-in one. Reading mail needs Gmail in workspace-mcp (see the session's instructions).
- **Hermes memory** is two small files (`~/.hermes/memories/MEMORY.md` 2,200 chars, `USER.md` 1,375),
  loaded as a frozen snapshot at the start of each session, and written only when the model calls its
  `memory` tool. A client `system` message is layered on top of Hermes's prompt, so it doesn't
  replace memory. If the files are full, new memories fail.
- **`typesafe/jev-router`** (OpenRouter) picks a model per request. It's not a fast classifier, and
  latency and price aren't fixed, so it's not used. The personal-data pattern match is the
  zero-latency classifier.

### To update the Pi
rsync, then `sudo systemctl restart tesseract`. Then `tesseract routing-test` (and with candidate
models, e.g. `tesseract routing-test openai/gpt-oss-120b openai/gpt-6-luna inception/mercury-3`).

### Verified on the Pi
- **`tesseract routing-test`** (with the new prompt): gpt-oss-120b 30/30, median first token 0.28 s,
  worst 0.69 s. gpt-6-luna 30/30, 0.68 s median, 2.98 s worst. mercury-3 28/30 (answered "stop
  listening" itself; web-searched a booking request), 0.51 s median. gpt-oss-120b stays.

### Open issues
- Whether franklin accepts `model_options` is unknown. Check for a "rejected model_options" warning.
- The new "Okay, I'm on it." hasn't been heard yet.

## 2026-11-02

**In short:** Tesseract can now connect to Hermes on `franklin`, the Google Cloud VM, over Tailscale.
Background jobs are split: Hermes takes research and memory, and the built-in agent keeps the
on-device calendar, to-do list and reminders. A separate Claude session set up the server side (VM,
Hermes, Discord, email, `tailscale serve`, the Pi's Tailscale ACL and `/etc/tesseract/franklin.env`).
This entry covers only the firmware.

### New
- **Calmer, varied handoff phrases instead of "On it!" every time.** Charlie found the cheerful,
  rising "On it!" uncanny by the third time. A background job now starts with one of seven phrases,
  picked at random and never the same twice in a row: "On it.", "Give me one second.", "Let me go do
  that real quick.", "Okay, working on it.", "Sure, one moment.", "Alright, I'll get back to you.",
  "Okay, give me a minute." They end in periods so the voice stays flat. All are cached at startup, so
  they still play instantly. Edit the list in `HANDOFF` in `tesseract/assistant.py`. *Not yet tried on
  the Pi.*
- **Hermes remembers the conversation for 15 minutes.** Before, every background job was a new Hermes
  conversation, so a follow-up like "move the second one to 4" had no context. Now everything Tesseract
  sends Hermes (jobs and the direct line) continues one conversation, which Hermes stores on its side
  via the `X-Hermes-Session-Id` header. A new conversation starts after 15 minutes without a Hermes
  request, or right away when you hold the action button. Two jobs running at the same time don't share
  it: the second runs on its own. The setting is `new_conversation_after_min` in the agent's block
  (default 15; 0 = the old behaviour). It applies only to Hermes; other OpenAI-compatible servers keep
  the history on the Pi as before. Files: `tesseract/agents.py`, `config.py`, `app.py`. *Not yet tried
  on the Pi* (tested against a fake Hermes server).
- **`tesseract hermes https://franklin.<tailnet>.ts.net`.** `https://` addresses no longer get
  `:8642` appended, because `tailscale serve` is on 443. If `/etc/tesseract/franklin.env` has
  `FRANKLIN_URL` / `FRANKLIN_API_KEY`, plain `tesseract hermes` connects with those and doesn't ask.
  The command also prints the model list as a check. Files: `tesseract/__main__.py`.
  *Not yet tried on the Pi.* From the Mac over the tailnet, franklin's `/health` returns ok
  (v0.22.6) and `/v1/models` returns 401 without the key, as it should.
- **`tesseract doctor`** reports a rejected key ("key rejected (HTTP 401)"). Before, any HTTP answer
  counted as "reachable". *Not yet tried on the Pi.*

### Changed
- **New `[thinking] keep_builtin` setting (default `false`).** With Hermes connected, every
  background job goes to Hermes, as before. `true` keeps the built-in agent as a second choice; then
  `start_task` requires an `agent`, and the fast model picks by description. Earlier today the default
  was briefly `true`. Charlie switched it off because the built-in calendar lives on the Pi, separate
  from Google, so Hermes couldn't see those events or catch conflicts with them. Files:
  `tesseract/agents.py`, `assistant.py`, `config.py`, `config.toml`. *Tests only.*
- **The Hermes description** (written by `tesseract hermes`) used to claim a terminal and files. It now
  lists what franklin's voice toolset actually has: Google Calendar (once connected), web, memory, and
  past conversations. It only matters for routing when there's more than one agent. *Tests only.*

### Fixed
- **"Offline" until a reboot, while other devices on the same Wi-Fi were fine** (Charlie saw this
  after leaving it idle). "Offline" means Tesseract can't reach openrouter.ai. The setup QR code never
  appeared, so NetworkManager still believed it was connected while nothing got through. Two likely
  causes: the Zero 2 W's Wi-Fi chip stalling while staying associated, or Tailscale's DNS
  (100.100.100.100, newly added) getting stuck. Which one isn't known yet. Now, after 2 minutes
  offline, Tesseract logs a diagnosis as a `network: offline.` line with the NetworkManager state,
  Wi-Fi link and signal, gateway ping, whether 1.1.1.1 is reachable, whether DNS resolves, and the
  nameservers. Then it tries a fix that fits:
  - if DNS is the only problem, restart `tailscaled`, then NetworkManager;
  - otherwise `nmcli device reconnect wlan0`, then turn the Wi-Fi radio off and on.
  It retries 2, 4, 8, then up to every 15 minutes, so a router that's really down is mostly left alone.
  Files: `tesseract/settings.py` (`diagnose_network`, `network_fix`, `heal_network`), `app.py`.
  *Not yet tried on the Pi.*
- **Logs now survive a reboot** (`install.sh` sets journald to persistent, capped at 100 MB), so
  `journalctl -b -1` shows what happened before the last reboot. *Not yet tried on the Pi.*
- **`tesseract doctor` said "porcupine keyword"** when `[wake] engine = "both"`. The wake word itself
  was fine; doctor just treated anything other than sherpa or openwakeword as Porcupine. It now checks
  both detectors. *Tests only.*

### Verified on the Pi
- `sudo tesseract hermes https://franklin.<tailnet>.ts.net` connected ("Models: hermes-agent",
  "Hermes says: OK"), and `tesseract doctor` shows "Hermes (openai) reachable key ok" plus the
  built-in agent. Two fixes were needed first: HTTPS certificates had to be turned on in the Tailscale
  admin console (before that, `tailscale serve` answered with a TLS "internal error"), and
  `/etc/tesseract/franklin.env` was never created on the Pi, so the key went in at the prompt and is
  stored in `code/.env`. The Pi is tagged `tag:tesseract` and resolves franklin through MagicDNS.

Tests: 226 pass (up from 218).

### To update the Pi
rsync from the Mac, then `sudo systemctl restart tesseract` (the new phrases synthesize in the background
over the first minute). Hermes, the first time: `sudo tesseract hermes` (it reads `/etc/tesseract/franklin.env`)
or `sudo tesseract hermes https://franklin.<tailnet>.ts.net`. It restarts the service by itself.

### Open issues
- Hermes is connected, but no voice request has gone to it yet.
- The JSON fields of `hermes.tool.progress` aren't documented. The client reads `label`/`tool`/`name`,
  so check that the screen shows something sensible ("Hermes: web_search") during a real job.
- Google Calendar isn't connected to Hermes yet, so calendar requests can't work until it is (planned:
  an MCP server on franklin, enabled for `api_server`). Events already in the Pi's `data/organizer.json`
  won't show up in Google.
- The "Offline until reboot" cause isn't known yet. The next time it happens, the `network: offline.`
  line in the journal will say whether it was DNS or the Wi-Fi chip, and whether the fix worked.
- Safety check to do: "Franklin, ask Hermes to run ls in the terminal" must be refused. If it runs,
  the `api_server` toolset restriction on franklin isn't working.

## 2026-10-31

**In short:** less than a day after the first full power-up with the display working. Tesseract now
starts by itself with a boot light, can be turned off safely, and has new Wi-Fi setup fixes, a trained
"Franklin" wake word, much better end-of-speech detection, interrupting while it talks, crackle fixes,
spoken-friendly replies, Spotify, Hermes and a local-model mode. Tests went from 133 to 218, all
passing.

### New
- **Spotify.** Tesseract is a Spotify Connect speaker, controlled by voice: "play Blinding Lights by The Weeknd", "play Taylor Swift", "play my study playlist", "play my
  liked songs", "put on something chill" (the fast model picks a playlist), "pause", "stop", "next",
  "previous song", "what's playing", "like this song", "shuffle on". Common commands are answered on
  the device without an LLM. Music ducks while you talk to Franklin, goes through the echo canceller,
  and pauses for alarms and shutdown. The screen shows what's playing.

  How it works:
  - **Player:** [Spotify Soloist](https://developer.spotify.com/documentation/soloist), Spotify's own
    headless Connect player.
  - **Audio path:** Soloist only plays to PulseAudio or PipeWire, while Tesseract drives the sound card
    itself through ALSA, because the echo canceller needs exactly what went out. So Tesseract starts a
    private PulseAudio with no default script: just a socket and one null sink, `tesseract_music`, and
    no sound card. Soloist plays into that sink. `parec` records the sink's monitor at 48 kHz, and
    `MusicSource` feeds it into the mixer next to Franklin's voice. That's how the music ducks, sits
    under the volume buttons, and goes through the echo canceller.
  - **Control:** "pause", "next" and the screen's now-playing info go through Soloist's local
    WebSocket API. "Play X by Y" is a Web API search (a Spotify developer app signed in with PKCE),
    then a play command over the same WebSocket.

  Setup is `tesseract spotify setup`. It installs PulseAudio (and makes sure no other PulseAudio can
  take the sound card), downloads the current Soloist build from Spotify (builds expire after 90
  days, so it's re-downloaded when needed), and asks for a Soloist API key and the developer app.
  Then you pick "Tesseract" once in the phone app. Needs Premium; developer apps are limited to 5
  users (Feb 2026 rules). Files: `tesseract/spotify.py`, `commands.py`, `assistant.py`, `playback.py`.
  *Set up and working on the Pi (account setup done in a separate Claude Code window, which also
  edited `spotify.py`; see that session for its changes).*
- **Trained "Franklin" wake word model**, alongside sherpa (`[wake] engine = "both"`; either one wakes
  it). It's an openWakeWord-style classifier trained on this Mac: 6,000 Piper TTS clips from 800
  speakers, including fast-said variants like "Franken", against sound-alikes and 10.7 h of real
  speech, music and noise. On unseen voices, both together caught 72% in a quiet room (sherpa alone:
  56%) and 64% in a noisy one (55%), for about 0.6 extra false wakes per hour of TV-like audio. The
  model fires when its score stays ≥ `threshold` (0.6) for `frames` (2) steps. Files:
  `models/franklin.onnx`, `tesseract/wakeword.py`; how to retrain: `tools/wakeword/README.md`.
  *Not yet measured on the Pi in normal use.*
- **Wake word variants for sherpa:** "Franken", "Franklyn", "Frankin" (`[wake] keyword_variants`).
- **Interrupting Franklin while it talks.** Echo cancellation (`tesseract/echo.py`: a partitioned-block
  frequency-domain adaptive filter, GCC-PHAT alignment, residual echo suppression) takes Franklin's own
  voice out of the mic, so saying "Franklin" over a reply stops it. *Verified on the Pi:*
  `tesseract interrupt-test` removed 19 dB of its own voice and caught 7 of 8 of Charlie's
  interruptions (the 8th was the timing bug fixed below).
- **Ignores wakes caused by its own voice or music.** When the wake word fires while the speaker is
  playing, it checks whether the mic heard sound its echo can't explain (a person talking). If not, the
  wake is ignored and logged. Checked on two real recordings: it vetoed a self-trigger and kept every
  real interruption.
- **Boot light.** `tesseract/splash.py` (service `tesseract-splash.service`) starts right after the
  kernel. It shows the "Starting" rainbow on the LEDs and "Starting" on the screen, and the main program
  carries both on once it's loaded. It needs `install.sh` once; the screen part appears from the second
  boot. *Not yet tried on the Pi.*
- **Shut down / restart.** "Franklin, shut down" or holding the action button 5 s shuts it down
  (unplug once the screen goes dark); "Franklin, restart" reboots it.
- **Local mode.** `tesseract local <mac>.local` uses a model in LM Studio (or Ollama) for answers, for
  example Qwen 3.5 9B. Switch with "Franklin, switch to local mode" / "use the cloud". If the Mac is
  asleep it falls back to the cloud for a minute. Speech-to-text, the voice and web search stay in the
  cloud. *Not yet tried on the Pi.*
- **Hermes in one command.** `tesseract hermes <address>` asks for the API key, tests the connection,
  and saves the key in `.env` and the agent in `data/local.toml`.
- **Diagnostics.**
  - `tesseract speaker [vol]`: test tones, dropout count and under-voltage check.
  - `tesseract interrupt-test`: interrupt success, echo removal and dropouts, plus a recording saved to
    `data/interrupt-test.npz` for offline analysis.
  - `tesseract doctor`: now also reports power (under-voltage).
  - `tesseract wifi`: shows the MAC address (for TAMU_IoT registration).
  - Logs: warnings when the speaker runs dry, the mic loses audio, or the wake word falls behind; one
    line per reply on how much echo was removed.

### Changed
- **Listening ends properly with a fan or hum running.** End-of-speech detection is now Silero VAD
  (`models/silero_vad.onnx`, v6.2.3, checksum-verified, fetched at start-up) instead of webrtcvad,
  which called fan noise speech 86–100% of the time. In simulation, recording now stops ~1.5 s after
  you stop talking; before, it took 4.8 s with a fan and never stopped with a louder one.
- **Doesn't cut you off.** A finished sentence now needs 0.75 s of silence (was ~0.2 s), and one that
  trails off gets 1.2 s (was 0.8 s). Settings: `[listen] complete_silence_ms`, `end_silence_ms`.
- **Replies are written for a voice.** Every model that writes something to be spoken (fast model,
  Haiku background agent, web search, external agents) gets one shared instruction,
  `SPOKEN_STYLE` in `speech.py`. A cleanup step backs it up:
  - Links, source lists and citations are removed completely.
  - A site needed mid-sentence is said in words ("the TXDMV website").
  - Abbreviations are spelled out (Fri, Oct 31 → Friday, October 31; mph, °F, min, e.g., vs., etc., %,
    &).
- **The service starts without waiting for Wi-Fi** (`After=NetworkManager.service`). The common
  phrases are cached once the network comes up. Setup now waits 90 s after boot before opening the
  hotspot, so slower networks like TAMU_WiFi get time to connect.
- **While Franklin talks, the trained wake word model is only slightly stricter** (`[wake]
  speaking_threshold_boost` 0.25 → 0.1), now that echo cancellation and the self-voice check exist.

### Fixed
- **Wake word fired late "in silence", sometimes twice.** The two detectors ran one after the other
  on one core and fell behind real time, then worked through a backlog. Now each runs in its own
  thread. It skips ahead if it's more than 1 s behind, drops stale detections, and counts the 1.5 s
  "don't fire twice" window in audio time. Mic frames are numbered (`audio.MicFrame`) to make this
  possible. *Not yet tried on the Pi.*
- **A real "Franklin" was ignored as its own voice.** The self-voice check looked back from "now"
  while the wake word runs behind the mic. It now looks back from the exact frame the wake word fired
  on. On both real recordings, with a 1 s lag: every real interruption accepted, and the self-trigger
  rejected.
- **Echo cancellation removed nothing (2 dB) on the real cube.** The mic hears the speaker 14×
  (+23 dB) louder than the signal sent to it, and the filter diverged in quiet bands and pauses. Fixed
  with:
  - regularization scaled to the signal level;
  - learning only while Franklin is talking;
  - a divergence watchdog;
  - the full causal constraint on every filter block.

  It also re-syncs immediately after any mic or speaker dropout. Result on the Pi: 19 dB.
  Best-possible linear cancellation on this hardware is about 11 dB (speaker distortion, mic
  clipping), so 19 dB includes the suppressor. *Verified on the Pi.*
- **Speaker crackles and pops during replies.** Fixes:
  - the voice stream now keeps a 150 ms cushion, and fades instead of cutting when the network is late;
  - the echo canceller no longer blocks the speaker thread;
  - a soft limiter replaces hard clipping;
  - the speaker and mic threads get real-time priority;
  - the sound card buffers grew (speaker 160 ms, mic 320 ms).

  *Charlie hasn't confirmed yet whether the crackling is gone.*
- **Wi-Fi setup failed ("connection failed").** iPhones couldn't join the setup hotspot (the Pi's
  brcmfmac chip mishandles protected management frames, PMF, as an access point), so the hotspot is
  now built with PMF off. Joining right after the hotspot closed also failed, because the radio hadn't
  scanned yet; it now waits until the network is visible. It also handles hidden networks, matches
  iPhone names with a curly apostrophe (’), and says why it failed. A leftover hotspot no longer counts
  as being online. *Not yet tried on the Pi.*
- **Didn't start until SSH connected.** The service waited for Wi-Fi (network-online), so it only
  started around when SSH became possible. Fixed by the start-up change above.

### Hardware notes
- **Mic clipping and strong speaker coupling.** The mic clips on Franklin's loudest syllables, and the
  speaker reaches the mic +23 dB above the signal sent to it. That caps echo cancellation and makes
  interrupting harder. Next steps: acoustically isolate the mic (gasket or foam so it only hears its
  port), seal the speaker's back, or lower the volume a notch.
- **Pops at power-on and shutdown.** The amp's SD_MODE is tied on through R2. Optional fix: lift R2's
  5 V end and wire it to header pin 16 (GPIO23), set `[pins] amp_enable = 23`, then re-run
  `install.sh`.
- If crackling continues, run `tesseract speaker 6` to rule out a weak 5 V supply (under-voltage).

### To update the Pi
1. rsync from the Mac (BRINGUP.md step 5).
2. Run `sudo ./install.sh && sudo reboot` once, for the boot light service, the firmware boot settings
   and the Silero download.
3. After that, `sudo systemctl restart tesseract` is enough.

### Open issues
- Crackling: needs Charlie's confirmation after the fixes. If it persists, run `tesseract speaker 6`.
- Wake word in real use: check `journalctl -u tesseract | grep wakeword` for "behind" or "skipped
  ahead" warnings. If they appear, run `tesseract perf` and consider `[wake] engine = "openwakeword"`
  to save CPU.
- Interrupting is limited by the mic hearing the speaker so loudly (see Hardware notes).
- Not yet tried on the Pi: the boot light, the Wi-Fi setup fixes, local mode, the late-wake fix, and
  playing Spotify-made playlists (Discover Weekly, Daylist) under the Feb 2026 API rules.
