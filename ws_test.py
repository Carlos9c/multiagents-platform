"""Temporary WebSocket test script for manual_review flow verification."""

import json
import sys

import websocket

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ID = 91

ws = websocket.WebSocket()
ws.connect(f"ws://localhost:8765/ws/projects/{PROJECT_ID}/chat")
ws.settimeout(15)

# Initial state
msg = json.loads(ws.recv())
print(f"INITIAL: type={msg.get('type')} phase={msg.get('phase')}", flush=True)

# Send user clarification while in reviewing phase
ws.send(
    json.dumps(
        {
            "type": "user_message",
            "content": (
                "El JWT_SECRET ya esta configurado en el .env como mysecret123. "
                "El test que falla es el de refresh token, que usa un endpoint diferente al de login."
            ),
        }
    )
)
print("SENT: user clarification about JWT_SECRET", flush=True)

# Collect responses
responses = []
try:
    while True:
        raw = ws.recv()
        m = json.loads(raw)
        responses.append(m)
        print(
            f"RECV: type={m.get('type')} event={m.get('event')} phase={m.get('phase')}", flush=True
        )
        content = m.get("content", "")
        if content:
            print(f"  CONTENT: {content[:500]}", flush=True)
except Exception as ex:
    print(f"DONE (timeout/close): {ex}", flush=True)

ws.close()
print(f"\nTotal responses: {len(responses)}", flush=True)
