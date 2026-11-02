"""Cruscotto Streamlit per i dati della pompa di calore (iC208 + IEV22D).

Mostra i CSV in data/ e, se attivata, la lettura dal vivo dal gateway Modbus.
Può scrivere solo i set point, dopo abilitazione e conferma esplicite.
"""

import csv
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from leggi_ichill import Bus  # noqa: E402
from registri import BITS  # noqa: E402
from scrivi_set import SETPOINTS as WRITABLE  # noqa: E402
from scrivi_set import SetpointError, register_for, write_setpoint  # noqa: E402

DATA_DIR = ROOT / "data"
WRITE_LOG = ROOT / "logs" / "scritture.csv"
CONTROLLER = 1
VALVE = 3
MAX_TREND = 720
UNITS = {1: "°C", 2: "°F", 3: "%RH", 4: "PSI", 5: "bar", 6: "rpm", 7: "mA", 8: "A", 9: "mV", 10: "V"}
SETPOINTS = {
    1536: "Set reale",
    1538: "Pompa di calore",
    1537: "Chiller",
    1539: "Sanitario",
    1540: "Antilegionella",
}
HOURS = {3584: "Compressore 1", 3590: "Pompa principale"}
PROBE_NAMES = {
    1: "Ingresso acqua",
    2: "Uscita acqua",
    3: "Alta pressione",
    4: "Serbatoio sanitario",
    5: "Aria esterna",
    6: "Mandata compressore",
    7: "Sonda remota display",
}
ANALOG = {2304: "AO1 modulazione inverter", 2305: "AO2 resistenza ausiliaria", 2306: "AO3 modulazione ventilatori", 2307: "AO4 limitazione inverter"}
LIVE_BLOCKS = {
    CONTROLLER: [(0, 7), (256, 24), (512, 6), (1280, 4), (1536, 9), (2048, 10), (2304, 4), (2816, 4), (3328, 12), (3584, 1), (3590, 1)],
    VALVE: [(0, 7), (256, 24)],
}
COLUMNS = ["ora", "indirizzo", "registro", "grezzo", "errore"]


def read_csv(source) -> pd.DataFrame:
    frame = pd.read_csv(source, sep=";")
    frame["ora"] = pd.to_datetime(frame["ora"])
    frame["grezzo"] = pd.to_numeric(frame["grezzo"], errors="coerce")
    return frame


