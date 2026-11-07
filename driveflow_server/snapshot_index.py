"""Read-only indexing of verified DriveFlow snapshots for Home Assistant.

Never mutates archives or the application's SQLite data. Historical HA values
are raw observations; only unambiguously reconciled missing segments contribute
an *additional* distance to GPS trips. Ambiguous rows remain review-only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math
import sqlite3
import tempfile
import zipfile


def create_tables(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS snapshot_indexed (backup_id TEXT PRIMARY KEY, indexed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshot_history (
          device_id TEXT NOT NULL, record_id TEXT NOT NULL, vehicle_key TEXT NOT NULL,
          kind TEXT NOT NULL, date_key TEXT NOT NULL, km REAL NOT NULL, minutes REAL NOT NULL,
          source TEXT NOT NULL, period_start INTEGER NOT NULL, period_end INTEGER NOT NULL,
          approved_km REAL NOT NULL DEFAULT 0, approved_minutes REAL NOT NULL DEFAULT 0,
          review INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(device_id, record_id));
        CREATE TABLE IF NOT EXISTS snapshot_trips (
          device_id TEXT NOT NULL, trip_id TEXT NOT NULL, vehicle_key TEXT NOT NULL,
          category TEXT NOT NULL, started_ms INTEGER NOT NULL, ended_ms INTEGER,
          distance_m REAL NOT NULL, max_speed REAL NOT NULL, gps_points INTEGER NOT NULL,
          origin TEXT NOT NULL, imported INTEGER NOT NULL,
          start_address TEXT NOT NULL, end_address TEXT NOT NULL,
          PRIMARY KEY(device_id,trip_id));
        CREATE TABLE IF NOT EXISTS snapshot_fuel (
          device_id TEXT NOT NULL, event_id INTEGER NOT NULL, vehicle_key TEXT NOT NULL,
          time_ms INTEGER NOT NULL, odometer_km REAL NOT NULL, liters REAL NOT NULL,
          total_eur REAL NOT NULL, fuel_type TEXT NOT NULL, note TEXT NOT NULL,
          PRIMARY KEY(device_id,event_id));
        CREATE TABLE IF NOT EXISTS snapshot_maintenance (
          device_id TEXT NOT NULL, event_id INTEGER NOT NULL, vehicle_key TEXT NOT NULL,
          label TEXT NOT NULL, due_at INTEGER NOT NULL, due_km REAL NOT NULL,
          done INTEGER NOT NULL, PRIMARY KEY(device_id,event_id));
        CREATE TABLE IF NOT EXISTS snapshot_parking (
          device_id TEXT NOT NULL, event_id INTEGER NOT NULL, vehicle_key TEXT NOT NULL,
          time_ms INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
          accuracy_m REAL NOT NULL, source TEXT NOT NULL,
          PRIMARY KEY(device_id,event_id));
        CREATE TABLE IF NOT EXISTS sync_detail_scopes (
          device_id TEXT NOT NULL,kind TEXT NOT NULL,last_sync TEXT NOT NULL,
          PRIMARY KEY(device_id,kind));
        CREATE TABLE IF NOT EXISTS sync_details (
          device_id TEXT NOT NULL, kind TEXT NOT NULL, event_id INTEGER NOT NULL,
          vehicle_key TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(device_id,kind,event_id));
    ''')


def table_columns(db, table):
    return {r[1] for r in db.execute(f'PRAGMA table_info({table})')}


def _norm(name):
    from sync_store import vehicle_key
    return vehicle_key(str(name or '')) or 'unknown_vehicle'


def _finite(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError):
        return default


def _rows(db, table, defaults):
    if table not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    cols = table_columns(db, table)
    expression = ','.join(f'"{name}"' if name in cols else f'{default} AS "{name}"' for name, default in defaults)
    return [dict(row) for row in db.execute(f'SELECT {expression} FROM "{table}"')]


def haversine(a_lat, a_lon, b_lat, b_lon):
    from math import sin, cos, asin, sqrt, radians
    dlat, dlon = radians(b_lat-a_lat), radians(b_lon-a_lon)
    a = sin(dlat/2)**2 + cos(radians(a_lat))*cos(radians(b_lat))*sin(dlon/2)**2
    return 12742000*asin(min(1.,sqrt(a)))


def _overlap_km(geometries, trip, start, end):
    if trip['origin'] == 'manual' or not trip['distance_m']:
        return None
    points = geometries.get(str(trip['id']), [])
    if len(points) < 2:
        return None
    total = shared = 0.
    for a,b in zip(points,points[1:]):
        if b['timestamp'] <= a['timestamp']:
            continue
        dist = haversine(a['lat'],a['lon'],b['lat'],b['lon'])
        if not 0 <= dist <= 3000:
            continue
        total += dist
        inside = max(0, min(b['timestamp'],end)-max(a['timestamp'],start))
        shared += dist * inside/(b['timestamp']-a['timestamp'])
    return (trip['distance_m']/1000)*(shared/total) if total >= 10 else None


def _reconcile(history, trips, geometries, tzname='Europe/Amsterdam'):
    """Conservative server equivalent of DriveFlow's HA history reconciler.

    Missing/fractional GPS segment without points => REVIEW (never double-add).
    Historical time rows are calendar-day-based, never interpreted as GPS trips.
    """
    zone = ZoneInfo(tzname)
    end_limit = int(datetime.now(timezone.utc).timestamp()*1000) + 86400000
    by_vehicle = defaultdict(list)
    for trip in trips:
        if trip['ended_ms'] is not None and not trip['imported']:
            by_vehicle[_norm(trip['vehicle'])].append(trip)
    km_rows = defaultdict(list)
    # Same archived observation windows shipped with the Android migration.
    # No new values are inferred where the timestamp is unavailable.
    fixes={}
    path=Path(__file__).with_name('ha_observation_windows.json')
    if path.is_file():
        try:
            fixes={r['id']:int(datetime.fromisoformat(r['end']).timestamp()*1000)
                   for r in json.loads(path.read_text(encoding='utf8'))}
        except (ValueError,KeyError,TypeError):
            fixes={}
    result = {}
    for row in history:
        row = dict(row)
        key = _norm(row['vehicle'])
        row['vehicle_key'] = key
        if not row['period_end'] and row['record_id'] in fixes:
            row['period_end']=fixes[row['record_id']]
        row['approved_km'] = row['approved_minutes'] = 0.
        row['review'] = 1
        result[str(row['record_id'])] = row
        if row['kind'] == 'km':
            km_rows[key].append(row)
    for key, records in km_rows.items():
        records.sort(key=lambda r: (int(r['period_end'] or 0),r['date_key'],str(r['record_id'])))
        last_end = 0
        for row in records:
            try:
                day = date.fromisoformat(row['date_key'])
                day_start = int(datetime.combine(day,datetime.min.time(),zone).timestamp()*1000)
            except (ValueError,TypeError):
                continue
            end = int(row['period_end'] or 0)
            has_period = last_end > 0 and end > last_end
            start = last_end if has_period else day_start
            if 0 < start < end <= end_limit and has_period and row['km'] >= 0:
                gps_km, partial = 0., False
                for trip in by_vehicle[key]:
                    tstart, tend = trip['started_ms'],trip['ended_ms']
                    if tstart >= end or tend <= start:
                        continue
                    if tstart < start or tend > end:
                        portion = _overlap_km(geometries,trip,start,end)
                        if portion is None:
                            partial = True
                            break
                        gps_km += portion
                    else:
                        gps_km += trip['distance_m']/1000
                if not partial and gps_km <= row['km'] + .5:
                    row['approved_km'] = max(0.,row['km']-gps_km)
                    row['review'] = 0
            if end > 0:
                last_end = end
    for row in history:
        if row['kind'] != 'time':
            continue
        record = result[str(row['record_id'])]
        try:
            day = date.fromisoformat(row['date_key'])
            start = int(datetime.combine(day,datetime.min.time(),zone).timestamp()*1000)
            end = int(datetime.combine(day+timedelta(days=1),datetime.min.time(),zone).timestamp()*1000)
        except (ValueError,TypeError):
            continue
        gps_minutes = sum(max(0,min(trip['ended_ms'],end)-max(trip['started_ms'],start))/60000
                          for trip in by_vehicle[record['vehicle_key']]
                          if trip['started_ms'] < end and trip['ended_ms'] > start)
        if row['minutes'] >= 0 and gps_minutes <= row['minutes']+1:
            record['approved_minutes'] = max(0.,row['minutes']-gps_minutes)
            record['review'] = 0
    return list(result.values())


def index_backup(db, backup_id, device_id, path:Path, stamp, tzname='Europe/Amsterdam'):
    if db.execute('SELECT 1 FROM snapshot_indexed WHERE backup_id=?',(backup_id,)).fetchone():
        return False
    with zipfile.ZipFile(path) as arc, tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp)/'gps_points.db'
        with arc.open('databases/gps_points.db') as stream, source.open('wb') as output:
            import shutil
            shutil.copyfileobj(stream,output,1024*1024)
        with closing(sqlite3.connect(f'file:{source}?mode=ro',uri=True)) as gps:
            gps.row_factory = sqlite3.Row
            if gps.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('archief GPS database is beschadigd')
            history = _rows(gps,'ha_history',[
                ('record_id',"''"),('vehicle',"''"),('kind',"''"),('date_key',"''"),
                ('km','0'),('minutes','0'),('source',"''"),('period_start','0'),('period_end','0')])
            trips = _rows(gps,'trips',[
                ('id',"''"),('vehicle',"''"),('category',"'Onbekend'"),('started','0'),('ended','NULL'),
                ('distance','0'),('max_speed','0'),('points','0'),('origin',"'gps'"),('imported','0'),
                ('start_address',"''"),('end_address',"''")])
            normalized_trips = [dict(t, started_ms=t['started'], ended_ms=t['ended'],
                                    distance_m=_finite(t['distance'])) for t in trips]
            # Only points needed for a historical *partial* overlap. Full route stays in archive.
            overlapping_ids = set()
            asset=Path(__file__).with_name('ha_observation_windows.json')
            try:
                fixed={r['id']:int(datetime.fromisoformat(r['end']).timestamp()*1000)
                       for r in json.loads(asset.read_text(encoding='utf8'))} if asset.exists() else {}
            except (ValueError,KeyError,TypeError):
                fixed={}
            for h in history:
                period_end=h['period_end'] or fixed.get(h['record_id'],0)
                if h['kind'] != 'km' or not period_end:
                    continue
                for t in normalized_trips:
                    if _norm(t['vehicle']) == _norm(h['vehicle']) and t['ended_ms'] and t['started_ms'] < period_end and t['ended_ms'] > h['period_start']:
                        overlapping_ids.add(t['id'])
            geometries=defaultdict(list)
            if overlapping_ids and 'points' in {r[0] for r in gps.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                # Bounded read of only overlapping GPS point trails.
                for trip_id in overlapping_ids:
                    for row in gps.execute('SELECT timestamp,lat,lon,accuracy FROM points WHERE trip_id=? ORDER BY timestamp,id',(trip_id,)):
                        ts,lat,lon,accuracy = row
                        if _finite(accuracy,1000) <= 100 and -90 <= _finite(lat,999) <= 90 and -180 <= _finite(lon,999) <= 180:
                            geometries[trip_id].append({'timestamp':ts,'lat':lat,'lon':lon})
            reconciled = _reconcile(history,normalized_trips,geometries,tzname)
        extras = []
        if 'databases/driveflow_extras.db' in arc.namelist():
            file = Path(tmp)/'driveflow_extras.db'
            with arc.open('databases/driveflow_extras.db') as stream, file.open('wb') as output:
                import shutil
                shutil.copyfileobj(stream,output,1024*1024)
            with closing(sqlite3.connect(f'file:{file}?mode=ro',uri=True)) as ex:
                ex.row_factory=sqlite3.Row
                if ex.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise ValueError('archief detaildatabase is beschadigd')
                extras = [
                    _rows(ex,'fuel', [('id','0'),('vehicle',"''"),('time_ms','0'),('odometer_km','0'),('liters','0'),('total_eur','0'),('fuel_type',"''"),('note',"''")]),
                    _rows(ex,'maintenance', [('id','0'),('vehicle',"''"),('label',"''"),('due_at','0'),('due_km','0'),('done','0')]),
                    _rows(ex,'parking', [('id','0'),('vehicle',"''"),('time_ms','0'),('lat','0'),('lon','0'),('accuracy_m','0'),('source',"''")])]
    # All cross-table changes are atomic; a failure leaves backup file untouched.
    with db:
        for row in reconciled:
            db.execute('''INSERT INTO snapshot_history(device_id,record_id,vehicle_key,kind,date_key,km,minutes,source,period_start,period_end,approved_km,approved_minutes,review)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,record_id) DO UPDATE SET
               vehicle_key=excluded.vehicle_key,kind=excluded.kind,date_key=excluded.date_key,km=excluded.km,
               minutes=excluded.minutes,source=excluded.source,period_start=excluded.period_start,
               period_end=excluded.period_end,approved_km=excluded.approved_km,
               approved_minutes=excluded.approved_minutes,review=excluded.review''',
               (device_id,row['record_id'],row['vehicle_key'],row['kind'],row['date_key'],
                _finite(row['km']),_finite(row['minutes']),row['source'],row['period_start'],row['period_end'],
                row['approved_km'],row['approved_minutes'],row['review']))
        for t in normalized_trips:
            if not t['id'] or not t['started_ms'] or t['ended_ms'] is None:
                continue
            db.execute('''INSERT INTO snapshot_trips(device_id,trip_id,vehicle_key,category,started_ms,ended_ms,distance_m,max_speed,gps_points,origin,imported,start_address,end_address)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,trip_id) DO UPDATE SET
                vehicle_key=excluded.vehicle_key,category=excluded.category,started_ms=excluded.started_ms,
                ended_ms=excluded.ended_ms,distance_m=excluded.distance_m,max_speed=excluded.max_speed,
                gps_points=excluded.gps_points,origin=excluded.origin,imported=excluded.imported,
                start_address=excluded.start_address,end_address=excluded.end_address''',
                (device_id,t['id'],_norm(t['vehicle']),t['category'],t['started_ms'],t['ended_ms'],
                 _finite(t['distance_m']),_finite(t['max_speed']),int(t['points'] or 0),t['origin'],int(t['imported'] or 0),
                 str(t['start_address'] or '')[:400],str(t['end_address'] or '')[:400]))
        for table,rows in zip(('snapshot_fuel','snapshot_maintenance','snapshot_parking'),extras):
            for r in rows:
                columns={'snapshot_fuel':['time_ms','odometer_km','liters','total_eur','fuel_type','note'],
                         'snapshot_maintenance':['label','due_at','due_km','done'],
                         'snapshot_parking':['time_ms','lat','lon','accuracy_m','source']}[table]
                values=[device_id,r['id'],_norm(r['vehicle'])]+[r[col] for col in columns]
                names=['device_id','event_id','vehicle_key']+columns
                updates=','.join(f'{x}=excluded.{x}' for x in names[2:])
                db.execute(f'''INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?'*len(names))})
                              ON CONFLICT(device_id,event_id) DO UPDATE SET {updates}''',values)
        db.execute('INSERT INTO snapshot_indexed (backup_id,indexed_at) VALUES (?,?)',(backup_id,stamp))
    return True


def index_existing_latest(db, files_dir, stamp, tzname='Europe/Amsterdam'):
    rows = db.execute('''SELECT b.* FROM backups b INNER JOIN
      (SELECT device_id,MAX(created_at) stamp FROM backups GROUP BY device_id) m
      ON b.device_id=m.device_id AND b.created_at=m.stamp ORDER BY b.created_at DESC''').fetchall()
    result=[]
    for row in rows:
        try:
            # The source has been validated on upload; recheck the stored ZIP
            # before exposing any previously backed-up values to HA.
            import hashlib,hmac
            path=files_dir/row['file_name']
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):
                    digest.update(block)
            if not hmac.compare_digest(digest.hexdigest(),row['sha256']):
                raise ValueError('oude backup SHA-256 is onjuist')
            if index_backup(db,row['id'],row['device_id'],files_dir/row['file_name'],stamp,tzname):
                result.append(row['id'])
        except (ValueError,RuntimeError,OSError,sqlite3.Error,zipfile.BadZipFile,KeyError) as exc:
            import logging
            logging.getLogger('driveflow').warning('Snapshot indexing failed for %s (%s)',row['id'],type(exc).__name__)
    return result


def save_detail_batch(db, device_id, payload, stamp):
    """Atomic additive sync of fuel, maintenance and parking details."""
    from sync_store import vehicle_key
    if not isinstance(payload,dict) or payload.get('format')!='driveflow-details-v1':
        raise ValueError('ongeldig detailformaat')
    cleaned=[]
    rules={
      'fuel':({'time_ms','odometer_km','liters','total_eur','fuel_type','note'},
              {'time_ms':(1,4102444800000),'odometer_km':(0,5000000),'liters':(0.01,1000),'total_eur':(0,100000)}),
      'maintenance':({'label','due_at','due_km','done'},
              {'due_at':(0,4102444800000),'due_km':(0,5000000)}),
      'parking':({'time_ms','lat','lon','accuracy_m','source'},
              {'time_ms':(1,4102444800000),'lat':(-90,90),'lon':(-180,180),'accuracy_m':(0,1000)}),
    }
    for kind,(fields,numbers) in rules.items():
        rows=payload.get(kind,[])
        if not isinstance(rows,list) or len(rows)>1000:
            raise ValueError('te veel detailgegevens')
        for r in rows:
            if not isinstance(r,dict) or not isinstance(r.get('id'),int) or not 0<r['id']<2**63 or not isinstance(r.get('vehicle'),str):
                raise ValueError('ongeldige detail-ID of voertuig')
            key=vehicle_key(r['vehicle'])
            if not key or len(r['vehicle'])>120:
                raise ValueError('ongeldige voertuignaam')
            data={'event_id':r['id']}
            for f in fields:
                x=r.get(f)
                if f in numbers:
                    if type(x) not in (int,float) or not math.isfinite(x) or not numbers[f][0]<=x<=numbers[f][1]:
                        raise ValueError('ongeldig veld: '+f)
                    data[f]=x
                elif f=='done':
                    if type(x) is not bool:
                        raise ValueError('ongeldige onderhoudsstatus')
                    data[f]=int(x)
                else:
                    if not isinstance(x,str) or len(x)>400:
                        raise ValueError('ongeldig tekstveld: '+f)
                    data[f]=x
            cleaned.append((device_id,kind,r['id'],key,json.dumps(data,ensure_ascii=False,separators=(',',':')),stamp))
    with db:
        if payload.get('replace') is True:
            for kind in rules:
                if kind in payload:
                    db.execute('DELETE FROM sync_details WHERE device_id=? AND kind=?',(device_id,kind))
                    db.execute('''INSERT INTO sync_detail_scopes(device_id,kind,last_sync)
                         VALUES (?,?,?) ON CONFLICT(device_id,kind) DO UPDATE
                         SET last_sync=excluded.last_sync''',(device_id,kind,stamp))
        db.executemany('''INSERT INTO sync_details(device_id,kind,event_id,vehicle_key,payload,updated_at)
          VALUES (?,?,?,?,?,?) ON CONFLICT(device_id,kind,event_id) DO UPDATE SET
          vehicle_key=excluded.vehicle_key,payload=excluded.payload,updated_at=excluded.updated_at''', cleaned)
    return {'ok':True,'details':len(cleaned)}
