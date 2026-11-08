FROM python:3.12-alpine
LABEL io.hass.version="0.6.7" io.hass.type="app" io.hass.arch="aarch64|amd64"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py sync_store.py snapshot_index.py detail_store.py smart_odometer.py smart_fuel.py parking_geoapify.py road_discovery_store.py discovery_places.py discovery_statistics.py /app/
EXPOSE 8098
CMD ["python", "-u", "/app/server.py"]
