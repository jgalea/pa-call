"""Runs a scripted ConversationRelay session through the real tunnel with a fake brain. No Twilio, no Claude."""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import pa_call
from brain import END


class FakeBrain:
    def __init__(self):
        self.messages = []
        self.replies = iter([
            "Boa tarde. [KEYS:2]",
            "Claro. [KEYS:91234567#]",
            f"Perfeito, então fica sábado às oito para quatro pessoas. Obrigado, boa noite! {END}",
        ])

    async def stream(self, heard):
        self.messages.append({"role": "user", "content": heard})
        text = next(self.replies)
        for i in range(0, len(text), 3):
            yield text[i:i + 3]
            await asyncio.sleep(0.01)

    def record(self, spoken, replace=False):
        self.messages.append({"role": "assistant", "content": spoken})

    def note(self, text):
        self.messages.append({"role": "user", "content": text})


async def main():
    session = pa_call.Session(FakeBrain(), "Boa tarde, fala a assistente da Ana Silva.")
    got = []
    async with serve(session.handle, "127.0.0.1", pa_call.PORT):
        tunnel, host = pa_call.start_tunnel()
        try:
            await pa_call.wait_reachable(host)
            import subprocess
            ip = [x for x in subprocess.run(["dig", "+short", host, "@one.one.one.one"], capture_output=True, text=True).stdout.split() if x[0].isdigit()][0]
            from websockets.exceptions import ConnectionClosed

            async def rejected(path):
                try:
                    async with connect(f"wss://{host}{path}", host=ip, port=443) as bad:
                        await bad.recv()
                except ConnectionClosed as e:
                    return e.rcvd is not None and e.rcvd.code == 1008
                return False

            assert await rejected("/"), "connection without the token was accepted"
            assert await rejected("/wrong-token"), "connection with a wrong token was accepted"
            async with connect(f"wss://{host}/{session.token}", host=ip, port=443) as ws:
                async def reader():
                    async for m in ws:
                        got.append(json.loads(m))
                r = asyncio.create_task(reader())
                await ws.send(json.dumps({"type": "setup", "callSid": "CAtest"}))
                await ws.send(json.dumps({"type": "prompt", "voicePrompt": "Estou, boa tarde.", "last": True}))
                await asyncio.sleep(1)
                await ws.send(json.dumps({"type": "prompt", "voicePrompt": "Restaurante, para reservas prima dois, para outros assuntos prima um. Quer?", "last": True}))
                await asyncio.sleep(1)
                assert await rejected(f"/{session.token}"), "a second connection hijacked the live session"
                await ws.send(json.dumps({"type": "prompt", "voicePrompt": "Pode marcar 9 1 2 3 4 5 6 7 cardinal?", "last": True}))
                await asyncio.sleep(1)
                await ws.send(json.dumps({"type": "prompt", "voicePrompt": "Sim, sábado às oito, quatro pessoas, está marcado.", "last": True}))
                await asyncio.sleep(8)
                r.cancel()
        finally:
            tunnel.kill()
    kinds = [m["type"] for m in got]
    spoken = "".join(m.get("token", "") for m in got if m["type"] == "text")
    print("messages:", kinds)
    print("spoken:", spoken)
    assert got[0] == {"type": "text", "token": "Boa tarde, fala a assistente da Ana Silva.", "last": True}, got[0]
    assert {"type": "sendDigits", "digits": "2"} in got, "keypress not sent"
    assert not any(m["type"] == "sendDigits" and m["digits"] != "2" for m in got), "long key sequence was not blocked"
    assert "[" not in spoken and "END" not in spoken, "control tag leaked into speech"
    assert kinds[-1] == "end", "call not ended after goodbye"
    assert sum(1 for m in got if m.get("last")) == 4, "each turn should close with last=true"
    print("OK")


asyncio.run(main())
