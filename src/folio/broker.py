from __future__ import annotations

import asyncio
import json
import sys


class Broker:
    """Private zond adapter protocol. Answers must never enter logs or persistent state."""

    async def start(self):
        self.reader = asyncio.StreamReader(limit=128 * 1024)
        protocol = asyncio.StreamReaderProtocol(self.reader)
        self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, sys.stdin
        )
        ready = json.loads(await self.reader.readline())
        if ready.get("type") != "ready" or ready.get("version") != 1:
            raise RuntimeError("unsupported zond adapter protocol")
        self.replies = asyncio.Queue()
        self.retry = asyncio.Event()
        self.closed = False
        self.pump_task = asyncio.create_task(self.pump())

    def close(self):
        self.pump_task.cancel()
        self.transport.close()

    async def pump(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if message.get("type") == "retry":
                    self.retry.set()
                else:
                    await self.replies.put(message)
        finally:
            self.closed = True
            await self.replies.put({"type": "error"})

    async def receive(self):
        message = await self.replies.get()
        if message.get("type") == "error":
            raise RuntimeError("zond adapter channel closed or request rejected")
        return message

    def send(self, method, **body):
        print(json.dumps({"method": method, **body}), flush=True)

    async def status(self, state):
        self.send("status", state=state)
        await self.recorded()

    async def recorded(self):
        if (await self.receive()).get("type") != "recorded":
            raise RuntimeError("unexpected zond response")

    async def challenge(self, provider, fields, page_origin):
        self.send(
            "challenge",
            prompt=f"Sign in to {provider} for Folio read access.",
            fields=fields,
            context={
                "provider": provider,
                "origin": page_origin,
                "purpose": "Read account history",
            },
            timeout_secs=300,
        )
        opened = await self.receive()
        if opened.get("type") != "opened":
            raise RuntimeError("expected zond challenge")
        return opened["challenge_id"]

    async def result(self, ident, outcome):
        self.send("result", challenge_id=ident, outcome=outcome)
        await self.recorded()

    async def withdraw(self, ident):
        self.send("withdraw", challenge_id=ident)
        # An answer may already be in flight. Discard its values on withdrawal.
        recorded = False
        answered = False
        while not (recorded and answered):
            reply = await self.receive()
            recorded |= reply.get("type") == "recorded"
            answered |= reply.get("type") == "answered" and reply.get("challenge_id") == ident
