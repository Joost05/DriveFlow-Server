"""DriveFlow 0.3 detailed read model. No writes to trip/history originals."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import json
import math
from sync_store import vehicle_key, CATEGORIES


def _blank():
    return dict(km_today=0.,km_week=0.,km_month=0.,km_total=0.,
                minutes_today=0.,minutes_week=0.,minutes_month=0.,minutes_total=0.,
                trips_today=0,trips_week=0,trips_month=0,trips_total=0,
                km_private=0.,km_business=0.,km_unknown=0.,
                private_km_today=0.,private_km_month=0.,private_km_total=0.,
                business_km_today=0.,business_km_month=0.,business_km_total=0.,
                private_minutes_month=0.,business_minutes_month=0.,
                private_trips_month=0,business_trips_month=0,
                historical_raw_km=0.,historical_approved_km=0.,historical_raw_minutes=0.,
                historical_approved_minutes=0.,historical_records=0,historical_review=0,
                combined_km_total=0.,combined_minutes_total=0.,
                max_speed_kmh=None,avg_trip_km=None,last_trip_at=None,
                last_trip_distance_km=None,last_trip_minutes=None,
                last_trip_from=None,last_trip_to=None,
                fuel_events=0,fuel_liters=0.,fuel_eur=0.,fuel_eur_month=0.,
                fuel_last_at=None,fuel_last_type=None,fuel_last_note=None,fuel_last_eur_per_liter=None,fuel_last_consumption_l_100km=None,
                maintenance_open=0,maintenance_done=0,maintenance_due=False,maintenance_next_label=None,maintenance_next_due_at=None,
                maintenance_next_due_km=None,parking_lat=None,parking_lon=None,
                parking_last_at=None,parking_accuracy_m=None,parking_source=None)


def _round(g):
    for k,v in list(g.items()):
        if isinstance(v,float) and math.isfinite(v):
            g[k] = round(v,3)


def summary(db, tzname='Europe/Amsterdam'):
    tz=ZoneInfo(tzname); now=datetime.now(tz); today=now.date()
    week_start=today-timedelta(days=today.weekday())
    month=(today.year,today.month)
    vehicles={}; totals=_blank(); categories={cat:_blank() for cat in CATEGORIES}

    def vehicle(key,name=None):
        if key not in vehicles:
            vehicles[key]=dict(key=key,name=name or {'seat_leon':'SEAT Leon','vw_caddy':'VW Caddy'}.get(key,key),
                               usage='Onbekend',odometer_km=None,odometer_at_ms=None,
                               bluetooth=None,**_blank())
        return vehicles[key]

    for r in db.execute('SELECT key,name,usage,odometer_km,odometer_at_ms FROM sync_vehicles'):
        v=vehicle(r['key'],r['name']); v.update(usage=r['usage'],odometer_km=r['odometer_km'],odometer_at_ms=r['odometer_at_ms'])

    # Confirmed readings remain independent of GPS trip totals and estimates.
    for r in db.execute('SELECT vehicle_key,km,time_ms,difference_km,source,photo_name FROM odometer_events ORDER BY time_ms DESC'):
        v=vehicle(r['vehicle_key'])
        v['odometer_confirmation_count']=v.get('odometer_confirmation_count',0)+1
        if v.get('odometer_confirmed_at') is None:
            v['odometer_confirmed_at']=datetime.fromtimestamp(r['time_ms']/1000,timezone.utc).isoformat()
            v['odometer_confirmed_km']=r['km'];v['odometer_gps_difference_km']=r['difference_km'] if r['difference_km'] is not None and abs(r['difference_km'])>1.0 else None
            v['odometer_confirmed_source']=r['source'];v['odometer_has_photo']=bool(r['photo_name'])

    for r in db.execute('SELECT vehicle_key,time_ms,station,eur_per_km FROM fuel_receipts ORDER BY time_ms DESC'):
        v=vehicle(r['vehicle_key'])
        v['fuel_receipt_count']=v.get('fuel_receipt_count',0)+1
        if v.get('fuel_receipt_last_at') is None:
            v['fuel_receipt_last_at']=datetime.fromtimestamp(r['time_ms']/1000,timezone.utc).isoformat()
            v['fuel_receipt_last_station']=r['station']
            v['fuel_receipt_last_eur_per_km']=r['eur_per_km']

    for r in db.execute('SELECT * FROM sync_vehicle_meta'):
        v=vehicle(r['vehicle_key'])
        v.update(plate=r['plate'],bluetooth=r['bluetooth'],fuel_unit=r['fuel_unit'],
                 fuel_consumption=r['fuel_consumption'],fuel_price_eur_liter=r['fuel_price_eur_liter'])

    # The same trip may exist in a full snapshot AND incremental sync: primary-key wins.
    # Imported synthetic HA trips must NOT be counted as GPS trips.
    trips={}
    for r in db.execute('SELECT * FROM snapshot_trips WHERE imported=0 AND (origin="manual" OR (distance_m>=100 AND max_speed>=10))'):
        trips[r['trip_id']] = dict(id=r['trip_id'],key=r['vehicle_key'],category=r['category'],
          started=r['started_ms'],ended=r['ended_ms'],distance=r['distance_m'],
          speed=r['max_speed'],points=r['gps_points'],origin=r['origin'],
          start_address=r['start_address'],end_address=r['end_address'])
    for r in db.execute('SELECT * FROM sync_trips'):
        item=trips.get(r['id'],{})
        trips[r['id']] = dict(item,id=r['id'],key=r['vehicle_key'],category=r['category'],
          started=r['started_ms'],ended=r['ended_ms'],distance=r['distance_m'],origin=r['origin'])
        vehicle(r['vehicle_key'],r['vehicle_name'])

    for t in trips.values():
        try:
            key=t['key']; cat=t['category'] if t['category'] in categories else 'Onbekend'
            dt=datetime.fromtimestamp(t['started']/1000,timezone.utc).astimezone(tz).date()
            minutes=max(0.,(t['ended']-t['started'])/60000.)
            km=max(0.,t['distance']/1000.)
            timestamp=datetime.fromtimestamp(t['ended']/1000,timezone.utc).isoformat()
        except (ValueError,KeyError,TypeError,OverflowError):
            continue
        v=vehicle(key)
        for obj in (v,totals,categories[cat]):
            obj['km_total']+=km; obj['minutes_total']+=minutes; obj['trips_total']+=1
            if dt==today:
                obj['km_today']+=km; obj['minutes_today']+=minutes; obj['trips_today']+=1
            if week_start<=dt<=today:
                obj['km_week']+=km; obj['minutes_week']+=minutes; obj['trips_week']+=1
            if (dt.year,dt.month)==month:
                obj['km_month']+=km; obj['minutes_month']+=minutes; obj['trips_month']+=1
        v[{'Privé':'km_private','Zakelijk':'km_business'}.get(cat,'km_unknown')]+=km
        if cat in ('Privé','Zakelijk'):
            prefix='private' if cat=='Privé' else 'business'
            v[prefix+'_km_total']+=km
            if dt==today:v[prefix+'_km_today']+=km
            if (dt.year,dt.month)==month:
                v[prefix+'_km_month']+=km
                v[prefix+'_minutes_month']+=minutes
                v[prefix+'_trips_month']+=1
        if t.get('speed'):
            # Database speed is recorded in km/h by the PointStore.
            v['max_speed_kmh']=max(v['max_speed_kmh'] or 0.,float(t['speed']))
        if v['last_trip_at'] is None or timestamp>v['last_trip_at']:
            v['last_trip_at']=timestamp;v['last_trip_distance_km']=km;v['last_trip_minutes']=minutes
            v['last_trip_from']=t.get('start_address') or None
            v['last_trip_to']=t.get('end_address') or None

    for r in db.execute('SELECT * FROM snapshot_history'):
        v=vehicle(r['vehicle_key'])
        cat= v.get('usage','Onbekend')
        # HA source observations do not reliably encode a private/business category.
        # They are NOT allocated to a category unless the vehicle's usage is confirmed;
        # the source remains distinct from trip classifications.
        for obj in (v,totals):
            obj['historical_records']+=1;obj['historical_review']+=int(r['review'])
            if r['kind']=='km':
                obj['historical_raw_km']+=r['km'];obj['historical_approved_km']+=r['approved_km']
            elif r['kind']=='time':
                obj['historical_raw_minutes']+=r['minutes'];obj['historical_approved_minutes']+=r['approved_minutes']

    for t in (totals,*categories.values(),*vehicles.values()):
        t['combined_km_total']=t['km_total']+t['historical_approved_km']
        t['combined_minutes_total']=t['minutes_total']+t['historical_approved_minutes']
        t['avg_trip_km']=t['km_total']/t['trips_total'] if t['trips_total'] else None

    # Use newer incremental sync detail events when available, fallback to archived snapshot.
    for typ, table, cols in (
        ('fuel','snapshot_fuel',('event_id','vehicle_key','time_ms','odometer_km','liters','total_eur','fuel_type','note')),
        ('maintenance','snapshot_maintenance',('event_id','vehicle_key','label','due_at','due_km','done')),
        ('parking','snapshot_parking',('event_id','vehicle_key','time_ms','lat','lon','accuracy_m','source')),
    ):
        events={}
        replacements={r['device_id'] for r in db.execute(
            'SELECT device_id FROM sync_detail_scopes WHERE kind=?',(typ,))}
        for r in db.execute('SELECT * FROM '+table):
            if r['device_id'] in replacements:continue
            events[(r['device_id'],r['event_id'])] = dict(r)
        for r in db.execute('SELECT * FROM sync_details WHERE kind=?',(typ,)):
            data=json.loads(r['payload']);data['vehicle_key']=r['vehicle_key']
            events[(r['device_id'],r['event_id'])] = data
        by_v=defaultdict(list)
        fingerprints=set()
        for e in events.values():
            if typ=='fuel':
                fingerprint=(e['vehicle_key'],e.get('time_ms'),e.get('odometer_km'),e.get('liters'),e.get('total_eur'))
            elif typ=='maintenance':
                fingerprint=(e['vehicle_key'],e.get('label'),e.get('due_at'),e.get('due_km'),e.get('done'))
            else:
                fingerprint=(e['vehicle_key'],e.get('time_ms'),e.get('lat'),e.get('lon'))
            if fingerprint in fingerprints:continue
            fingerprints.add(fingerprint)
            by_v[e['vehicle_key']].append(e)
        for key,rows in by_v.items():
            v=vehicle(key)
            if typ=='fuel':
                rows.sort(key=lambda e:(e['time_ms'],e['event_id']))
                v['fuel_events']=len(rows)
                v['fuel_liters']=sum(float(e['liters']) for e in rows)
                v['fuel_eur']=sum(float(e['total_eur']) for e in rows)
                v['fuel_eur_month']=sum(float(e['total_eur']) for e in rows if (
                  (lambda d:(d.year,d.month)==month)(datetime.fromtimestamp(e['time_ms']/1000,timezone.utc).astimezone(tz))))
                last=rows[-1];v['fuel_last_type']=last.get('fuel_type');v['fuel_last_note']=last.get('note')
                v['fuel_last_at']=datetime.fromtimestamp(last['time_ms']/1000,timezone.utc).isoformat()
                v['fuel_last_eur_per_liter']=float(last['total_eur'])/float(last['liters']) if last['liters'] else None
                prev=next((x for x in reversed(rows[:-1]) if x['odometer_km'] < last['odometer_km']),None)
                if prev is not None:
                    diff=last['odometer_km']-prev['odometer_km']
                    if 20<=diff<=2500:v['fuel_last_consumption_l_100km']=100*last['liters']/diff
            elif typ=='maintenance':
                remaining=[e for e in rows if not int(e['done'])]
                v['maintenance_open']=len(remaining);v['maintenance_done']=len(rows)-len(remaining)
                v['maintenance_next_label']=min(remaining,key=lambda e:(e.get('due_at') or 9999999999999,e.get('due_km') or 99999999)).get('label') if remaining else None
                nowms=int(datetime.now(timezone.utc).timestamp()*1000)
                odo=v.get('odometer_km')
                v['maintenance_due']=any((e.get('due_at',0)>0 and e['due_at']<=nowms)
                    or (e.get('due_km',0)>0 and odo is not None and e['due_km']<=odo)
                    for e in remaining)
                dates=[int(e['due_at']) for e in remaining if e['due_at']]
                kms=[float(e['due_km']) for e in remaining if e['due_km']]
                v['maintenance_next_due_at']=datetime.fromtimestamp(min(dates)/1000,timezone.utc).isoformat() if dates else None
                v['maintenance_next_due_km']=min(kms) if kms else None
            elif typ=='parking':
                last=max(rows,key=lambda e:e['time_ms'])
                v['parking_last_at']=datetime.fromtimestamp(last['time_ms']/1000,timezone.utc).isoformat()
                v['parking_lat']=last['lat'];v['parking_lon']=last['lon'];v['parking_accuracy_m']=last['accuracy_m']
                v['parking_source']=last.get('source')

    # Fleet totals for costs and service; month total of history stays separate.
    for v in vehicles.values():
        for field in ('fuel_events','fuel_liters','fuel_eur','fuel_eur_month','maintenance_open','maintenance_done'):
            totals[field]+=v[field]
        _round(v)
    _round(totals)
    for obj in categories.values():_round(obj)
    last_sync=db.execute('SELECT MAX(last_sync_at) FROM sync_devices').fetchone()[0]
    indexed=db.execute('SELECT COUNT(*) FROM snapshot_indexed').fetchone()[0]
    return {'version':'0.5.0','timezone':tzname,'generated_at':datetime.now(timezone.utc).isoformat(),
      'last_sync_at':last_sync,'indexed_backups':indexed,
      'vehicles':sorted(vehicles.values(),key=lambda x:x['key']),
      'totals':totals,'categories':categories,
      'source':'unique_completed_trips_plus_conservatively_reconciled_HA_history',
      'note':'Ruwe HA-observaties apart; alleen goedgekeurd ontbrekend HA-deel bij GPS-kilometers. Privé/zakelijk alleen per rit classificatie.'}


def list_trip_details(db,key=None,limit=50):
    limit=min(max(1,int(limit)),100)
    # Details without thousands of GPS points; those are only in the validated ZIP.
    data={}
    for r in db.execute('SELECT * FROM snapshot_trips WHERE imported=0 AND (origin="manual" OR (distance_m>=100 AND max_speed>=10))'):
        data[r['trip_id']]=dict(r)
    for r in db.execute('SELECT id,vehicle_key,category,started_ms,ended_ms,distance_m,origin FROM sync_trips'):
        item=data.get(r['id'],{});item.update(dict(trip_id=r['id'],vehicle_key=r['vehicle_key'],category=r['category'],
          started_ms=r['started_ms'],ended_ms=r['ended_ms'],distance_m=r['distance_m'],origin=r['origin']))
        data[r['id']]=item
    rows=[v for v in data.values() if key is None or v.get('vehicle_key')==key]
    rows.sort(key=lambda x:x.get('started_ms',0),reverse=True)
    return rows[:limit]


def list_records(db,kind,key=None,limit=100):
    """Authenticated, bounded raw detail listing for future HA panels/diagnostics."""
    tables={'history':'snapshot_history','fuel':'snapshot_fuel',
            'maintenance':'snapshot_maintenance','parking':'snapshot_parking'}
    if kind not in tables:raise ValueError('onbekend recordtype')
    limit=min(max(int(limit),1),500)
    collected={}
    replacements=set()
    if kind!='history':
        replacements={r['device_id'] for r in db.execute(
           'SELECT device_id FROM sync_detail_scopes WHERE kind=?',(kind,))}
    for r in db.execute('SELECT * FROM '+tables[kind]):
        if r['device_id'] in replacements:continue
        if key is not None and r['vehicle_key']!=key:continue
        collected[(r['device_id'],r['record_id'] if kind=='history' else r['event_id'])]=dict(r)
    if kind!='history':
        for r in db.execute('SELECT * FROM sync_details WHERE kind=?',(kind,)):
            if key is not None and r['vehicle_key']!=key:continue
            row=json.loads(r['payload']);row.update(device_id=r['device_id'],vehicle_key=r['vehicle_key'])
            collected[(r['device_id'],r['event_id'])]=row
    order={'history':'date_key','fuel':'time_ms','maintenance':'due_at','parking':'time_ms'}[kind]
    result=list(collected.values())
    result.sort(key=lambda x:(x.get(order) or '') if kind=='history' else (x.get(order) or 0), reverse=True)
    return result[:limit]