@st.cache_data(ttl=60)
def load_folder() -> pd.DataFrame:
    frames = [read_csv(path) for path in sorted(DATA_DIR.glob("*.csv"))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


@st.cache_resource
def bus_lock() -> threading.Lock:
    """Evita che lettura dal vivo e scrittura usino il bus insieme."""
    return threading.Lock()


def log_write(name: str, volatile: bool, result: dict) -> None:
    WRITE_LOG.parent.mkdir(exist_ok=True)
    new_file = not WRITE_LOG.exists()
    with open(WRITE_LOG, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        if new_file:
            writer.writerow(["ora", "set", "tipo", "registro", "prima", "scritto", "dopo", "esito"])
        writer.writerow(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                name,
                "temporaneo" if volatile else "permanente",
                result["registro"],
                result["prima"],
                result["scritto"],
                result["dopo"],
                "ok" if result["esito"] else "diverso",
            ]
        )


def read_live(host: str, port: int) -> None:
    """Legge il gateway e aggiunge la lettura al CSV del giorno."""
    rows = []
    with bus_lock(), socket.create_connection((host, port), timeout=5) as sock:
        for slave, blocks in LIVE_BLOCKS.items():
            bus = Bus(sock, slave, 1.0, rows)
            _, error = bus.read(0)
            if error == "nessuna risposta":
                if slave == CONTROLLER:
                    raise ConnectionError(f"il controllore all'indirizzo {slave} non risponde")
                continue
            for register, count in blocks:
                bus.read_many(register, count)

    now = datetime.now()
    path = DATA_DIR / f"live_{now:%Y%m%d}.csv"
    frame = pd.DataFrame(rows, columns=COLUMNS[1:])
    frame.insert(0, "ora", now.strftime("%Y-%m-%d %H:%M:%S"))
    DATA_DIR.mkdir(exist_ok=True)
    frame.to_csv(path, sep=";", index=False, mode="a", header=not path.exists())
    load_folder.clear()


def signed(value: float) -> int:
    value = int(value)
    return value - 65536 if value >= 32768 else value


def to_dict(frame: pd.DataFrame, slave: int) -> dict:
    rows = frame[(frame["indirizzo"] == slave) & frame["grezzo"].notna()]
    return {int(reg): int(value) for reg, value in zip(rows["registro"], rows["grezzo"])}


def identity(regs: dict) -> str:
    if not {0, 1, 2}.issubset(regs):
        return "non disponibile"
    code = regs[1].to_bytes(2, "big").decode("ascii", "replace")
    suffix = chr((regs[2] & 0x3F) + 0x20).strip("! ")
    firmware = regs[0] & 0xFF
    text = f"{code}{regs[2] >> 6}{suffix}, firmware {firmware // 10}.{firmware % 10}"
    if 5 in regs:
        day, month, year = regs[5] >> 11, regs[5] >> 7 & 15, regs[5] & 127
        text += f" del {day:02d}/{month:02d}/{2000 + year}"
    return text


def probes(regs: dict) -> list:
    result = []
    for number in range(12):
        raw = regs.get(256 + 2 * number)
        status = regs.get(257 + 2 * number)
        if raw is None or status is None or not (status >> 8) & 0x0F:
            continue
        value = signed(raw) / 10 if status >> 12 & 1 else signed(raw)
        unit = UNITS.get(status >> 8 & 0x0F, "?")
        result.append(
            {
                "Sonda": f"Pb{number + 1}",
                "Funzione": PROBE_NAMES.get(number + 1, ""),
                "Valore": value,
                "Unità": unit,
                "Errore": bool(status & 1),
            }
        )
    return result


def active_bits(regs: dict, first: int, last: int) -> list:
    """Restituisce i nomi dei bit attivi. Nelle uscite si guarda solo l'MSB, cioè i bit 8-15."""
    names = []
    for register in range(first, last + 1):
        table = BITS.get(register, {})
        value = regs.get(register, 0)
        names += [table[bit] for bit in sorted(table) if value >> bit & 1]
    return names


def circuit(controller: dict, valve: dict) -> list:
    rows = []

    def add(label, regs, register, unit, decimals=True):
        if register in regs:
            value = signed(regs[register])
            rows.append({"Grandezza": label, "Valore": value / 10 if decimals else value, "Unità": unit})

    add("Alta pressione", controller, 260, "bar")
    add("Bassa pressione", valve, 258, "bar")
    add("Evaporazione", valve, 260, "°C")
    add("Aspirazione", valve, 256, "°C")
    add("Surriscaldamento", valve, 262, "K")
    add("Apertura valvola", valve, 264, "%")
    add("Passi valvola", valve, 266, "passi", decimals=False)
    return rows


def clock(regs: dict) -> str:
    if not {2816, 2817, 2818, 2819}.issubset(regs):
        return "non disponibile"
    return (
        f"{regs[2818] >> 8:02d}/{regs[2818] & 0xFF:02d}/{regs[2819]} "
        f"{regs[2817] >> 8:02d}:{regs[2816] & 0xFF:02d}:{regs[2816] >> 8:02d}"
    )


def mode(regs: dict) -> str:
    status = regs.get(1280, 0) >> 8
    if not status & 1:
        return "Spenta"
    if status & 8:
        return "PDC"
    if status & 4:
        return "Chiller"
    return "Accesa"


def alarms(regs: dict) -> list:
    names = []
    for register in range(3328, 3340):
        value = regs.get(register, 0)
        table = BITS.get(register, {})
        names += [table.get(bit, f"{register} bit {bit}") for bit in range(16) if value >> bit & 1]
    return names


def history(frame: pd.DataFrame) -> pd.DataFrame:
    recent = sorted(frame["ora"].unique())[-MAX_TREND:]
    rows = []
    for stamp, group in frame[frame["ora"].isin(recent)].groupby("ora"):
        for probe in probes(to_dict(group, CONTROLLER)):
            label = probe["Funzione"] or probe["Sonda"]
            rows.append({"ora": stamp, "serie": f"{label} ({probe['Unità']})", "valore": probe["Valore"]})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).pivot_table(index="ora", columns="serie", values="valore")


