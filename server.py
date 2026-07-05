#!/usr/bin/env python3
"""CollabVM MCP Server - control CollabVM VMs as MCP tools"""

import json
import sys
import threading
import time
import base64
from io import BytesIO
from PIL import Image
import websocket


# --- Guacamole protocol ---

def encode(*args):
    parts = [f"{len(str(a))}.{a}" for a in args]
    return ",".join(parts) + ";"

def decode(msg):
    if not msg or not msg.endswith(";"):
        return []
    result = []
    s = msg[:-1]
    while s:
        try:
            dot = s.index(".")
            length = int(s[:dot])
            content = s[dot+1:dot+1+length]
            result.append(content)
            s = s[dot+1+length:]
            if s.startswith(","):
                s = s[1:]
        except (ValueError, IndexError):
            break
    return result


# --- CollabVM connection ---

class CollabVMConn:
    def __init__(self):
        self.ws = None
        self.connected = False
        self.has_turn = False
        self.turn_event = threading.Event()
        self.frame = None
        self.frame_lock = threading.Lock()

    def connect(self, url, vm_id):
        if self.ws:
            self.disconnect()

        self.connected = False
        ready = threading.Event()

        def on_open(ws):
            ws.send(encode("connect", vm_id))
            self.connected = True
            ready.set()

        def on_message(ws, msg):
            parts = decode(msg)
            if not parts:
                return
            cmd = parts[0]

            if cmd in ("png", "jpeg") and len(parts) >= 5:
                x, y = int(parts[2]), int(parts[3])
                try:
                    data = base64.b64decode(parts[4])
                    tile = Image.open(BytesIO(data))
                    with self.frame_lock:
                        if self.frame is None:
                            self.frame = Image.new("RGB", (1024, 768), "black")
                        self.frame.paste(tile, (x, y))
                except Exception:
                    pass

            elif cmd == "size" and len(parts) >= 4:
                w, h = int(parts[2]), int(parts[3])
                with self.frame_lock:
                    self.frame = Image.new("RGB", (w, h), "black")

            elif cmd == "turn":
                # turn,0 = turn acquired; turn,N = N people ahead
                if len(parts) >= 2 and parts[1] == "0":
                    self.has_turn = True
                    self.turn_event.set()
                elif len(parts) == 1:
                    self.has_turn = True
                    self.turn_event.set()

            elif cmd == "nop":
                ws.send("3.nop;")

        def on_error(ws, err):
            pass

        def on_close(ws, code, msg):
            self.connected = False
            self.has_turn = False

        self.ws = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            header={"Origin": "https://computernewb.com"},
        )
        t = threading.Thread(
            target=lambda: self.ws.run_forever(subprotocols=["guacamole"]),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=10)
        return self.connected

    def take_turn(self, timeout=90):
        if not self.ws:
            return False
        self.turn_event.clear()
        self.has_turn = False
        self.ws.send(encode("turn"))
        return self.turn_event.wait(timeout=timeout)

    def end_turn(self):
        if self.ws and self.has_turn:
            self.ws.send(encode("turn", "0"))
            self.has_turn = False

    def send_key(self, keysym, down=True):
        if self.ws:
            self.ws.send(encode("key", "1" if down else "0", str(keysym)))

    def press_key(self, keysym, delay=0.05):
        self.send_key(keysym, True)
        time.sleep(delay)
        self.send_key(keysym, False)
        time.sleep(delay)

    def type_text(self, text, delay=0.05):
        for ch in text:
            if ch == "\n":
                self.press_key(0xFF0D, delay)
            elif ch == "\t":
                self.press_key(0xFF09, delay)
            elif ch == "\x7f":
                self.press_key(0xFFFF, delay)
            else:
                # Unicode keysym: 0x01000000 | codepoint
                self.press_key(0x01000000 | ord(ch), delay)

    def send_chat(self, message):
        if self.ws:
            self.ws.send(encode("chat", message))

    def screenshot(self):
        with self.frame_lock:
            if self.frame is None:
                return None
            buf = BytesIO()
            self.frame.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()

    def disconnect(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.connected = False
        self.has_turn = False


# --- MCP server ---

conn = CollabVMConn()

TOOLS = [
    {
        "name": "cvm_connect",
        "description": "Connect to a CollabVM VM. Server URL is the WebSocket base URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {
                    "type": "string",
                    "description": "WebSocket URL e.g. wss://computernewb.com/collab-vm/",
                },
                "vm_id": {
                    "type": "string",
                    "description": "VM identifier e.g. vm3",
                },
            },
            "required": ["server_url", "vm_id"],
        },
    },
    {
        "name": "cvm_take_turn",
        "description": "Request a turn on the VM. Blocks until the turn is granted (up to 90s).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cvm_end_turn",
        "description": "Release the current turn.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cvm_type",
        "description": "Type text into the VM. Must have the turn first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "delay": {
                    "type": "number",
                    "description": "Delay between keystrokes in seconds (default 0.05)",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "cvm_key",
        "description": "Send a single key event (X11 keysym). For a full keypress pass down=true then down=false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keysym": {
                    "type": "integer",
                    "description": "X11 keysym e.g. 65307=Escape, 65293=Return, 65307=Esc",
                },
                "down": {"type": "boolean", "description": "True=press, False=release"},
            },
            "required": ["keysym", "down"],
        },
    },
    {
        "name": "cvm_screenshot",
        "description": "Get a PNG screenshot of the current VM state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cvm_chat",
        "description": "Send a message in the CollabVM chat.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "cvm_disconnect",
        "description": "Disconnect from the VM.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name, args):
    if name == "cvm_connect":
        ok = conn.connect(args["server_url"], args["vm_id"])
        return text_result(f"{'Connected' if ok else 'Failed to connect'} to {args['vm_id']}")

    if name == "cvm_take_turn":
        ok = conn.take_turn()
        return text_result("Turn acquired" if ok else "Timed out waiting for turn")

    if name == "cvm_end_turn":
        conn.end_turn()
        return text_result("Turn released")

    if name == "cvm_type":
        conn.type_text(args["text"], args.get("delay", 0.05))
        return text_result(f"Typed {len(args['text'])} chars")

    if name == "cvm_key":
        conn.send_key(args["keysym"], args["down"])
        return text_result("Key sent")

    if name == "cvm_screenshot":
        data = conn.screenshot()
        if data is None:
            return text_result("No frame available yet")
        return {
            "content": [{"type": "image", "data": data, "mimeType": "image/png"}],
            "isError": False,
        }

    if name == "cvm_chat":
        conn.send_chat(args["message"])
        return text_result("Chat sent")

    if name == "cvm_disconnect":
        conn.disconnect()
        return text_result("Disconnected")

    return text_result(f"Unknown tool: {name}", error=True)


def text_result(msg, error=False):
    return {"content": [{"type": "text", "text": msg}], "isError": error}


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "collabvm-mcp", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method.startswith("notifications/"):
        return None  # no response for notifications

    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
