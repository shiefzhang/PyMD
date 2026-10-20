"""极简 CDP 客户端：只用标准库，够跑 Runtime.evaluate / Page.captureScreenshot。

WebView2 只要带上 --remote-debugging-port 就会开一个 Chrome DevTools 端口，
于是可以像调 puppeteer 一样直接驱动 Tauri 页面里的 DOM，比模拟鼠标可靠得多。
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.request


class WS:
    def __init__(self, url, timeout=15):
        assert url.startswith("ws://"), url
        hostport, path = url[5:].split("/", 1)
        path = "/" + path
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake 被关闭")
            buf += chunk
        head, _, self.rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise RuntimeError("handshake 失败: %s" % status.decode("utf-8", "replace"))

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, n):
        out = bytearray()
        if self.rest:
            take = min(n, len(self.rest))
            out += self.rest[:take]
            self.rest = self.rest[take:]
        while len(out) < n:
            chunk = self.sock.recv(min(1 << 20, n - len(out)))
            if not chunk:
                raise RuntimeError("socket 已关闭")
            out += chunk
        return bytes(out)

    def recv(self):
        payload = b""
        while True:
            b1, b2 = self._read(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if masked:
                mk = self._read(4)
                data = self._read(length)
                data = bytes(c ^ mk[i % 4] for i, c in enumerate(data))
            else:
                data = self._read(length)
            if opcode == 0x9:  # ping
                continue
            if opcode == 0x8:
                raise RuntimeError("服务端关闭了连接")
            payload += data
            if fin:
                return payload.decode("utf-8", "replace")


class CDP:
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def eval(self, expression, await_promise=False):
        out = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if out.get("exceptionDetails"):
            detail = out["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise RuntimeError("页面里抛异常: %s" % text)
        return out["result"].get("value")

    def shot(self, path, clip=None, scale=1):
        params = {"format": "png"}
        if clip:
            params["clip"] = dict(clip, scale=scale)
        data = self.call("Page.captureScreenshot", **params)["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        return path


def wait_targets(port, timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages
        except Exception as exc:  # 端口还没起来
            last = exc
        time.sleep(0.4)
    raise RuntimeError("等不到调试端口 %d：%r" % (port, last))
