<div align="center">

# pa-call

[![License](https://img.shields.io/badge/LICENSE-MIT-5C9E31?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/PYTHON-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![Built by](https://img.shields.io/badge/BUILT%20BY-JGALEA-8A2BE2?style=for-the-badge&logo=github&logoColor=white)](https://github.com/jgalea)

**An AI assistant that makes phone calls for you, from your own number.**

</div>

Restaurant bookings, moving an appointment, chasing a reply, asking a shop whether something's in stock. You give it a number and a brief, it calls from your mobile number, says it's your AI assistant, handles the conversation inside the limits you set, and hands you back the outcome and a transcript.

It's for your own errands, one call at a time. It isn't a dialler.

## How it works

Twilio places the call with your mobile as the verified caller ID, so the other side sees your number and any callback rings your phone. Twilio's ConversationRelay does the speech-to-text, the voice and barge-in, and streams text over a websocket. `pa_call.py` serves that websocket locally through a Cloudflare quick tunnel and streams Claude's replies into it.

The model writes `[KEYS:12]` to press phone-menu keys and `[END]` to hang up after its goodbye. Both are stripped before anything is spoken.

## Install

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/jgalea/pa-call && cd pa-call
uv sync
brew install cloudflared
```

Run it with `uv run python pa_call.py ...`, or put a small wrapper on your PATH.

## Setup

1. A Twilio account, upgraded from trial. Trial accounts can only call verified numbers and play a trial notice.
2. Credentials in the macOS Keychain, typed at a hidden prompt:
   ```
   security add-generic-password -a pa-call -s pa-call-twilio-account-sid -U -w
   security add-generic-password -a pa-call -s pa-call-twilio-auth-token -U -w
   security add-generic-password -a pa-call -s claude-anthropic-api -U -w
   ```
   `ANTHROPIC_API_KEY` in the environment overrides the last one.
3. `~/.config/pa-call/config.json`:
   ```json
   {
     "owner": "Ana Silva",
     "from": "+3519XXXXXXXX",
     "callback": "+3519XXXXXXXX",
     "language": "pt-PT"
   }
   ```
   Optional: `model`, `calls_dir`, and per-language voice overrides such as `"tts": {"pt-PT": {"provider": "Google", "voice": "..."}}`.
4. `pa-call verify-caller-id`. Twilio rings your mobile; type the code it prints.
5. `pa-call check`.

## Use

```
pa-call call +3512XXXXXXXX --callee "Restaurante Exemplo" \
  --brief "Table for 4 this Saturday at 20:00 under Ana Silva. 19:30 to 21:00 is fine. Ask for a high chair."

pa-call call +349XXXXXXXX --callee "Clínica Dental" --brief @brief.txt --lang es-ES --max-minutes 5
pa-call call ... --dry-run    # write the opening line without dialling
```

It shows the opening line and asks before dialling (`--yes` skips that). Each call writes `transcript.txt`, `transcript.json` and `summary.md` to `calls_dir` (default `~/.local/share/pa-call/calls`). The summary's first line is `OUTCOME: DONE`, `PARTIAL`, `FAILED`, `NO_ANSWER` or `VOICEMAIL`.

Default voices are Amazon neural: `pt-PT` Inês, `es-ES` Lucía, `en-GB` Amy. Transcription is Deepgram.

## Rules it follows

- The first thing it says is that it's an AI assistant calling on your behalf. The EU AI Act requires that disclosure.
- It only agrees to what the brief allows. Anything else (deposits, fees, other dates, contracts) goes back to you.
- It never gives card numbers, bank details, passwords or document numbers.
- If they want to speak to you, it offers your callback number.

Don't use it for cold calls or marketing. Automated marketing calls need prior consent in most of Europe, and in Portugal calling businesses is only allowed if they haven't opted out (Lei 41/2004, art. 13-A).

## Test

`offline_test.py` runs a scripted call through a real tunnel with a fake brain: opening line, phone menu, read-back, hang-up. No Twilio or Claude needed.

```
uv run python offline_test.py
```
