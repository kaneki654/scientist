"""
scirelay — SCIENTIST's reverse-tethering relay (wire-compatible with the gnirehtet Android client).

The phone runs the gnirehtet client (a VpnService). It sends every IPv4 packet the phone produces through
`adb reverse localabstract:gnirehtet tcp:31416` to this relay, which terminates TCP / forwards UDP using the
PC's own network, and writes the answers back as IPv4 packets.

Why not the stock gnirehtet relay: it silently drops TCP segments whose sequence number is behind what it
expects. TCP keep-alive probes are exactly that (seq = next-1), so long-lived connections — Facebook,
Messenger, push / chat apps — are declared dead by Android after a few unanswered probes ("Connection timed
out"). This relay ACKs duplicates and keep-alives, advertises a proper MSS, retransmits if an ACK goes
missing, and RSTs packets of unknown connections so apps reconnect immediately after a tunnel reset.

Protocol (gnirehtet): on connect the relay sends the client id as a 4-byte big-endian integer, then both
sides exchange raw IPv4 packets back-to-back (framed by the IPv4 total-length field).
"""
import array
import asyncio
import random
import socket
import struct
import sys
import threading
import time

PROTO_TCP, PROTO_UDP = 6, 17
FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
MASK = 0xFFFFFFFF
CLIENT_MTU = 16384                   # MTU of the gnirehtet client's tun interface
MAX_PAYLOAD = CLIENT_MTU - 40
OUR_WINDOW = 65535                   # no window scaling: we don't send the wscale option, so neither side scales
SERVER_BUFFER_HIGH = 512 * 1024      # pause reading a server socket above this much unsent data
TUNNEL_HIGH_WATER = 4 * 1024 * 1024  # stop pushing data to a phone when its tunnel backs up
CONNECT_TIMEOUT = 10
UDP_IDLE = 120
RTO = 1.0
MAX_RETRIES = 10
LOCALHOST_ALIAS = bytes([10, 0, 2, 2])   # like gnirehtet: 10.0.2.2 on the phone = this PC's localhost

_CS_FMT = "<H" if sys.byteorder == "little" else ">H"


