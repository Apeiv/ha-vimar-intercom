"""Peer SIP finto su localhost (targa / Tab / proxy cloud Vimar) per i test end-to-end.

Solo 127.0.0.1: UDP (modalità locale) o TLS con un certificato autofirmato
(modalità cloud). Il resto dell'impianto (sip_client, hub, media_handler, le
view /av e /audio_ws) è il codice vero: vedi rig.py.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import os
import random
import ssl
import tempfile

from custom_components.vimar_intercom import sip_client as sip

USER, DOMAIN, PASSWORD = "60901", "impianto.test", "segreto"
PANEL = "55001"


class Msg:
    def __init__(self, raw: str):
        self.raw = raw
        self.kind, self.hdrs, self.body, self.first = sip._parse(raw)

    def h(self, name: str) -> str:
        return self.hdrs.get(name.lower(), "")

    @property
    def cid(self):
        return self.h("call-id")

    @property
    def method(self):
        return self.kind if isinstance(self.kind, str) else self.h("cseq").split()[-1]

    @property
    def code(self):
        return self.kind if isinstance(self.kind, int) else None

    @property
    def branch(self):
        via = (self.hdrs.get("_via_all") or [""])[0]
        return next((p[7:] for p in via.split(";") if p.startswith("branch=")), "")

    def __repr__(self):
        return f"<{self.first} cid={self.cid} cseq={self.h('cseq')}>"


def peer_sdp(audio_port: int, video_port: int, key: str | None = None, pt: int = 96) -> str:
    """SDP della targa; con `key` in SRTP (a=crypto), come sul cloud Vimar."""
    proto = "RTP/SAVP" if key else "RTP/AVP"
    crypto = f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{key}\r\n" if key else ""
    return ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=baresip\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {audio_port} {proto} 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n{crypto}"
            f"m=video {video_port} {proto} {pt}\r\na=rtpmap:{pt} H264/90000\r\n"
            f"a=fmtp:{pt} packetization-mode=1\r\na=sendrecv\r\n{crypto}")


def is_(method=None, code=None, cseq=None, cid=None):
    """Predicato sui messaggi del log del peer."""
    def pred(m):
        return ((method is None or m.kind == method) and (code is None or m.kind == code)
                and (cseq is None or m.h("cseq").endswith(cseq)) and (cid is None or m.cid == cid))
    return pred


async def answer_200(peer, inv):
    """La targa risponde: 100, 180, poi 200 OK con il suo SDP."""
    peer.reply(inv, 100, "Trying")
    peer.reply(inv, 180, "Ringing")
    await asyncio.sleep(0.05)
    peer.pending_invite = None
    peer.reply(inv, 200, "OK", body=peer.sdp())


def response(req: Msg, code: int, reason: str, to_tag: str | None = None,
             body: str = "", extra: str = "") -> str:
    vias = "".join(f"Via: {v}\r\n" for v in req.hdrs.get("_via_all", []))
    to = req.h("to")
    if to_tag and ";tag=" not in to:
        to += f";tag={to_tag}"
    ctype = "Content-Type: application/sdp\r\n" if body else ""
    return (f"SIP/2.0 {code} {reason}\r\n{vias}From: {req.h('from')}\r\nTo: {to}\r\n"
            f"Call-ID: {req.cid}\r\nCSeq: {req.h('cseq')}\r\n{extra}{ctype}"
            f"Content-Length: {len(body.encode())}\r\n\r\n{body}")


def _self_signed(tmp: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    cp, kp = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cp, kp


class FakePeer:
    """Proxy + targa in un colpo solo. Risponde da solo a REGISTER (con digest),
    OPTIONS, MESSAGE, INFO, BYE e CANCEL; gli INVITE li gestisce `on_invite`.
    Come il relay cloud Vimar, non manda MAI l'ACK del nostro 200 OK a uno squillo
    (m4r1k, 40517: nessun ACK anche a chiamata perfetta)."""

    def __init__(self, transport: str = "udp"):
        self.transport = transport
        self.log: list[Msg] = []
        self._new = asyncio.Event()
        self.on_invite = None          # async fn(peer, msg)
        self.register_code = None      # forza una risposta finale alla REGISTER
        self.info_auth = False         # il proxy sfida (407) gli INFO senza Proxy-Authorization
        self._inv_branch: dict[str, str] = {}   # branch dell'INVITE mandato, per il CANCEL
        self._forked: dict[str, str] = {}       # cid → branch del ramo doppio (fork_ring)
        self.connections = 0
        self.client_addr = None
        self._writer = None
        self._udp = None
        self.pending_invite: Msg | None = None   # INVITE ricevuto e non ancora concluso
        self.to_tag = "tgt" + str(random.randint(1000, 9999))
        self.retransmit_non2xx = False  # come un vero UAS UDP: rimanda il non-2xx fino all'ACK
        self._unacked: dict[str, tuple[str, asyncio.Task]] = {}
        # RTP della targa: riceve la voce del nostro microfono
        self.rtp_audio = self.rtp_video = None
        self.audio_rx: list[bytes] = []
        self.key: str | None = None   # SRTP della targa (Rig(srtp=True))
        self.pt = 96                  # payload type video che la targa offre
        # Proxy cloud (tls): mette il suo Record-Route e, come Flexisip, scarta senza
        # risposta le richieste nel dialogo che non lo riportano come Route o non
        # sono dirette al Contact (finiscono in `dropped`).
        self.record_route = ""
        self.strict_route = transport == "tls"
        self.dropped: list[Msg] = []
        self.silent: set[str] = set()   # metodi che riceve ma non risponde mai (cloud sordo)

    # ─── trasporto ──────────────────────────────────────────────────────
    async def start(self):
        loop = asyncio.get_running_loop()
        if self.transport == "udp":
            peer = self

            class P(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    peer.client_addr = addr
                    peer._on_raw(data.decode(errors="replace"))

            self._udp, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
            self.port = self._udp.get_extra_info("sockname")[1]
        else:
            self._tmp = tempfile.mkdtemp()
            cp, kp = _self_signed(self._tmp)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cp, kp)
            self._server = await asyncio.start_server(self._conn, "127.0.0.1", 0, ssl=ctx)
            self.port = self._server.sockets[0].getsockname()[1]
            self.record_route = f"<sip:127.0.0.1:{self.port};transport=tls;lr>"
        self.contact_uri = f"sip:{PANEL}@127.0.0.1:{self.port}"

        peer = self

        class Rtp(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if data and (data[0] & 0xC0) == 0x80:
                    peer.audio_rx.append(data)

        self.rtp_audio, _ = await loop.create_datagram_endpoint(Rtp, local_addr=("127.0.0.1", 0))
        self.rtp_video, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        return self

    async def _conn(self, reader, writer):
        self.connections += 1
        self._writer = writer
        buf = b""
        try:
            while chunk := await reader.read(8192):
                buf += chunk
                while b"\r\n\r\n" in buf:
                    end = buf.index(b"\r\n\r\n") + 4
                    head = buf[:end].decode(errors="replace")
                    if not head.strip():
                        buf = buf[end:]
                        continue  # keepalive CRLF
                    cl = next((int(ln.split(":", 1)[1]) for ln in head.split("\r\n")
                               if ln.lower().startswith("content-length:")), 0)
                    if len(buf) < end + cl:
                        break
                    raw, buf = buf[:end + cl].decode(errors="replace"), buf[end + cl:]
                    self._on_raw(raw)
        except (ConnectionError, ssl.SSLError):
            pass

    def drop(self):
        """Il proxy chiude la connessione TLS (riavvio, bilanciatore)."""
        if self._writer:
            self._writer.transport.abort()
            self._writer = None

    def send(self, raw: str):
        if self.transport == "udp":
            self._udp.sendto(raw.encode(), self.client_addr)
        elif self._writer:
            self._writer.write(raw.encode())

    async def stop(self):
        for _, t in self._unacked.values():
            t.cancel()
        for t in (self._udp, self.rtp_audio, self.rtp_video):
            if t:
                t.close()
        if self.transport == "tls":
            self.drop()
            self._server.close()

    @property
    def audio_port(self):
        return self.rtp_audio.get_extra_info("sockname")[1]

    @property
    def video_port(self):
        return self.rtp_video.get_extra_info("sockname")[1]

    def sdp(self):
        return peer_sdp(self.audio_port, self.video_port, self.key, self.pt)

    # ─── ricezione ──────────────────────────────────────────────────────
    def _on_raw(self, raw: str):
        if not raw.strip():
            return
        m = Msg(raw)
        self.log.append(m)
        self._new.set()
        if self._misrouted(m) or m.kind in self.silent:
            self.dropped.append(m)
            return
        asyncio.get_running_loop().create_task(self._auto(m))

    def _misrouted(self, m: Msg) -> bool:
        """Richiesta nel dialogo senza il nostro Record-Route come Route, o non diretta
        al Contact: il proxy la butta via (RFC 3261 §12.2.1.1 non rispettato)."""
        if not self.strict_route or isinstance(m.kind, int) or ";tag=" not in m.h("to"):
            return False
        if m.kind not in ("ACK", "BYE", "INFO", "UPDATE"):
            return False
        if m.kind == "ACK" and any(x.kind == "INVITE" and x.branch == m.branch for x in self.log):
            return False   # ACK di un non-2xx: transazione dell'INVITE, stessa strada
        return m.h("route") != self.record_route or m.first.split()[1] != self.contact_uri

    async def wait_for(self, pred, timeout: float = 5.0, start: int = 0) -> Msg:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            for m in self.log[start:]:
                if pred(m):
                    return m
            left = end - loop.time()
            if left <= 0:
                raise AssertionError(f"timeout: nessun messaggio atteso fra {self.log[start:]}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), left)
            except TimeoutError:
                pass

    def got(self, pred) -> list[Msg]:
        return [m for m in self.log if pred(m)]

    def reply(self, req: Msg, code: int, reason: str, body: str = "", tag: bool = True):
        extra = f"Contact: <{self.contact_uri}>\r\n" if code < 300 else ""
        if req.kind == "INVITE" and self.record_route and code < 300:
            extra += f"Record-Route: {self.record_route}\r\n"
        raw = response(req, code, reason, self.to_tag if tag and code > 100 else None, body, extra)
        self.send(raw)
        if req.kind == "INVITE" and code >= 300 and self.retransmit_non2xx:
            self._unacked[req.branch] = (raw, asyncio.get_running_loop().create_task(self._retx(raw)))
        return raw

    async def _retx(self, raw):
        delay = 0.1
        for _ in range(6):
            await asyncio.sleep(delay)
            self.send(raw)
            delay *= 2

    # ─── risposte automatiche ───────────────────────────────────────────
    async def _auto(self, m: Msg):
        if isinstance(m.kind, int):
            dup = self._forked.pop(m.cid, None) if m.kind == 200 and m.method == "INVITE" else None
            if dup:  # il relay annulla il suo ramo doppio ~70 ms dopo il nostro 200 OK
                await asyncio.sleep(0.07)
                self.request("CANCEL", m.cid, 1, sip._tag(m.h("from")), branch=dup)
            return
        if m.kind == "INFO" and self.info_auth and "proxy-authorization" not in m.hdrs:
            self.send(response(m, 407, "Proxy Authentication Required", extra=(
                f'Proxy-Authenticate: Digest realm="{DOMAIN}", nonce="n-info"\r\n')))
            return
        if m.kind == "REGISTER":
            if self.register_code:
                self.reply(m, self.register_code, "Forced", tag=False)
            elif "authorization" not in m.hdrs:
                self.send(response(m, 401, "Unauthorized", extra=(
                    f'WWW-Authenticate: Digest realm="{DOMAIN}", nonce="n0nce", qop="auth"\r\n')))
            else:
                ok = check_digest(m, "REGISTER", m.h("authorization"))
                self.reply(m, 200 if ok else 403, "OK" if ok else "Forbidden", tag=False)
        elif m.kind in ("OPTIONS", "MESSAGE", "INFO", "BYE"):
            self.reply(m, 200, "OK")
        elif m.kind == "CANCEL":
            self.reply(m, 200, "OK")
            inv = self.pending_invite
            if inv and inv.cid == m.cid:
                self.pending_invite = None
                self.reply(inv, 487, "Request Terminated")
        elif m.kind == "ACK":
            hit = self._unacked.pop(m.branch, None)
            if hit:
                hit[1].cancel()
        elif m.kind == "INVITE" and self.on_invite:
            self.pending_invite = m
            await self.on_invite(self, m)

    # ─── richieste della targa verso HA ─────────────────────────────────
    def request(self, method: str, cid: str, cseq: int, from_tag: str, to_tag: str | None = None,
                body: str = "", branch: str | None = None, cseq_method: str | None = None) -> str:
        # Il CANCEL porta il branch dell'INVITE che annulla (RFC 3261 §9.1).
        branch = branch or (self._inv_branch.get(cid) if method == "CANCEL" else None) \
            or "z9hG4bKp" + str(random.randint(10**6, 10**7))
        if method == "INVITE":
            self._inv_branch[cid] = branch
        to = f"<sip:{USER}@{DOMAIN}>" + (f";tag={to_tag}" if to_tag else "")
        ctype = "Content-Type: application/sdp\r\n" if body else ""
        proto = "UDP" if self.transport == "udp" else "TLS"
        rr = f"Record-Route: {self.record_route}\r\n" if self.record_route and method == "INVITE" else ""
        raw = (f"{method} sip:{USER}@127.0.0.1 SIP/2.0\r\n"
               f"Via: SIP/2.0/{proto} 127.0.0.1:{self.port};branch={branch}\r\n{rr}"
               f"From: <sip:{PANEL}@{DOMAIN}>;tag={from_tag}\r\nTo: {to}\r\n"
               f"Call-ID: {cid}\r\nCSeq: {cseq} {cseq_method or method}\r\n"
               f"Contact: <{self.contact_uri}>\r\nUser-Agent: baresip-fake\r\n"
               f"Max-Forwards: 70\r\n{ctype}Content-Length: {len(body.encode())}\r\n\r\n{body}")
        self.send(raw)
        return raw

    def fork_ring(self, cid: str, from_tag: str, body: str) -> str:
        """Lo squillo come lo biforca il relay cloud: due INVITE dello stesso Call-ID
        con branch diversi a pochi ms l'uno dall'altro (40515 e 40517 sul campo), e
        un CANCEL del ramo doppio ~70 ms dopo il nostro 200 OK. Restituisce il
        branch del primo ramo (quello vero)."""
        real, dup = "z9hG4bKf1" + cid, "z9hG4bKf2" + cid
        self.request("INVITE", cid, 1, from_tag, body=body, branch=real)
        self.request("INVITE", cid, 1, from_tag, body=body, branch=dup)
        self._inv_branch[cid] = real
        self._forked[cid] = dup
        return real


def check_digest(m: Msg, method: str, header: str) -> bool:
    p = {}
    for item in header.replace("Digest ", "").split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            p[k.strip()] = v.strip().strip('"')
    ha1 = hashlib.md5(f"{USER}:{p['realm']}:{PASSWORD}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{p['uri']}".encode()).hexdigest()
    if p.get("qop") == "auth":
        exp = hashlib.md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}".encode()).hexdigest()
    else:
        exp = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
    return exp == p.get("response")
