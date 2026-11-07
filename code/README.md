# Tesseract firmware

Python firmware for the Raspberry Pi Zero 2 W inside Tesseract. Say **"Franklin"**, ask anything, and
it answers out loud. Quick things (timers, the time, the weather, a fact) come from a fast model in
about a second. Bigger jobs ("add this event to my calendar", "what's on my to-do list") go to a
background agent (the built-in thinking model, or your own agent such as Hermes): Franklin says "On
it.", the LED strip shows progress, and the result is read out when it's ready. The display shows the
time, timers, and what it's doing, with captions while it talks.

```
"Franklin" ─► wake word (on-device) ─► listen; transcribe during your pause (speculative STT)
           ─► timers / time / volume ─► answered on the device, no LLM
           ─► fast model (streamed, with tools) ─► answer, weather, web search, or "On it." ─┐
           ─► text-to-speech, sentence by sentence (fixed phrases cached) ─► speaker        │
                                          thinking model, in the background ◄──────────────┘
                                          to-do list, calendar, notes, web ─► result read out
              └──────────────────── all through OpenRouter, one API key ────────────────────┘
```

## ⚠️ The v1 board feeds the display 5 V

`J1` pin 2 (display VDD) is on the PCB's `+5V` net, but the NHD-1.91-176176UBC3 is a 3.3 V part
(**3.5 V max**), so 5 V will likely kill the $36 OLED. [`current_wiring.png`](current_wiring.png)
already fixes this by powering display VDD from Pi header **pin 17** (3V3). Make sure the cable
wire from `J1` pin 2 is cut or pulled out of its housing, so display pin 2 touches *only* the pin-17
wire. If both are connected, the PCB's 5 V is shorted straight into the Pi's 3.3 V rail. For the
next PCB, connect J1 pin 2 to `+3V3`. (At full white the panel draws ~285 mA at 3.3 V. Tesseract's
UI is mostly black, which keeps it to a fraction of that and also helps against burn-in.)

The display's 2x10 header is numbered odd/even (pins 1, 3, 5, … 19 on one row and 2, 4, … 20 on the
other), while `J1` carries display pins 1–10 and `J8` carries pins 11–20, in order. Wire `J1` pin
*n* to display pin *n* and `J8` pin *n* to display pin *n* + 10.

## Pins

Every pin lives in the `[pins]` section at the top of [`config.toml`](config.toml). Change it there,
then run `python -m tesseract doctor`, which flags pins that can't do the job you gave them. The
defaults match [`current_wiring.png`](current_wiring.png).

| Function | PCB connector | GPIO | Header pin | Notes |
| --- | --- | --- | --- | --- |
| SW4 action (talk/stop) | J7-6 | 26 | 37 | |
| SW3 volume up | J7-5 | 22 | 15 | |
| SW1 volume down | J7-3 | 5 | 29 | |
| SW2 mute | J7-4 | 6 | 31 | |
| LED_DIN | J6-7 | 12 | 32 | PWM0 (on-board audio is disabled so the LED driver can use it) |
| Display D/C# | J6-3 | 27 | 13 | |
| Display RES# | J6-2 | 17 | 11 | |
| Display SCLK / SDIN | J6-5 / J6-4 | 11 / 10 | 23 / 19 | fixed by SPI0 |
| Display CS# | J7-2 | 8 | 24 | `[pins] display_cs`; on this board the wire is on pin 22, so config.toml says 25 |
| I2S BCLK / LRCLK (mic + amp) | J7-1 / J6-6 | 18 / 19 | 12 / 35 | fixed by PCM |
| Mic data (SD) | J7-7 | 20 | 38 | fixed by PCM |
| Amp data (DIN) | J6-1 | 21 | 40 | fixed by PCM |
| Display VDD | (display pin 2) | 3V3 | 17 | see the warning above |
| PCB 5V / 3V3 / GND | J4-1 / J4-2 / J4-3 | | 2 / 1 / 6 | |
| Audio driver "amp enable" (unused) | | 4 | 7 | driven high while the speaker plays: must have NOTHING connected (on GPIO25 it blanked the display) |