def checksum(data):
    """Internet checksum, computed on native-order 16-bit words (fast); pack the result with _CS_FMT."""
    if len(data) & 1:
        data += b"\0"
    s = sum(array.array("H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def ipv4(src, dst, proto, l4, ident):
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(l4), ident, 0x4000, 64, proto, 0, src, dst)
    return hdr[:10] + struct.pack(_CS_FMT, checksum(hdr)) + hdr[12:] + l4


def tcp_packet(src, sport, dst, dport, seq, ack, flags, window, payload=b"", options=b"", ident=0):
    seg = struct.pack("!HHIIBBHHH", sport, dport, seq & MASK, ack & MASK, ((20 + len(options)) // 4) << 4,
                      flags, window, 0, 0) + options + payload
    cs = checksum(src + dst + struct.pack("!BBH", 0, PROTO_TCP, len(seg)) + seg)
    return ipv4(src, dst, PROTO_TCP, seg[:16] + struct.pack(_CS_FMT, cs) + seg[18:], ident)


def udp_packet(src, sport, dst, dport, payload, ident=0):
    dgram = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    cs = checksum(src + dst + struct.pack("!BBH", 0, PROTO_UDP, len(dgram)) + dgram) or 0xFFFF
    return ipv4(src, dst, PROTO_UDP, dgram[:6] + struct.pack(_CS_FMT, cs) + dgram[8:], ident)


def host_of(ip):
    return "127.0.0.1" if ip == LOCALHOST_ALIAS else socket.inet_ntoa(ip)


# ════════════════════════════════════════════════════════════════════
#  TCP
# ════════════════════════════════════════════════════════════════════
class _ServerSide(asyncio.Protocol):
    def __init__(self, conn):
        self.conn = conn

    def connection_made(self, transport):
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        transport.set_write_buffer_limits(high=OUR_WINDOW)

    def data_received(self, data):
        self.conn.on_server_data(data)

    def eof_received(self):
        self.conn.on_server_eof()
        return True                      # keep the write side open: the phone may still be sending

    def connection_lost(self, exc):
        self.conn.on_server_lost(exc)

    def pause_writing(self):
        self.conn.server_write_paused = True

    def resume_writing(self):
        self.conn.server_write_paused = False
        self.conn.send_ack()             # window update: the phone may send again


class TcpConn:
    def __init__(self, tunnel, key):
        self.t = tunnel
        self.key = key
        _, self.cip, self.cport, self.sip, self.sport = key
        self.state = "CONNECTING"
        self.transport = None
        self.rcv_nxt = 0                 # next sequence number expected from the phone
        self.isn = random.getrandbits(32)
        self.snd_una = self.snd_nxt = self.isn
        self.peer_window = 0
        self.peer_mss = 536
        self.pending = bytearray()       # server data not yet sent to the phone
        self.unacked = bytearray()       # sent to the phone, not yet acknowledged
        self.server_eof = self.client_fin = self.fin_sent = False
        self.server_write_paused = self.reading_paused = False
        self.last_progress = time.monotonic()
        self.retries = 0
        self.closed = False
        self.bytes_up = self.bytes_down = 0
        self.born = time.monotonic()

    # --- packets to the phone ---
    def window(self):
        if not self.transport:
            return OUR_WINDOW
        return max(0, OUR_WINDOW - self.transport.get_write_buffer_size())

    def send(self, flags, seq=None, payload=b"", options=b""):
        self.t.send(tcp_packet(self.sip, self.sport, self.cip, self.cport,
                               self.snd_nxt if seq is None else seq, self.rcv_nxt, flags,
                               self.window(), payload, options, self.t.next_ident()))

    def send_ack(self):
        if not self.closed and self.state in ("ESTABLISHED", "SYN_ACK_SENT"):
            self.send(ACK)

    def send_rst(self):
        self.t.send(tcp_packet(self.sip, self.sport, self.cip, self.cport, self.snd_nxt, self.rcv_nxt,
                               RST | ACK, 0, ident=self.t.next_ident()))

    # --- packets from the phone ---
    def on_syn(self, seq, window, options):
        self.rcv_nxt = (seq + 1) & MASK
        self.peer_window = window
        i = 0
        while i < len(options):          # read the phone's MSS option
            kind = options[i]
            if kind == 0:
                break
            if kind == 1:
                i += 1
                continue
            if i + 1 >= len(options):
                break
            length = options[i + 1]
            if kind == 2 and length == 4 and i + 4 <= len(options):
                self.peer_mss = struct.unpack_from("!H", options, i + 2)[0]
            i += max(2, length)
        asyncio.ensure_future(self._connect())

    async def _connect(self):
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await asyncio.wait_for(
                loop.create_connection(lambda: _ServerSide(self), host_of(self.sip), self.sport), CONNECT_TIMEOUT)
        except Exception:
            self.t.stats["tcp_failed"] += 1
            self.t.send(tcp_packet(self.sip, self.sport, self.cip, self.cport, 0, self.rcv_nxt, RST | ACK, 0,
                                   ident=self.t.next_ident()))
            self.close()
            return
        if self.closed:
            transport.abort()
            return
        self.transport = transport
        self.state = "SYN_ACK_SENT"
        self.send(SYN | ACK, seq=self.isn, options=struct.pack("!BBH", 2, 4, MAX_PAYLOAD))
        self.snd_nxt = (self.isn + 1) & MASK
        self.last_progress = time.monotonic()

    def on_packet(self, seq, ack, flags, window, payload):
        if self.closed:
            return
        if flags & RST:
            self.close(abort=True)
            return
        if self.state == "CONNECTING":
            return                       # SYN retransmit while we're still connecting to the server
        if flags & SYN:
            if self.state == "SYN_ACK_SENT":   # our SYN-ACK got lost: repeat it
                self.send(SYN | ACK, seq=self.isn, options=struct.pack("!BBH", 2, 4, MAX_PAYLOAD))
            return
        if flags & ACK:
            self.on_ack(ack, window)
        if seq != self.rcv_nxt:
            # duplicate, TCP keep-alive probe (seq = next-1) or out of order: always answer with an ACK.
            # (The stock gnirehtet relay stays silent here, so keep-alive connections time out.)
            self.send_ack()
            return
        need_ack = False
        if payload:
            if self.transport and not self.client_fin:
                self.transport.write(payload)
                self.t.stats["up"] += len(payload)
                self.bytes_up += len(payload)
            self.rcv_nxt = (self.rcv_nxt + len(payload)) & MASK
            need_ack = True
        if flags & FIN and not self.client_fin:
            self.client_fin = True
            self.rcv_nxt = (self.rcv_nxt + 1) & MASK
            need_ack = True
            if self.transport and self.transport.can_write_eof():
                try:
                    self.transport.write_eof()
                except OSError:
                    pass
        if need_ack:
            self.send_ack()
        self.flush()
        self.maybe_finish()

    def on_ack(self, ack, window):
        self.peer_window = window
        if self.state == "SYN_ACK_SENT":
            if ack == self.snd_nxt:
                self.state = "ESTABLISHED"
                self.snd_una = ack
                self.last_progress = time.monotonic()
            return
        acked = (ack - self.snd_una) & MASK
        in_flight = (self.snd_nxt - self.snd_una) & MASK
        if 0 < acked <= in_flight:
            data_acked = min(acked, len(self.unacked))
            del self.unacked[:data_acked]
            self.snd_una = ack
            self.last_progress = time.monotonic()
            self.retries = 0

    # --- server side ---
    def on_server_data(self, data):
        self.t.stats["down"] += len(data)
        self.bytes_down += len(data)
        self.pending += data
        if len(self.pending) > SERVER_BUFFER_HIGH and not self.reading_paused and self.transport:
            self.transport.pause_reading()
            self.reading_paused = True
        self.flush()

    def on_server_eof(self):
        self.server_eof = True
        self.flush()

    def on_server_lost(self, exc):
        if self.closed:
            return
        if exc is not None:
            self.send_rst()
            self.close()
        else:
            self.server_eof = True
            self.flush()
            self.maybe_finish()

    # --- flow ---
    def flush(self):
        if self.closed or self.state != "ESTABLISHED":
            return
        while self.pending and not self.t.congested():
            room = self.peer_window - len(self.unacked)
            if room <= 0:
                break
            n = min(room, self.peer_mss, MAX_PAYLOAD, len(self.pending))
            chunk = bytes(self.pending[:n])
            del self.pending[:n]
            if not self.unacked:
                self.last_progress = time.monotonic()
            self.send(PSH | ACK, payload=chunk)
            self.snd_nxt = (self.snd_nxt + n) & MASK
            self.unacked += chunk
        if self.reading_paused and len(self.pending) < SERVER_BUFFER_HIGH // 2 and self.transport:
            self.transport.resume_reading()
            self.reading_paused = False
        if self.server_eof and not self.pending and not self.fin_sent:
            self.send(FIN | ACK)
            self.snd_nxt = (self.snd_nxt + 1) & MASK
            self.fin_sent = True
        self.maybe_finish()

    def tick(self, now):
        """Retransmit if the phone stopped acknowledging (a packet got lost somewhere)."""
        if self.closed or self.state != "ESTABLISHED":
            return
        in_flight = (self.snd_nxt - self.snd_una) & MASK
        if in_flight and now - self.last_progress > RTO * (1 + self.retries):
            self.retries += 1
            if self.retries > MAX_RETRIES:
                self.send_rst()
                self.close(abort=True)
                return
            if self.unacked:
                n = min(len(self.unacked), self.peer_mss, MAX_PAYLOAD)
                self.send(PSH | ACK, seq=self.snd_una, payload=bytes(self.unacked[:n]))
            elif self.fin_sent:
                self.send(FIN | ACK, seq=(self.snd_nxt - 1) & MASK)
            self.last_progress = now
            self.t.stats["retransmits"] += 1

    def maybe_finish(self):
        if self.client_fin and self.fin_sent and self.snd_una == self.snd_nxt:
            self.close()

    def close(self, abort=False):
        if self.closed:
            return
        self.closed = True
        if self.transport:
            (self.transport.abort if abort else self.transport.close)()
        self.t.conns.pop(self.key, None)


# ════════════════════════════════════════════════════════════════════
#  UDP
# ════════════════════════════════════════════════════════════════════
class UdpConn(asyncio.DatagramProtocol):
    def __init__(self, tunnel, key):
        self.t = tunnel
        self.key = key
        _, self.cip, self.cport, self.sip, self.sport = key
        self.transport = None
        self.queue = []
        self.last = time.monotonic()
        self.closed = False
        asyncio.ensure_future(self._open())

    async def _open(self):
        loop = asyncio.get_running_loop()
        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: self, remote_addr=(host_of(self.sip), self.sport))
        except Exception:
            self.close()
            return
        for d in self.queue:
            self.transport.sendto(d)
        self.queue.clear()

    def send(self, payload):
        self.last = time.monotonic()
        self.t.stats["up"] += len(payload)
        if self.transport:
            self.transport.sendto(payload)
        elif len(self.queue) < 64:
            self.queue.append(payload)

    def datagram_received(self, data, addr):
        self.last = time.monotonic()
        self.t.stats["down"] += len(data)
        self.t.send(udp_packet(self.sip, self.sport, self.cip, self.cport, data, self.t.next_ident()))

    def error_received(self, exc):
        pass

    def tick(self, now):
        if now - self.last > UDP_IDLE:
            self.close()

    def close(self, abort=False):
        if self.closed:
            return
        self.closed = True
        if self.transport:
            self.transport.close()
        self.t.conns.pop(self.key, None)


# ════════════════════════════════════════════════════════════════════
#  Tunnel (one per phone) and relay server
# ════════════════════════════════════════════════════════════════════
class Tunnel:
    def __init__(self, relay, reader, writer, cid):
        self.relay, self.reader, self.writer, self.cid = relay, reader, writer, cid
        self.conns = {}
        self.ident = random.getrandbits(16)
        self.stats = relay.stats

    def next_ident(self):
        self.ident = (self.ident + 1) & 0xFFFF
        return self.ident

    def send(self, packet):
        if not self.writer.is_closing():
            self.writer.write(packet)

    def congested(self):
        return self.writer.transport.get_write_buffer_size() > TUNNEL_HIGH_WATER

    async def run(self):
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        self.writer.write(struct.pack("!I", self.cid))
        ticker = asyncio.ensure_future(self._ticker())
        buf = bytearray()
        try:
            while True:
                data = await self.reader.read(256 * 1024)
                if not data:
                    break
                buf += data
                while len(buf) >= 20:
                    version = buf[0] >> 4
                    if version == 4:
                        total = (buf[2] << 8) | buf[3]
                    elif version == 6:           # not routed by the client, but skip it cleanly
                        total = 40 + ((buf[4] << 8) | buf[5])
                    else:
                        raise ValueError("bad packet stream")
                    if total < 20:
                        raise ValueError("bad packet length")
                    if len(buf) < total:
                        break
                    packet = bytes(buf[:total])
                    del buf[:total]
                    if version == 4:
                        self.handle(packet)
                if self.congested():
                    await self.writer.drain()
                    for c in list(self.conns.values()):
                        if isinstance(c, TcpConn):
                            c.flush()
        except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            ticker.cancel()
            for c in list(self.conns.values()):
                c.close(abort=True)
            self.writer.close()

    async def _ticker(self):
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            for c in list(self.conns.values()):
                c.tick(now)
            if not self.congested():
                for c in list(self.conns.values()):
                    if isinstance(c, TcpConn) and c.pending:
                        c.flush()

    def handle(self, p):
        ihl = (p[0] & 0x0F) * 4
        if struct.unpack_from("!H", p, 6)[0] & 0x3FFF:   # fragments: not produced by the client's tun
            return
        proto, src, dst = p[9], p[12:16], p[16:20]
        l4 = p[ihl:]
        if proto == PROTO_TCP and len(l4) >= 20:
            sport, dport, seq, ack, off, flags, win = struct.unpack_from("!HHIIBBH", l4)
            hl = (off >> 4) * 4
            key = (PROTO_TCP, src, sport, dst, dport)
            conn = self.conns.get(key)
            if conn is None:
                if flags & SYN and not flags & ACK:
                    conn = self.conns[key] = TcpConn(self, key)
                    self.stats["tcp_opened"] += 1
                    conn.on_syn(seq, win, l4[20:hl])
                elif not flags & RST:
                    # unknown connection (e.g. opened before a tunnel reset): reset it so the app reconnects now
                    plen = len(l4) - hl + (1 if flags & FIN else 0)
                    self.send(tcp_packet(dst, dport, src, sport, ack, seq + plen, RST | ACK, 0,
                                         ident=self.next_ident()))
            else:
                conn.on_packet(seq, ack, flags, win, l4[hl:])
        elif proto == PROTO_UDP and len(l4) >= 8:
            sport, dport, length = struct.unpack_from("!HHH", l4)
            key = (PROTO_UDP, src, sport, dst, dport)
            conn = self.conns.get(key)
            if conn is None:
                conn = self.conns[key] = UdpConn(self, key)
            conn.send(l4[8:length])


class Relay:
    """Runs the relay on its own thread + asyncio loop. start() returns False if the port is taken."""

    def __init__(self, port=31416, log=None):
        self.port = port
        self.log = log or (lambda msg, level="info": None)
        self.loop = None
        self.server = None
        self.thread = None
        self.tunnels = {}
        self.next_cid = 0
        self.stats = {"up": 0, "down": 0, "tcp_opened": 0, "tcp_failed": 0, "retransmits": 0}
        self._ready = threading.Event()
        self._error = None

    def running(self):
        return self.thread is not None and self.thread.is_alive() and self.server is not None

    def start(self, timeout=5):
        if self.running():
            return True
        self._ready.clear()
        self._error = None
        self.thread = threading.Thread(target=self._main, name="scirelay", daemon=True)
        self.thread.start()
        self._ready.wait(timeout)
        return self.running()

    def _main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.server = self.loop.run_until_complete(
                asyncio.start_server(self._client, "127.0.0.1", self.port))
        except OSError as e:
            self._error = e
            self.server = None
            self._ready.set()
            return
        self.log(f"[relay] listening on 127.0.0.1:{self.port}", "sys")
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.server = None
            self.loop.close()

    async def _client(self, reader, writer):
        cid = self.next_cid
        self.next_cid += 1
        tunnel = self.tunnels[cid] = Tunnel(self, reader, writer, cid)
        self.log(f"[relay] phone tunnel #{cid} connected", "info")
        try:
            await tunnel.run()
        finally:
            self.tunnels.pop(cid, None)
            self.log(f"[relay] phone tunnel #{cid} closed", "info")

    def dump(self):
        """One line per open connection (for debugging stalls)."""
        now = time.monotonic()
        lines = []
        for t in list(self.tunnels.values()):
            for c in list(t.conns.values()):
                kind = "TCP" if isinstance(c, TcpConn) else "UDP"
                line = f"#{t.cid} {kind} {host_of(c.sip)}:{c.sport}"
                if kind == "TCP":
                    line += (f" {c.state} age={now - c.born:.0f}s up={c.bytes_up} down={c.bytes_down} "
                             f"pending={len(c.pending)} unacked={len(c.unacked)} win={c.peer_window} "
                             f"mss={c.peer_mss} srvbuf={c.transport.get_write_buffer_size() if c.transport else '-'} "
                             f"fin(c={int(c.client_fin)},s={int(c.server_eof)},sent={int(c.fin_sent)})")
                else:
                    line += f" idle={now - c.last:.0f}s"
                lines.append(line)
        return lines

    def connection_count(self):
        return sum(len(t.conns) for t in list(self.tunnels.values()))

    def stop(self):
        loop = self.loop
        if not loop or not self.thread:
            return

        def shutdown():
            if self.server:
                self.server.close()
            for t in list(self.tunnels.values()):
                t.writer.close()
            loop.call_later(0.2, loop.stop)
        try:
            loop.call_soon_threadsafe(shutdown)
        except RuntimeError:
            pass
        self.thread.join(timeout=3)
        self.thread = None


if __name__ == "__main__":          # standalone: python scirelay.py [port]
    r = Relay(int(sys.argv[1]) if len(sys.argv) > 1 else 31416, log=lambda m, l="info": print(m, flush=True))
    if not r.start():
        sys.exit(f"cannot listen on port {r.port}: {r._error}")
    try:
        while True:
            time.sleep(10)
            s = r.stats
            print(f"tunnels={len(r.tunnels)} conns={r.connection_count()} up={s['up'] >> 10}KB "
                  f"down={s['down'] >> 10}KB tcp={s['tcp_opened']} failed={s['tcp_failed']} "
                  f"retx={s['retransmits']}", flush=True)
            if "--dump" in sys.argv:
                for line in r.dump():
                    print("   ", line, flush=True)
    except KeyboardInterrupt:
        r.stop()
