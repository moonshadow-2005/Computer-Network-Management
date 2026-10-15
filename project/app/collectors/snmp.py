"""Small read-only SNMP v2c client for the OIDs used by this course.

No SET/TRAP support is intentionally provided.  This avoids a dependency on
Net-SNMP in the isolated DEV network.  Every response is checked against its
request ID, community and sender before any value is accepted.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from dataclasses import dataclass


class SnmpError(Exception):
    pass


class SnmpEndOfMib(SnmpError):
    pass


def _length(n: int) -> bytes:
    if n < 128:
        return bytes((n,))
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(raw),)) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes((tag,)) + _length(len(value)) + value


def _integer(value: int) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)
    return _tlv(0x02, value.to_bytes(size, "big", signed=True))


def _subid(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative OID component")
    chunks = [n & 0x7F]
    n >>= 7
    while n:
        chunks.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(chunks))


def _oid(parts: tuple[int, ...]) -> bytes:
    if len(parts) < 2 or parts[0] not in (0, 1, 2) or parts[1] < 0:
        raise ValueError("invalid OID")
    if parts[0] < 2 and parts[1] > 39:
        raise ValueError("invalid OID second component")
    payload = _subid(40 * parts[0] + parts[1])
    for part in parts[2:]:
        payload += _subid(part)
    return _tlv(0x06, payload)


def _read(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    if pos + 2 > len(data):
        raise SnmpError("truncated SNMP value")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first & 0x80:
        count = first & 0x7F
        if not 1 <= count <= 3 or pos + count > len(data):
            raise SnmpError("invalid SNMP length")
        size = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    else:
        size = first
    if pos + size > len(data):
        raise SnmpError("truncated SNMP payload")
    return tag, data[pos:pos + size], pos + size


def _expect(data: bytes, tag: int, pos: int = 0) -> tuple[bytes, int]:
    found, payload, end = _read(data, pos)
    if found != tag:
        raise SnmpError(f"unexpected SNMP tag {found:#x}")
    return payload, end


def _int(data: bytes) -> int:
    value, end = _expect(data, 0x02)
    if end != len(data) or not value:
        raise SnmpError("invalid SNMP integer")
    return int.from_bytes(value, "big", signed=True)


def _decode_oid(data: bytes) -> tuple[int, ...]:
    raw, end = _expect(data, 0x06)
    if end != len(data) or not raw:
        raise SnmpError("invalid SNMP OID")
    numbers = []
    acc = 0
    for byte in raw:
        acc = (acc << 7) | (byte & 0x7F)
        if not byte & 0x80:
            numbers.append(acc)
            acc = 0
    if raw[-1] & 0x80:
        raise SnmpError("incomplete SNMP OID")
    first = numbers.pop(0)
    if first < 40:
        return (0, first, *numbers)
    if first < 80:
        return (1, first - 40, *numbers)
    return (2, first - 80, *numbers)


def parse_oid(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(piece) for piece in value.strip(".").split("."))
    except ValueError as exc:
        raise ValueError("OID must contain decimal integers") from exc


@dataclass(frozen=True)
class SnmpValue:
    oid: tuple[int, ...]
    value: int | str | None
    tag: int


def _request(community: str, request_id: int, oid: tuple[int, ...], next_item: bool) -> bytes:
    binding = _tlv(0x30, _oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA1 if next_item else 0xA0,
               _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, binding))
    return _tlv(0x30, _integer(1) + _tlv(0x04, community.encode("utf-8")) + pdu)


def _request_bulk(community: str, request_id: int, oid: tuple[int, ...],
                  max_repetitions: int) -> bytes:
    if not 1 <= max_repetitions <= 16:
        raise ValueError("SNMP GETBULK repetitions must be between 1 and 16")
    binding = _tlv(0x30, _oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA5, _integer(request_id) + _integer(0) +
               _integer(max_repetitions) + _tlv(0x30, binding))
    return _tlv(0x30, _integer(1) + _tlv(0x04, community.encode("utf-8")) + pdu)


def _response_values(packet: bytes, request_id: int, community: str) -> list[SnmpValue]:
    body, end = _expect(packet, 0x30)
    if end != len(packet):
        raise SnmpError("trailing SNMP bytes")
    version_tag, version_value, pos = _read(body)
    if version_tag != 0x02 or int.from_bytes(version_value, "big", signed=True) != 1:
        raise SnmpError("SNMP version mismatch")
    returned_community, pos = _expect(body, 0x04, pos)
    if returned_community != community.encode("utf-8"):
        raise SnmpError("SNMP community mismatch")
    pdu, pos = _expect(body, 0xA2, pos)
    if pos != len(body):
        raise SnmpError("trailing SNMP message bytes")
    rid_tag, rid_value, pos = _read(pdu)
    if rid_tag != 0x02 or int.from_bytes(rid_value, "big", signed=True) != request_id:
        raise SnmpError("SNMP request ID mismatch")
    status_tag, status_value, pos = _read(pdu, pos)
    index_tag, _, pos = _read(pdu, pos)
    if status_tag != 0x02 or index_tag != 0x02:
        raise SnmpError("invalid SNMP error fields")
    error_status = int.from_bytes(status_value, "big", signed=True)
    if error_status:
        raise SnmpError("SNMP agent reported an error")
    bindings, pos = _expect(pdu, 0x30, pos)
    if pos != len(pdu):
        raise SnmpError("trailing SNMP PDU bytes")
    values: list[SnmpValue] = []
    binding_pos = 0
    while binding_pos < len(bindings):
        binding, binding_pos = _expect(bindings, 0x30, binding_pos)
        oid_tag, oid_payload, value_pos = _read(binding)
        if oid_tag != 0x06:
            raise SnmpError("missing SNMP object ID")
        oid = _decode_oid(_tlv(oid_tag, oid_payload))
        tag, raw, value_pos = _read(binding, value_pos)
        if value_pos != len(binding):
            raise SnmpError("trailing SNMP binding bytes")
        if tag == 0x82:
            value = None
        elif tag in (0x80, 0x81):
            raise SnmpError("SNMP object unavailable")
        elif tag in (0x02, 0x41, 0x42, 0x43, 0x46):
            value = int.from_bytes(raw, "big", signed=tag == 0x02)
        elif tag == 0x04:
            value = raw.decode("utf-8", errors="replace")
        elif tag == 0x40 and len(raw) == 4:
            value = str(ipaddress.IPv4Address(raw))
        elif tag == 0x06:
            value = ".".join(str(x) for x in _decode_oid(_tlv(tag, raw)))
        elif tag == 0x05:
            value = None
        else:
            raise SnmpError(f"unsupported SNMP value tag {tag:#x}")
        values.append(SnmpValue(oid, value, tag))
    return values


def _response(packet: bytes, request_id: int, community: str) -> SnmpValue:
    values = _response_values(packet, request_id, community)
    if len(values) != 1:
        raise SnmpError("unexpected number of SNMP bindings")
    if values[0].tag == 0x82:
        raise SnmpEndOfMib("end of SNMP table")
    return values[0]


class SnmpClient:
    def __init__(self, community: str, timeout_s: float = 1.5):
        if not community:
            raise ValueError("SNMP community is required")
        self._community = community
        self.timeout_s = timeout_s
        self._bulk_disabled: set[str] = set()

    def _exchange(self, address: str, packet: bytes, request_id: int) -> bytes:
        ipaddress.IPv4Address(address)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout_s)
                sock.sendto(packet, (address, 161))
                response, sender = sock.recvfrom(65535)
        except socket.timeout as exc:
            raise SnmpError("SNMP query timed out") from exc
        except OSError as exc:
            raise SnmpError("SNMP transport failed") from exc
        if sender != (address, 161):
            raise SnmpError("SNMP sender mismatch")
        return response

    def _call(self, address: str, oid: tuple[int, ...], next_item: bool) -> SnmpValue:
        request_id = secrets.randbelow(2**31 - 1) + 1
        packet = _request(self._community, request_id, oid, next_item)
        response = self._exchange(address, packet, request_id)
        result = _response(response, request_id, self._community)
        if not next_item and result.oid != oid:
            raise SnmpError("SNMP object ID mismatch")
        return result

    def _call_bulk(self, address: str, oid: tuple[int, ...],
                   max_repetitions: int) -> list[SnmpValue]:
        request_id = secrets.randbelow(2**31 - 1) + 1
        packet = _request_bulk(self._community, request_id, oid, max_repetitions)
        return _response_values(self._exchange(address, packet, request_id),
                                request_id, self._community)

    def get(self, address: str, oid: str | tuple[int, ...]) -> SnmpValue:
        return self._call(address, parse_oid(oid) if isinstance(oid, str) else oid, False)

    def get_next(self, address: str, oid: str | tuple[int, ...]) -> SnmpValue:
        parsed = parse_oid(oid) if isinstance(oid, str) else oid
        value = self._call(address, parsed, True)
        if value.oid <= parsed:
            raise SnmpError("SNMP table did not advance")
        return value

    def get_bulk(self, address: str, oid: str | tuple[int, ...],
                 max_repetitions: int = 8) -> list[SnmpValue]:
        parsed = parse_oid(oid) if isinstance(oid, str) else oid
        values = self._call_bulk(address, parsed, max_repetitions)
        cursor = parsed
        for value in values:
            if value.tag == 0x82:
                break
            if value.oid <= cursor:
                raise SnmpError("SNMP GETBULK table did not advance")
            cursor = value.oid
        return values

    def walk(self, address: str, prefix: str, limit: int) -> list[SnmpValue]:
        root = parse_oid(prefix)
        cursor = root
        found: list[SnmpValue] = []
        while len(found) < limit:
            if address not in self._bulk_disabled:
                try:
                    batch = self.get_bulk(address, cursor, min(8, limit - len(found)))
                    if not batch:
                        raise SnmpError("SNMP GETBULK returned no bindings")
                except SnmpError:
                    self._bulk_disabled.add(address)
                    try:
                        batch = [self.get_next(address, cursor)]
                    except SnmpEndOfMib:
                        break
            else:
                try:
                    batch = [self.get_next(address, cursor)]
                except SnmpEndOfMib:
                    break

            for value in batch:
                if value.tag == 0x82:
                    return found
                if value.oid <= cursor:
                    raise SnmpError("SNMP table did not advance")
                if value.oid[:len(root)] != root:
                    return found
                found.append(value)
                cursor = value.oid
                if len(found) >= limit:
                    return found
        return found