Free for future use: GPIO 2/3 (I2C, e.g. a light sensor for auto-brightness), 4, 7, 13, 16, 23, 24.

## Setup

**[BRINGUP.md](BRINGUP.md)** walks through everything, from a blank microSD card to a headless
speaker: power checks, flashing, install, a test for each part, and going headless. In short:

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager, setting the hostname, Wi-Fi, SSH
   and time zone there. 64-bit is required because onnxruntime has no 32-bit ARM builds.
2. Copy `code/` to the Pi, then run `sudo ./install.sh` (it asks for your OpenRouter key) and reboot.
3. Stop the service while testing (`sudo systemctl disable --now tesseract`), then bring each part
   up on its own:
   ```bash
   tesseract doctor                  # pins, overlay, sound card, API key, Wi-Fi, location, agents
   tesseract location "Austin, Texas"
   tesseract display                 # cycles every screen
   tesseract leds                    # cycles every LED animation
   tesseract buttons                 # prints each button press
   tesseract record 4                # mic level + playback; tune mic_gain_db
   tesseract say "Hello"             # TTS -> amp -> speaker
   tesseract wake                    # wake-word detections; tune the threshold
   tesseract ask "What's the weather tomorrow?"
   ```
   (`install.sh` adds the `tesseract` command; it runs as root by itself.)
4. Turn the service back on with `sudo systemctl enable --now tesseract`. Follow it with
   `journalctl -u tesseract -f`, and restart it after editing config with
   `sudo systemctl restart tesseract`.

`sudo` is needed because the LED driver uses DMA through `/dev/mem`.

## Taking it somewhere new (Wi-Fi and location)

- **Wi-Fi, before you leave:** `tesseract wifi add "Home Wi-Fi"` saves it (it asks for the password)
  and it joins by itself when it's in range. `tesseract wifi` lists saved and nearby networks.
- **Wi-Fi, when you get there:** if it can't find a known network for 45 s (90 s right after it boots), it opens a setup
  hotspot and shows a QR code. Scan it with your phone, the setup page pops up, pick your network.
  Hold both volume buttons for 3 s (or say "Franklin, set up Wi-Fi") to open it any time.
  School Wi-Fi like eduroam works too: the page has a username field, or
  `tesseract wifi add TAMU_WiFi --user yournetid`. For a campus IoT network (TAMU_IoT), register the
  MAC address `tesseract wifi` prints.
- **Phone hotspot:** on an iPhone, turn on *Maximize Compatibility* (the Pi Zero 2 W is 2.4 GHz only),
  then `tesseract wifi add "Your iPhone"` while the hotspot is on, so it can match the iPhone's curly
  apostrophe.
- **Location:** say *"Franklin, I'm in Chicago"*, or run `tesseract location "Chicago, Illinois"`. It
  updates `[assistant] location` and switches the clock to that place's time zone.

