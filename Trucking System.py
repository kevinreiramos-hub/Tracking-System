import streamlit as st
import pandas as pd
import numpy as np
import folium
import random
import statistics
import requests
import sqlite3
import hashlib
import hmac
import os
import json
from datetime import date, datetime
from streamlit_folium import st_folium
from streamlit_js_eval import streamlit_js_eval
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# =============================================================================
# 1. CONFIG + CONSTANTS
# =============================================================================
st.set_page_config(page_title="Cord Chemicals Delivery", layout="wide")

DEPOT_NAME = "Cord Chemicals"
DEPOT_LAT, DEPOT_LNG = 14.5844537, 121.0475689
DEFAULT_MAPBOX_TOKEN = "pk.eyJ1Ijoia2V2aW5yZWkyIiwiYSI6ImNtcHl4ejY4ejA1ODYydHB2dDN3NXppcm0ifQ.Xpq-jmcdlyoVLCwDGulA4g"
OSRM_DEFAULT = "http://router.project-osrm.org"
DB_PATH = "delivery_app.db"

# Geofence threshold in meters (e.g., 150 meters from delivery point)
GEOFENCE_RADIUS_METERS = 150.0

ACCOUNTS = pd.DataFrame({
    "Account Name": [
        "De Luxe Electrical & Hdwe. Supply",
        "Firestone Trading",
        "Fishermen Center",
        "Jr Multi Business Resources, Inc.",
        "Marswin Marketing Inc",
        "Ace Hardware (SM Megamall)",
        "Ace Hardware (Alabang)",
    ],
    "Address": [
        "162 N Carpio St, Grace Park East, Caloocan, 1403 Metro Manila",
        "415 San Nicolas St, San Nicolas, Manila, 1010 Metro Manila",
        "823 Tetuan St, Santa Cruz, Manila, 1003 Metro Manila",
        "111 Don Manuel Agregado Street, Quezon City, 1113 Metro Manila",
        "408 San Nicolas St, San Nicolas, Manila, Metro Manila",
        "202 EDSA cor. Dona Julia Vargas Ave, Mandaluyong City, 1550 Metro Manila",
        "2nd Flr, Festival Mall, Zapote Wing, Corporate Ave, Alabang, Muntinlupa, 1770 Metro Manila",
    ],
    "Territory": ["Caloocan", "Manila", "Manila", "Quezon City", "Manila", "Mandaluyong", "Muntinlupa"],
    "Latitude": [14.646187, 14.5999652, 14.6003507, 14.6315267, 14.6000217, 14.58631, 14.4189642],
    "Longitude": [120.983901, 120.9702905, 120.977661, 121.001982, 120.9702217, 121.057465, 121.040753],
})

DRIVERS = ["Alex Colorito", "Ritchel Junio", "Jomer Lumauig"]
TRUCKS = [
    "Isuzu / WQQ-440",
    "Isuzu 6 Wheeler / NLD-2075",
    "Isuzu 4 Wheeler / NAW-3984",
    "Isuzu / RJA-613",
]
TRAFFIC_LEVELS = {
    "Free flow (no traffic)": 1.0, "Light": 1.15, "Moderate": 1.35, "Heavy (rush hour)": 1.7,
}

SEED_USERS = [
    ("dispatch", "Dispatcher", "dispatcher", "dispatch123"),
    ("alex", "Alex Colorito", "driver", "driver123"),
    ("ritchel", "Ritchel Junio", "driver", "driver123"),
    ("jomer", "Jomer Lumauig", "driver", "driver123"),
]

