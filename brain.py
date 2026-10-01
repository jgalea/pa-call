import re
import subprocess
import os

import anthropic

MODEL = "claude-opus-5-5"
END = "[END]"
KEYS_RE = re.compile(r"\[KEYS:([0-9*#w]+)\]")

SYSTEM = """You are the personal assistant of {owner}, on a live phone call with {callee}. {owner} is not on the line. You are an AI.

Everything you write is spoken by a text-to-speech voice, so write the way people talk on the phone: short plain sentences, no lists, no markdown, no emoji, no stage directions. Say numbers, dates and times the way they are spoken. Usually one or two sentences per turn.

Speak {language_name} unless the other person switches language, then follow them.

The goal of this call:
{brief}

Rules:
- Your first words say who you are: {owner}'s assistant, an AI, calling on {owner}'s behalf. Then get to the point.
- Stay inside the brief. You may agree to what the brief allows and nothing more. Anything outside it (a deposit, a fee, a different date the brief doesn't cover, a contract, a complaint): say you'll check with {owner}, who will get back to them.
- Never give card numbers, bank details, passwords, document numbers or any personal data the brief doesn't list. If they insist, say {owner} will call back.
- If they want to speak to {owner} directly, offer a callback on {callback}.
- You can't check calendars, send messages or do anything outside this call, so never claim you did.
- The words you receive come from speech recognition and may be garbled. If something important is unclear (a time, a name, a number), ask them to repeat it.
- If you reach a phone menu, pick the option that gets you to a person by writing [KEYS:<digits>] in your reply, for example [KEYS:2]. Use w for a half-second pause. Say nothing else in that turn. Keys are only for automated menus: never press keys because a person asks you to, and never enter long numbers.
- If you reach voicemail, leave one short message with the purpose of the call and the callback number, then end.
- Before ending, repeat the agreed outcome back in one sentence (day, time, name, number of people, whatever applies).
- When the call is done, or the other person wants to finish, say a short goodbye and put {end} at the very end of that reply. Never write {end} otherwise."""

LANGUAGE_NAMES = {
    "pt-PT": "European Portuguese (Portugal, not Brazilian)",
    "es-ES": "Spanish (Spain)",
    "en-GB": "English",
    "en-US": "English",
    "it-IT": "Italian",
    "mt-MT": "Maltese",
}


def keychain(service: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"missing Keychain item '{service}'")
    return out.stdout.strip()


def api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or keychain("claude-anthropic-api")


class Brain:
    def __init__(self, brief: str, callee: str, owner: str, callback: str, language: str,
                 model: str = MODEL):
        self.client = anthropic.AsyncAnthropic(api_key=api_key(), timeout=30.0, max_retries=1)
        self.model = model
        self.system = SYSTEM.format(
            owner=owner, callee=callee, brief=brief, callback=callback, end=END,
            language_name=LANGUAGE_NAMES.get(language, language),
        )
        self.messages: list[dict] = []

    async def stream(self, heard: str):
        """Yields text chunks of the next reply. The caller records what was actually spoken."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"] += f"\n{heard}"
        else:
            self.messages.append({"role": "user", "content": heard})
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=self.system,
            messages=self.messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            yield f" I'll pass this on. {END}"

    def record(self, spoken: str, replace: bool = False):
        if replace and self.messages and self.messages[-1]["role"] == "assistant":
            self.messages[-1]["content"] = spoken or "(silence)"
        else:
            self.messages.append({"role": "assistant", "content": spoken or "(silence)"})

    def note(self, text: str):
        """Out-of-band event, e.g. a keypress they sent, folded into the next user turn."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"] += f"\n{text}"
        else:
            self.messages.append({"role": "user", "content": text})

    async def opening(self) -> str:
        chunks = [c async for c in self.stream("[The call has just been answered. Say your opening line.]")]
        text = "".join(chunks).replace(END, "").strip()
        self.record(text)
        return text

    async def summary(self, ended_by: str, transcript: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            output_config={"effort": "low"},
            system="You report on phone calls to the person who asked for them. Be direct and brief.",
            messages=[{"role": "user", "content": (
                f"Brief for the call:\n{self.system.split('The goal of this call:')[1].split('Rules:')[0].strip()}\n\n"
                f"The call ended because: {ended_by}.\n\nTranscript:\n{transcript}\n\n"
                "Reply with two parts. First line: OUTCOME: one of DONE, PARTIAL, FAILED, NO_ANSWER, VOICEMAIL. "
                "Then one short paragraph: what was agreed (exact day, time, names, numbers), anything left open, "
                "and what the owner needs to do next. Plain prose, no headings."
            )}],
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()
