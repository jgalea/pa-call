import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

from websockets.asyncio.server import serve
from twilio.rest import Client

from brain import END, KEYS_RE, Brain, keychain

CONFIG = Path.home() / ".config" / "pa-call" / "config.json"
PORT = 8765
FINAL = {"completed", "busy", "no-answer", "failed", "canceled"}
VOICES = {
    "pt-PT": ("Amazon", "Ines-Neural"),
    "es-ES": ("Amazon", "Lucia-Neural"),
    "en-GB": ("Amazon", "Amy-Neural"),
}


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"no config at {CONFIG}; see README")
    return json.loads(CONFIG.read_text())


def twilio() -> Client:
    return Client(keychain("pa-call-twilio-account-sid"), keychain("pa-call-twilio-auth-token"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Session:
    def __init__(self, brain: Brain, opening: str):
        self.brain = brain
        self.opening = opening
        self.transcript: list[dict] = []
        self.ws = None
        self.task: asyncio.Task | None = None
        self.parts: list[str] = []
        self.greeted = False
        self.done = asyncio.Event()
        self.ended_by = "call ended"

    def say(self, who: str, text: str):
        self.transcript.append({"t": round(time.time(), 1), "who": who, "text": text})
        log(f"{who:>4}:", text)

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))

    async def handle(self, ws):
        self.ws = ws
        greeter = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "setup":
                    log("connected", msg.get("callSid"))
                    greeter = asyncio.create_task(self.greet_if_silent())
                elif kind == "prompt":
                    self.parts.append(msg.get("voicePrompt", ""))
                    if msg.get("last", True):
                        heard = " ".join(self.parts).strip()
                        self.parts = []
                        self.say("them", heard)
                        await self.cancel()
                        self.task = asyncio.create_task(self.respond(heard))
                elif kind == "interrupt":
                    partial = msg.get("utteranceUntilInterrupt", "")
                    if self.task and not self.task.done():
                        self.task.cancel()
                    self.brain.record(partial + " [cut off]", replace=True)
                    self.say("pa", f"(interrupted after: {partial})")
                elif kind == "dtmf":
                    self.brain.note(f"[they pressed {msg.get('digit')}]")
                elif kind == "error":
                    log("relay error:", msg.get("description"))
        finally:
            if greeter:
                greeter.cancel()
            self.done.set()

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def greet_if_silent(self):
        await asyncio.sleep(4)
        if not self.greeted:
            self.greeted = True
            self.brain.messages = [{"role": "user", "content": "[The call was answered but nobody has spoken yet.]"}]
            await self.speak_fixed(self.opening)

    async def speak_fixed(self, text: str):
        await self.send({"type": "text", "token": text, "last": True})
        self.brain.record(text)
        self.say("pa", text)

    async def respond(self, heard: str):
        # First turn: use the pre-generated opening unless what they said needs a real answer (a menu, a question).
        if not self.greeted:
            self.greeted = True
            if len(heard.split()) <= 12 and "?" not in heard:
                self.brain.messages = [{"role": "user", "content": f"[The call was answered. They said: {heard}]"}]
                await self.speak_fixed(self.opening)
                return
            heard = f"[The call was answered. They said: {heard}]"

        spoken, hold, finish = "", "", False

        async def emit(t: str):
            nonlocal spoken
            if t:
                spoken += t
                await self.send({"type": "text", "token": t, "last": False})

        async for chunk in self.brain.stream(heard):
            hold += chunk
            while True:
                i = hold.find("[")
                if i == -1:
                    await emit(hold)
                    hold = ""
                    break
                await emit(hold[:i])
                hold = hold[i:]
                j = hold.find("]")
                if j == -1:
                    break
                tag, hold = hold[: j + 1], hold[j + 1 :]
                if tag == END:
                    finish = True
                elif m := KEYS_RE.fullmatch(tag):
                    log("keys:", m.group(1))
                    await self.send({"type": "sendDigits", "digits": m.group(1)})
        if hold and not hold.startswith("["):
            await emit(hold)
        await self.send({"type": "text", "token": "", "last": True})
        self.brain.record(spoken.strip())
        if spoken.strip():
            self.say("pa", spoken.strip())
        if finish:
            self.ended_by = "assistant finished the call"
            # Let the goodbye play out before hanging up.
            await asyncio.sleep(1.5 + len(spoken.split()) / 2.5)
            await self.send({"type": "end", "handoffData": json.dumps({"reason": "done"})})


def start_tunnel() -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        line = proc.stdout.readline()
        if m := re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line):
            return proc, m.group(0).replace("https://", "")
    proc.kill()
    sys.exit("cloudflared did not produce a tunnel URL")


async def wait_reachable(host: str):
    # Resolve through Cloudflare DNS: probing the fresh hostname via the local resolver too early caches NXDOMAIN.
    for _ in range(45):
        await asyncio.sleep(1)
        ip = subprocess.run(["dig", "+short", host, "@one.one.one.one"], capture_output=True, text=True).stdout.split()
        ip = next((x for x in ip if re.fullmatch(r"[\d.]+", x)), None)
        if not ip:
            continue
        # Async subprocess: the websocket server answering this probe lives on the same event loop.
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}",
            "--resolve", f"{host}:443:{ip}", f"https://{host}/", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        if out.decode().strip() == "426":  # the websocket server answering a plain GET
            return
    sys.exit(f"tunnel {host} never became reachable")


def twiml(host: str, language: str, provider: str, voice: str) -> str:
    return (
        "<Response><Connect><ConversationRelay "
        f"url={quoteattr('wss://' + host + '/')} language={quoteattr(language)} "
        f"ttsProvider={quoteattr(provider)} voice={quoteattr(voice)} "
        'transcriptionProvider="Deepgram" dtmfDetection="true" interruptible="any" />'
        "</Connect></Response>"
    )


