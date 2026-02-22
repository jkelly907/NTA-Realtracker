from flask import Flask, jsonify, render_template
import requests
import csv
import time
import os
from google.transit import gtfs_realtime_pb2

app = Flask(__name__)

API_KEY = os.environ.get("NTA_API_KEY", "")  # <-- Replace with your NTA API key

VEHICLES_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"
TRIP_UPDATES_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"

# Local CSV files — assumed to be in same folder as app.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OPERATOR_NAMES = {
    "5402": "Dublin Bus",
    "5249": "Go-Ahead Ireland",
    "5399": "Bus Éireann",
    "5240": "Bus Éireann Expressway",
    "5403": "Irish Rail",
    "5186": "Waterford Transit",
    "5242": "GoBus",
}

# --- STATIC GTFS CACHE ---
static_cache = {
    'routes': {},       # route_id -> { short_name, long_name }
    'trips': {},        # trip_id  -> { route_id, headsign }
    'stops': {},        # stop_id  -> { name, lat, lon }
    'stop_times': {},   # trip_id  -> [ { stop_id, stop_sequence, arrival_time } ]
    'loaded_at': 0,
}
STATIC_TTL = 3600

# --- LIVE DATA CACHE ---
live_cache = {
    'vehicles': [],
    'updates': {},
    'stats': {'total': 0, 'on_time': 0, 'delayed': 0, 'early': 0},
    'vehicles_fetched_at': 0,
    'updates_fetched_at': 0,
}
UPDATES_TTL = 60


def load_csv(filename):
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        print(f"⚠️  {filename} not found in {BASE_DIR}")
        return []
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def load_static_gtfs():
    print("📂 Loading static GTFS from local files...")

    routes = {}
    for row in load_csv('routes.txt'):
        routes[row['route_id']] = {
            'short_name': row.get('route_short_name', '').strip(),
            'long_name':  row.get('route_long_name', '').strip(),
        }

    trips = {}
    for row in load_csv('trips.txt'):
        trips[row['trip_id']] = {
            'route_id': row.get('route_id', ''),
            'headsign': row.get('trip_headsign', '').strip(),
        }

    stops = {}
    for row in load_csv('stops.txt'):
        stops[row['stop_id']] = {
            'name': row.get('stop_name', '').strip(),
            'lat':  float(row.get('stop_lat', 0) or 0),
            'lon':  float(row.get('stop_lon', 0) or 0),
        }

    # stop_times.txt maps each trip to its ordered list of stops.
    # We sort by stop_sequence so we can find "which stop comes next"
    # for any given vehicle based on where it currently is in its trip.
    stop_times = {}
    for row in load_csv('stop_times.txt'):
        tid = row['trip_id']
        if tid not in stop_times:
            stop_times[tid] = []
        stop_times[tid].append({
            'stop_id':       row['stop_id'],
            'stop_sequence': int(row.get('stop_sequence', 0)),
            'arrival_time':  row.get('arrival_time', ''),
        })
    # Sort each trip's stops by sequence number
    for tid in stop_times:
        stop_times[tid].sort(key=lambda x: x['stop_sequence'])

    static_cache['routes']     = routes
    static_cache['trips']      = trips
    static_cache['stops']      = stops
    static_cache['stop_times'] = stop_times
    static_cache['loaded_at']  = time.time()

    print(f"✅ Loaded: {len(routes)} routes, {len(trips)} trips, "
          f"{len(stops)} stops, {len(stop_times)} trip timetables")


def ensure_static_loaded():
    if time.time() - static_cache['loaded_at'] > STATIC_TTL:
        load_static_gtfs()


def get_operator(route_id):
    prefix = route_id.split("_")[0] if "_" in route_id else ""
    return OPERATOR_NAMES.get(prefix, "Unknown Operator")


def resolve_route_name(route_id, trip_id):
    route_info = static_cache['routes'].get(route_id, {})
    trip_info  = static_cache['trips'].get(trip_id, {})
    short_name = route_info.get('short_name', '')
    headsign   = trip_info.get('headsign', '')
    if not short_name:
        short_name = route_id.split('_')[-1] if '_' in route_id else route_id
    return short_name, headsign


def resolve_stop_name(stop_id):
    return static_cache['stops'].get(stop_id, {}).get('name', stop_id)


def get_next_stop(trip_id, current_stop_ids):
    """
    Works out the next stop for a vehicle using two sources:

    1. The live trip update contains a list of upcoming stops with delays.
       The FIRST stop in that list is the next one the vehicle will call at.
       This is the most accurate method when delay data is available.

    2. If no live stop data, fall back to stop_times.txt — the scheduled
       timetable — and return the first stop in the sequence (start of route).
       This is less precise but better than showing nothing.
    """
    # Method 1: use live stop list if available
    if current_stop_ids:
        first_stop_id = current_stop_ids[0]
        name = resolve_stop_name(first_stop_id)
        return {'stop_id': first_stop_id, 'stop_name': name}

    # Method 2: fall back to scheduled stop_times
    scheduled = static_cache['stop_times'].get(trip_id, [])
    if scheduled:
        first = scheduled[0]
        return {
            'stop_id':   first['stop_id'],
            'stop_name': resolve_stop_name(first['stop_id']),
        }

    return None


