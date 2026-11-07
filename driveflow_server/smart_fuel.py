"""Receipt evidence linked to existing fuel events; image bytes kept outside JSON tables."""
import base64
import hashlib
import math
import os
import re
import uuid
from pathlib import Path
from sync_store import vehicle_key

ID=re.compile(r'^[a-f0-9-]{36}$')
EXT={'jpg','png','webp','heic','heif'}

def create_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS fuel_receipts (
      device_id TEXT NOT NULL, id TEXT NOT NULL, fuel_id INTEGER NOT NULL,
      vehicle_key TEXT NOT NULL, vehicle_name TEXT NOT NULL,
      time_ms INTEGER NOT NULL, station TEXT NOT NULL, source TEXT NOT NULL,
      liters REAL NOT NULL, total_eur REAL NOT NULL, unit_price REAL NOT NULL,
      odometer_km REAL NOT NULL, eur_per_km REAL, fuel_type TEXT NOT NULL,
      ocr_text TEXT NOT NULL, photo_name TEXT NOT NULL, photo_sha256 TEXT NOT NULL,
      received_at TEXT NOT NULL, PRIMARY KEY(device_id,id), UNIQUE(device_id,fuel_id))''')

def save(db,base:Path,device:str,payload:dict,now:str):
    if not isinstance(payload,dict):raise ValueError('ongeldige tankbon')
    uid=payload.get('id');name=payload.get('vehicle');source=payload.get('source')
    if not isinstance(uid,str) or not ID.fullmatch(uid):raise ValueError('ongeldige ID')
    if not isinstance(name,str) or not 1<=len(name)<=100:raise ValueError('ongeldig voertuig')
    if source not in ('camera','gallery'):raise ValueError('ongeldige bron')
    fuel_id=payload.get('fuel_id');timestamp=payload.get('time_ms')
    if not isinstance(fuel_id,int) or fuel_id<=0:raise ValueError('ongeldige tankbeurt')
    if not isinstance(timestamp,int) or not 1_500_000_000_000<timestamp<4_100_000_000_000:raise ValueError('ongeldige tijd')
    def number(field,low,high,nullable=False):
        v=payload.get(field)
        if nullable and v is None:return None
        if not isinstance(v,(int,float)) or not math.isfinite(v) or not low<=v<=high:raise ValueError('ongeldig '+field)
        return float(v)
    liters=number('liters',0.01,1000);total=number('total_eur',0.01,10000)
    odometer=number('odometer_km',0,5_000_000);price=number('unit_price',0.01,100)
    cost=number('eur_per_km',0,100,True)
    station=payload.get('station','');ocr=payload.get('ocr_text','');fuel_type=payload.get('fuel_type','')
    if not isinstance(station,str) or len(station)>150 or not isinstance(ocr,str) or len(ocr)>4000 or not isinstance(fuel_type,str) or len(fuel_type)>60:raise ValueError('ongeldige tekst')
    photo=payload.get('photo_base64');ext=payload.get('photo_extension')
    if not isinstance(photo,str) or len(photo)>21_000_000 or ext not in EXT:raise ValueError('ongeldige foto')
    try:raw=base64.b64decode(photo,validate=True)
    except Exception as exc:raise ValueError('ongeldige fotocodering') from exc
    if not 1<=len(raw)<=15_000_000:raise ValueError('foto te groot of leeg')
    sha=hashlib.sha256(raw).hexdigest();filename=uid+'.'+ext
    previous=db.execute('SELECT total_eur,photo_sha256 FROM fuel_receipts WHERE device_id=? AND id=?',(device,uid)).fetchone()
    if previous:
        if float(previous[0])!=total or previous[1]!=sha:raise ValueError('conflicterende bon-ID')
        return {'ok':True,'already_present':True,'id':uid}
    folder=base/'fuel_receipts'/device
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/filename;temp=folder/(uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as file:
            file.write(raw);file.flush();os.fsync(file.fileno())
        temp.replace(target)
    finally:temp.unlink(missing_ok=True)
    try:
        db.execute('''INSERT INTO fuel_receipts
          (device_id,id,fuel_id,vehicle_key,vehicle_name,time_ms,station,source,liters,total_eur,unit_price,odometer_km,eur_per_km,fuel_type,ocr_text,photo_name,photo_sha256,received_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (device,uid,fuel_id,vehicle_key(name),name,timestamp,station,source,liters,total,price,odometer,cost,fuel_type,ocr,filename,sha,now))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {'ok':True,'already_present':False,'id':uid}

def records(db,vehicle=None,limit=100):
    sql='SELECT device_id,id,fuel_id,vehicle_key,vehicle_name,time_ms,station,source,liters,total_eur,unit_price,odometer_km,eur_per_km,fuel_type,ocr_text,photo_name,photo_sha256 FROM fuel_receipts'
    args=[]
    if vehicle:sql+=' WHERE vehicle_key=?';args.append(vehicle)
    sql+=' ORDER BY time_ms DESC LIMIT ?';args.append(min(max(int(limit),1),500))
    return [dict(r) for r in db.execute(sql,args)]