async def place_call(args, cfg):
    language = args.lang or cfg.get("language", "pt-PT")
    provider, voice = VOICES.get(language, ("ElevenLabs", "UgBBYS2sOqTuMpoF3BR0"))
    provider = args.tts_provider or cfg.get("tts", {}).get(language, {}).get("provider", provider)
    voice = args.voice or cfg.get("tts", {}).get(language, {}).get("voice", voice)
    brief = Path(args.brief[1:]).read_text() if args.brief.startswith("@") else args.brief

    brain = Brain(brief, args.callee, cfg["owner"], cfg["callback"], language, model=args.model or cfg.get("model") or "claude-opus-5-5")
    opening = "".join([c async for c in brain.stream("[The call has just been answered. Say your opening line.]")])
    opening = opening.replace(END, "").strip()
    brain.messages = []

    print(f"\nCalling {args.callee} at {args.to} from {cfg['from']}")
    print(f"Language {language}, voice {provider}/{voice}, cap {args.max_minutes} min")
    print(f"Opening: {opening}\n")
    if args.dry_run:
        return
    if not args.yes and input("Place the call? [y/N] ").strip().lower() != "y":
        sys.exit("cancelled")

    session = Session(brain, opening)
    tunnel = None
    async with serve(session.handle, "127.0.0.1", PORT):
        try:
            tunnel, host = start_tunnel()
            log("tunnel", host)
            await wait_reachable(host)
            client = twilio()
            call = client.calls.create(
                to=args.to, from_=cfg["from"], twiml=twiml(host, language, provider, voice),
                time_limit=int(args.max_minutes * 60),
            )
            log("dialling", call.sid)
            status = call.status
            while status not in FINAL:
                await asyncio.sleep(2)
                status = client.calls(call.sid).fetch().status
            log("call status", status)
            if status != "completed":
                session.ended_by = f"call {status}"
            elif not session.transcript:
                session.ended_by = "answered but nothing was said (possibly voicemail or a hang-up)"
        finally:
            if tunnel:
                tunnel.kill()

    text = "\n".join(f"{e['who']}: {e['text']}" for e in session.transcript) or "(no conversation)"
    summary = await brain.summary(session.ended_by, text)
    slug = re.sub(r"[^a-z0-9]+", "-", args.callee.lower()).strip("-")[:40]
    out = Path(cfg.get("calls_dir", "~/.local/share/pa-call/calls")).expanduser() / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "transcript.json").write_text(json.dumps(
        {"to": args.to, "callee": args.callee, "brief": brief, "language": language,
         "voice": f"{provider}/{voice}", "ended_by": session.ended_by, "status": status,
         "turns": session.transcript}, ensure_ascii=False, indent=2))
    (out / "transcript.txt").write_text(text + "\n")
    (out / "summary.md").write_text(summary + "\n")
    print(f"\n{summary}\n\nSaved to {out}")


def verify_caller_id(args, cfg):
    number = args.number or cfg["from"]
    v = twilio().validation_requests.create(phone_number=number, friendly_name=cfg["owner"])
    print(f"Twilio is calling {number} now. When asked, type this code on the keypad: {v.validation_code}")


def check(args, cfg):
    ok = True
    for item in ("pa-call-twilio-account-sid", "pa-call-twilio-auth-token", "claude-anthropic-api"):
        try:
            keychain(item)
            print(f"ok    keychain {item}")
        except RuntimeError:
            ok = False
            print(f"MISS  keychain {item}")
    if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0:
        ok = False
        print("MISS  cloudflared")
    else:
        print("ok    cloudflared")
    try:
        client = twilio()
        acct = client.api.accounts(client.account_sid).fetch()
        print(f"ok    twilio account {acct.friendly_name} ({acct.type}, {acct.status})")
        if acct.type == "Trial":
            print("WARN  trial accounts can only call verified numbers and play a trial message")
        verified = [c.phone_number for c in client.outgoing_caller_ids.list()]
        mark = "ok  " if cfg["from"] in verified else "MISS"
        ok = ok and cfg["from"] in verified
        print(f"{mark}  caller ID {cfg['from']} verified (verified: {', '.join(verified) or 'none'})")
    except RuntimeError:
        print("SKIP  twilio checks (no credentials)")
    except Exception as e:
        ok = False
        print(f"FAIL  twilio: {e}")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(prog="pa-call", description="Phone calls on your behalf.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("call", help="place a call")
    c.add_argument("to", help="number to call, E.164, e.g. +3512XXXXXXXX")
    c.add_argument("--callee", required=True, help="who you're calling, e.g. 'Restaurante Mar do Inferno'")
    c.add_argument("--brief", required=True, help="goal and limits of the call, or @file")
    c.add_argument("--lang", help="pt-PT (default), es-ES, en-GB...")
    c.add_argument("--tts-provider")
    c.add_argument("--voice")
    c.add_argument("--model")
    c.add_argument("--max-minutes", type=float, default=8)
    c.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    c.add_argument("--dry-run", action="store_true", help="generate the opening line without dialling")
    v = sub.add_parser("verify-caller-id", help="verify your own number as caller ID with Twilio")
    v.add_argument("number", nargs="?")
    sub.add_parser("check", help="check config, credentials, tunnel and caller ID")
    args = p.parse_args()
    cfg = load_config()
    if args.cmd == "call":
        asyncio.run(place_call(args, cfg))
    elif args.cmd == "verify-caller-id":
        verify_caller_id(args, cfg)
    else:
        check(args, cfg)


if __name__ == "__main__":
    main()
