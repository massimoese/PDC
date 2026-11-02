"""Legge lo stato di un Dixell iCHILL 200 via Modbus RTU su gateway trasparente.

Basato sulla lista registri Hidros "MODBUS-RTU Ichill 200, Release FW 2.3".
Esegue solo letture (funzione 03) e non scrive nulla sul controllore.
Rispetta i 500 ms minimi tra due richieste indicati dal documento.

Uso:
    python leggi_ichill.py 192.168.1.230
    python leggi_ichill.py 192.168.1.230 --slaves 1
"""

import argparse
import csv
import socket
import time
from datetime import datetime

WAIT = 0.5
MAX_WORDS = 40
UNITS = {0: "", 1: "°C", 2: "°F", 3: "%RH", 4: "PSI", 5: "bar", 6: "rpm", 7: "mA", 8: "A", 9: "mV", 10: "V"}
EXCEPTIONS = {
    1: "funzione non supportata",
    2: "area non valida",
    3: "elemento non presente",
    4: "errore di lettura",
    6: "dispositivo occupato",
}
SETPOINTS = {
    1536: "Set reale",
    1537: "Set chiller",
    1538: "Set pompa di calore",
    1539: "Set sanitario",
    1540: "Set antilegionella",
    1541: "Set chiller volatile",
    1542: "Set PDC volatile",
    1543: "Set sanitario volatile",
    1544: "Set antilegionella volatile",
}
HOURS = {
    3584: "Compressore 1",
    3585: "Compressore 2",
    3586: "Compressore 3",
    3587: "Compressore 4",
    3588: "Compressore 5",
    3589: "Compressore 6",
    3590: "Pompa evaporatore 1 / ventilatore",
    3591: "Pompa evaporatore 2 / ventilatore",
    3592: "Pompa condensatore 1",
    3593: "Pompa condensatore 2",
    3594: "Pompa sanitario",
    3595: "Pannelli solari",
    3596: "Pompa free cooling",
}
BITMAPS = [
    ("Ingressi digitali", 512, 6),
    ("Funzioni macchina", 1280, 4),
    ("Uscite relè", 2048, 12),
    ("Allarmi", 3328, 12),
]


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