def show_status(snapshot: pd.DataFrame) -> tuple:
    controller = to_dict(snapshot, CONTROLLER)
    valve = to_dict(snapshot, VALVE)
    active_alarms = alarms(controller)
    special = controller.get(1283, 0) >> 8
    functions = controller.get(1282, 0) >> 8

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Modalità", mode(controller))
    col2.metric("Sanitario", "In corso" if special & 1 else "No")
    col3.metric("Sbrinamento", "In corso" if functions & 3 else "No")
    col4.metric("Allarmi", len(active_alarms))

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Compressore", "Acceso" if controller.get(2055, 0) >> 8 & 1 else "Spento")
    col6.metric("Inverter", f"{controller[2304]} %" if 2304 in controller else "n.d.")
    col7.metric("Valvola sanitario", "Attiva" if controller.get(2052, 0) >> 8 & 1 else "No")
    col8.metric("Pompa principale", "Accesa" if controller.get(2048, 0) >> 9 & 1 else "Spenta")

    if active_alarms:
        st.error("Allarmi attivi: " + ", ".join(active_alarms))

    left, right = st.columns(2)
    with left:
        st.subheader("Sonde")
        st.dataframe(pd.DataFrame(probes(controller)), hide_index=True, width="stretch")
        st.caption("Funzioni da tabella Enerblue Orange Inverter, da confermare sullo schema della macchina.")
    with right:
        st.subheader("Set point")
        table = [
            {"Set": name, "°C": signed(controller[reg]) / 10}
            for reg, name in SETPOINTS.items()
            if reg in controller
        ]
        st.dataframe(
            pd.DataFrame(table),
            hide_index=True,
            width="stretch",
            column_config={"°C": st.column_config.NumberColumn(format="%.1f")},
        )
        st.caption("Valori in decimi di °C.")

    st.subheader("Circuito frigorifero")
    st.dataframe(pd.DataFrame(circuit(controller, valve)), hide_index=True, width="stretch")
    return controller, valve


def show_details(snapshot: pd.DataFrame, controller: dict, valve: dict) -> None:
    with st.expander("Dispositivi"):
        st.write(f"**Indirizzo {CONTROLLER}:** {identity(controller)}")
        st.write(f"**Indirizzo {VALVE}:** {identity(valve)}")
        st.write(f"**Orologio del controllore:** {clock(controller)}")

    with st.expander("Ingressi e uscite attivi"):
        st.write("**Uscite:** " + (", ".join(active_bits(controller, 2048, 2057)) or "nessuna"))
        st.write("**Ingressi:** " + (", ".join(active_bits(controller, 512, 517)) or "nessuno"))
        table = [{"Uscita": name, "Valore": controller[reg]} for reg, name in ANALOG.items() if reg in controller]
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")

    with st.expander("Ore di funzionamento"):
        table = [{"Dato": name, "Grezzo": controller[reg]} for reg, name in HOURS.items() if reg in controller]
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
        st.caption("Scala non indicata dal documento: da confrontare con il display.")

    with st.expander("Registri grezzi"):
        st.dataframe(snapshot.drop(columns="ora"), hide_index=True, width="stretch")


st.set_page_config(page_title="Monitor PDC", layout="wide")
st.title("Monitor pompa di calore")

with st.sidebar:
    st.header("Lettura dal vivo")
    live = st.toggle("Attiva", value=False)
    host = st.text_input("IP gateway", "192.168.1.230")
    port = st.number_input("Porta", 1, 65535, 4196)
    interval = st.selectbox("Aggiorna ogni", [30, 60, 120, 300], format_func=lambda s: f"{s} s")
    st.caption("Funziona solo dalla rete di casa e con la pagina aperta.")
    allow_write = st.toggle("Abilita modifica set point", value=False)

    st.header("Dati salvati")
    uploads = st.file_uploader("Aggiungi CSV", type="csv", accept_multiple_files=True)
    stored = load_folder()
    if not stored.empty and not live:
        stamps = sorted(stored["ora"].unique(), reverse=True)
        chosen = st.selectbox("Lettura", stamps, format_func=lambda s: pd.Timestamp(s).strftime("%d/%m/%Y %H:%M:%S"))
    else:
        chosen = None


