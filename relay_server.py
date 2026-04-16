#!/usr/bin/env python3
"""
ArthisLand Relay Server  (v3 — Multi-Player, Cloud-Ready)
==========================================================
Supports 2-4 players per room.  Each player gets a unique numeric ID (1-4).
Accepted connection types: plain TCP (standalone builds) and WebSocket (WebGL).

PROTOCOL
---------
Client → Relay
  HOST:CODE            create a room (code = 4-digit number)
  JOIN:CODE            join an existing room
  STATE:...            game state  (relayed as  P:<id>,STATE:...  to others)
  NAME:name            player name (relayed as  P:<id>,NAME:name  to others)
  SCENE:scene          scene sync  (relayed as  P:<id>,SCENE:...  to others)
  PING                 keepalive — absorbed, not relayed

Relay → Client
  WAITING              host is waiting for the first joiner
  CONNECTED:<id>[,<id2>,...]
                       you are player <id>; existing player IDs follow
  JOINED:<id>          a new player joined with this ID
  LEFT:<id>            that player disconnected
  BUSY / NOTFOUND / FULL / INVALID  — error responses

Forwarded game messages:
  P:<sender_id>,<original_message>

CLOUD DEPLOYMENT
-----------------
  Railway:  set PORT env var; start command = python relay_server.py
  Render:   start command = python relay_server.py --port $PORT
  Fly.io:   fly launch && fly deploy
  Docker:   docker build -t arthisland-relay . && docker run -p 7777:7777 arthisland-relay

LOCAL DEV
----------
  python relay_server.py              # port 7777
  python relay_server.py --port 9000
"""

import argparse
import base64
import hashlib
import os
import socket
import struct
import sys
import threading
from typing import Dict, List, Optional, Tuple

# ── WebSocket constants ───────────────────────────────────────────────────────

_WS_GUID  = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_TEXT  = 0x01
_OP_CLOSE = 0x08
_OP_PING  = 0x09
_OP_PONG  = 0x0A

# ── Low-level WebSocket helpers ───────────────────────────────────────────────

def _recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_handshake(sock: socket.socket) -> bool:
    raw = b""
    sock.settimeout(10)
    try:
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            raw += chunk
    except OSError:
        return False

    request = raw.decode("utf-8", errors="ignore")
    headers: Dict[str, str] = {}
    for line in request.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    key = headers.get("sec-websocket-key", "")
    if not key:
        return False

    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()
    ).decode()

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    try:
        sock.sendall(response.encode())
    except OSError:
        return False
    return True


def _ws_recv_frame(sock: socket.socket) -> Optional[Tuple[int, bytes]]:
    while True:
        header = _recv_exactly(sock, 2)
        if not header:
            return None

        opcode      = header[0] & 0x0F
        masked      = bool(header[1] & 0x80)
        payload_len = header[1] & 0x7F

        if payload_len == 126:
            ext = _recv_exactly(sock, 2)
            if not ext:
                return None
            payload_len = struct.unpack(">H", ext)[0]
        elif payload_len == 127:
            ext = _recv_exactly(sock, 8)
            if not ext:
                return None
            payload_len = struct.unpack(">Q", ext)[0]

        mask_key = b""
        if masked:
            mask_key = _recv_exactly(sock, 4)
            if not mask_key:
                return None

        payload = _recv_exactly(sock, payload_len)
        if payload is None:
            return None

        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == _OP_CLOSE:
            return None
        if opcode == _OP_PING:
            _ws_send_frame(sock, _OP_PONG, payload)
            continue
        return opcode, payload


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes):
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
    try:
        sock.sendall(header + payload)
    except OSError:
        pass

# ── Connection wrapper ────────────────────────────────────────────────────────