Wi-Fi passwords live in NetworkManager, never in `config.toml`, which ends up in git. Step by step in
[BRINGUP.md](BRINGUP.md#taking-it-somewhere-new-a-demo-at-home).

## The wake word "Franklin"

Two detectors listen at once (`[wake] engine = "both"`), and either one wakes it. They miss
different things, so together they catch more than either alone:

- **A model trained for "Franklin"** (`models/franklin.onnx`, openWakeWord style). It was trained on
  6,000 clips of "Franklin" (and the way it comes out said fast: "Franken", "Franklen") from 800
  synthetic speakers, with room echo and noise, against sound-alikes and 7.5 hours of real speech,
  music and noise. It fires when its score stays above `threshold` for `frames` 80 ms steps in a
  row: a real "Franklin" holds its peak, while a random syllable spikes once. How it was made, and
  how to retrain it: [tools/wakeword](tools/wakeword/README.md).
- **sherpa-onnx keyword spotting**: a small streaming speech model that listens for whatever word
  `[assistant] name` is set to, plus `keyword_variants` like "Franken". It needs no training, so it
  still works if you rename Franklin.

Measured on audio neither had trained on:

| | quiet room | noisy room | false wakes |
| --- | --- | --- | --- |
| sherpa alone (before) | 56% | 55% | — |
| both (now) | 72% | 64% | about 0.6 more per hour of TV-like audio |

Those percentages are for robotic macOS voices, which are much harder to catch than a person. On
held-out human-like voices, the trained model alone catches 82%. The cost is a few more wakes on
near-words like "Frankenstein".

Tune it in the room with `tesseract wake`:
- **If it misses you:** set `threshold = 0.5`, or lower `keyword_threshold` to 0.25.
- **If it wakes on its own:** set `threshold = 0.7` or `frames = 3` (0.3 false wakes per hour in
  testing), or raise `keyword_threshold` to 0.35.

To go back to sherpa alone, set `engine = "sherpa"`.

Tesseract also ignores its own name when its reply says it (for example "Okay, back to Franklin").

Other engines, if you want them later:

- **openWakeWord alone** (`engine = "openwakeword"`), the trained model on its own. It's quieter
  about false wakes but catches less in noise than both together.
- **Porcupine (Picovoice)** (`engine = "porcupine"`). Picovoice no longer offers a personal free
  tier: sign-up needs a business email. It's kept only for people who already have an access key.

## How it behaves

**Buttons:** action = talk, stop, or dismiss an alarm (hold it to cancel everything and forget the
conversation; keep holding for 5 s to shut down). Volume up/down (hold to repeat; both for 3 s = Wi-Fi
setup). Mute (the LEDs glow red while muted).

**Power:** it starts by itself when plugged in. A few seconds in, the boot light (`tesseract/splash.py`,
its own tiny service) puts the "Starting" rainbow on the LEDs and "Starting" on the screen, and the
speaker carries both on once it has loaded, 20-30 s later. To turn it off, say *"Franklin, shut down"*
or hold the action button 5 s, then unplug it once the screen goes dark. *"Franklin, restart"*
reboots it.

**Speaker pops:** the amp's SD_MODE pin is tied on through R2, so the amp is live whenever it has
power, and it pops when the sound card's clock starts (start-up) and stops (shutdown) and when power
comes and goes. The fix is one wire: lift R2's 5 V end and connect that end to header pin 16
(GPIO23), set `[pins] amp_enable = 23`, and re-run `install.sh`. The sound driver then keeps the amp
off until the clock is running and turns it off before the clock stops. GPIO23 idles low at
power-on, so the amp also stays off during boot.

**Display:** a minimal black screen in Inter. Idle shows the date in grey above a large, light
clock (no AM/PM). Below it, in one or two rows of the same style: timers (a small bar that drains,
plus the countdown), the background job (a bar that fills while it works, next to what it's doing right now, e.g. *Checking calendar*; *Needs you*, *Ready* or
*Failed*), mic off, offline and the next alarm. *Listening / Transcribing / Thinking / Searching the
web* sit in the middle of the screen with a live indicator, with your words faintly underneath.
While it speaks, the screen shows only closed captions: the sentence you're hearing right now, switching as the voice reaches the next one (long sentences scroll at about speaking pace). A timer going off shows its name with the bar
pulsing. Replies on screen and out loud never include markdown or raw links: a link becomes its
words or just the site's name ("weather.gov"). It shifts a few pixels every minute against OLED
burn-in.

**Brightness and night:** say *"Franklin, dim the screen"*, *"make the screen brighter"*, *"screen
brightness 4"*, or *"turn off the screen"* (0-10; it's remembered across restarts), or run
`tesseract brightness 4`. *"Good night, Franklin"* (or "I'm going to bed") dims the screen and the LEDs
and turns the clock warm red; from 06:30 it slowly brightens back to normal, fully by 07:30. *"Good
morning"* ends it early. If you forget to say good night, it starts by itself at 02:00. The times are
`[night]` in `config.toml`.

**LEDs** while you're talking to it:

| State | LEDs |
| --- | --- |
| Wake word | quick bloom from the centre |
| Listening | cyan, swelling with your voice |
| Transcribing | blue ripple |
| Thinking | violet light gliding back and forth |
| Working (web search, weather) | the same in gold |
| Speaking | white, pulsing with the voice; red when it reads a job's failure, amber for a job's question |
| Error | two red blinks |
| Alarm/timer ringing | orange pulse |
| Volume change | white bar |

and when idle, most urgent first:

| Background | LEDs |
| --- | --- |
| Mic muted | red glow (always wins) |
| A job needs your answer | an amber light running down the strip |
| A job failed | slow red pulse |
| Quiet mode: a result is ready | slow green pulse until you tap the button or ask for it (green means only this) |
| A job is running | violet progress bar, shimmering at its edge |
| A timer is running | a dim warm bar that drains as the soonest timer runs down, pulsing in its last 10 s |
| Nothing | off |

All LED states cross-fade, and the LEDs dim with the screen at night.

**Quiet mode:** *"Franklin, turn on quiet mode"*. Finished background jobs aren't read out; the LEDs glow
green and the screen says *Ready* until you tap the action button or ask (*"what did you find?"*,
*"read it"*). A job's question waits the same way (amber light). *"Quiet mode off"* reads anything
waiting. While it's on, a bell with a slash sits in the screen's top-left corner. It's remembered
across restarts.

**Mute by voice:** *"Franklin, mute"* / *"stop listening"* turns the mic off, silently (the mute button turns it
back on, since it can't hear you then). *"Mute the sound"* / *"unmute"* is the speaker.

**Voice features:** questions, timers, alarms (persist across reboots), volume, screen brightness, good night, the weather (Open-Meteo,
for `[assistant] location`), web search (via OpenRouter's web plugin), and background jobs. If Franklin
ends with a question, it keeps listening a few seconds without needing the wake word. Saying
"Franklin" while it talks interrupts it: echo cancellation (`tesseract/echo.py`) takes its own voice
back out of the mic first, since the speaker is centimetres away and otherwise drowns you out. It
learns the speaker-to-mic path during the first couple of seconds it talks after starting, then keeps it. "Franklin, stop" / "never mind" end a turn on the device,
without a cloud round trip.

## Two models and background jobs

Everything you say goes to the first of these that takes it:

1. **On the device, no LLM** (`commands.py`): "set a timer for ten minutes", "set a pasta timer
   for 5 minutes", "how much time is left?", "cancel the timer", "what time is it?", "what's the
   date?", "turn it up", "volume 4". The reply comes from the TTS cache, so audio starts right after
   the transcript. Anything unusual ("set a timer and remind me to flip the steak") falls through.
2. **Straight to your agent** (with Hermes connected): anything about your own stuff (email, inbox,
   calendar, meetings, schedule, "my week", docs, sheets, Drive, budget, notes, to-do list, contacts)
   skips the fast model and goes to Hermes, with the last exchange attached for context. A pattern
   match on the Pi, so it takes no time and the fast model can't answer "I can't see your email".
3. **The fast model** (`[llm]`, gpt-oss-120b on Cerebras/Groq): answers stable knowledge itself, and
   its tool choice routes the rest: `web_search` for any other fact (it's told never to guess a name,
   number or date), `get_weather`, timers/alarms/volume/mute/quiet mode (confirmations spoken
   directly, with no second LLM round), or `start_task` for anything else on your behalf; it's told
   never to say "I can't". `tesseract routing-test [MODEL ...]` scores this on 30 requests and times
   it, to compare models.
4. **The thinking model** (`[thinking]`, Claude Haiku 5.5): `start_task` says **"On it."** or one of six other calm
   variations (cached, instant, never the same twice in a row) and the job runs in the background. With no `[[agents]]` configured, the
   built-in agent (`thinker.py`) does it with a to-do list, a calendar (with reminders that ring
   like alarms) and notes stored on the device in `data/organizer.json`, plus the weather, web
   search, and reading web pages. If it needs something only you know ("What time on Friday?"), it
   asks out loud as soon as the speaker is free, the LEDs show the amber scanner, and your next
   words go to it ("never mind" drops the job). The result is read out when it's done.

Things to try: *"Add the robotics club meeting this Friday at 6 to my calendar."* · *"What's on my
to-do list today?"* · *"Remember that my locker code is 4 1 7."* · *"Remind me about the dentist
on Tuesday at 3, half an hour before."* · *"How's that going?"* · *"Stop that job."*

The calendar and to-do list live on the device, not in Google. To use your real accounts, add an
`[[agents]]` entry for an agent that has them (Hermes, or an n8n webhook): with exactly one agent
configured, every background job goes to it. (`[thinking] keep_builtin = true` keeps the built-in
agent as a second choice, but its calendar stays on the device, apart from your agent's.)

## Agents (Hermes and friends)

Add `[[agents]]` blocks to `config.toml` (commented examples are at the bottom). With **one** agent,
every background job goes to it and you never have to say its name; with several, Franklin picks by
each `description`, or you name one ("ask Hermes to ..."); with none, the built-in agent is used.
`[thinking] keep_builtin = true` adds the built-in agent as one more choice.
Supported types:
any OpenAI-compatible endpoint (**Hermes Agent**, OpenClaw, Open WebUI, LiteLLM, ...), **Home
Assistant** (smart home control), a **webhook** (n8n, Zapier, your own server), or a **command**
(for example `ssh workstation claude -p ...`).

To connect Hermes, enable its API server on the machine where it runs, then run
`tesseract hermes <that machine's address>` on Tesseract. It asks for the key, checks that Hermes
answers, and saves everything (the key in `.env`, the agent in `data/local.toml`).
`tesseract hermes off` disconnects it. For a server on your tailnet behind `tailscale serve`, give the
`https://` address (`tesseract hermes https://franklin.<tailnet>.ts.net`; no port is added). If
`/etc/tesseract/franklin.env` holds `FRANKLIN_URL` and `FRANKLIN_API_KEY`, plain `tesseract hermes`
uses them instead of asking. On the Hermes machine:

```bash
cat >> ~/.hermes/.env <<'EOF'
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_KEY=pick-a-long-secret
EOF
hermes gateway
```

(Or by hand: put the same secret in Tesseract's `.env` as `HERMES_API_KEY` and uncomment the Hermes
block at the bottom of `config.toml`, with `url = "http://<that machine>:8642/v1"`.)

Things you can then say:

- *"Franklin, what's on my calendar today?"* "On it.", and the answer is read out when Hermes replies.
- *"Franklin, find a Thai place near campus that's open late."* The LED progress bar grows while Hermes
  works, and the screen shows each step ("Searching the web"); then a chime and a short spoken summary.
  If Hermes ends with a question, your next words go back to it.
- *"Franklin, how's that going?"* / *"Franklin, stop that job."*
- Follow-ups work: *"What's on my calendar Friday?"* then *"Move the second one to 4."* Everything you
  ask Hermes is one conversation (kept on the Hermes side), and a new one starts after 15 minutes without
  a Hermes request (`new_conversation_after_min` in the agent's block) or when you hold the action button.
- *"Franklin, let me talk to Hermes."* This opens a direct line: each turn goes straight to Hermes and is
  spoken as it streams, while the display shows what Hermes is doing ("Hermes: Searching the web"). Say
  *"back to Franklin"* to return.

What Hermes can do by voice is whatever tools its API server is given (`platform_toolsets.api_server`
in Hermes's `config.yaml`); keep the terminal out of it, since anyone near the speaker can talk to it.
Hermes is asked for low reasoning effort per request (`reasoning_effort` in its agent block; `""` =
Hermes's own setting), and `service_tier = "priority"` makes it faster at the cost of using your ChatGPT
plan's limit faster. Every reply logs `agents: Hermes replied in X s (first sign of life after Y s, N
tool steps)`.

## Spotify

Tesseract can be a Spotify Connect speaker you drive by voice. It needs **Spotify Premium**.

| Say | |
| --- | --- |
| *"Franklin, play Blinding Lights by The Weeknd"* | that song, then similar music (Spotify's autoplay) |
| *"play Taylor Swift"* / *"play some songs by Taylor Swift"* | the artist |
| *"play my study playlist"* / *"play the playlist Road Trip"* | your own playlists first, then anyone's |
| *"play the album After Hours"* | an album |
| *"play my liked songs"* | your Liked Songs, shuffled |
| *"put on something chill for studying"* | the fast model picks a playlist |
| *"pause"* / *"stop"* / *"play"* / *"resume"* | |
| *"next"* / *"skip"* / *"previous song"* | |
| *"what's playing?"* / *"like this song"* / *"shuffle on"* | |

The common ones are handled on the device, with no LLM round trip. While you talk to Franklin, the
music ducks, and echo cancellation takes it out of the mic, so "Franklin" still works over it. The
screen shows what's playing under the clock. The volume buttons control everything, and the Spotify
app's slider sets the music's level within that.

The player is [Spotify Soloist](https://developer.spotify.com/documentation/soloist), Spotify's own
headless Connect player. Soloist only plays to PipeWire or PulseAudio, and Tesseract drives the sound
card itself, so Soloist gets a private PulseAudio that has a null sink and no sound card. Tesseract
records that sink and mixes it with Franklin's voice.

**Setup** (once), over SSH: `tesseract spotify setup`. It installs PulseAudio (and makes sure no other
PulseAudio can ever start and take the sound card), downloads Soloist from Spotify, then asks for:

1. A **Soloist API key** from <https://developer.spotify.com/dashboard/soloist>, made with the Premium account.
2. A **developer app** for the Web API, which Soloist doesn't have and Franklin needs for search,
   playlists and Liked Songs. Use redirect URI `http://127.0.0.1:8888/callback` and add yourself
   under User Management. When the browser lands on a page that won't load, paste its address back.
   If you're already signed in, it keeps that.

Then, on your phone on the same Wi-Fi, open Spotify, tap the devices button and pick **Tesseract**
once. Soloist keeps that sign-in.

`tesseract spotify` shows how it's doing; `tesseract spotify off` turns it off.

Limits:

- **Builds expire.** Soloist builds stop working 90 days after they're built. Tesseract downloads the
  current one from Spotify when that happens.
- **Developer app.** Since February 2026, developer apps are limited to 5 users, and the app's owner
  needs Premium. Search returns at most 10 results, which is plenty for "play X".
- **Artists.** Soloist can't play an artist directly, so "play Taylor Swift" plays their most popular
  songs, then autoplay takes over.

## Local mode (a model on your computer)

Answers can come from a model running on your own computer instead of the cloud, for example Qwen
3.5 9B in LM Studio on a Mac. Speech-to-text, the voice and web search stay in the cloud.

1. LM Studio: download the model and load it. In the **Developer** tab, start the server and turn
   on **Serve on Local Network**. Turn the model's thinking off if it has a setting for it (a voice
   assistant can't wait for it to think).
2. On Tesseract: `tesseract local <your Mac's name>.local`. It finds LM Studio on port 1234, lists
   the loaded models, and switches over (add the model name if more than one is loaded).
3. Then *"Franklin, switch to local mode"* / *"use the cloud"*, or `tesseract local on` / `off`.

If the Mac is asleep or you take Tesseract somewhere else, it uses the cloud models for that answer
and tries the Mac again a minute later. `[local_llm] use_for = "all"` sends background jobs there
too (needs a model that's good with tools). Ollama works the same way (port 11434).

## Speed

Where the time goes after you stop talking, and what this code does about each part:

| Stage | What's done |
| --- | --- |
| End-of-speech + STT | Speech is told apart from fans, hum and TV noise by **Silero VAD**, a small neural model (webrtcvad, before, took a loud fan for a voice and kept listening). **Speculative**: 200 ms into a pause the audio goes to speech-to-text while it keeps listening. A finished sentence ends the turn after 750 ms of quiet, transcript already in hand; one that trails off ("set a timer for") gets up to 1.2 s. Once you've talked for 6 s, you're explaining something, so it waits for a 1.6 s pause (thinking pauses don't end it), up to 60 s in all. Speaking again throws the guess away. |
| Connection setup | The TLS connection to OpenRouter opens **when the wake word fires**, while you're still talking. HTTP/2 keep-alive is reused for STT, the LLM, and parallel TTS. |
| STT | `whisper-large-v3-turbo` on **Groq** first, sent as compact 16 kHz mono. |
| Common commands | Timers, the time and volume never reach an LLM (`commands.py`). |
| LLM | gpt-oss-120b routed to **Cerebras → Groq → SambaNova**; streamed; minimal reasoning; a short-answer prompt with the changing parts (time, timers) last so providers can cache the rest. |
| Tool results | Timer/alarm/volume confirmations, "On it." and web-search answers are spoken directly, **without a second LLM round**. Weather is one fast Open-Meteo call, not a web search. |
| TTS | Fixed phrases ("On it.", "Timer set for 10 minutes.", "Got it.") come from a **disk cache**, synthesized once. Otherwise the **first sentence is spoken while the LLM is still writing the rest**, the next one synthesizes while the current one plays, and leading silence is trimmed. |
| Playback | While a reply is still downloading, the mixer keeps only ~30 ms of silence queued in the sound card (it used to fill the whole ~128 ms buffer, which the first word then waited behind). |
| Wi-Fi | Power saving is disabled (it adds 100+ ms to every packet on the Zero 2 W). |
| On-device | Wake word ONNX models run directly on one core (no TensorFlow / scikit-learn stack). Audio is converted once and shared; the display sends only changed pixels. |

Every turn logs `heard ... (transcript ready X s after you stopped ...)` and `latency: first audio X s
after you stopped talking`, so `journalctl -u tesseract -f` shows where time goes. The `ask` and
`say` commands print timings too, for comparing models. Alternatives are listed next to each model in
`config.toml`.

## Developing on a laptop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/                   # wake-word parity, full-turn, agents, DSP, ...
.venv/bin/python -m tesseract run --sim             # laptop mic + speakers; screen + LEDs -> data/sim.png
```

In `--sim` you can type a request instead of speaking, or press `a` / `h` / `+` / `-` / `m` then
Enter for the buttons.

## Files

| | |
| --- | --- |
| `BRINGUP.md` | step-by-step first power-up to headless speaker |
| `CHANGELOG.md` | what changed, day by day (kept up to date by the AI working on the code) |
| `config.toml` | everything you'd want to change: **pins**, models, voice, timings, LEDs, agents |
| `tesseract/__main__.py` | the `tesseract` command: `run`, `doctor`, the test tools, `hermes`, `spotify`, `wifi`, ... |
| `tesseract/app.py`, `state.py` | the state machine (every event goes through one queue); what the screen and LEDs show |
| `tesseract/audio.py`, `playback.py`, `dsp.py` | I2S capture/playback, mixer, resampling |
| `tesseract/wakeword.py`, `vad.py` | "Franklin" detection, end-of-speech detection |
| `tesseract/commands.py` | answered on the device: timers, time, volume, mute, quiet mode, brightness, good night, music controls |
| `tesseract/cloud.py`, `speech.py`, `assistant.py` | OpenRouter, sentence-streamed TTS + phrase cache + captions, the fast model's prompt + tools and the straight-to-Hermes routing |
| `tesseract/routing_test.py` | `tesseract routing-test`: scores and times the fast model's tool choices |
| `tesseract/agents.py`, `thinker.py` | background jobs; external agents; the built-in thinking agent |
| `tesseract/organizer.py`, `weather.py` | the on-device to-do list/calendar/notes; Open-Meteo weather |
| `tesseract/settings.py`, `portal.py` | location + time zone, Wi-Fi (nmcli); the phone setup hotspot |
| `tesseract/leds.py`, `display.py`, `panel.py`, `buttons.py` | WS2812B animations, the UI, the SSD1333 driver, buttons |
| `tesseract/echo.py` | echo cancellation, so "Franklin" works over its own voice and music |
| `tesseract/spotify.py` | Spotify: Soloist (through a private PulseAudio) into the mixer, the Web API for "play X by Y" |
| `tesseract/splash.py` | the boot light: LEDs and screen a few seconds after power-on |
| `tesseract/alarms.py`, `sounds.py` | timers/alarms, synthesized chimes |
| `models/franklin.onnx` | the trained wake word (the other models are downloaded by `install.sh`) |
| `tools/wakeword/` | how `franklin.onnx` was made, and how to retrain it |
| `tests/` | `pytest tests/`: runs on a laptop, no hardware needed |
| `fonts/` | Inter (SIL Open Font License), the display's typeface |
| `boot/tesseract-audio.dts` | sound card device-tree overlay |
| `install.sh`, `systemd/` | setup and the service |
