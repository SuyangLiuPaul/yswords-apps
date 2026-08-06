"""Screenshot a Flutter web app once its CONTENT is up, not after a timer.

Why this exists: `chrome --headless --screenshot --virtual-time-budget`
fires on a clock. SeekSparks' Browse window fetches and parses several
whole-Bible JSONs before its first meaningful paint, and that work is
CPU-bound, so a virtual-time budget cannot fast-forward through it —
every attempt landed on the loading splash.

So: drive Chrome over the DevTools protocol and POLL a predicate until
the page really is ready, then capture. Speaks raw websocket frames
against the CDP endpoint so there is no dependency to install.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PORT = 9333


class WS:
    """Minimal RFC-6455 client — text frames out, text frames in."""

    def __init__(self, url):
        _, rest = url.split('://', 1)
        hostport, path = rest.split('/', 1)
        host, port = hostport.split(':')
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((
            f'GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
        ).encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b'\r\n\r\n', 1)[1]

    def send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = b'\x81'
        mask = os.urandom(4)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 1 << 16:
            hdr += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack('>Q', n)
        self.s.sendall(hdr + mask +
                       bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def recv(self):
        while True:
            self._fill(2)
            b1, b2 = self.buf[0], self.buf[1]
            ln = b2 & 0x7F
            off = 2
            if ln == 126:
                self._fill(4)
                ln = struct.unpack('>H', self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                self._fill(10)
                ln = struct.unpack('>Q', self.buf[2:10])[0]
                off = 10
            self._fill(off + ln)
            payload = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if (b1 & 0x0F) == 1:          # text frame
                return json.loads(payload)


def main(url, out, predicate, width=1400, height=880, timeout=240):
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
         f'--remote-debugging-port={PORT}',
         f'--window-size={width},{height}',
         '--user-data-dir=/tmp/cdp_shot_profile', url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(
                    f'http://127.0.0.1:{PORT}/json'))
                page = [t for t in tabs if t.get('type') == 'page']
                if page:
                    ws_url = page[0]['webSocketDebuggerUrl']
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            print('could not reach CDP', file=sys.stderr)
            return 1

        ws = WS(ws_url)
        mid = [0]

        def cmd(method, params=None):
            mid[0] += 1
            ws.send({'id': mid[0], 'method': method, 'params': params or {}})
            while True:
                m = ws.recv()
                if m.get('id') == mid[0]:
                    return m

        cmd('Runtime.enable')
        deadline = time.time() + timeout
        ready = False
        while time.time() < deadline:
            r = cmd('Runtime.evaluate',
                    {'expression': predicate, 'returnByValue': True})
            if r.get('result', {}).get('result', {}).get('value') is True:
                ready = True
                break
            time.sleep(2)

        if not ready:
            print('predicate never became true — capturing anyway',
                  file=sys.stderr)
        # Let one more frame settle after the predicate flips.
        time.sleep(20)
        shot = cmd('Page.captureScreenshot', {'format': 'png'})
        data = shot.get('result', {}).get('data')
        if not data:
            print('no screenshot data', file=sys.stderr)
            return 1
        open(out, 'wb').write(base64.b64decode(data))
        print(f'wrote {out} ({os.path.getsize(out)} bytes), ready={ready}')
        return 0
    finally:
        proc.terminate()


if __name__ == '__main__':
    # Flutter paints into a canvas, so there is no DOM text to wait on.
    # The reliable tell that the app is past its splash is the semantics
    # tree: Flutter publishes it once real content is on screen.
    # First attempt keyed on innerText length and fired on the SPLASH —
    # its daily verse alone is long enough to pass. Key on markers that
    # only exist once the Workbench is up AND the chapter has rendered:
    # "Word Study" is the Analysis tab, and a verse reference like
    # "1:1" only appears once Browse has rows.
    PRED = ("(function(){var t=document.body.innerText||'';"
            "return t.indexOf('Word Study')!==-1 && /\\d+:\\d+/.test(t)"
            " && t.indexOf('\u5bfb\u5149')===-1;})()")
    sys.exit(main(sys.argv[1], sys.argv[2], PRED))
