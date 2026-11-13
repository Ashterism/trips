#!/usr/bin/env python3
"""Build a cached JSON timetable from Brittany Ferries' public GTFS feed."""
from __future__ import annotations
import csv
import io
import json
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = "https://transport.data.gouv.fr/resources/83427/download"
START = date(2026, 11, 23)
DAY_COUNT = 7
FRANCE = ZoneInfo("Europe/Paris")
UNITED_KINGDOM = ZoneInfo("Europe/London")
FARE_SAMPLES = {
    "2026-11-23": {"08:30": 134, "16:30": 134, "23:30": 153},
    "2026-11-24": {"08:30": 78, "16:30": 134, "23:30": 153},
    "2026-11-25": {"08:30": 78, "16:30": 134, "23:30": 153},
    "2026-11-26": {"08:30": 78, "16:30": 134, "23:30": 153},
    "2026-11-27": {"16:30": 153, "23:30": 153},
    "2026-11-28": {"08:30": 134, "16:30": 134, "23:30": 153},
    "2026-11-29": {"08:30": 134, "16:30": 153, "23:30": 153},
}

def table(archive: zipfile.ZipFile, wanted: str) -> list[dict[str, str]]:
    member = next((name for name in archive.namelist() if Path(name).name.lower() == wanted.lower()), None)
    if member is None:
        return []
    with archive.open(member) as stream:
        return list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")))

def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()

def gtfs_minutes(value: str) -> int:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 60 + minutes + (1 if seconds >= 30 else 0)

def active_services(day: date, calendars: list[dict[str, str]], exceptions: list[dict[str, str]]) -> set[str]:
    active = set()
    weekday = day.strftime("%A").lower()
    for row in calendars:
        if parse_date(row["start_date"]) <= day <= parse_date(row["end_date"]) and row.get(weekday) == "1":
            active.add(row["service_id"])
    for row in exceptions:
        if parse_date(row["date"]) == day:
            if row["exception_type"] == "1":
                active.add(row["service_id"])
            elif row["exception_type"] == "2":
                active.discard(row["service_id"])
    return active

def port_time(service_day: date, minutes: int, agency_timezone: ZoneInfo) -> datetime:
    midnight = datetime.combine(service_day, time.min, tzinfo=agency_timezone)
    return midnight.astimezone(timezone.utc) + timedelta(minutes=minutes)

def main() -> None:
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": "Ashterism-trips timetable refresh"})
    with urllib.request.urlopen(request, timeout=45) as response:
        archive_bytes = response.read()
        source_last_modified = response.headers.get("Last-Modified")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        agencies = table(archive, "agency.txt")
        stops = table(archive, "stops.txt")
        trips = table(archive, "trips.txt")
        stop_times = table(archive, "stop_times.txt")
        calendars = table(archive, "calendar.txt")
        exceptions = table(archive, "calendar_dates.txt")
        feed_info = table(archive, "feed_info.txt")
    agency_zone = next((row.get("agency_timezone") for row in agencies if row.get("agency_timezone")), None)
    if not agency_zone:
        raise ValueError("The GTFS feed has no agency_timezone")
    agency_timezone = ZoneInfo(agency_zone)
    stop_names = {row["stop_id"]: row.get("stop_name", "").upper() for row in stops}
    parents = {row["stop_id"]: row.get("parent_station", "") for row in stops}
    full_names = {stop_id: (name + " " + stop_names.get(parents.get(stop_id, ""), "")).strip() for stop_id, name in stop_names.items()}
    origins = {stop_id for stop_id, name in full_names.items() if "OUISTREHAM" in name}
    destinations = {stop_id for stop_id, name in full_names.items() if "PORTSMOUTH" in name}
    if not origins or not destinations:
        raise ValueError("Could not identify Ouistreham and Portsmouth stops in the GTFS feed")
    rows_by_trip: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in stop_times:
        rows_by_trip[row["trip_id"]].append(row)
    trip_services = {row["trip_id"]: row["service_id"] for row in trips}
    for rows in rows_by_trip.values():
        rows.sort(key=lambda row: int(row.get("stop_sequence", "0")))
    result: dict[str, list[dict[str, object]]] = {}
    for offset in range(DAY_COUNT):
        service_day = START + timedelta(days=offset)
        key = service_day.isoformat()
        active = active_services(service_day, calendars, exceptions)
        sailings = []
        for trip_id, rows in rows_by_trip.items():
            if trip_services.get(trip_id) not in active:
                continue
            for index, origin in enumerate(rows):
                if origin.get("stop_id") not in origins:
                    continue
                destination = next((row for row in rows[index + 1:] if row.get("stop_id") in destinations), None)
                if destination is None:
                    continue
                dep_raw = origin.get("departure_time") or origin.get("arrival_time")
                arr_raw = destination.get("arrival_time") or destination.get("departure_time")
                if not dep_raw or not arr_raw:
                    continue
                dep_minutes, arr_minutes = gtfs_minutes(dep_raw), gtfs_minutes(arr_raw)
                while arr_minutes <= dep_minutes:
                    arr_minutes += 1440
                departure = port_time(service_day, dep_minutes, agency_timezone)
                arrival = port_time(service_day, arr_minutes, agency_timezone)
                duration = round((arrival - departure).total_seconds() / 60)
                if duration <= 0:
                    continue
                local_departure = departure.astimezone(FRANCE)
                local_arrival = arrival.astimezone(UNITED_KINGDOM)
                dep, arr = local_departure.strftime("%H:%M"), local_arrival.strftime("%H:%M")
                sailings.append({
                    "dep": dep,
                    "arr": arr,
                    "mins": duration,
                    "next": local_arrival.date() > local_departure.date(),
                    "fare": FARE_SAMPLES.get(key, {}).get(dep),
                })
        unique = {(item["dep"], item["arr"], item["mins"]): item for item in sailings}
        result[key] = sorted(unique.values(), key=lambda item: (item["dep"], item["arr"]))
    if not any(result.values()):
        raise ValueError("No Caen–Portsmouth sailings found for the selected week")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "source": "Brittany Ferries published GTFS via transport.data.gouv.fr",
        "sourceUrl": "https://transport.data.gouv.fr/datasets/horaires-des-traversees-brittany-ferries",
        "licence": "Licence Ouverte 2.0",
        "validity": {
            "start": feed_info[0].get("feed_start_date") if feed_info else None,
            "end": feed_info[0].get("feed_end_date") if feed_info else None,
        },
        "lastModified": source_last_modified,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "days": result,
    }
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "_site/sailings.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("Built timetable for " + str(len(result)) + " dates and " + str(sum(len(day) for day in result.values())) + " sailings")

if __name__ == "__main__":
    main()