class Connection:
    """
    Uniform send / recv over TCP or WebSocket.
    All public methods are thread-safe with respect to *sending* (separate lock).
    """

    def __init__(self, sock: socket.socket, is_ws: bool):
        self.sock    = sock
        self.is_ws   = is_ws
        self._buf    = b""       # TCP line-buffer only
        self._wlock  = threading.Lock()

    # ── send ──────────────────────────────────────────────────────────────────

    def send_text(self, text: str):
        """Send one signalling or notification message."""
        data = text.encode("utf-8")
        with self._wlock:
            if self.is_ws:
                _ws_send_frame(self.sock, _OP_TEXT, data)
            else:
                try:
                    self.sock.sendall(data + b"\n")
                except OSError:
                    pass

    def relay_data(self, sender_id: int, line: bytes):
        """
        Forward one decoded game-data line to this peer, prefixed with the
        sender's player ID.  Each call sends exactly one message.
        """
        msg = f"P:{sender_id},".encode() + line
        with self._wlock:
            if self.is_ws:
                _ws_send_frame(self.sock, _OP_TEXT, msg)
            else:
                try:
                    self.sock.sendall(msg + b"\n")
                except OSError:
                    pass

    # ── recv ──────────────────────────────────────────────────────────────────

    def recv_line(self) -> Optional[str]:
        """Read the next complete signalling message (HOST / JOIN command)."""
        if self.is_ws:
            result = _ws_recv_frame(self.sock)
            if result is None:
                return None
            _, payload = result
            return payload.decode("utf-8", errors="ignore").strip()
        else:
            self.sock.settimeout(10)
            try:
                while True:
                    if b"\n" in self._buf:
                        idx  = self._buf.index(b"\n")
                        line = self._buf[:idx].decode("utf-8", errors="ignore").strip()
                        self._buf = self._buf[idx + 1:]
                        return line
                    chunk = self.sock.recv(256)
                    if not chunk:
                        return None
                    self._buf += chunk
                    if len(self._buf) > 512:
                        return None
            except OSError:
                return None

    def recv_lines(self) -> Optional[List[bytes]]:
        """
        Read the next batch of game data.
        Returns a list of individual message lines (stripped, non-empty).
        Returns None on disconnect.
        """
        if self.is_ws:
            result = _ws_recv_frame(self.sock)
            if result is None:
                return None
            _, payload = result
            lines = [l.strip() for l in payload.split(b"\n") if l.strip()]
            return lines if lines else []
        else:
            try:
                self.sock.settimeout(None)
                data = self.sock.recv(4096)
                if not data:
                    return None
                lines = [l.strip() for l in data.split(b"\n") if l.strip()]
                return lines
            except OSError:
                return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

# ── Room (multi-player) ───────────────────────────────────────────────────────

class Room:
    MAX_PLAYERS = 4

    def __init__(self, code: str):
        self.code    = code
        self._players: Dict[int, Connection] = {}
        self._lock   = threading.Lock()
        self._next   = 1
        self.has_peer = threading.Event()   # set once 2+ players are present

    def join(self, conn: Connection) -> Tuple[int, List[int]]:
        """Add a player.  Returns (new_player_id, existing_player_ids)."""
        with self._lock:
            existing = list(self._players.keys())
            pid = self._next
            self._next += 1
            self._players[pid] = conn
            if len(self._players) >= 2:
                self.has_peer.set()
        return pid, existing

    def leave(self, pid: int) -> List[Connection]:
        """Remove a player.  Returns remaining connections."""
        with self._lock:
            self._players.pop(pid, None)
            return list(self._players.values())

    def is_full(self) -> bool:
        with self._lock:
            return len(self._players) >= self.MAX_PLAYERS

    def count(self) -> int:
        with self._lock:
            return len(self._players)

    def others(self, pid: int) -> List[Connection]:
        """Return connections of all players except pid."""
        with self._lock:
            return [c for p, c in self._players.items() if p != pid]

    def broadcast_text(self, sender_pid: int, text: str):
        """Send a text notification to all players except sender."""
        for conn in self.others(sender_pid):
            conn.send_text(text)

    def broadcast_data(self, sender_pid: int, line: bytes):
        """Forward one data line to all players except sender."""
        for conn in self.others(sender_pid):
            conn.relay_data(sender_pid, line)

# ── Relay server ──────────────────────────────────────────────────────────────

