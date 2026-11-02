"""Scrittura controllata dei set point del controllore iC208.

Puo' scrivere solo i registri elencati in SETPOINTS, entro i limiti indicati.
Dopo la scrittura rilegge il registro e confronta il valore.
"""

import socket
import time

from leggi_ichill import Bus, EXCEPTIONS, WAIT, crc16

CONTROLLER = 1
SETPOINTS = {
    "Pompa di calore": {"saved": 1538, "volatile": 1542, "min": 20.0, "max": 45.0},
    "Sanitario": {"saved": 1539, "volatile": 1543, "min": 35.0, "max": 55.0},
    "Chiller": {"saved": 1537, "volatile": 1541, "min": 7.0, "max": 25.0},
    "Antilegionella": {"saved": 1540, "volatile": 1544, "min": 55.0, "max": 70.0},
}


class SetpointError(Exception):
    """Scrittura rifiutata o non verificabile."""


def register_for(name: str, volatile: bool) -> int:
    return SETPOINTS[name]["volatile" if volatile else "saved"]


def build_write(register: int, raw: int) -> bytes:
    pdu = bytes([CONTROLLER, 6]) + register.to_bytes(2, "big") + raw.to_bytes(2, "big", signed=True)
    return pdu + crc16(pdu)


def write_setpoint(host: str, port: int, name: str, celsius: float, volatile: bool) -> dict:
    limits = SETPOINTS[name]
    if not limits["min"] <= celsius <= limits["max"]:
        raise SetpointError(f"{celsius:.1f} °C è fuori dai limiti {limits['min']}-{limits['max']} °C")

    register = register_for(name, volatile)
    raw = int(round(celsius * 10))
    request = build_write(register, raw)

    with socket.create_connection((host, port), timeout=5) as sock:
        bus = Bus(sock, CONTROLLER, 1.0, [])
        time.sleep(WAIT)
        values, error = bus.read(register)
        if values is None:
            raise SetpointError(f"lettura iniziale non riuscita: {error}")
        before = values[0]

        time.sleep(WAIT)
        sock.sendall(request)
        reply = bus._receive(len(request))
        bus.last = time.monotonic()
        if len(reply) == 5 and reply[1] == 0x86 and crc16(reply[:3]) == reply[3:]:
            code = reply[2]
            raise SetpointError(f"rifiutata dal controllore, eccezione {code}: {EXCEPTIONS.get(code, 'sconosciuta')}")
        if reply != request:
            raise SetpointError("conferma assente o non valida: controlla il valore sul display")

        values, error = bus.read(register)
        if values is None:
            raise SetpointError(f"scrittura inviata, ma rilettura non riuscita: {error}")
        after = values[0]

    return {"registro": register, "prima": before, "scritto": raw, "dopo": after, "esito": after == raw}
