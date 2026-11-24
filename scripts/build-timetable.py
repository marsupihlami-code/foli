"""Build Zaani's static fallback from an official Föli GTFS ZIP."""
import argparse
import csv
import gzip
import io
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo


BUCKETS = 16
HELSINKI = ZoneInfo("Europe/Helsinki")


def records(feed, table):
    with feed.open(table + ".txt") as source:
        yield from csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig"))


def gtfs_date(value):
    return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")


def dates_between(first, last):
    return (first + timedelta(days=step) for step in range((last - first).days + 1))


def service_days(feed, first):
    services = defaultdict(set)
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if "calendar.txt" in feed.namelist():
        for row in records(feed, "calendar"):
            for day in dates_between(max(first, gtfs_date(row["start_date"])),
                                     gtfs_date(row["end_date"])):
                if row[weekdays[day.weekday()]] == "1":
                    services[row["service_id"]].add(day)
    if "calendar_dates.txt" in feed.namelist():
        for row in records(feed, "calendar_dates"):
            day = gtfs_date(row["date"])
            if day < first:
                continue
            days = services[row["service_id"]]
            if row["exception_type"] == "1":
                days.add(day)
            elif row["exception_type"] == "2":
                days.discard(day)
    return services


def seconds(value):
    h, m, s = map(int, value.split(":"))
    return h * 3600 + m * 60 + s


def write_gzip(path, data):
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(encoded, mtime=0))


def build(source, output, first):
    with ZipFile(source) as feed:
        services = service_days(feed, first)
        patterns = {}
        service_pattern = {}
        by_day = defaultdict(list)
        for service, days in services.items():
            if not days:
                continue
            pattern = tuple(sorted(days))
            if pattern not in patterns:
                group = len(patterns)
                patterns[pattern] = group
                for day in pattern:
                    by_day[day].append(group)
            service_pattern[service] = patterns[pattern]

        routes = {row["route_id"]: row["route_short_name"] or row["route_long_name"]
                  for row in records(feed, "routes")}
        trips = {row["trip_id"]: (service_pattern[row["service_id"]],
                                 routes[row["route_id"]], row["trip_headsign"])
                 for row in records(feed, "trips") if row["service_id"] in service_pattern}
        stops = [[row["stop_id"], row["stop_name"], float(row["stop_lat"]), float(row["stop_lon"])]
                 for row in records(feed, "stops")
                 if row["location_type"] in ("", "0") and row["stop_lat"] and row["stop_lon"]]

        last_sequence = defaultdict(int)
        for row in records(feed, "stop_times"):
            if row["trip_id"] in trips:
                trip, sequence = row["trip_id"], int(row["stop_sequence"])
                last_sequence[trip] = max(last_sequence[trip], sequence)

        departures = defaultdict(lambda: defaultdict(set))
        labels = {}
        longest = 0
        for row in records(feed, "stop_times"):
            trip = row["trip_id"]
            if (trip not in trips or row["pickup_type"] == "1" or not row["departure_time"]
                    or int(row["stop_sequence"]) == last_sequence[trip]):
                continue
            group, line, headsign = trips[trip]
            label = (line, row["stop_headsign"] or headsign)
            index = labels.setdefault(label, len(labels))
            departure = seconds(row["departure_time"])
            longest = max(longest, departure)
            departures[row["stop_id"]][group].add((departure, index))

    if not by_day or not departures or not stops:
        raise ValueError("GTFS contains no usable timetables")
    end = max(by_day)
    output.mkdir(parents=True, exist_ok=True)
    for bucket in range(BUCKETS):
        group = {stop: {service: sorted(rows) for service, rows in schedules.items()}
                 for stop, schedules in departures.items()
                 if sum(map(ord, stop)) % BUCKETS == bucket}
        write_gzip(output / f"{bucket}.json.gz", group)
    # GTFS defines the time origin as local noon minus twelve elapsed hours.
    days = [[int(datetime.combine(day, time(12), HELSINKI).timestamp()) - 43200, by_day[day]]
            for day in dates_between(first, end)]
    write_gzip(output / "index.json.gz", {
        "from": first.isoformat(), "through": end.isoformat(), "buckets": BUCKETS,
        "longest": longest, "days": days, "labels": list(labels), "stops": stops,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gtfs_zip", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--first", type=date.fromisoformat,
                        default=datetime.now(HELSINKI).date() - timedelta(days=2))
    args = parser.parse_args()
    build(args.gtfs_zip, args.output, args.first)