class RelayServer:
    HOST_WAIT_SEC = 300   # how long host waits for the first peer

    def __init__(self, port: int = 7777):
        self._rooms: Dict[str, Room] = {}
        self._lock  = threading.Lock()

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", port))
        self._srv.listen(256)
        print(f"[Relay] Listening on 0.0.0.0:{port}  (TCP + WebSocket)", flush=True)

    def run(self):
        while True:
            try:
                sock, addr = self._srv.accept()
                threading.Thread(
                    target=self._dispatch, args=(sock, addr), daemon=True
                ).start()
            except Exception as e:
                print(f"[Relay] Accept error: {e}", flush=True)

    # ── Detect protocol & dispatch ────────────────────────────────────────────

    def _dispatch(self, sock: socket.socket, addr):
        # OS-level keepalive so dead peers are detected quickly
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

        try:
            sock.settimeout(10)
            peek = sock.recv(4, socket.MSG_PEEK)
        except OSError as e:
            print(f"[Relay] Peek error from {addr}: {e}", flush=True)
            try:
                sock.close()
            except Exception:
                pass
            return

        is_ws = peek[:3] == b"GET"

        if is_ws:
            if not _ws_handshake(sock):
                print(f"[Relay] Bad WS handshake from {addr}", flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                return
            print(f"[Relay] WebSocket client connected from {addr[0]}", flush=True)
        else:
            print(f"[Relay] TCP client connected from {addr[0]}", flush=True)

        conn = Connection(sock, is_ws)

        try:
            line = conn.recv_line()
            if not line:
                conn.close()
                return

            if line.startswith("HOST:"):
                self._on_host(conn, line[5:].strip(), addr)
            elif line.startswith("JOIN:"):
                self._on_join(conn, line[5:].strip(), addr)
            else:
                conn.send_text("INVALID")
                conn.close()
        except Exception as exc:
            print(f"[Relay] Dispatch error from {addr}: {exc}", flush=True)
            conn.close()

    # ── HOST ──────────────────────────────────────────────────────────────────

    def _on_host(self, conn: Connection, code: str, addr):
        if not _valid_code(code):
            conn.send_text("INVALID")
            conn.close()
            return

        with self._lock:
            if code in self._rooms:
                conn.send_text("BUSY")
                conn.close()
                return
            room = Room(code)
            self._rooms[code] = room

        host_id, _ = room.join(conn)   # host is always player 1
        ptype = "WS" if conn.is_ws else "TCP"
        print(f"[Relay] Room {code} created by {addr[0]} ({ptype})", flush=True)

        conn.send_text("WAITING")

        # Block until a peer joins (or timeout)
        joined = room.has_peer.wait(timeout=self.HOST_WAIT_SEC)
        if not joined:
            room.leave(host_id)
            with self._lock:
                self._rooms.pop(code, None)
            print(f"[Relay] Room {code} timed out", flush=True)
            conn.close()
            return

        # Tell host their ID and the ID(s) of players already in the room
        others_at_connect = [pid for pid in room._players if pid != host_id]
        id_list = ",".join(str(p) for p in [host_id] + others_at_connect)
        conn.send_text(f"CONNECTED:{id_list}")
        print(f"[Relay] Room {code} — host connected as player {host_id}", flush=True)

        # Block on host's receive loop until they disconnect
        self._player_loop(room, host_id, conn, code, addr)

    # ── JOIN ──────────────────────────────────────────────────────────────────

    def _on_join(self, conn: Connection, code: str, addr):
        if not _valid_code(code):
            conn.send_text("INVALID")
            conn.close()
            return

        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                conn.send_text("NOTFOUND")
                conn.close()
                return
            if room.is_full():
                conn.send_text("FULL")
                conn.close()
                return

        player_id, existing = room.join(conn)  # wakes host if 2nd player
        ptype = "WS" if conn.is_ws else "TCP"
        print(f"[Relay] {addr[0]} ({ptype}) joined room {code} as player {player_id}", flush=True)

        # Tell joiner their ID and all existing player IDs
        id_list = ",".join(str(p) for p in [player_id] + existing)
        conn.send_text(f"CONNECTED:{id_list}")

        # Tell existing players about the new arrival
        room.broadcast_text(player_id, f"JOINED:{player_id}")

        print(f"[Relay] Room {code} — {room.count()} player(s) active", flush=True)

        # Block on this player's receive loop
        self._player_loop(room, player_id, conn, code, addr)

    # ── Per-player relay loop ─────────────────────────────────────────────────

    def _player_loop(self, room: Room, pid: int, conn: Connection,
                     code: str, addr):
        """
        Runs on the player's own dispatch thread.
        Reads game data from this player and broadcasts it to all others.
        Cleans up when the player disconnects.
        """
        try:
            conn.sock.settimeout(None)   # no timeout during play
            while True:
                lines = conn.recv_lines()
                if lines is None:
                    break   # disconnect
                for line in lines:
                    if line == b"PING":
                        continue   # keepalive — don't forward
                    room.broadcast_data(pid, line)
        except Exception:
            pass
        finally:
            remaining = room.leave(pid)
            ptype = "WS" if conn.is_ws else "TCP"
            print(f"[Relay] Player {pid} ({ptype}) left room {code} "
                  f"({len(remaining)} remaining)", flush=True)

            for other_conn in remaining:
                other_conn.send_text(f"LEFT:{pid}")

            if not remaining:
                with self._lock:
                    self._rooms.pop(code, None)
                print(f"[Relay] Room {code} closed", flush=True)

            conn.close()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _valid_code(code: str) -> bool:
    return len(code) == 4 and code.isdigit()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ArthisLand Relay Server")
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("PORT", 7777)),
        help="Port to listen on  (default: $PORT env or 7777)"
    )
    args = parser.parse_args()

    server = RelayServer(port=args.port)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[Relay] Shutting down.", flush=True)
        sys.exit(0)
