"""DriveFlow 0.2 incremental, idempotent *summary* sync.

Backups remain untouched. No GPS routes or HA historical observations are
counted here, so Home Assistant sensor totals cannot double-count an import.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import re
import sqlite3

CATEGORIES = ('Privé', 'Zakelijk', 'Onbekend')
KEY_RE = re.compile(r'^[a-z0-9_]{2,72}$')


def create_tables(db: sqlite3.Connection) -> None:
    db.executescript('''
      CREATE TABLE IF NOT EXISTS sync_trips (
        id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        vehicle_key TEXT NOT NULL,
        vehicle_name TEXT NOT NULL,
        category TEXT NOT NULL,
        started_ms INTEGER NOT NULL,
        ended_ms INTEGER NOT NULL,
        distance_m REAL NOT NULL,
        origin TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_sync_trips_vehicle ON sync_trips(vehicle_key,started_ms);
      CREATE INDEX IF NOT EXISTS idx_sync_trips_date ON sync_trips(started_ms);
      CREATE TABLE IF NOT EXISTS sync_vehicles (
        key TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        name TEXT NOT NULL,
        usage TEXT NOT NULL,
        odometer_km REAL,
        odometer_at_ms INTEGER,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sync_vehicle_meta (
        vehicle_key TEXT PRIMARY KEY,plate TEXT,bluetooth TEXT,fuel_unit TEXT,
        fuel_consumption REAL,fuel_price_eur_liter REAL,updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sync_devices (
        device_id TEXT PRIMARY KEY,
        last_sync_at TEXT NOT NULL
      );
    ''')


def vehicle_key(name: str) -> str:
    """Stable identity across earlier SEAT Leon / SEAT Leon 5F names."""
    normalized = ''.join(ch.lower() for ch in name if ch.isalnum())
    if normalized.startswith('seatleon'):
        return 'seat_leon'
    if normalized.startswith('vwcaddy') or normalized.startswith('volkswagencaddy'):
        return 'vw_caddy'
    key = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:72]
    return key if KEY_RE.fullmatch(key) else ''


def valid_number(value, lo, hi):
    return type(value) in (float, int) and math.isfinite(value) and lo <= value <= hi


def validate(payload: dict) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError('object verwacht')
    if payload.get('format') != 'driveflow-trip-sync-v1':
        raise ValueError('ongeldig syncformaat')
    trips, vehicles = payload.get('trips'), payload.get('vehicles')
    if not isinstance(trips, list) or not isinstance(vehicles, list) or len(trips) > 150 or len(vehicles) > 128:
        raise ValueError('batch is ongeldig of te groot')
    cleaned_trips, cleaned_vehicles = [], []
    known_ids = set()
    future_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 86400000
    for t in trips:
        if not isinstance(t, dict):
            raise ValueError('ongeldige rit')
        trip_id = t.get('id')
        name = t.get('vehicle', '')
        key = vehicle_key(name) if isinstance(name, str) else ''
        start, end, meters = t.get('started_ms'), t.get('ended_ms'), t.get('distance_m')
        category = t.get('category')
        origin = t.get('origin', 'gps')
        if (not isinstance(trip_id, str) or not 1 <= len(trip_id) <= 120 or
                any(ord(x) < 32 for x in trip_id) or trip_id in known_ids or
                not key or len(name) > 100 or category not in CATEGORIES or
                type(start) is not int or type(end) is not int or
                not 946684800000 <= start < end <= future_ms or end - start > 7 * 86400000 or
                not valid_number(meters, 0, 2_000_000) or origin not in ('gps', 'manual')):
            raise ValueError('ongeldig ritveld')
        known_ids.add(trip_id)
        cleaned_trips.append({'id': trip_id, 'vehicle_key': key, 'vehicle': name,
                              'category': category, 'started_ms': start, 'ended_ms': end,
                              'distance_m': float(meters), 'origin': origin})
    seen = set()
    for v in vehicles:
        if not isinstance(v, dict):
            raise ValueError('ongeldig voertuig')
        name, usage = v.get('name'), v.get('usage')
        key = vehicle_key(name) if isinstance(name, str) else ''
        if not key or len(name) > 100 or key in seen or usage not in CATEGORIES:
            raise ValueError('ongeldige voertuigvelden')
        seen.add(key)
        reading = v.get('odometer_km')
        reading_at = v.get('odometer_at_ms')
        if reading is not None and (not valid_number(reading, 0, 5_000_000) or
                                    type(reading_at) is not int or
                                    not 946684800000 <= reading_at <= future_ms):
            raise ValueError('ongeldige bevestigde kilometerstand')
        plate=v.get('plate',''); bluetooth=v.get('bluetooth','')
        fuel_unit=v.get('fuel_unit','')
        consumption=v.get('fuel_consumption')
        price=v.get('fuel_price_eur_liter')
        if (not isinstance(plate,str) or len(plate)>24 or
            not isinstance(bluetooth,str) or len(bluetooth)>120 or
            not isinstance(fuel_unit,str) or fuel_unit not in ('','kml','l100km') or
            (consumption is not None and not valid_number(consumption,0,1000)) or
            (price is not None and not valid_number(price,0,100))):
            raise ValueError('ongeldige voertuigdetails')
        cleaned_vehicles.append({'key': key, 'name': name, 'usage': usage,
                                 'odometer_km': float(reading) if reading is not None else None,
                                 'odometer_at_ms': reading_at if reading is not None else None,
                                 'plate':plate,'bluetooth':bluetooth,'fuel_unit':fuel_unit,
                                 'fuel_consumption':consumption,'fuel_price_eur_liter':price})
    return cleaned_trips, cleaned_vehicles


def save_batch(db: sqlite3.Connection, device_id: str, payload: dict, stamp: str) -> dict:
    trips, vehicles = validate(payload)
    inserted, revised = 0, 0
    # Explicit transaction across both resources: one atomic acknowledgment.
    with db:
        for t in trips:
            old = db.execute('SELECT 1 FROM sync_trips WHERE id=?', (t['id'],)).fetchone()
            inserted += int(old is None)
            revised += int(old is not None)
            db.execute('''INSERT INTO sync_trips
              (id,device_id,vehicle_key,vehicle_name,category,started_ms,ended_ms,distance_m,origin,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET
              device_id=excluded.device_id,vehicle_key=excluded.vehicle_key,
              vehicle_name=excluded.vehicle_name,category=excluded.category,
              started_ms=excluded.started_ms,ended_ms=excluded.ended_ms,
              distance_m=excluded.distance_m,origin=excluded.origin,updated_at=excluded.updated_at''',
              (t['id'], device_id, t['vehicle_key'], t['vehicle'], t['category'], t['started_ms'],
               t['ended_ms'], t['distance_m'], t['origin'], stamp))
        for v in vehicles:
            # Never replace a known confirmed odometer with NULL from another app install.
            old = db.execute('SELECT odometer_km,odometer_at_ms FROM sync_vehicles WHERE key=?', (v['key'],)).fetchone()
            newer = v['odometer_km'] is not None and (old is None or old['odometer_at_ms'] is None or v['odometer_at_ms'] >= old['odometer_at_ms'])
            km = v['odometer_km'] if newer else old['odometer_km'] if old else None
            km_at = v['odometer_at_ms'] if newer else old['odometer_at_ms'] if old else None
            db.execute('''INSERT INTO sync_vehicles(key,device_id,name,usage,odometer_km,odometer_at_ms,updated_at)
              VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
              device_id=excluded.device_id,name=excluded.name,usage=excluded.usage,
              odometer_km=excluded.odometer_km,odometer_at_ms=excluded.odometer_at_ms,
              updated_at=excluded.updated_at''', (v['key'],device_id,v['name'],v['usage'],km,km_at,stamp))
            db.execute('''INSERT INTO sync_vehicle_meta(vehicle_key,plate,bluetooth,fuel_unit,fuel_consumption,fuel_price_eur_liter,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(vehicle_key) DO UPDATE SET
                plate=CASE WHEN excluded.plate!='' THEN excluded.plate ELSE sync_vehicle_meta.plate END,
                bluetooth=CASE WHEN excluded.bluetooth!='' THEN excluded.bluetooth ELSE sync_vehicle_meta.bluetooth END,
                fuel_unit=CASE WHEN excluded.fuel_unit!='' THEN excluded.fuel_unit ELSE sync_vehicle_meta.fuel_unit END,
                fuel_consumption=COALESCE(excluded.fuel_consumption,sync_vehicle_meta.fuel_consumption),
                fuel_price_eur_liter=COALESCE(excluded.fuel_price_eur_liter,sync_vehicle_meta.fuel_price_eur_liter),
                updated_at=excluded.updated_at''',
                (v['key'],v['plate'],v['bluetooth'],v['fuel_unit'],v['fuel_consumption'],v['fuel_price_eur_liter'],stamp))
        db.execute('''INSERT INTO sync_devices(device_id,last_sync_at) VALUES (?,?)
          ON CONFLICT(device_id) DO UPDATE SET last_sync_at=excluded.last_sync_at''',(device_id,stamp))
    return {'ok': True, 'inserted': inserted, 'updated': revised, 'vehicles': len(vehicles), 'last_sync_at': stamp}


def summary(db: sqlite3.Connection, tzname: str = 'Europe/Amsterdam') -> dict:
    tz = ZoneInfo(tzname)
    localnow = datetime.now(tz)
    today, month = localnow.date(), (localnow.year, localnow.month)
    by_key: dict[str, dict] = {}
    totals = {'km_today': 0., 'km_month': 0., 'km_total': 0., 'minutes_today': 0., 'minutes_month': 0., 'trips_month': 0}
    categories = {category: {'km_today':0., 'km_month':0., 'km_total':0., 'minutes_month':0., 'trips_month':0} for category in CATEGORIES}
    for row in db.execute('SELECT key,name,usage,odometer_km,odometer_at_ms FROM sync_vehicles'):
        by_key[row['key']] = {'key': row['key'], 'name': row['name'], 'usage': row['usage'],
                              'odometer_km': row['odometer_km'], 'odometer_at_ms': row['odometer_at_ms'],
                              'km_today': 0., 'km_month': 0., 'km_total':0., 'minutes_today':0.,
                              'minutes_month':0., 'trips_month':0,'last_trip_at':None}
    for t in db.execute('SELECT vehicle_key,vehicle_name,category,started_ms,ended_ms,distance_m FROM sync_trips'):
        key = t['vehicle_key']
        if key not in by_key:
            by_key[key] = {'key':key,'name':t['vehicle_name'],'usage':'Onbekend','odometer_km':None,
                           'odometer_at_ms':None,'km_today':0.,'km_month':0.,'km_total':0.,
                           'minutes_today':0.,'minutes_month':0.,'trips_month':0,'last_trip_at':None}
        v = by_key[key]
        day = datetime.fromtimestamp(t['started_ms']/1000, timezone.utc).astimezone(tz).date()
        km = t['distance_m'] / 1000.0
        minutes = max(0., (t['ended_ms']-t['started_ms'])/60000.)
        category = t['category']
        last = datetime.fromtimestamp(t['ended_ms']/1000, timezone.utc).isoformat()
        if v['last_trip_at'] is None or v['last_trip_at'] < last:
            v['last_trip_at'] = last
        v['km_total'] += km
        totals['km_total'] += km
        categories[category]['km_total'] += km
        if day == today:
            v['km_today'] += km
            v['minutes_today'] += minutes
            totals['km_today'] += km
            totals['minutes_today'] += minutes
            categories[category]['km_today'] += km
        if (day.year,day.month) == month:
            v['km_month'] += km
            v['minutes_month'] += minutes
            v['trips_month'] += 1
            totals['km_month'] += km
            totals['minutes_month'] += minutes
            totals['trips_month'] += 1
            categories[category]['km_month'] += km
            categories[category]['minutes_month'] += minutes
            categories[category]['trips_month'] += 1
    newest = db.execute('SELECT MAX(last_sync_at) FROM sync_devices').fetchone()[0]
    for v in by_key.values():
        for field in ('km_today','km_month','km_total','minutes_today','minutes_month'):
            v[field] = round(v[field],3)
    for group in [totals,*categories.values()]:
        for field in list(group):
            if isinstance(group[field], float): group[field] = round(group[field],3)
    return {'version':'0.2.0','timezone':tzname,'generated_at':datetime.now(timezone.utc).isoformat(),
            'last_sync_at':newest,'vehicles':sorted(by_key.values(),key=lambda x:x['key']),
            'totals':totals,'categories':categories,
            'source':'completed_nonimported_trips_only',
            'note':'Historische HA-records en snapshots worden NIET bij deze kilometers opgeteld.'}