# =============================================================================
# 2. DATABASE LAYER
# =============================================================================
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pw(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return salt, digest


def verify_pw(password, salt, digest):
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return hmac.compare_digest(check, digest)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, name TEXT, role TEXT, salt TEXT, pwd TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT, driver TEXT, truck TEXT, status TEXT,
        stops_json TEXT, total_km REAL, time_str TEXT,
        created_by TEXT, created_at TEXT)""")
    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        for username, name, role, pw in SEED_USERS:
            salt, digest = hash_pw(pw)
            cur.execute("INSERT INTO users VALUES (?,?,?,?,?)", (username, name, role, salt, digest))
    conn.commit()
    conn.close()


def get_user(username):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_password(username, new_pw):
    salt, digest = hash_pw(new_pw)
    conn = get_conn()
    conn.execute("UPDATE users SET salt=?, pwd=? WHERE username=?", (salt, digest, username))
    conn.commit()
    conn.close()


def create_assignment(driver, truck, stops, total_km, time_str, created_by):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO assignments
        (run_date, driver, truck, status, stops_json, total_km, time_str, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (date.today().isoformat(), driver, truck, "Assigned",
         json.dumps(stops), total_km, time_str, created_by, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_assignments(driver=None):
    conn = get_conn()
    if driver:
        rows = conn.execute("SELECT * FROM assignments WHERE driver=? ORDER BY run_date DESC, id DESC",
                            (driver,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM assignments ORDER BY run_date DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_assignment(aid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_assignment(aid, stops, status):
    conn = get_conn()
    conn.execute("UPDATE assignments SET stops_json=?, status=? WHERE id=?",
                 (json.dumps(stops), status, aid))
    conn.commit()
    conn.close()


def delete_assignment(aid):
    conn = get_conn()
    conn.execute("DELETE FROM assignments WHERE id=?", (aid,))
    conn.commit()
    conn.close()


init_db()

# =============================================================================
# 3. BULLETPROOF INSTANT NATIVE URL PATH AUTH PROTOCOL
# =============================================================================
if "auth" not in st.session_state:
    st.session_state.auth = None

# Pure execution script injected directly into browser execution stream
st.components.v1.html(
    """
    <script>
    const sessionStr = window.localStorage.getItem('cord_user_session');
    if (sessionStr) {
        try {
            const data = JSON.parse(sessionStr);
            if (data && data.username) {
                const url = new URL(window.parent.location.href);
                if (url.searchParams.get('user') !== data.username) {
                    url.searchParams.set('user', data.username);
                    window.parent.location.href = url.href;
                }
            }
        } catch(e) {}
    }
    </script>
    """,
    height=0, width=0
)

# Process instant backend queries if present inside the browser URL parameters
query_user = st.query_params.get("user")
if query_user and st.session_state.auth is None:
    rec = get_user(query_user.strip().lower())
    if rec:
        st.session_state.auth = {"username": rec["username"], "name": rec["name"], "role": rec["role"]}

# Render form gate only if url verification checks fall flat
if st.session_state.auth is None:
    st.title("🔐 Cord Chemicals Delivery — Sign in")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        rec = get_user(u.strip().lower())
        if rec and verify_pw(p, rec["salt"], rec["pwd"]):
            user_payload = {"username": rec["username"], "name": rec["name"], "role": rec["role"]}
            st.session_state.auth = user_payload
            st.query_params["user"] = rec["username"]
            
            # Save token to localStorage to protect future refreshes
            escaped_payload = json.dumps(user_payload).replace("'", "\\'")
            streamlit_js_eval(
                data_string=f"localStorage.setItem('cord_user_session', '{escaped_payload}');",
                key="set_local_session"
            )
            st.rerun()
        else:
            st.error("Invalid username or password.")
    with st.expander("Demo accounts (change passwords after first login)"):
        st.markdown(
            "- **Dispatcher:** `dispatch` / `dispatch123`\n"
            "- **Drivers:** `alex`, `ritchel`, `jomer` — all `driver123`"
        )
    st.stop()

USER = st.session_state.auth

# =============================================================================
# 4. SHARED HELPER FUNCTIONS
# =============================================================================
def single_haversine(lat1, lon1, lat2, lon2):
    """Calculates distance between two points in meters."""
    R = 6371000.0
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    c = 2.0 * np.arcsin(np.sqrt(a))
    return R * c


def get_osrm_route_with_steps(start_lat, start_lng, end_lat, end_lng, server):
    """Fetches full geometric shape coordinates alongside step-by-step street text maneuvers from OSRM."""
    url = f"{server}/route/v1/driving/{start_lng},{start_lat};{end_lng},{end_lat}?overview=full&geometries=geojson&steps=true"
    steps_list = []
    try:
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if data.get("routes"):
                route = data["routes"][0]
                coords = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]
                
                # Parse OSRM steps
                for leg in route.get("legs", []):
                    for step in leg.get("steps", []):
                        maneuver = step.get("maneuver", {})
                        instruction = maneuver.get("instruction", "")
                        street = step.get("name", "")
                        distance = step.get("distance", 0.0)
                        
                        if instruction:
                            if street and street != "":
                                desc = f"{instruction} onto {street}"
                            else:
                                desc = instruction
                            steps_list.append({"text": desc, "distance": distance})
                            
                return coords, steps_list
    except Exception:
        pass
    return [[start_lat, start_lng], [end_lat, end_lng]], [{"text": "Proceed toward destination", "distance": 0.0}]


def haversine_matrix(coords):
    rad = np.radians(np.asarray(coords, dtype=float))
    lat = rad[:, 0][:, None]
    lng = rad[:, 1][:, None]
    dlat = lat - lat.T
    dlng = lng - lng.T
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlng / 2.0) ** 2
    c = 2.0 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return 6371000.0 * c


@st.cache_data(show_spinner=False)
def get_road_matrices(coords_tuple, server):
    coords = list(coords_tuple)
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"{server}/table/v1/driving/{coord_str}?annotations=duration,distance"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == "Ok":
                return data.get("durations"), data.get("distances")
    except Exception:
        pass
    return None, None


@st.cache_data(show_spinner=False)
def get_mapbox_matrices(coords_tuple, token):
    coords = list(coords_tuple)
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"https://api.mapbox.com/directions-matrix/v1/mapbox/driving-traffic/{coord_str}"
    params = {"annotations": "duration,distance", "access_token": token}
    try:
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == "Ok":
                return data.get("durations"), data.get("distances")
    except Exception:
        pass
    return None, None


@st.cache_data(show_spinner=False)
def get_mapbox_congested_route_with_steps(p_lat, p_lng, c_lat, c_lng, token):
    """Fetches congestion traffic maps together with step text directions from Mapbox."""
    url = f"https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{p_lng},{p_lat};{c_lng},{c_lat}"
    params = {
        "geometries": "geojson", 
        "overview": "full",
        "annotations": "congestion", 
        "steps": "true",
        "banner_instructions": "true",
        "access_token": token
    }
    steps_list = []
    try:
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                coords = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]
                cong = route["legs"][0].get("annotation", {}).get("congestion")
                
                # Parse step text metadata
                for leg in route.get("legs", []):
                    for step in leg.get("steps", []):
                        text_instruction = step.get("maneuver", {}).get("instruction", "")
                        distance = step.get("distance", 0.0)
                        if text_instruction:
                            steps_list.append({"text": text_instruction, "distance": distance})
                            
                return coords, cong, steps_list
    except Exception:
        pass
    return None, None, [{"text": "Follow designated track layout", "distance": 0.0}]


def congestion_color(level):
    return {"low": "#2ECC40", "moderate": "#FF9500", "heavy": "#FF4136",
            "severe": "#8B0000"}.get(level, "#9E9E9E")


def draw_congested_path(m, coords, cong, weight, opacity):
    if not coords:
        return
    if not cong or len(cong) != len(coords) - 1:
        folium.PolyLine(coords, color="#9E9E9E", weight=weight, opacity=opacity).add_to(m)
        return
    a, n = 0, len(cong)
    for j in range(1, n + 1):
        if j == n or cong[j] != cong[a]:
            folium.PolyLine(coords[a:j + 1], color=congestion_color(cong[a]),
                            weight=weight, opacity=opacity).add_to(m)
            a = j


def fmt_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min"
    return "<1 min"


def route_totals(order, durations, distances, factor):
    tot_t = tot_d = 0.0
    for a, b in zip(order[:-1], order[1:]):
        tot_t += durations[a][b] * factor
        tot_d += distances[a][b]
    return tot_t, tot_d


def analyze_route(route_df, itinerary, distances, factor, durations, territory_map):
    stops = []
    for pos, node in enumerate(itinerary):
        if node == 0:
            continue
        prev_n, next_n = itinerary[pos - 1], itinerary[pos + 1]
        depot_km = distances[0][node] / 1000.0
        detour_km = max(0.0, (distances[prev_n][node] + distances[node][next_n]
                              - distances[prev_n][next_n]) / 1000.0)
        name = route_df.iloc[node]["Account Name"]
        stops.append({"name": name, "territory": territory_map.get(name, "—"),
                      "depot_km": depot_km, "detour_km": detour_km})
    depot_kms = [s["depot_km"] for s in stops]
    median_km = statistics.median(depot_kms) if depot_kms else 0.0
    far_threshold = max(12.0, 2.2 * median_km)
    detour_threshold = max(14.0, 2.5 * median_km)
    outliers = []
    for s in stops:
        reasons = []
        if s["depot_km"] > far_threshold:
            reasons.append(f"{s['depot_km']:.0f} km from the depot")
        if s["detour_km"] > detour_threshold:
            reasons.append(f"adds a {s['detour_km']:.0f} km detour to the loop")
        if reasons:
            s["reasons"] = reasons
            outliers.append(s)
    groups = {}
    for s in stops:
        groups.setdefault(s["territory"], []).append(s["name"])
    clusters = {t: names for t, names in groups.items() if len(names) >= 2}
    return {"stops": stops, "outliers": outliers, "clusters": clusters, "median_km": median_km}


def heuristic_comment(findings, num_deliveries, total_km, total_time_str):
    r = random.Random()
    parts = []
    openers = [
        f"Looking at today's {num_deliveries}-stop run ({total_km:.1f} km, about {total_time_str} of driving):",
        f"Quick read on this {num_deliveries}-drop route — roughly {total_km:.1f} km and ~{total_time_str} behind the wheel:",
        f"Dispatcher's take on the {num_deliveries} stops you've lined up ({total_km:.1f} km / ~{total_time_str}):",
        f"Here's how this {num_deliveries}-stop loop shapes up — {total_km:.1f} km, around {total_time_str} of road time:",
    ]
    parts.append(r.choice(openers))
    outliers = findings["outliers"]
    if outliers:
        for o in outliers:
            reason = " and ".join(o["reasons"])
            same_area = findings["clusters"].get(o["territory"], [])
            tips = [
                f"**{o['name']}** sits well off the cluster — it's {reason}. Unless it's urgent, consider dropping it from today and folding it into a dedicated {o['territory']} run.",
                f"**{o['name']}** is the odd one out here ({reason}). I'd reschedule it for a day you already have {o['territory']} deliveries so the truck isn't making a long solo trip.",
                f"Watch **{o['name']}** — it {reason}. Pulling it off this route would noticeably cut fuel and time; batch it with future {o['territory']} stops instead.",
                f"**{o['name']}** stretches the loop ({reason}). If the order can wait, hold it for a {o['territory']}-focused trip rather than detouring the whole truck for one drop.",
            ]
            line = r.choice(tips)
            if len(same_area) >= 2:
                line += f" You'd be covering {o['territory']} anyway with {len(same_area)} accounts there, so the wait shouldn't cost you a service day."
            parts.append(line)
    else:
        parts.append(r.choice([
            "Good news — every stop is reasonably clustered, so there's no obvious outlier to drop. The sequence is efficient as-is.",
            "Nothing looks off-grid here; the stops are close enough that the route is already tight. No reschedules needed.",
            "All stops fall within a sensible radius of each other — no fuel-wasting detours to flag today.",
        ]))
    clusters = findings["clusters"]
    if clusters:
        biggest_t = max(clusters, key=lambda t: len(clusters[t]))
        parts.append(r.choice([
            f"You've got {len(clusters[biggest_t])} accounts in **{biggest_t}** — keep those grouped back-to-back so the driver clears the area in one sweep.",
            f"**{biggest_t}** has {len(clusters[biggest_t])} stops bunched together; servicing them consecutively is the easy efficiency win here.",
            f"Tip: the **{biggest_t}** cluster ({len(clusters[biggest_t])} stops) is your anchor — build the day around finishing it in one pass.",
        ]))
    parts.append(r.choice([
        "Adjust the picking list in the sidebar and re-run to compare.",
        "Tweak the stops and recalculate if you want to test a leaner version.",
        "Re-optimize after any change to see the new numbers.",
        "Drop or add accounts on the left and run it again to weigh the trade-off.",
    ]))
    return "\n\n".join(parts)


def call_anthropic(api_key, model, prompt):
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": model, "max_tokens": 450, "temperature": 1.0,
            "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def generate_ai_comment(findings, num_deliveries, total_km, total_time_str, ordered_names, api_key, model):
    if api_key:
        outlier_txt = "; ".join(f"{o['name']} ({', '.join(o['reasons'])})" for o in findings["outliers"]) or "none"
        cluster_txt = "; ".join(f"{t}: {len(n)} stops" for t, n in findings["clusters"].items()) or "none"
        flavor = random.choice(["concise", "practical", "candid", "encouraging", "no-nonsense"])
        prompt = (
            "You are a Metro Manila delivery dispatcher AI advising on a single truck's route. "
            f"Write a {flavor} advisory of 90-140 words (plain text, no headers, no bullet symbols). "
            "Flag any stop that is too far or off the grid for an efficient loop and recommend either "
            "removing it from today or rescheduling it to a day with other deliveries in the same area; "
            "also note any area where stops cluster so they can be batched. Vary your wording naturally.\n\n"
            f"Route order (after depot): {', '.join(ordered_names)}\n"
            f"Total: {total_km:.1f} km, ~{total_time_str} driving, {num_deliveries} deliveries.\n"
            f"Flagged far/off-grid stops: {outlier_txt}\n"
            f"Same-area clusters: {cluster_txt}\nDepot: Cord Chemicals, Mandaluyong."
        )
        try:
            text = call_anthropic(api_key, model, prompt)
            if text:
                return text
        except Exception as e:
            return heuristic_comment(findings, num_deliveries, total_km, total_time_str) + \
                f"\n\n_(Live AI unavailable: {e} — showing the built-in analysis.)_"
    return heuristic_comment(findings, num_deliveries, total_km, total_time_str)


def optimize_single_route(df, objective, traffic_factor, provider, server, mapbox_token, solver_seconds):
    coords = [tuple(x) for x in df[["Latitude", "Longitude"]].values.tolist()]
    durations = distances = None
    source = None
    if provider.startswith("Mapbox") and mapbox_token and len(coords) <= 10:
        durations, distances = get_mapbox_matrices(tuple(coords), mapbox_token)
        if durations is not None:
            source = "mapbox"
    if durations is None:
        durations, distances = get_road_matrices(tuple(coords), server)
        if durations is not None:
            source = "osrm"
    if durations is None:
        hav = haversine_matrix(coords)
        distances = hav.tolist()
        durations = (hav / 6.94).tolist()
        source = "fallback"
    factor = 1.0 if source == "mapbox" else traffic_factor
    dur = np.array(durations, dtype=float)
    dist = np.array(distances, dtype=float)
    cost = dur * factor if objective == "time" else dist
    cost = np.nan_to_num(cost, nan=1e9, posinf=1e9)
    matrix = np.round(cost).astype(int).tolist()

    manager = pywrapcp.RoutingIndexManager(len(matrix), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def cost_callback(from_index, to_index):
        return matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_idx = routing.RegisterTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(int(solver_seconds))
    solution = routing.SolveWithParameters(params)
    if not solution:
        return None
    order = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        order.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))
    order.append(manager.IndexToNode(index))
    return {"order": order, "durations": durations, "distances": distances, "source": source, "factor": factor}


def build_route_map(full_seq, current_idx, use_mapbox, token, server, driver_coords=None, center_on_driver=False):
    """Generates the interactive Folium map visualization, centered dynamic payload tracking."""
    # Base viewport centering coordinate hierarchy selector
    if center_on_driver and driver_coords and driver_coords.get("latitude"):
        center_lat = driver_coords["latitude"]
        center_lng = driver_coords["longitude"]
        zoom_val = 15
    else:
        center = full_seq[current_idx]
        center_lat = center["lat"]
        center_lng = center["lng"]
        zoom_val = 13

    if use_mapbox:
        tiles_url = ("https://api.mapbox.com/styles/v1/mapbox/streets-v12/tiles/256/"
                     f"{{z}}/{{x}}/{{y}}?access_token={token}")
        m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_val,
                       tiles=tiles_url, attr="© Mapbox © OpenStreetMap")
    else:
        m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_val)

    depot = full_seq[0]
    folium.Marker([depot["lat"], depot["lng"]], popup=depot["name"],
                  icon=folium.Icon(color="black", icon="home")).add_to(m)

    # Plot signed-in active telemetry hardware pin location
    if driver_coords and driver_coords.get("latitude"):
        folium.Marker(
            [driver_coords["latitude"], driver_coords["longitude"]],
            popup="🎯 Your Signed-in Device Location",
            icon=folium.Icon(color="darkpurple", icon="circle-user", prefix="fa")
        ).add_to(m)

    navigation_steps = []

    for i in range(1, current_idx + 1):
        a, b = full_seq[i - 1], full_seq[i]
        active = (i == current_idx)
        
        if use_mapbox:
            coords, cong, steps_data = get_mapbox_congested_route_with_steps(a["lat"], a["lng"], b["lat"], b["lng"], token)
            if active:
                navigation_steps = steps_data
            if coords:
                draw_congested_path(m, coords, cong, weight=7 if active else 4,
                                    opacity=0.95 if active else 0.55)
            else:
                folium.PolyLine([[a["lat"], a["lng"]], [b["lat"], b["lng"]]], color="#9E9E9E",
                                weight=3, opacity=0.5, dash_array="6,8").add_to(m)
        else:
            pts, steps_data = get_osrm_route_with_steps(a["lat"], a["lng"], b["lat"], b["lng"], server)
            if active:
                navigation_steps = steps_data
            folium.PolyLine(pts, color="#0033CC" if active else "#3366FF",
                            weight=4 if active else 3, opacity=0.65 if active else 0.45,
                            dash_array="10,8").add_to(m)
                            
        last = (i == len(full_seq) - 1)
        if last:
            continue
        if active:
            folium.Marker([b["lat"], b["lng"]], popup=f"CURRENT TARGET:<br>{b['name']}",
                          icon=folium.Icon(color="red", icon="play")).add_to(m)
        else:
            folium.Marker([b["lat"], b["lng"]], popup=f"Visited:<br>{b['name']}",
                          icon=folium.Icon(color="blue", icon="ok")).add_to(m)
                          
    return m, navigation_steps


def render_step_tracker(full_seq, step_key, use_mapbox, token, server, map_height, remarks_map=None, driver_coords=None):
    remarks_map = remarks_map or {}
    last_pos = len(full_seq) - 1
    num_deliveries = last_pos - 1

    if step_key not in st.session_state:
        st.session_state[step_key] = 1
        
    # Initialization flags for view centering operations
    center_flag_key = f"{step_key}_center_on_driver"
    if center_flag_key not in st.session_state:
        st.session_state[center_flag_key] = False

    # Control Interface Buttons
    c_prev, c_center, c_next = st.columns([1, 2, 1])
    with c_prev:
        if st.button("⬅️ Previous Stop", disabled=(st.session_state[step_key] <= 1),
                     use_container_width=True, key=f"{step_key}_prev"):
            st.session_state[step_key] -= 1
            st.session_state[center_flag_key] = False
    with c_center:
        if st.button("🎯 Center Map on My Location", use_container_width=True, key=f"{step_key}_recenter"):
            st.session_state[center_flag_key] = True
    with c_next:
        if st.button("Next Stop ➡️", disabled=(st.session_state[step_key] >= last_pos),
                     use_container_width=True, key=f"{step_key}_next"):
            st.session_state[step_key] += 1
            st.session_state[center_flag_key] = False

    cur = max(1, min(st.session_state[step_key], last_pos))
    st.session_state[step_key] = cur
    dest = full_seq[cur]

    st.markdown("---")
    
    # Generate route shapes and extract step instructions payload
    m, nav_steps = build_route_map(
        full_seq, cur, use_mapbox, token, server, 
        driver_coords=driver_coords, 
        center_on_driver=st.session_state[center_flag_key]
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        if cur == last_pos:
            st.markdown(f"### 🏁 Next Target: Return to Base\n**{dest['name']}**")
        else:
            st.markdown(f"### 🎯 Next Target: Stop {cur} of {num_deliveries}\n**{dest['name']}**")
            rmk = remarks_map.get(dest["name"], "")
            if rmk:
                st.caption(f"*Dispatcher Instructions: {rmk}*")

        # --- GOOGLE MAPS STYLE TURN-BY-TURN DIRECTION GUIDE CARD ---
        st.markdown("#### 🧭 Driving & Navigation Guide")
        if nav_steps:
            # Build scrollable container element for steps metrics tracking
            nav_html = "<div style='max-height: 280px; overflow-y: auto; padding: 10px; border: 1px solid #ddd; border-radius: 6px; background-color: #f9f9f9;'>"
            for idx, step in enumerate(nav_steps):
                dist_str = f"{step['distance']:.0f}m" if step['distance'] < 1000 else f"{step['distance']/1000:.1f}km"
                nav_html += f"""
                <div style='display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; border-bottom: 1px solid #eee; padding-bottom: 6px;'>
                    <span style='font-size: 14px; color: #333;'><b>{idx + 1}.</b> {step['text']}</span>
                    <span style='font-size: 12px; color: #666; font-weight: bold; margin-left: 10px; white-space: nowrap;'>{dist_str}</span>
                </div>
                """
            nav_html += "</div>"
            st.components.v1.html(nav_html, height=295)
        else:
            st.info("No text navigation instructions found for this sector segment.")

        st.markdown("### 📋 Sequence Journey Itinerary")
        st.markdown(f"🚩 **Start:** {full_seq[0]['name']}")
        for step in range(1, len(full_seq)):
            name = full_seq[step]["name"]
            if step == last_pos:
                st.markdown(f"{'✅' if cur >= last_pos else '🏁'} **Return to Base:** {name}")
            elif step < cur:
                st.markdown(f"✅ **Stop {step}:** ~~{name}~~ *(done)*")
            elif step == cur:
                st.markdown(f"🎯 **Stop {step}: {name}**")
            else:
                st.markdown(f"⏳ **Stop {step}:** {name}")
                
    with col2:
        st_folium(m, width=800, height=map_height, key=f"{step_key}_map_{cur}_run_{st.session_state[center_flag_key]}", returned_objects=[])
        if use_mapbox:
            st.caption("Route traffic indicators: 🟢 clear · 🟠 moderate · 🔴 heavy · 🟥 severe · ⚪ no data (Mapbox Engine).")


def depot_node():
    return {"name": DEPOT_NAME, "lat": DEPOT_LAT, "lng": DEPOT_LNG}

# =============================================================================
# 5. SIDEBAR (account) + ROLE ROUTING
# =============================================================================
with st.sidebar:
    st.markdown(f"**Signed in:** {USER['name']}  \n*Role: {USER['role']}*")
    if st.button("Log out", use_container_width=True):
        st.session_state.auth = None
        st.query_params.clear()
        # Clean local storage context parameters completely on logout
        streamlit_js_eval(
            data_string="localStorage.removeItem('cord_user_session');",
            key="clear_local_session"
        )
        st.rerun()
    with st.expander("Change my password"):
        np1 = st.text_input("New password", type="password", key="np1")
        np2 = st.text_input("Confirm", type="password", key="np2")
        if st.button("Update password"):
            if np1 and np1 == np2:
                set_password(USER["username"], np1)
                st.success("Password updated.")
            else:
                st.error("Passwords are empty or don't match.")
    st.divider()


# =============================================================================
# 6. DISPATCHER PAGE (ADMIN DASHBOARD)
# =============================================================================
def dispatcher_page():
    st.title("🗺️ Dispatcher — Build & Assign Routes")

    with st.sidebar:
        st.header("⚙️ Dispatch Setup")
        st.subheader("👷 Assignment")
        driver = st.selectbox("Driver", options=DRIVERS)
        truck = st.selectbox("Truck Name / Plate Number", options=TRUCKS)

        st.subheader("📍 Picking Locations")
        selected_names = st.multiselect("Select accounts to deliver to",
                                         options=ACCOUNTS["Account Name"].tolist())

        st.divider()
        st.subheader("🚚 Optimization")
        objective_label = st.radio("Optimize for", ["Fastest time (recommended)", "Shortest distance"])
        objective = "time" if objective_label.startswith("Fastest") else "distance"

        provider = st.selectbox("Routing data", ["Mapbox (live traffic)", "OSRM (free, no traffic)"])
        mapbox_token = ""
        if provider.startswith("Mapbox"):
            mapbox_token = st.text_input("Mapbox access token", value=DEFAULT_MAPBOX_TOKEN, type="password")
            traffic_factor = 1.0
            st.caption("🚦 Travel times use Mapbox live traffic.")
        else:
            traffic_label = st.select_slider("Traffic conditions (estimate)",
                                             options=list(TRAFFIC_LEVELS.keys()), value="Moderate")
            traffic_factor = TRAFFIC_LEVELS[traffic_label]
        solver_seconds = st.slider("Solver effort (seconds)", 1, 15, 3)

        st.subheader("🗺️ Map")
        map_height = st.slider("Map height (px)", 400, 800, 520, step=20)

        st.subheader("🤖 AI Assistant")
        ai_api_key = st.text_input("Anthropic API key (optional)", type="password")
        with st.expander("Advanced"):
            osrm_server = st.text_input("OSRM server URL", value=OSRM_DEFAULT)
            ai_model = st.text_input("AI model", value="claude-sonnet-4-6")

    if "remarks" not in st.session_state:
        st.session_state.remarks = {}

    st.subheader("📋 Delivery Manifest")
    st.caption(f"Driver: **{driver}** |  Truck: **{truck}** |  Depot: **{DEPOT_NAME}**")
    picked = ACCOUNTS[ACCOUNTS["Account Name"].isin(selected_names)].reset_index(drop=True)

    if picked.empty:
        st.info("👈 Use **Picking Locations** in the sidebar to add accounts.")
    else:
        disp = picked[["Account Name", "Address", "Territory"]].copy()
        disp.insert(0, "No", range(1, len(disp) + 1))
        disp["Remarks"] = [st.session_state.remarks.get(n, "") for n in disp["Account Name"]]
        edited = st.data_editor(disp, hide_index=True, num_rows="fixed", use_container_width=True,
                                disabled=["No", "Account Name", "Address", "Territory"],
                                column_config={"No": st.column_config.NumberColumn("No #", width="small"),
                                               "Remarks": st.column_config.TextColumn("Remarks")},
                                key="manifest_table")
        for _, row in edited.iterrows():
            st.session_state.remarks[row["Account Name"]] = row["Remarks"]

    if st.button("⚡ Calculate Optimal Route", type="primary", disabled=picked.empty):
        depot_row = pd.DataFrame([{"Account Name": DEPOT_NAME, "Latitude": DEPOT_LAT, "Longitude": DEPOT_LNG}])
        stops = picked[["Account Name", "Latitude", "Longitude"]]
        route_df = pd.concat([depot_row, stops]).reset_index(drop=True)
        with st.spinner("Fetching travel times and optimizing..."):
            result = optimize_single_route(route_df, objective, traffic_factor, provider,
                                           osrm_server, mapbox_token, solver_seconds)
        if result:
            factor = result["factor"]
            order = result["order"]
            ordered = [{"name": route_df.iloc[n]["Account Name"],
                        "lat": float(route_df.iloc[n]["Latitude"]),
                        "lng": float(route_df.iloc[n]["Longitude"])} for n in order[1:-1]]
            t_time, t_dist = route_totals(order, result["durations"], result["distances"], factor)
            terr = dict(zip(ACCOUNTS["Account Name"], ACCOUNTS["Territory"]))
            findings = analyze_route(route_df, order, result["distances"], factor, result["durations"], terr)
            st.session_state.disp_route = {
                "ordered": ordered, "driver": driver, "truck": truck,
                "total_km": t_dist / 1000.0, "time_str": fmt_duration(t_time),
                "source": result["source"],
            }
            st.session_state.disp_step = 1
            st.session_state.disp_ai = generate_ai_comment(
                findings, len(ordered), t_dist / 1000.0, fmt_duration(t_time),
                [s["name"] for s in ordered], ai_api_key, ai_model)
        else:
            st.error("No solution found.")

    route = st.session_state.get("disp_route")
    if route:
        st.divider()
        m1, m2, m3 = st.columns(3)
        m1.metric("Deliveries", len(route["ordered"]))
        m2.metric("Total distance", f"{route['total_km']:.1f} km")
        m3.metric("Est. drive time", route["time_str"])
        src_msg = {"mapbox": "🚦 Mapbox live traffic", "osrm": "🛣️ OSRM (no traffic)",
                   "fallback": "⚠️ straight-line estimate"}[route["source"]]
        st.caption(f"Routing source: {src_msg}")

        if st.session_state.get("disp_ai"):
            with st.container(border=True):
                st.markdown("#### 🤖 AI Dispatcher Comment")
                st.markdown(st.session_state.disp_ai)

        full_seq = [depot_node()] + route["ordered"] + [depot_node()]
        use_mapbox = bool(DEFAULT_MAPBOX_TOKEN)
        render_step_tracker(full_seq, "disp_step", use_mapbox, DEFAULT_MAPBOX_TOKEN,
                            OSRM_DEFAULT, map_height, st.session_state.get("remarks", {}))

        st.divider()
        if st.button(f"📌 Assign this route to {route['driver']}", type="primary"):
            stops_payload = [{"name": s["name"], "lat": s["lat"], "lng": s["lng"],
                              "remarks": st.session_state.get("remarks", {}).get(s["name"], ""),
                              "delivered": False, "arrival_time": None, "auto_verified": False} for s in route["ordered"]]
            aid = create_assignment(route["driver"], route["truck"], stops_payload,
                                    route["total_km"], route["time_str"], USER["name"])
            st.success(f"Assigned to {route['driver']} (assignment #{aid}). It now shows on their account.")

    st.divider()
    st.subheader("📑 Live Assignment Monitoring Dashboard")
    rows = list_assignments()
    if not rows:
        st.caption("No assignments yet.")
    for a in rows:
        with st.expander(f"#{a['id']} · {a['run_date']} · {a['driver']} · {a['truck']} · {a['status']}"):
            stops = json.loads(a["stops_json"])
            done = sum(1 for s in stops if s.get("delivered"))
            st.write(f"**Progress:** {done}/{len(stops)} delivered · {a['total_km']:.1f} km · ~{a['time_str']} · Dispatcher: {a['created_by']}")
            
            # Live synchronization tracking breakdown for dispatch dashboard
            breakdown_data = []
            for idx, s in enumerate(stops):
                breakdown_data.append({
                    "Stop #": idx + 1,
                    "Location Name": s["name"],
                    "Status": "✅ Completed" if s.get("delivered") else "⏳ Pending",
                    "Arrival Timestamp": s.get("arrival_time") if s.get("arrival_time") else "—",
                    "Verification Mode": "🤖 Auto-GPS Geofence" if s.get("auto_verified") else ("✍️ Manual" if s.get("delivered") else "—")
                })
            st.table(breakdown_data)
            
            if st.button("Delete Assignment", key=f"del_{a['id']}", type="secondary"):
                delete_assignment(a["id"])
                st.rerun()


# =============================================================================
# 7. DRIVER PAGE WITH AUTO-GPS TRACKING
# =============================================================================
def driver_page():
    st.title(f"🚚 {USER['name']} — My Deliveries")
    
    # --- GPS Location Collection Component ---
    loc_json = streamlit_js_eval(
        data_string="navigator.geolocation.getCurrentPosition(function(pos){return JSON.stringify({latitude: pos.coords.latitude, longitude: pos.coords.longitude})}, function(err){return null;}, {enableHighAccuracy:true});", 
        key="get_location"
    )
    
    driver_coords = None
    if loc_json:
        try:
            driver_coords = json.loads(loc_json)
        except Exception:
            pass

    mine = list_assignments(driver=USER["name"])
    if not mine:
        st.info("No routes assigned to you yet. Your dispatcher will assign one.")
        return

    labels = {f"#{a['id']} · {a['run_date']} · {a['truck']} · {a['status']}": a["id"] for a in mine}
    choice = st.selectbox("Choose an assignment", options=list(labels.keys()))
    aid = labels[choice]

    if st.session_state.get("drv_current_aid") != aid:
        st.session_state.drv_current_aid = aid
        st.session_state.drv_step = 1

    a = get_assignment(aid)
    stops = json.loads(a["stops_json"])

    # LIVE AUTOMATED GPS GEOFENCE CHECK
    db_changed = False
    if driver_coords and driver_coords.get("latitude"):
        st.sidebar.success(f"📍 GPS Active: {driver_coords['latitude']:.5f}, {driver_coords['longitude']:.5f}")
        
        for s in stops:
            if not s.get("delivered"):
                dist_to_stop = single_haversine(
                    driver_coords["latitude"], driver_coords["longitude"],
                    s["lat"], s["lng"]
                )
                # Auto check-in trigger if vehicle enters coordinate radius
                if dist_to_stop <= GEOFENCE_RADIUS_METERS:
                    s["delivered"] = True
                    s["auto_verified"] = True
                    s["arrival_time"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
                    db_changed = True
                    st.toast(f"🤖 Automatically checked into: {s['name']}! Location verified by GPS.", icon="✅")
        
        if db_changed:
            status = "Completed" if all(s["delivered"] for s in stops) else \
                     ("In progress" if any(s["delivered"] for s in stops) else "Assigned")
            update_assignment(aid, stops, status)
            st.rerun()
    else:
        st.sidebar.warning("⚠️ Waiting for mobile phone GPS signal. Ensure location permissions are permitted.")

    m1, m2, m3 = st.columns(3)
    m1.metric("Stops", len(stops))
    m2.metric("Distance", f"{a['total_km']:.1f} km")
    m3.metric("Est. drive time", a["time_str"])
    st.caption(f"Truck: **{a['truck']}** ·  Status: **{a['status']}** ·  Depot: **{DEPOT_NAME}**")

    full_seq = [depot_node()] + [{"name": s["name"], "lat": s["lat"], "lng": s["lng"]} for s in stops] + [depot_node()]
    remarks_map = {s["name"]: s.get("remarks", "") for s in stops}
    use_mapbox = bool(DEFAULT_MAPBOX_TOKEN)
    
    # Pass live telemetry stream markers down to the rendering interface
    render_step_tracker(full_seq, "drv_step", use_mapbox, DEFAULT_MAPBOX_TOKEN,
                        OSRM_DEFAULT, 520, remarks_map, driver_coords=driver_coords)

    st.divider()
    st.subheader("✅ Delivery Checklist")
    new_flags = []
    for i, s in enumerate(stops):
        time_lbl = f" (Arrived: {s['arrival_time']})" if s.get("arrival_time") else ""
        mode_lbl = " [🤖 GPS Verified]" if s.get("auto_verified") else ""
        label = f"{i + 1}. {s['name']}{time_lbl}{mode_lbl}" + (f"  —  _{s['remarks']}_" if s.get("remarks") else "")
        
        new_flags.append(st.checkbox(label, value=s.get("delivered", False), key=f"chk_{aid}_{i}"))
        
    if st.button("💾 Save progress manually", type="primary"):
        for i, (s, flag) in enumerate(zip(stops, new_flags)):
            if flag and not s.get("delivered"):
                s["delivered"] = True
                s["arrival_time"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
                s["auto_verified"] = False
            elif not flag:
                s["delivered"] = False
                s["arrival_time"] = None
                s["auto_verified"] = False
                
        status = "Completed" if all(s["delivered"] for s in stops) else \
                 ("In progress" if any(s["delivered"] for s in stops) else "Assigned")
        update_assignment(aid, stops, status)
        st.success(f"Saved. Status: {status}.")
        st.rerun()


# Route to the correct page by role
if USER["role"] == "dispatcher":
    dispatcher_page()
else:
    driver_page()
