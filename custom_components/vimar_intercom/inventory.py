"""Inventory of the plant's devices, built from the SIP traffic.

On these plants every mobile device shares one SIP user and differs only by
identifier and name. Users therefore cannot tell which phones are paired, and
that matters: generating a new QR rotates the shared credential and unpairs
the others.

The information already flows through the traffic the integration handles:

* the ``200 OK`` to a ``REGISTER`` lists a ``Contact`` for each binding of the
  account, with its ``+sip.instance`` and the granted ``expires``;
* incoming requests carry ``MyName``, ``Mobile-IMEI``, ``User-Agent`` and a
  ``Via`` chain whose ``received=``/``rport=`` reveal the real address.

This module gathers those pieces into one list. It is deliberately pure (no
Home Assistant, no sockets) so it can be tested on its own.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict

# "<sip:60901@127.0.0.1:5060;transport=tls>;tag=abc" → "60901"
_SIP_ID = re.compile(r"sips?:([^@;>\s]+)@")
_URI_HOST = re.compile(r"sips?:[^@;>\s]+@([^;>\s]+)")
_INSTANCE = re.compile(r'\+sip\.instance\s*=\s*"?<?urn:uuid:([^">\s;]+)', re.I)
_EXPIRES = re.compile(r";\s*expires\s*=\s*(\d+)", re.I)
_RECEIVED = re.compile(r";\s*received\s*=\s*([^;\s]+)", re.I)
_RPORT = re.compile(r";\s*rport\s*=\s*(\d+)", re.I)


# Longest text kept per field: these values come from any SIP peer.
MAX_FIELD_LEN = 128
# What a masked device identifier starts with (see mask_device_id).
MASK_PREFIX = "***"


def _clip(value: str) -> str:
    return value[:MAX_FIELD_LEN]


def mask_device_id(device_id: str) -> str:
    """The identifier as shown in Home Assistant: its last 6 characters.

    The full value stays in memory for merging; the state attribute (and
    what is restored from it after a restart) only carries the mask.
    """
    if not device_id or len(device_id) <= 6 or device_id.startswith(MASK_PREFIX):
        return device_id
    return MASK_PREFIX + device_id[-6:]


def sip_id(value: str) -> str:
    """The SIP user inside a URI or a From/To header."""
    match = _SIP_ID.search(value or "")
    return match.group(1) if match else ""


def _uri_host(value: str) -> str:
    match = _URI_HOST.search(value or "")
    return match.group(1) if match else ""


def _first(pattern, value: str) -> str:
    match = pattern.search(value or "")
    return match.group(1) if match else ""


@dataclass
class Device:
    """A device seen on the plant."""

    sip_id: str
    device_id: str = ""
    name: str = ""
    user_agent: str = ""
    address: str = ""
    expires: int | None = None
    is_self: bool = False
    registered: bool = False
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def as_dict(self, public: bool = False) -> dict:
        data = asdict(self)
        data["first_seen"] = round(self.first_seen)
        data["last_seen"] = round(self.last_seen)
        if public:
            # Every Home Assistant user sees the attribute: where a phone
            # connects from (its home or mobile IP) is not theirs to see.
            data["device_id"] = mask_device_id(self.device_id)
            data.pop("address")
        return data


class DeviceInventory:
    """Collects the devices seen, without duplicates."""

    def __init__(self, max_devices: int = 64):
        self._devices: dict[str, Device] = {}
        self._max_devices = max_devices

    # ─── Collection ──────────────────────────────────────────────────

    def note_binding(
        self, contact: str, *, own_device_id: str = "", own_name: str = "",
    ) -> Device | None:
        """Record a binding taken from a ``Contact`` of a 200 response.

        ``own_device_id`` is our ``+sip.instance`` UUID. A binding says the
        device is registered now, and for how long: it is the only source that
        also lists devices that are not talking at the moment.
        """
        if not contact:
            return None
        user = sip_id(contact)
        if not user:
            return None
        instance = _first(_INSTANCE, contact)
        expires = _first(_EXPIRES, contact)
        is_self = bool(own_device_id and instance == own_device_id)
        device = self._merge(
            key=instance or f"{user}@{_uri_host(contact)}",
            sip_id=user,
            device_id=instance,
            address=_uri_host(contact),
            expires=int(expires) if expires else None,
            registered=True,
            is_self=is_self,
            # A binding carries no name: ours gets the one we paired with, so
            # the list does not show a bare identifier.
            name=own_name if is_self else "",
        )
        return device

    def note_peer(self, headers: dict, *, own_device_id: str = "") -> Device | None:
        """Record a device from an incoming SIP request.

        ``headers`` are those already parsed by ``sip_client._parse``: the
        sender, its identity headers and the Via chain, which gives the address
        it really came from.
        """
        if not headers:
            return None
        user = sip_id(headers.get("from", ""))
        if not user:
            return None
        device_id = (headers.get("mobile-imei") or "").strip()
        via_chain = headers.get("_via_all") or ([headers["via"]] if headers.get("via") else [])
        address = ""
        for via in via_chain:
            # The last Via of the chain is the original sender's, and that is
            # where its public or LAN address shows up.
            received, rport = _first(_RECEIVED, via), _first(_RPORT, via)
            if received:
                address = f"{received}:{rport}" if rport else received
        return self._merge(
            key=device_id or f"{user}@{address}",
            sip_id=user,
            device_id=device_id,
            name=(headers.get("myname") or "").strip(),
            user_agent=(headers.get("user-agent") or "").strip(),
            address=address,
            is_self=bool(own_device_id and device_id == own_device_id),
        )

    def _find_masked(self, device_id: str, *, restored_only: bool) -> str | None:
        """The key of a device whose masked identifier matches this one.

        restored_only: only devices restored with a masked identifier (the
        live ones carry the full value).
        """
        masked = mask_device_id(device_id)
        if not masked.startswith(MASK_PREFIX):
            return None
        masked = masked.lower()
        for key, device in self._devices.items():
            if restored_only and not device.device_id.startswith(MASK_PREFIX):
                continue
            if mask_device_id(device.device_id).lower() == masked:
                return key
        return None

    def _merge(self, key: str, **values) -> Device:
        # Lowercase: the same phone must not show up twice because one
        # message spells its identifier in capitals.
        key = _clip(key).lower()
        device = self._devices.get(key)
        if device is None and values.get("device_id"):
            # Restored after a restart with a masked identifier: same device.
            old = self._find_masked(values["device_id"], restored_only=True)
            if old is not None:
                device = self._devices.pop(old)
                self._devices[key] = device
        if device is None:
            if len(self._devices) >= self._max_devices:
                # An odd plant or unexpected traffic: forget the oldest rather
                # than grow without bound.
                oldest = min(self._devices, key=lambda k: self._devices[k].last_seen)
                del self._devices[oldest]
            device = Device(sip_id=values.get("sip_id", ""))
            self._devices[key] = device
        for name, value in values.items():
            if isinstance(value, str):
                value = _clip(value)
            # A value missing from this message must not erase what an
            # earlier message already told us. expires=0 is a value (the
            # binding is gone), not a missing one.
            missing = value is None or value == "" or value is False
            if missing and getattr(device, name, None):
                continue
            setattr(device, name, value)
        device.last_seen = time.time()
        return device

    def load(self, items) -> int:
        """Seed the inventory from a saved snapshot (the sensor's last state).

        Phones only show up when they talk to us, and the list lived only in
        memory: every restart emptied it. Registration state is not restored,
        the next REGISTER tells it again. Returns how many were loaded.
        """
        loaded = 0
        for item in items or ():
            if not isinstance(item, dict) or not isinstance(item.get("sip_id"), (str, int)):
                continue
            sip = _clip(str(item["sip_id"]))
            text = {name: _clip(item[name]) for name in
                    ("device_id", "name", "user_agent", "address")
                    if isinstance(item.get(name), str) and item[name]}
            if not sip:
                continue
            device_id = text.get("device_id", "")
            key = (device_id or f"{sip}@{text.get('address', '')}").lower()
            if (key in self._devices or len(self._devices) >= self._max_devices
                    or (device_id and self._find_masked(device_id, restored_only=False)
                        is not None)):
                continue
            device = Device(sip_id=sip)
            for name, value in text.items():
                setattr(device, name, value)
            if item.get("is_self") is True:
                device.is_self = True
            for name in ("first_seen", "last_seen"):
                if isinstance(item.get(name), (int, float)):
                    setattr(device, name, float(item[name]))
            self._devices[key] = device
            loaded += 1
        return loaded

    def forget_bindings(self) -> None:
        """Forget the registration state before reading it again."""
        for device in self._devices.values():
            device.registered = False

    # ─── Reading ─────────────────────────────────────────────────────

    def snapshot(self, public: bool = False) -> list[dict]:
        """The known devices, most recent first.

        public: the form shown in Home Assistant, with the identifier masked
        and no address.
        """
        return [
            device.as_dict(public)
            for device in sorted(
                self._devices.values(), key=lambda d: d.last_seen, reverse=True
            )
        ]

    def describe(self, names: dict[str, str] | None = None,
                 public: bool = False) -> list[str]:
        """Readable lines, for diagnostics and logs (public: see snapshot)."""
        lines = []
        for device in sorted(self._devices.values(), key=lambda d: d.last_seen, reverse=True):
            label = device.name or (names or {}).get(device.sip_id) or device.sip_id
            parts = [f"{label} ({device.sip_id})"]
            if device.address and not public:
                parts.append(device.address)
            if device.user_agent:
                parts.append(device.user_agent.split("|")[0])
            if device.registered and device.expires:
                parts.append(f"registered, {device.expires}s")
            if device.is_self:
                parts.append("this installation")
            lines.append(", ".join(parts))
        return lines

    def __len__(self) -> int:
        return len(self._devices)