@st.fragment(run_every=f"{interval}s" if live else None)
def dashboard() -> None:
    notice = st.empty()
    data = load_folder()
    if uploads:
        data = pd.concat([data, *[read_csv(file) for file in uploads]], ignore_index=True)
    if data.empty:
        st.info("Attiva la lettura dal vivo oppure carica un CSV creato da leggi_ichill.py.")
    else:
        show_data(data)

    # La lettura avviene dopo il disegno, così la pagina resta leggibile durante il ciclo.
    if live:
        try:
            read_live(host, int(port))
        except OSError as error:
            notice.warning(f"Lettura dal vivo non riuscita: {error}")


def show_data(data: pd.DataFrame) -> None:
    stamp = data["ora"].max() if live or chosen is None else chosen
    snapshot = data[data["ora"] == stamp]
    st.caption(f"Lettura delle {pd.Timestamp(stamp):%H:%M:%S} del {pd.Timestamp(stamp):%d/%m/%Y}")
    controller, valve = show_status(snapshot)

    trend = history(data)
    st.subheader("Andamento delle sonde")
    if len(trend) > 1:
        options = list(trend.columns)
        selected = st.multiselect("Sonde", options, default=options)
        if selected:
            st.line_chart(trend[selected])
    else:
        st.info("Serve almeno una seconda lettura per disegnare l'andamento.")

    show_details(snapshot, controller, valve)


@st.fragment
def setpoint_panel() -> None:
    st.subheader("Modifica set point")
    st.warning("Questa sezione scrive sul controllore reale. Tieni la macchina sotto controllo durante la prova.")

    name = st.selectbox("Set point", list(WRITABLE))
    kind = st.radio(
        "Tipo di modifica",
        ["Temporanea", "Permanente"],
        horizontal=True,
        help="Temporanea usa il registro volatile. Permanente salva in memoria: usala solo di rado.",
    )
    volatile = kind == "Temporanea"
    register = register_for(name, volatile)
    limits = WRITABLE[name]

    data = load_folder()
    current = None
    if not data.empty:
        latest = data[data["ora"] == data["ora"].max()]
        raw = to_dict(latest, CONTROLLER).get(register)
        current = signed(raw) / 10 if raw is not None else None
    start = current if current is not None else limits["min"]
    start = min(max(start, limits["min"]), limits["max"])
    shown = f"{current:.1f} °C" if current is not None else "non disponibile"
    st.caption(f"Registro {register}. Ultimo valore letto: {shown}. Limiti app: {limits['min']:.1f}-{limits['max']:.1f} °C.")

    with st.form("scrittura", clear_on_submit=True):
        value = st.number_input("Nuovo valore (°C)", limits["min"], limits["max"], start, 0.5, format="%.1f")
        confirmed = st.checkbox(f"Confermo la scrittura nel registro {register}")
        submitted = st.form_submit_button("Scrivi")

    if not submitted:
        return
    if not confirmed:
        st.error("Scrittura non inviata: manca la conferma.")
        return

    try:
        with bus_lock():
            result = write_setpoint(host, int(port), name, float(value), volatile)
    except (OSError, SetpointError) as error:
        st.error(f"Scrittura non riuscita: {error}")
        return

    log_write(name, volatile, result)
    if result["esito"]:
        st.success(f"{name}: da {signed(result['prima']) / 10:.1f} a {signed(result['dopo']) / 10:.1f} °C, verificato in rilettura.")
    else:
        st.error(f"Il controllore ora riporta {signed(result['dopo']) / 10:.1f} °C invece di {signed(result['scritto']) / 10:.1f} °C.")


dashboard()
if allow_write:
    setpoint_panel()