def fetch_vehicles():
    try:
        response = requests.get(VEHICLES_URL, headers={"x-api-key": API_KEY}, timeout=10)
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        vehicles = []
        for entity in feed.entity:
            if entity.HasField('vehicle'):
                v = entity.vehicle
                lat = v.position.latitude
                lon = v.position.longitude
                if lat == 0.0 and lon == 0.0:
                    continue

                route_id   = v.trip.route_id
                trip_id    = v.trip.trip_id
                short_name, headsign = resolve_route_name(route_id, trip_id)

                vehicles.append({
                    'id':            entity.id,
                    'trip_id':       trip_id,
                    'route_id':      route_id,
                    'route_name':    short_name,
                    'destination':   headsign,
                    'operator':      get_operator(route_id),
                    'lat':           lat,
                    'lon':           lon,
                    'bearing':       v.position.bearing,
                    'speed':         round(v.position.speed * 3.6, 1) if v.position.speed else None,
                    'vehicle_id':    v.vehicle.id,
                    'vehicle_label': v.vehicle.label or v.vehicle.id,
                    'license_plate': v.vehicle.license_plate or None,
                    'timestamp':     v.timestamp,
                    'delay':         0,
                    'stops':         [],
                    'next_stop':     None,
                })

        if len(vehicles) > 10:
            live_cache['vehicles'] = vehicles
            live_cache['vehicles_fetched_at'] = time.time()
            print(f"✅ Vehicles: {len(vehicles)} fetched")
        else:
            print(f"⚠️  Vehicles API returned {len(vehicles)} — keeping cached {len(live_cache['vehicles'])}")

    except Exception as e:
        print(f"⚠️  Vehicles fetch failed: {e} — keeping cached data")


def fetch_trip_updates():
    try:
        response = requests.get(TRIP_UPDATES_URL, headers={"x-api-key": API_KEY}, timeout=10)
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        updates = {}
        for entity in feed.entity:
            if entity.HasField('trip_update'):
                tu      = entity.trip_update
                trip_id = tu.trip.trip_id
                stops   = []
                for stu in tu.stop_time_update:
                    delay   = stu.arrival.delay if stu.HasField('arrival') else 0
                    stop_id = stu.stop_id
                    stops.append({
                        'stop_id':    stop_id,
                        'stop_name':  resolve_stop_name(stop_id),
                        'delay':      delay,
                        'delay_mins': round(delay / 60, 1),
                    })
                max_delay = max((s['delay'] for s in stops), default=0)
                updates[trip_id] = {
                    'route_id':  tu.trip.route_id,
                    'stops':     stops,
                    'max_delay': max_delay,
                }

        if len(updates) > 10:
            live_cache['updates'] = updates
            live_cache['updates_fetched_at'] = time.time()
            print(f"✅ Trip updates: {len(updates)} fetched")
        else:
            print(f"⚠️  Trip updates returned {len(updates)} — keeping cached delays")

    except Exception as e:
        print(f"⚠️  Trip updates fetch failed: {e} — keeping cached delays")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data')
def api_data():
    ensure_static_loaded()
    now = time.time()

    fetch_vehicles()

    if now - live_cache['updates_fetched_at'] > UPDATES_TTL:
        fetch_trip_updates()
    else:
        age = int(now - live_cache['updates_fetched_at'])
        print(f"ℹ️  Using cached trip updates ({age}s old)")

    vehicles = []
    for v in live_cache['vehicles']:
        v = dict(v)
        trip_id = v['trip_id']

        if trip_id in live_cache['updates']:
            update    = live_cache['updates'][trip_id]
            v['delay']  = update['max_delay']
            v['stops']  = update['stops']
            # Next stop = first entry in live stop list
            live_stop_ids = [s['stop_id'] for s in update['stops']]
        else:
            live_stop_ids = []

        v['next_stop'] = get_next_stop(trip_id, live_stop_ids)
        vehicles.append(v)

    on_time = sum(1 for v in vehicles if abs(v['delay']) < 60)
    delayed = sum(1 for v in vehicles if v['delay'] >= 60)
    early   = sum(1 for v in vehicles if v['delay'] < -60)

    return jsonify({
        'vehicles':     vehicles,
        'stats':        {'total': len(vehicles), 'on_time': on_time,
                         'delayed': delayed, 'early': early},
        'vehicles_age': int(now - live_cache['vehicles_fetched_at']),
        'updates_age':  int(now - live_cache['updates_fetched_at']),
    })


@app.route('/api/trip-route/<trip_id>')
def trip_route(trip_id):
    """
    Returns the full ordered stop list for a trip with lat/lon for each stop.
    The frontend uses this to draw the route line and stop dots on the map.
    We use stop_times.txt (already loaded) to get the sequence, then
    look up each stop's coordinates from stops.txt.
    """
    ensure_static_loaded()
    scheduled = static_cache['stop_times'].get(trip_id, [])
    if not scheduled:
        return jsonify({'stops': [], 'polyline': []})

    stops_out = []
    polyline   = []
    for st in scheduled:
        stop_id   = st['stop_id']
        stop_info = static_cache['stops'].get(stop_id, {})
        lat = stop_info.get('lat', 0)
        lon = stop_info.get('lon', 0)
        if lat == 0 and lon == 0:
            continue
        stops_out.append({
            'stop_id':       stop_id,
            'stop_name':     stop_info.get('name', stop_id),
            'lat':           lat,
            'lon':           lon,
            'arrival_time':  st.get('arrival_time', ''),
            'stop_sequence': st.get('stop_sequence', 0),
        })
        polyline.append([lat, lon])

    return jsonify({'stops': stops_out, 'polyline': polyline})


if __name__ == '__main__':
    print("\n🚌 NTA Live Tracker starting...")
    load_static_gtfs()
    print("📍 Open http://localhost:8080 in your browser\n")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)