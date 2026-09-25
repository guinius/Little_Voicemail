# Telegram migration: requirements and calling design

## Context

Little Voicemail's messaging today is entirely built on Signal, reached
through `signal-cli`'s JSON-RPC daemon (`src/signal_client.py`,
`src/signal_link.py`). That is deliberately "by the book" - a documented,
supported client, nothing automated that Signal doesn't expect a
third-party tool to do.

We explored adding **live calling** - a real conversation, not another
voice note - and confirmed neither Signal nor Telegram exposes calling
through any officially sanctioned third-party integration:

- **Signal**: `signal-cli` has no `CallMessage` support at all (tracked,
  unresolved for years in
  [AsamK/signal-cli#1735](https://github.com/AsamK/signal-cli/issues/1735)).
  Building it would mean cross-compiling RingRTC for an unsupported
  Linux-arm64 target *and* patching signal-cli's Java stack to relay call
  signalling - each independently a multi-week-plus undertaking.
- **Telegram**: the official Bot API (what messaging will use, see below)
  has no calling surface at all. The only way to place or join a call is
  [`pytgcalls`](https://github.com/pytgcalls/pytgcalls) driving a *personal*
  Telegram account over MTProto as if it were a real client - the same
  "userbot" pattern behind Telegram's past music-bot ban wave.

Given that, the decision (see prior conversation) is to move messaging to
Telegram's Bot API - fully sanctioned, no ban risk, matches today's Signal
behaviour - and add calling on top via the MTProto/pytgcalls route as a
**deliberately isolated, opt-in, accepted-risk feature**, not something
silently bundled into the same account or code path as messaging.

This document is the requirements checklist for that migration (so nothing
in the current Signal build gets dropped by accident) plus the full design
for the calling feature, which is now implemented in the device state
machine (`src/app.py`, `src/telegram_call_client.py`) against a Telegram
call client interface, ahead of the messaging-side port itself.

## Architecture decision

Two separate Telegram integrations, on purpose:

| | Messaging (voice notes) | Calling |
|---|---|---|
| Transport | Official Bot API | MTProto, via a personal account |
| Library | plain HTTPS (`sendVoice` / webhook or long-poll) | Pyrogram + pytgcalls |
| ToS status | Fully sanctioned | Accepted risk (see prior discussion) |
| Config | `telegram.bot_token` *(not yet added - see below)* | `telegram.calling.*` (added) |
| Default | N/A (required once ported) | **Off** (`calling.enabled: false`) |
| Module | `telegram_client.py` *(not yet written)* | `telegram_call_client.py` (written) |

Keeping them in separate modules, separate accounts/credentials, and a
separate config subtree means calling can be disabled, reworked, or ripped
out entirely without touching messaging at all - and a parent who wants
voice notes only, no live calling, never has to run the riskier half.

## Part 1 - Messaging parity checklist (Signal → Telegram)

Not yet built - this is the checklist for that work, so porting doesn't
silently drop something the Signal build already does today.

| # | Feature | Where it lives today | Telegram equivalent |
|---|---|---|---|
| 1 | Send a recording as a voice message | `signal_client.send_voice_note()` (`src/signal_client.py:307`) | Bot API `sendVoice` |
| 2 | Receive a voice message, queue it against a contact | `signal_client._handle_data_message()` (`:243`) | Bot API `getUpdates`/webhook, `Message.voice` |
| 3 | Cross-device read receipt clears the lamp | `signal_client._handle_envelope()` sync messages (`:221`) | **No Bot API equivalent** - see open questions |
| 4 | Device/account linking flow (QR code) | `signal_link.py`, web routes `/api/signal/link/*` (`src/web/app.py:534-582`), `/signal` page | Not needed the same way - a bot token is issued once by @BotFather and pasted in, no per-device linking. Needs a new, much simpler "paste your bot token" web flow. |
| 5 | Per-contact identifier | `contacts[].number` (Signal E.164 number) | `contacts[].telegram_id` (**added** this session - a contact's Telegram user id, which is also their private-chat id for Bot API purposes) |
| 6 | Resolve inbound sender → contact slot | `Config.slot_for_number()` | `Config.slot_for_telegram_id()` (**added** this session) |
| 7 | Contact nickname auto-fill from account contacts | `signal_client.list_contacts()` (`:333`) | No Bot API equivalent (a bot cannot list a user's contacts) - contacts likely have to be added by each contact messaging the bot once (`/start`) so their id can be captured |
| 8 | Web UI Signal tab (link/unlink/status/QR) | `src/web/templates/system.html`, `/signal` route | New "Telegram" section: paste bot token, show connection status, list of contacts who have messaged the bot |
| 9 | systemd service running the daemon | `services/signal-cli.service` | Not needed for messaging - Bot API is plain HTTPS, no local daemon (unlike calling, which does need its own long-running client - see `TelegramCallClient`) |
| 10 | Voice note audio format compatibility (iOS/Android/Desktop) | `audio.py` encodes AAC/M4A specifically because Signal iOS can't play Opus (`audio.py:1-16`) | Needs re-verifying against Telegram clients - Telegram's own voice messages are OGG/Opus, so this constraint may relax or invert; do not assume the AAC choice still applies without checking |
| 11 | Hardware/OS requirements tied to signal-cli's JVM | `HARDWARE.md`, `install.sh`, `tools/image/*.sh` | Bot API needs nothing special (plain HTTPS) - likely *simplifies* hardware requirements once Signal's JVM dependency for messaging is gone. Calling still needs Pyrogram/pytgcalls' native deps regardless. |
| 12 | README/SETUP/HARDWARE docs | `README.md`, `SETUP.md`, `HARDWARE.md` | Rewrite the Signal-specific sections |
| 13 | Tests | `tests/test_signal_client.py`, `tests/test_signal_link.py` | New `tests/test_telegram_client.py` mirroring their structure |

**Not started in this session** - deliberately out of scope for this pass,
which focused on the calling feature specifically (see the conversation
this doc followed from). Row 3 in particular needs a product decision, not
just an implementation, before it can be built - see below.

## Part 2 - Calling feature (implemented this session)

### Button semantics

- **Hold a contact button `CALL_HOLD_SECONDS` (3s)** while it is selected →
  places a call to that contact, instead of the button lifting to record a
  voice note. A press shorter than 3s behaves exactly as it does today
  (selects for a possible voice note).
- **Push-to-talk is the one way to end a call** - dialling out, ringing in
  unanswered, or connected - deliberately the single unambiguous control
  rather than also overloading the contact button's own press for some of
  those. (This reuses the PTT button rather than adding a new physical
  control, per the original request.)
- **An incoming call rings** (the configured ringtone, looped) **and
  blinks that contact's lamp** until: that same button is pressed
  (answers), it goes unanswered for `ring_timeout_seconds` (30s default -
  treated as missed), or push-to-talk cancels/declines it.

### The "wrong button pressed" proposal

This was the open design question: what happens if a *different* contact's
button is pressed by accident while a call is ringing in, dialling out, or
connected?

**Proposal (implemented): it is ignored outright - no state change, no
side effect, on any button but push-to-talk.** Concretely:

- While **ringing**, only the ringing contact's own button answers.
  Pressing any other button does nothing - it does not decline the call,
  does not redirect it to that other contact, does not silence the ring.
- While **dialling out or connected**, pressing *any* contact button
  (including the one you're already talking to) does nothing. Only
  push-to-talk ends the call.

**Why not something else:**

- *Hang up and switch to the new contact* - risky for exactly the
  scenario this is guarding against: six buttons and a PTT close together
  invites an accidental brush mid-conversation, and hanging up on a parent
  mid-sentence from a slip is a worse failure than a wrong press doing
  nothing.
- *Hang up on the wrong button, but don't call the new one* - still drops
  the call unnecessarily from an accidental touch.
- *A "busy" flash on the other lamps as feedback* - considered, not built:
  it adds real complexity (a second LED-override channel layered on top of
  the call's own pattern) for a case a young child is unlikely to need
  explained - the blinking/solid lamp already shows which button matters,
  and doing nothing on the others is self-explanatory by not looking like
  anything happened. Worth adding later if it turns out kids do get
  confused in practice.

This also matches the codebase's existing convention: `RECORDING`,
`SENDING`, and `PLAYING` already ignore any other contact press outright
(`_handle_contact_press`, `src/app.py`) rather than trying to be clever
about it - calling extends the same rule rather than inventing a new one.

### States and timing

Three new states added to `PhoneApp.State`: `CALLING` (dialling out,
unanswered), `RINGING` (inbound, unanswered), `IN_CALL` (connected, either
direction). Two timeouts, both configurable
(`telegram.calling.ring_timeout_seconds` / `dial_timeout_seconds`,
defaults 30s/45s): an unanswered incoming call is treated as missed; an
unanswered outgoing call gives up and returns to idle.

### LED patterns

- Dialling out: slow blink (0.8s period) - `CALLING_BLINK`.
- Ringing in: faster blink (0.4s period) - `RINGING_BLINK`. Deliberately
  faster than the ~1s pending-message blink, so a live call reads as more
  urgent than an unheard message at a glance, and distinct from both the
  dial-out pattern and the 1.5s send-in-progress blink.
- Connected: solid, the same visual language already used for "this
  contact is the one currently active" (a selection).

### Quiet time

An incoming call during quiet time is declined without ringing or
lighting anything - the same rule already applied to messages arriving
during quiet time. **Flagged as worth confirming rather than assumed
obvious**, because it's a real trade-off, not a mechanical application of
an existing rule: a message queues silently and appears on the button once
quiet time ends, but a call has no equivalent - a declined call is simply
gone, with nothing for the parent to fall back on except trying again
later. If that's wrong for how quiet time is meant to work, it needs
revisiting.

If quiet time *starts* while a call is already ringing, dialling, or
connected, that call is ended the same way an in-progress selection is
already dropped when quiet time begins.

### What happens on an unknown caller / a busy device

- A call from a Telegram id not matching any enabled contact is declined
  automatically (mirrors "voice message from unknown number; ignoring" for
  messaging).
- A call arriving while the device is doing anything other than sitting
  idle (recording, sending, playing back messages) is also declined
  automatically, rather than interrupting whatever the child is already
  doing.

### Files touched this session

- `src/telegram_call_client.py` (new) - the `TelegramCallClient`
  interface PhoneApp is written against, with a Pyrogram/pytgcalls-backed
  implementation. **Its actual outgoing-call, answer, and hang-up methods
  are stubbed with a `TelegramCallError` and marked `TODO(telegram-calling)`
  in the code** - pytgcalls' public documentation covers joining group
  voice chats in detail but its private 1:1 call API surface (which the
  library's own description lists as a feature) could not be confirmed
  from outside an environment with it actually installed. Everything else
  (session lifecycle, reconnect-with-backoff, the callback shape PhoneApp
  wires up to) is real and unit-testable via a fake; the four TODOs are
  the one piece that needs pinning down against whatever pytgcalls version
  actually gets installed, on real hardware, against a real account -
  not something to guess confidently from outside that environment.
- `src/app.py` - the call state machine: `_check_call_hold`,
  `_begin_outgoing_call`, `_run_outgoing_call`, `_check_call_timeouts`,
  `_on_incoming_call`, `_ring_loop`, `_answer_call`, `_on_call_connected`,
  `_end_call`, `_on_call_ended_remotely`, plus the `_handle_contact_press`
  / `_handle_ptt` / `_refresh_leds` changes described above. Also extracted
  `_check_quiet_time_transition()` out of `_tick_loop()` as its own testable
  method (previously inlined) - needed so calling's "quiet time starting
  ends an in-progress call" behaviour could be unit tested the same way
  the factory-reset combo and button test mode already are, rather than
  racing the real 0.25s tick.
- `src/config.py` - `telegram.calling.*` config subtree; `contacts[].telegram_id`;
  `Config.slot_for_telegram_id()`; **and a pre-existing bug fix**:
  `contact()`/`set_contact()` required a Signal `number` specifically to
  consider a contact enabled/reachable, which would have silently disabled
  every Telegram-only contact (no `number`, only `telegram_id`) the moment
  one exists. Now either identifier makes a contact reachable.
- `src/main.py` - constructs a `TelegramCallClient` and passes it to
  `PhoneApp` only when `telegram.calling.enabled` is true; `None`
  otherwise, which is fully inert (every call code path in `app.py` checks
  for `self.calls is None` first).
- `tests/test_calls.py` (new, 20 tests) - the state machine above,
  including the wrong-button-press behaviour explicitly.
- `requirements-calling.txt` (new) - `pyrogram`/`pytgcalls`, kept out of
  the main `requirements.txt` deliberately: calling is opt-in and every
  other device should not need to install its native dependencies.

### Config added

```jsonc
"telegram": {
  "calling": {
    "enabled": false,        // opt-in; off by default
    "api_id": "",
    "api_hash": "",
    "session_string": "",    // from a one-time MTProto login, not yet built (see below)
    "ring_timeout_seconds": 30,
    "dial_timeout_seconds": 45
  }
}
```
Each contact also gains `"telegram_id": ""`.

## Hardware implications

Dropping signal-cli (and with it, the JVM/libsignal requirement `HARDWARE.md`
"Choosing a board" documents as the hard floor) changes what's viable for
**messaging** - not for calling, which has its own, separate native-library
story. Not yet tested on real hardware; reasoning from the documented cause
of each existing exclusion, the same way `HARDWARE.md` does.

| Board | Today (Signal) | With Telegram messaging |
|---|---|---|
| Pico / Pico 2 W | ❌ Not a Linux computer at all | ❌ **Unchanged** - this was never about signal-cli; no Linux, no filesystem, no Flask/TLS regardless of messaging backend |
| Pi Zero v1.3 (no wireless) | ❌ Two blockers | ❌ **Still excluded** - no WiFi at all is a hardware fact, not a software one |
| **Pi Zero W** (1st gen, *with* wireless) | ❌ Excluded solely because ARMv6 can't run signal-cli's JVM ("Server VM is only supported on ARMv7+ VFP") | ✅ **Opens up, with one specific risk to verify - see below.** That blocker is JVM-specific; Telegram's Bot API is plain HTTPS through Flask, no JVM or native libsignal involved. |

### Firmed up: does `pip install -r requirements.txt` actually work on armv6l?

Checked rather than assumed, since this is the one place a wheel-availability
gap could silently turn "opens up" back into "doesn't work":

- **`cryptography` has no PyPI wheel for any 32-bit ARM platform** -
  confirmed directly against a recent release's file listing: wheels exist
  for `aarch64`/`x86_64`/macOS/Windows only. On stock PyPI that would mean
  building from source (Rust + OpenSSL headers) on install - slow and
  RAM-risky on a single ARM11 core with 512 MB.
- **That's not what actually happens on this board, though.** Raspberry Pi
  OS ships `/etc/pip.conf` pointing pip at
  [piwheels.org](https://www.piwheels.org) by default - a project that
  builds and hosts prebuilt wheels specifically for Pi hardware, including
  `cryptography` for armv6l. `install.sh`'s `pip install -r requirements.txt`
  has no `--index-url` override, so it already inherits this and should
  pull a prebuilt wheel with no script changes needed.
- **The genuine remaining risk**: piwheels doesn't always build natively for
  armv6l - for many packages (cryptography's build history among them) it
  builds once on armv7 hardware and relabels the wheel for armv6l "with a
  few exceptions" where that doesn't hold. This isn't hypothetical for this
  exact package: `cryptography` 36.0.1 shipped an armv6l wheel that crashed
  with `Illegal Instruction` specifically on Pi Zero hardware, fixed by
  pinning back a patch version. So: generally works, has broken before for
  exactly this reason on exactly this chip - whatever version actually gets
  pinned needs a smoke test on real armv6l hardware before shipping, not
  assumed safe because a wheel exists.
- **Everything else in `requirements.txt`** (Flask, Werkzeug, cheroot,
  smbus2, segno) is pure Python with no compiled extensions - confirmed no
  architecture risk there.
- **Forward-looking, for the not-yet-built Telegram messaging client**:
  avoid a bot framework that pulls in `aiohttp` (its speedups are a C
  extension - a fresh instance of the same risk class) and hand-roll the
  Bot API calls on stdlib `urllib`/`http.client` instead, the same way
  `signal_client.py` already hand-rolls its own transport rather than
  depending on a library. Avoids reintroducing this exact question for a
  dependency that isn't even needed yet.

**Calling does not follow the same logic and likely still needs arm64.**
`pytgcalls`/`tgcalls` is a native C++ extension doing continuous real-time
audio (jitter buffer, opus, echo cancellation) - a much heavier load than
messaging's record-then-batch-encode, and this project has already hit
exactly this wall once before: libsignal's native library is only built
for arm64 (see git history, "Supply libsignal's native library for
arm64"). Whether pytgcalls ships an ARMv6 binary at all, and whether a
single ARM11 core could run real-time audio if it did, are both open and
probably-negative questions. Net: a messaging-only build could plausibly
target a Pi Zero W (1st gen); a build with calling turned on should still
assume arm64 (Zero 2 W or better), same as today.

## Open questions / follow-up work

1. **Read receipts (parity row 3).** Telegram's Bot API has nothing
   equivalent to Signal's sync-message read receipts - a bot can't see
   whether a human read a message elsewhere. Needs a product decision:
   drop the "lamp clears when a parent reads it on their own phone"
   behaviour, or find another signal for it (e.g. the parent's own
   Telegram client sending anything back counts as "seen").
2. **Getting `telegram.calling.session_string` populated.** This session
   built the config field and the client that consumes it, but not the
   *login flow* that produces it (an MTProto login is interactive - phone
   number, then a code sent to that number, then the session string it
   yields). That's real work, likely a `signal_link.py`-shaped module and
   web UI flow, not yet built.
3. **The four `TODO(telegram-calling)` stubs in `telegram_call_client.py`.**
   Need pytgcalls actually installed against a pinned version to verify
   the private-call API surface before calling is functionally real
   end-to-end. Right now a hold-to-call attempt fails gracefully (goes to
   `CALLING` then immediately back to `IDLE` with a recorded error) rather
   than doing anything harmful - that's deliberate, not an oversight, but
   it means the feature is not yet actually able to place a call.
4. **Whether declining a call during quiet time is the right call** - see
   the "Quiet time" section above.
5. **The messaging parity checklist itself (Part 1)** is not yet built -
   this document is its requirements list, per the original ask, not its
   implementation.
