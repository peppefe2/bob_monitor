#!/usr/bin/env python3
"""Tiene d'occhio i tavoli che si liberano da Bob Alchimia a Spicchi (widget ResDiary)
e manda una notifica ntfy. Controlla solo in una fascia del giorno, considera solo
certi orari del tavolo e ignora i posti rilasciati a mezzanotte."""

import argparse
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://booking.resdiary.com/api/Restaurant/Bobalchimiaaspicchi/AvailabilityForDateRange"
BOOKING_URL = "https://www.bobalchimiaspicchi.com/prenota2.php"
TZ = ZoneInfo("Europe/Rome")
WINDOW_DAYS = 5                         # prenotabile da oggi a oggi+5
RELEASE_GRACE = timedelta(minutes=45)   # margine dopo la mezzanotte prima di considerare "rilasciata" la nuova data
ERROR_ALERT_AFTER = 15                  # errori API consecutivi prima di avvisare (~30 min a un controllo ogni 2)
STATE_FILE = Path(__file__).with_name("state.json")

PARTY_SIZES = [int(x) for x in os.environ.get("PARTY_SIZES", "2,3,4,5").split(",")]
SLOT_FROM, SLOT_TO = os.environ.get("SLOT_FROM", "20:30"), os.environ.get("SLOT_TO", "22:15")        # orario del tavolo
ACTIVE_FROM, ACTIVE_TO = os.environ.get("ACTIVE_FROM", "08:00"), os.environ.get("ACTIVE_TO", "23:50")  # fascia di controllo
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
HC_PING_URL = os.environ.get("HC_PING_URL", "").strip()   # opzionale: URL di ping di Healthchecks.io

GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def area_name(name):
    # "VERANDA ADULTI, FUMATORI (no minori 18y e passeg.)" -> "VERANDA ADULTI FUMATORI"
    return " ".join(re.sub(r"\(.*?\)", "", name).replace(",", " ").split())


def fetch(party_size, start, end):
    """Slot 'AAAA-MM-GG|HH:MM|AREA' nella fascia oraria per quel numero di persone,
    più tutti gli orari liberi 'AAAA-MM-GG|HH:MM' a qualsiasi ora (per il ping a Healthchecks)."""
    body = json.dumps({
        "DateFrom": f"{start.isoformat()}T00:00:00",
        "DateTo": f"{end.isoformat()}T00:00:00",
        "PartySize": party_size,
        "ChannelCode": "ONLINE",
        "PromotionId": None,
        "AvailabilityType": "Reservation",
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    slots, all_times = set(), set()
    for d in data["AvailableDates"]:
        areas = {a["Id"]: area_name(a["Name"]) for a in d.get("Areas", [])}
        for t in d["AvailableTimes"]:
            all_times.add(f'{d["Date"][:10]}|{t["TimeSlot"][:5]}')
            if not SLOT_FROM <= t["TimeSlot"][:5] <= SLOT_TO:
                continue
            for area_id in t.get("AvailableAreaIds") or [None]:
                slots.add(f'{d["Date"][:10]}|{t["TimeSlot"][:5]}|{areas.get(area_id, "?")}')
    return slots, all_times


def notify(title, message, priority=4, tags=("pizza",)):
    payload = {
        "topic": NTFY_TOPIC, "title": title, "message": message,
        "priority": priority, "tags": list(tags), "click": BOOKING_URL,
        "actions": [{"action": "view", "label": "Prenota", "url": BOOKING_URL}],
    }
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).close()


def fmt(slot, sizes):
    d, time, area = slot.split("|")
    day = date.fromisoformat(d)
    return f"{GIORNI[day.weekday()]} {day:%d/%m} · {time} · {area} · per {'/'.join(map(str, sizes))}"


def save(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def log(msg):
    print(f"[{datetime.now(TZ):%d/%m %H:%M}] {msg}", flush=True)


def check(state):
    """Un controllo: restituisce il nuovo stato."""
    now = datetime.now(TZ)

    # Fuori fascia non si controlla né si tocca lo stato: il primo controllo delle 8:00
    # confronta con l'ultimo della sera prima.
    if not ACTIVE_FROM <= f"{now:%H:%M}" <= ACTIVE_TO:
        return state

    today = now.date()
    try:
        current, all_times = {}, set()
        for ps in PARTY_SIZES:
            slots, times = fetch(ps, today, today + timedelta(days=WINDOW_DAYS + 1))
            all_times |= times
            for slot in slots:
                current.setdefault(slot, set()).add(ps)
    except Exception as e:
        state = {**state, "errors": state.get("errors", 0) + 1}
        log(f"Errore API ({state['errors']} di fila): {e}")
        if state["errors"] == ERROR_ALERT_AFTER:
            notify("Monitor Bob in errore", f"L'API non risponde da {state['errors']} controlli: {e}",
                   priority=3, tags=("warning",))
        return state

    # Ultima data che era già prenotabile al controllo precedente: gli slot nuovi oltre questa data
    # sono il rilascio di mezzanotte e diventano solo la base di confronto, senza notifica.
    released_until = state.get("released_until")
    previous = {k: set(v) for k, v in state.get("slots", {}).items()}
    freed = []
    if released_until:  # al primo avvio si registra solo lo stato iniziale
        for slot, sizes in current.items():
            new_sizes = sizes - previous.get(slot, set())
            if new_sizes and slot[:10] <= released_until:
                freed.append((slot, sorted(new_sizes)))

    if freed:
        lines = [fmt(slot, sizes) for slot, sizes in sorted(freed)]
        title = "Si è liberato un tavolo" if len(lines) == 1 else f"Si sono liberati {len(lines)} tavoli"
        notify(title, "\n".join(lines))
        log(title + "\n" + "\n".join(lines))
    else:
        log(f"Nessun tavolo liberato ({len(current)} slot disponibili nella fascia oraria).")

    # Segnale "sono vivo" a Healthchecks.io, solo se l'API ha restituito almeno un orario libero
    # (a qualsiasi ora). Se i ping smettono di arrivare per il periodo impostato lì — run non avviati,
    # token scaduto, API cambiata che risponde vuota — è Healthchecks a mandare l'allarme su ntfy.
    if HC_PING_URL and all_times:
        try:
            urllib.request.urlopen(HC_PING_URL, timeout=10).close()
        except Exception as e:
            log(f"Ping a Healthchecks non riuscito: {e}")

    return {
        "released_until": ((now - RELEASE_GRACE).date() + timedelta(days=WINDOW_DAYS)).isoformat(),
        "errors": 0,
        "slots": {k: sorted(v) for k, v in sorted(current.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=0, help="minuti di esecuzione (0 = un solo controllo)")
    ap.add_argument("--every", type=float, default=2, help="minuti tra un controllo e l'altro")
    args = ap.parse_args()

    if os.environ.get("NTFY_TEST") == "true":
        notify("Monitor Bob attivo", "Le notifiche arrivano correttamente.", priority=3, tags=("white_check_mark",))

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    end = time.monotonic() + args.duration * 60
    log(f"Inizio: tavoli {SLOT_FROM}-{SLOT_TO} per {PARTY_SIZES} persone, "
        f"controllo ogni {args.every:g} min tra le {ACTIVE_FROM} e le {ACTIVE_TO}")
    while True:
        state = check(state)
        save(state)
        if time.monotonic() + args.every * 60 > end:
            break
        time.sleep(args.every * 60)
    log("Fine del run.")


if __name__ == "__main__":
    main()
