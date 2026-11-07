# Bring-up: from a soldered board to a headless speaker

This takes about 1.5 hours, most of it waiting on installs. It starts from a wired board and a blank
microSD card.

**You need:**
- the microSD card (16 GB or more) and a card reader for your Mac
- a 5 V / 3 A USB-C power supply for the board's power input (J2)
- a 2.4 GHz Wi-Fi network (the Pi Zero 2 W has no 5 GHz radio)
- an [OpenRouter](https://openrouter.ai/keys) API key with a few dollars of credit
- a multimeter

Every `ssh` command below uses `pi` as the username. Replace it with whatever you choose in step 2.

---

## 1. Check before power

Do this with everything plugged in but **no power connected**. Use the meter's continuity (beep)
mode.

| Probe | Expect | If not |
| --- | --- | --- |
| Pi header pin 2 (5V) ↔ pin 6 (GND) | no steady beep | short on the 5 V rail; find it before powering |
| Pi pin 1 (3V3) ↔ pin 6 (GND) | no steady beep | short on the 3.3 V rail |
| Pi pin 2 (5V) ↔ pin 1 (3V3) | no steady beep | almost always the old `J1` pin-2 wire still touching display VDD. Cut it or pull it out of the housing |
| display pin 2 (VDD) ↔ Pi pin 17 | beep | the display's 3.3 V wire is missing |
| strip +5V / DIN / GND ↔ `J5` pins 1 / 2 / 3 | beep, in that order | swap the wires. Reversed power kills the first LED |
| either speaker wire ↔ GND | no beep | the amp's outputs must never touch ground |

A short beep that stops is just capacitors charging. Only a steady beep near 0 Ω means a short.

Also check that the LED wire goes to the strip's **DIN** end; the arrows on the strip point away
from it.

## 2. Flash the microSD card

1. Install Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/),
   or run `brew install --cask raspberry-pi-imager`.
2. **Device:** Raspberry Pi Zero 2 W.
   **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**. It must be 64-bit, because
   onnxruntime has no 32-bit builds.
   **Storage:** your microSD card.
3. In the customisation step, set:
   - **hostname:** `tesseract`
   - **time zone:** yours (the clock uses it)
   - **username and password:** pick them and remember them
   - **Wi-Fi:** network name, password and **country** (Wi-Fi stays off without a country)
   - **SSH:** enabled, with password authentication
4. Write the card. This erases it. Eject it when Imager says it's done.

## 3. First power-up, with the peripherals unplugged

Leaving the display, LEDs and speaker off means a wiring mistake can't damage them while you install
software.

1. At the PCB, unplug `J1` and `J8` (display), `J5` (LEDs) and `J3` (speaker).
2. Put the microSD card in the Pi.
3. Plug the USB-C supply into the board's power input (`J2`). **Never also plug in the Pi's own
   "PWR IN" micro-USB port.** Two supplies on the same 5 V rail will fight each other.
4. The Pi's green LED should start flickering within a few seconds. Measure against GND:
   - `J4` pin 1: 4.8–5.25 V
   - `J4` pin 2: 3.25–3.35 V
5. If nothing lights up and you read 0 V, the USB-C breakout probably lacks the 5.1 kΩ CC resistors
   that USB-C chargers need before they turn on. Try a USB-A-to-USB-C cable, which doesn't need them.
6. Leave it for about **3 minutes**. The first boot expands the filesystem, applies your settings, and
   reboots once on its own.

## 4. Log in over SSH

From Terminal on your Mac:

```bash
ssh pi@tesseract.local
```

Type `yes` to accept the fingerprint, then enter your password.

If you get `Could not resolve hostname`:
- Wait 2 more minutes and try again.
- Otherwise, find "tesseract" in your router's client list and use its IP address, as in
  `ssh pi@192.168.1.42`.
- If it never joins the network, the Wi-Fi was probably 5 GHz-only or the country wasn't set.
  Re-flash the card.

Then update the OS. This takes 10–20 minutes on a Zero 2 W:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

## 5. Copy the firmware over

`code/` isn't committed to GitHub yet, so `git clone` would fetch the old prototype. Copy it straight
from your Mac instead. Run this on the Mac, not the Pi:

```bash
cd ~/CS/Hardware/"Personal Projects"/tesseract
rsync -av --exclude venv --exclude .venv --exclude data --exclude .env --exclude __pycache__ --exclude .pytest_cache \
    code pi@tesseract.local:tesseract/
```

This creates `~/tesseract/code` on the Pi. Run the same command again whenever you change code on the
Mac.

## 6. Install

```bash
ssh pi@tesseract.local
cd ~/tesseract/code
sudo ./install.sh
```

