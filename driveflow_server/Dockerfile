FROM python:3.12-alpine
LABEL io.hass.version="0.6.11" io.hass.type="app" io.hass.arch="aarch64|amd64"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir "ijson>=3.3,<4"
COPY server.py sync_store.py snapshot_index.py detail_store.py smart_odometer.py smart_fuel.py parking_geoapify.py road_discovery_store.py discovery_places.py discovery_statistics.py province_statistics.py discovery2_upload.py discovery2_analysis.py discovery2_shadow.py discovery2_clients.py discovery2_readiness.py discovery2_review.py discovery2_audit.py compare_discovery2_catalogs.py /app/
EXPOSE 8098
CMD ["python", "-u", "/app/server.py"]