class Bus:
    def __init__(self, sock: socket.socket, slave: int, timeout: float, rows: list):
        self.sock = sock
        self.slave = slave
        self.timeout = timeout
        self.rows = rows
        self.last = 0.0

    def _receive(self, expected: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        data = b""
        while time.monotonic() < deadline and len(data) < expected:
            self.sock.settimeout(max(0.01, min(0.1, deadline - time.monotonic())))
            try:
                chunk = self.sock.recv(256)
            except socket.timeout:
                if data:
                    break
                continue
            if not chunk:
                break
            data += chunk
        return data

    def read(self, reg: int, count: int = 1):
        """Restituisce (lista di valori, None) oppure (None, motivo)."""
        pause = WAIT - (time.monotonic() - self.last)
        if pause > 0:
            time.sleep(pause)
        request = bytes([self.slave, 3]) + reg.to_bytes(2, "big") + count.to_bytes(2, "big")
        self.sock.sendall(request + crc16(request))
        reply = self._receive(5 + 2 * count)
        self.last = time.monotonic()
        if not reply:
            return None, "nessuna risposta"
        if len(reply) < 5 or crc16(reply[:-2]) != reply[-2:] or reply[0] != self.slave:
            return None, "risposta non valida"
        if reply[1] == 0x83:
            return None, f"eccezione {reply[2]}: {EXCEPTIONS.get(reply[2], 'sconosciuta')}"
        if reply[1] != 3 or reply[2] != 2 * count or len(reply) != 5 + 2 * count:
            return None, "risposta non valida"
        return [int.from_bytes(reply[3 + 2 * i : 5 + 2 * i], "big") for i in range(count)], None

    def read_many(self, reg: int, count: int) -> dict:
        """Legge a blocchi; se un blocco fallisce riprova registro per registro."""
        result = {}
        for start in range(reg, reg + count, MAX_WORDS):
            size = min(MAX_WORDS, reg + count - start)
            values, error = self.read(start, size)
            if values is not None:
                for offset, value in enumerate(values):
                    result[start + offset] = (value, None)
            elif size == 1:
                result[start] = (None, error)
            else:
                for single in range(start, start + size):
                    one, error = self.read(single)
                    result[single] = (one[0], None) if one else (None, error)
        for register, (value, error) in sorted(result.items()):
            self.rows.append([self.slave, register, value if value is not None else "", error or ""])
        return result


def signed(value: int) -> int:
    return value - 65536 if value >= 32768 else value


def active_bits(value: int) -> str:
    bits = [f"MSB bit{b}" for b in range(8) if value >> (8 + b) & 1]
    bits += [f"LSB bit{b}" for b in range(8) if value >> b & 1]
    return ", ".join(bits)


def show_identity(bus: Bus) -> bool:
    data = bus.read_many(0, 12)
    if data[0][0] is None:
        print(f"  Identificazione: {data[0][1]}")
        return data[0][1] != "nessuna risposta"
    print(f"  Famiglia {data[0][0] >> 8}, firmware {data[0][0] & 0xFF}")
    code1, code2 = data[1][0], data[2][0]
    if code1 is not None and code2 is not None:
        text = "".join(chr(b) if 32 <= b < 127 else "?" for b in code1.to_bytes(2, "big"))
        print(f"  Codice strumento grezzo: {text} {code2 >> 6} ({code1:04X} {code2:04X})")
    return True


def show_probes(bus: Bus) -> None:
    data = bus.read_many(256, 24)
    print("  Sonde")
    for number in range(12):
        reg = 256 + 2 * number
        raw, raw_error = data[reg]
        status, _ = data[reg + 1]
        if raw is None:
            print(f"    Pb{number + 1:<2d} ({reg}): {raw_error}")
            continue
        if status is None:
            print(f"    Pb{number + 1:<2d} ({reg}): grezzo {signed(raw)}")
            continue
        high = status >> 8
        unit_code = high & 0x0F
        if unit_code == 0:
            continue
        value = signed(raw) / 10 if high >> 4 & 1 else signed(raw)
        fault = "  ERRORE SONDA" if status & 1 else ""
        print(f"    Pb{number + 1:<2d} ({reg}): {value} {UNITS.get(unit_code, '?')}{fault}")


def show_named(bus: Bus, title: str, names: dict, scale: float = 1) -> None:
    print(f"  {title}")
    for reg, (value, error) in bus.read_many(min(names), len(names)).items():
        if value is None:
            print(f"    {names[reg]} ({reg}): {error}")
        else:
            print(f"    {names[reg]} ({reg}): {signed(value) * scale:g}")


def show_bitmaps(bus: Bus) -> None:
    for title, reg, count in BITMAPS:
        print(f"  {title}")
        found = False
        for register, (value, error) in bus.read_many(reg, count).items():
            if value is None:
                print(f"    {register}: {error}")
                found = True
            elif value:
                print(f"    {register}: {value:04X}  ({active_bits(value)})")
                found = True
        if not found:
            print("    nessun bit attivo")


def show_extra(bus: Bus) -> None:
    print("  Uscite analogiche, in %")
    outputs = bus.read_many(2304, 12)
    for reg in range(2304, 2316, 2):
        value, error = outputs[reg]
        print(f"    {reg}: {value if value is not None else error}")
    print("  Orologio")
    for reg, (value, error) in bus.read_many(2816, 4).items():
        print(f"    {reg}: {value if value is not None else error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lettura stato iCHILL 200, sola lettura")
    parser.add_argument("host", help="IP del gateway")
    parser.add_argument("--port", type=int, default=4196)
    parser.add_argument("--slaves", default="1,3", help="indirizzi separati da virgola")
    parser.add_argument("--timeout", type=float, default=1.0, help="attesa in secondi")
    parser.add_argument("--out", default="ichill.csv", help="file CSV di uscita")
    args = parser.parse_args()

    rows = []
    with socket.create_connection((args.host, args.port), timeout=5) as sock:
        print(f"Connesso a {args.host}:{args.port}")
        for slave in (int(s) for s in args.slaves.split(",")):
            print(f"\nIndirizzo {slave}")
            bus = Bus(sock, slave, args.timeout, rows)
            if not show_identity(bus):
                continue
            show_probes(bus)
            show_named(bus, "Set point grezzi", SETPOINTS)
            show_named(bus, "Ore di funzionamento", HOURS)
            show_bitmaps(bus)
            show_extra(bus)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["ora", "indirizzo", "registro", "grezzo", "errore"])
        writer.writerows([stamp, *row] for row in rows)
    print(f"\nSalvato in {args.out}")


if __name__ == "__main__":
    main()