- This takes **15–30 minutes**, because a few packages compile on the Pi.
- It asks for your OpenRouter key; paste it in.
- It ends by printing **Done** and a short checklist.
- It's safe to re-run if it fails partway, for example on a network hiccup.
- `Needs 64-bit Raspberry Pi OS` means you flashed the 32-bit image. Re-flash with the 64-bit one.

Then shut down cleanly:

```bash
sudo poweroff
```

## 7. Plug the peripherals in

1. Wait for the green LED to stay off, then unplug the power.
2. Plug in `J1`, `J8`, `J5` and `J3`. Power on and wait about a minute, then SSH back in.
3. The installer set Tesseract up as a service that starts at boot. Only one program can use the sound
   card, LEDs and display at a time, so turn the service off while you test:

```bash
sudo systemctl disable --now tesseract
```

The installer also added a `tesseract` command that works from any folder. It runs as root by
itself, since the LEDs, Wi-Fi and the clock need that, so there's no need to type `sudo`.

## 8. System check

```bash
tesseract doctor
```

Expect:
- `── pins` → `ok`
- `── boot config` → four lines ending in `ok`
- `── sound card` → a card named `sndrpigooglevoi`, then `-> using hw:CARD=sndrpigooglevoi,DEV=0`
- `── OpenRouter` → `key ok`
- `── wake word` → `listening for "Franklin": ok` and `…/models/franklin.onnx: ok` (both detectors)
- `all good` at the end

If the sound card is missing, check these, then re-run `sudo ./install.sh` and reboot:
- `ls /boot/firmware/overlays/tesseract-audio.dtbo` should list the file.
- `grep tesseract /boot/firmware/config.txt` should show the managed block.
- `dmesg | grep -i voicehat` shows the driver's error, if there is one.

## 9. Test each part

Each test runs on its own and stops by itself, except `buttons` and `wake`, which you stop with
Ctrl-C. After editing `config.toml`, just re-run the test; nothing needs restarting yet.

### Display

```bash
tesseract display
```

It cycles every screen for about 35 seconds: clock, Listening, Thinking, and so on. The LEDs run
too.

| Symptom | Fix |
| --- | --- |
| stays black | check that `ls /dev/spidev0.0` exists; measure 3.3 V on display pin 2; check the J1/J8 cable order (J1 pin *n* → display pin *n*, J8 pin *n* → display pin *n*+10) |
| snow or garbage | `[display] spi_hz = 4_000_000`, and keep the SPI wires short |
| red and blue swapped | `[display] color_order = "BGR"` |
| upside down or sideways | `[display] rotation = 180` (or 90 / 270) |
| mirrored | `[display] mirror = "horizontal"` (or `"vertical"`, `""`) |
| too bright or dim | `tesseract brightness 6` (0-10), or say "Franklin, dim the screen" |

### LEDs

```bash
tesseract leds
```

You should see a rainbow, then cyan, blue, violet, gold, white, red and orange.

| Symptom | Fix |
| --- | --- |
| nothing lights | 5 V on `J5` pin 1? wire from header pin 32 to `J6` pin 7? wire on the strip's DIN end? |
| red and green swapped | `[leds] strip_type = "RGB"` |
| only some LEDs light | `[leds] count` = the number of LEDs on your strip |
| first LED flickers or shows random colors | the Pi's 3.3 V data signal is marginal for 5 V WS2812Bs; add a 74AHCT125 level shifter on LED_DIN |

### Buttons

```bash
tesseract buttons
```

Press each button. You should get one `pressed` and one `released` line per press, with the right
name. If a name is wrong, two wires are swapped. You can fix that by swapping the numbers in
`[pins]` instead of resoldering. Stop with Ctrl-C.

### Mic and speaker (no internet needed)

```bash
tesseract record 4
```

Talk at arm's length for 4 seconds. The level bar should move. It then prints a loudness in dBFS and
plays the recording back through the speaker.

- **Aim for −35 to −20 dBFS** at arm's length. Change `[audio] mic_gain_db` (default 20) by the
  difference: reading −42 means raise it by about 10.
- When you talk loudly up close, the `peak` value must stay under about 30000. Clipped audio ruins
  wake-word detection, so too much gain is worse than too little.

| Symptom | Fix |
| --- | --- |
| bar flat, about −90 dBFS | try `[audio] mic_channel = 1`; check the mic wires (SD: header pin 38 → `J7`-7, WS: pin 35 → `J6`-6, SCK: pin 12 → `J7`-1) and 3.3 V on `J4` pin 2 |
| no sound on playback | speaker on `J3`; amp data wire (header pin 40 → `J6`-1); 5 V on the board |
| too quiet / distorted | `[audio] volume` (0–10) / lower `[audio] max_volume_db` to -6 |

### Cloud: speech-to-text, both models and text-to-speech

First tell it where it is; the weather needs that. This also sets the clock's time zone:

```bash
tesseract location "Austin, Texas"
tesseract say "Hello, I'm Franklin."
tesseract ask "What's the weather tomorrow?"
tesseract ask "Add a dentist appointment next Tuesday at 3pm to my calendar"
```

- `say` should print a first-audio time of about 0.5–1.5 s.
- The weather question should show `[Checking the weather]`, then a one-sentence answer.
- The calendar request goes through the fast model, which says "On it." (or a variation), to the thinking model.
  The background job prints its progress, then its result. If it asks a question, type the answer
  at the `>` prompt.

- `API key rejected`: fix `OPENROUTER_API_KEY` in `.env`.
- `Out of OpenRouter credits`: add credit at openrouter.ai.

### Wake word

```bash
tesseract wake
```

- Stand where you'll normally use it, 1–3 m away. Say "Franklin" at a normal volume, several times.
  Each detection prints `*** WAKE ***`.
- Then talk normally for a minute, including "Frank" and "frankly". It should stay quiet.
- Stop with Ctrl-C.

| Symptom | Fix |
| --- | --- |
| misses | first re-check the mic level above; then `[wake] threshold = 0.5` (the trained model) or `keyword_threshold = 0.25` (sherpa) |
| wakes on its own | `[wake] threshold = 0.7` or `frames = 3` (the trained model), or `keyword_threshold = 0.35`, up to 0.4 (sherpa) |

## 10. Full run in the foreground

```bash
tesseract run -v
```

Wait for `ready: say "Franklin"`, which takes 10–20 seconds. Then try:
- "Franklin, what time is it?" This is answered on the device; look for `answered on the device` in
  the log.
- "Franklin, set a timer for one minute". The display counts down, and a dim warm bar drains on the
  LEDs. When it rings, press any button.
- "Franklin, add the robotics club meeting this Friday to my calendar". You should hear "On it." (or a variation) and
  see the violet progress bar. When it asks what time, answer, and it reads out the result.
- Each turn logs `latency: first audio X s after you stopped talking`. With a timer or "what time is
  it" that's mostly the speech-to-text round trip; for a question the model answers, the target is
  about a second.
- **Action** button: tap to talk, hold to cancel and forget the conversation.
- **Mute**: the LEDs glow red. **Volume**: a white bar.

Stop with Ctrl-C.

## 11. Go headless

```bash
sudo systemctl enable --now tesseract
journalctl -u tesseract -f          # wait for "ready", then Ctrl-C
sudo reboot
```

About a minute after the reboot, the clock should appear and it should answer to "Franklin" with no
SSH session open. That's it: it's a standalone speaker now.

---

## Taking it somewhere new (a demo at home)

**Before you leave**, while it's still online, save the Wi-Fi it'll find there:

```bash
tesseract wifi add "Home Wi-Fi"      # asks for the password; nothing switches now
tesseract wifi                       # what's saved, and what's in range
```

It joins the strongest saved network it can see, at any boot. You can save networks that aren't in
range; nothing changes until it sees them.

**Phone hotspot** (a good backup for demos anywhere). The Pi Zero 2 W only does 2.4 GHz Wi-Fi:

1. iPhone: Settings → Personal Hotspot → turn on **Allow Others to Join** and **Maximize
   Compatibility** (without it, newer iPhones use 5 GHz and Tesseract can't see the hotspot).
   Android: set the hotspot's band to 2.4 GHz.
2. Leave the Personal Hotspot screen open while you add it, so the hotspot is visible, then:
   ```bash
   tesseract wifi add "Charlie's iPhone"     # your phone's name; asks for the hotspot password
   ```
   iPhones name the hotspot with a curly apostrophe (’). If the hotspot is on while you add it,
   `tesseract wifi add` finds the real name for you; it says "Using the network in range called …".
3. At the demo, turn the hotspot on (and open that screen, since iPhones hide an idle hotspot)
   **before** plugging Tesseract in.

If it won't join, run `tesseract wifi` with the Personal Hotspot screen open. Not listed under "in
range" means Tesseract can't see it: Maximize Compatibility is off (5 GHz), or the iPhone stopped
advertising it. Listed but not joining means the password or name is off: `tesseract wifi forget
"<name>"`, then add it again while it's in range.

**Campus Wi-Fi (Texas A&M).** Two ways; the first keeps your NetID password off the device:

- **TAMU_IoT (recommended).** Run `tesseract wifi` and note the **MAC address** line. On campus, with
  your phone or laptop on TAMU_WiFi, sign in at <https://iotregistration.net.tamu.edu> (NetID + Duo)
  and add a device with that MAC address. Then save the network. It's registered by MAC address,
  so press Enter when it asks for a password:
  ```bash
  tesseract wifi add TAMU_IoT
  ```
- **TAMU_WiFi with your NetID.** Use your NetID in lower case; it asks for your password:
  ```bash
  tesseract wifi add TAMU_WiFi --user yournetid
  ```
  The password is stored in NetworkManager's root-only file on the SD card. Redo this whenever
  your NetID password changes. eduroam works the same way with `--user yournetid@tamu.edu`.

You can save all of these at once. With several in range, it picks the strongest. The phone setup
page below also has a username field for school Wi-Fi.

**When you get there**, plug it in. If it can't find any network it knows within about 90 s of booting, it
opens Wi-Fi setup by itself:

1. The screen shows a QR code, and the LEDs show the amber "needs you" light.
2. Point your phone's camera at the QR code and join **Tesseract-Setup**. The password is on the
   screen if you'd rather type it.
3. The setup page pops up by itself; if it doesn't, open `10.42.0.1` in the browser.
4. Pick the network, type its password and where you are, then tap **Connect**.
5. Tesseract leaves its hotspot, joins your network and says "Connected to …". With a wrong
   password, setup comes back with the error.

You can open setup any time too: hold **both volume buttons for 3 seconds**, say "Franklin, set up
Wi-Fi", or run `tesseract wifi setup`. Do the same again to cancel it. If nobody uses it for 5
minutes, it closes and tries the networks it knows again.

**Location:** just say "Franklin, I'm in Chicago." It looks the place up, saves it to `config.toml`,
and moves the clock to that time zone. Or run `tesseract location "Chicago, Illinois"`.
`tesseract setup` asks for the location and any Wi-Fi networks, one by one.

### Stuck in Wi-Fi setup? Get in through its own hotspot

When Tesseract is showing the setup QR code, it is its own Wi-Fi network, and you can SSH in over it:

1. On the Mac, join **Tesseract-Setup** with the password on its screen.
2. `ssh pi@10.42.0.1`, then right away `sudo systemctl stop tesseract`. The hotspot stays up, and
   setup no longer closes it after 5 idle minutes.
3. From the Mac (still on Tesseract-Setup), copy code over as usual, with `10.42.0.1` in place of
   `tesseract.local`.
4. `sudo reboot`. It tries the networks it knows, and opens setup again if none are around.

While you're in, `journalctl -u tesseract -b | grep -iE "portal|wifi"` shows why a join failed, and
`journalctl -u NetworkManager -b | tail -40` shows NetworkManager's side.

## Turning it off and on (no SSH needed)

Once step 11 is done, it's a normal appliance. Plug it in and the clock appears about a minute later,
ready for "Franklin". You don't need a laptop.

**To turn it off**, do one of these, then unplug it once the screen goes dark:

- say *"Franklin, shut down"*
- hold the **action button for 5 seconds** (at 1 second it cancels whatever it's doing, as before;
  keep holding)
- over SSH: `sudo poweroff`

The screen says "Shutting down", the LEDs fade out, and the screen goes dark a few seconds before
the Pi stops. Give it about 5 more seconds, then unplug it. *"Franklin, restart"* reboots it, which is
handy if something gets stuck in a demo.

**To turn it back on**, plug it in again. The Pi has no power button: plugging it in is the only way.
The LEDs and screen come on a few seconds later (the boot light), and it's ready about 20-30 s after
that. To see how long each part of boot takes: `systemd-analyze` and
`journalctl -b -u tesseract-splash -o short-monotonic | head -3` (seconds since the kernel started;
add ~2 s for the Pi's firmware).

## Afterwards

| To | Do |
| --- | --- |
| change a setting | edit `~/tesseract/code/config.toml`, then `sudo systemctl restart tesseract` |
| change the location or Wi-Fi | see "Taking it somewhere new" above |
| change the screen brightness | `tesseract brightness 6` (0-10, `up`, `down`, `night`, `morning`), or ask Franklin; works with it running |
| update the code | re-run the `rsync` from step 5, then `sudo systemctl restart tesseract`. Re-run `sudo ./install.sh` if `install.sh`, `requirements.txt` or `boot/` changed |
| hear crackling or pops | `sudo systemctl stop tesseract`, then `tesseract speaker 6`: clean test tones, sound-card dropouts and the power supply, with what each result means |
| check that you can interrupt it | `sudo systemctl stop tesseract`, then `tesseract interrupt-test`: it talks for ~30 s while you say "Franklin", shows whether it heard you, how much of its own voice it removed, and any audio dropouts, and saves the recording to `data/interrupt-test.npz` |
| see what it's doing | `journalctl -u tesseract -f`, or `journalctl -u tesseract -b` since the last boot |
| connect your own agent or Spotify | `tesseract hermes <address>` / `tesseract spotify setup`: see [README.md](README.md#agents-hermes-and-friends) |
| run a test command again | `sudo systemctl stop tesseract` first, then `sudo systemctl start tesseract` after (`location`, `wifi` and `setup` work with it running) |
| unplug it | say "Franklin, shut down" or hold the action button 5 s first (see above); pulling the plug usually works but can corrupt the SD card |
