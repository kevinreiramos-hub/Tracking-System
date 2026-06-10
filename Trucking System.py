import streamlit as st
import pandas as pd
import numpy as np
import folium
import random
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
# 1. APPLICATION SYSTEM CONFIGURATION
# =============================================================================
st.set_page_config(page_title="Hardware Sales Tracker", layout="wide")

# Default coordinates if GPS is initializing (Mandaluyong, Metro Manila)
DEFAULT_LAT, DEFAULT_LNG = 14.5844537, 121.0475689
MAPBOX_TOKEN = "pk.eyJ1Ijoia2V2aW5yZWkyIiwiYSI6ImNtcHl4ejY4ejA1ODYydHB2dDN3NXppcm0ifQ.Xpq-jmcdlyoVLCwDGulA4g"
DB_PATH = "sales_tracking.db"
GEOFENCE_RADIUS_METERS = 50.0  # Arrived radius for field visits

# Pre-seeded Hardware Store Accounts Database
HARDWARE_STORES = pd.DataFrame({
    "Store Name": [
        "De Luxe Electrical & Supply",
        "Firestone Hardware Trading",
        "Fishermen Tools Center",
        "JR Multi Business Resources",
        "Marswin Hardware Marketing",
        "Ace Hardware Megamall",
        "Ace Hardware Alabang",
    ],
    "Address": [
        "162 N Carpio St, Caloocan",
        "415 San Nicolas St, Manila",
        "823 Tetuan St, Santa Cruz, Manila",
        "111 Don Manuel Agregado Street, Quezon City",
        "408 San Nicolas St, Manila",
        "SM Megamall, Mandaluyong City",
        "Festival Mall, Alabang, Muntinlupa",
    ],
    "Latitude": [14.646187, 14.5999652, 14.6003507, 14.6315267, 14.6000217, 14.58631, 14.4189642],
    "Longitude": [120.983901, 120.9702905, 121.001982, 121.001982, 120.9702217, 121.057465, 121.040753],
})

SEED_USERS = [
    ("admin", "Brand Manager", "admin", "admin123"),
    ("sales1", "John Doe (Sales)", "sales", "sales123"),
    ("sales2", "Jane Smith (Sales)", "sales", "sales123"),
]

# =============================================================================
# 2. LOCAL ENGINE STORAGE & DATABASE LAYER
# =============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return salt, digest

def verify_password(password, salt, digest):
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return hmac.compare_digest(check, digest)

def init_database():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, name TEXT, role TEXT, salt TEXT, pwd TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS itineraries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assigned_date TEXT, salesperson TEXT, status TEXT,
        stores_json TEXT, travel_mode TEXT, created_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tracking_logs (
        username TEXT PRIMARY KEY, last_lat REAL, last_lng REAL, last_sync TEXT)""")
    
    cur.execute("SELECT COUNT(*) AS count FROM users")
    if cur.fetchone()["count"] == 0:
        for username, name, role, pw in SEED_USERS:
            salt, digest = hash_password(pw)
            cur.execute("INSERT INTO users VALUES (?,?,?,?,?)", (username, name, role, salt, digest))
    conn.commit()
    conn.close()

def get_user_record(username):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None

def save_itinerary(salesperson, stores, travel_mode):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""INSERT INTO itineraries (assigned_date, salesperson, status, stores_json, travel_mode, created_at)
        VALUES (?,?,?,?,?,?)""",
        (date.today().isoformat(), salesperson, "Pending", json.dumps(stores), travel_mode, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def get_itineraries(salesperson=None):
    conn = get_db_connection()
    if salesperson:
        rows = conn.execute("SELECT * FROM itineraries WHERE salesperson=? ORDER BY id DESC", (salesperson,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM itineraries ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_itinerary(itinerary_id, stores, status):
    conn = get_db_connection()
    conn.execute("UPDATE itineraries SET stores_json=?, status=? WHERE id=?", (json.dumps(stores), status, itinerary_id))
    conn.commit()
    conn.close()

def update_live_location(username, lat, lng):
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO tracking_logs VALUES (?,?,?,?)", (username, lat, lng, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_live_locations():
    conn = get_db_connection()
    rows = conn.execute("SELECT t.*, u.name FROM tracking_logs t JOIN users u ON t.username = u.username").fetchall()
    conn.close()
    return [dict(r) for r in rows]

init_database()

# =============================================================================
# 3. IDENTITY AND AUTHENTICATION GATEWAY
# =============================================================================
if "auth" not in st.session_state:
    st.session_state.auth = None

query_user = st.query_params.get("user")
if query_user and st.session_state.auth is None:
    user_rec = get_user_record(query_user.strip().lower())
    if user_rec:
        st.session_state.auth = {"username": user_rec["username"], "name": user_rec["name"], "role": user_rec["role"]}

if st.session_state.auth is None:
    st.title("🛡️ Hardware Store Field Team Tracking System")
    with st.form("login_gateway"):
        user_input = st.text_input("Account Username")
        pass_input = st.text_input("Password", type="password")
        submit_btn = st.form_submit_button("Log In", type="primary")
    if submit_btn:
        user_rec = get_user_record(user_input.strip().lower())
        if user_rec and verify_password(pass_input, user_rec["salt"], user_rec["pwd"]):
            st.session_state.auth = {"username": user_rec["username"], "name": user_rec["name"], "role": user_rec["role"]}
            st.query_params["user"] = user_rec["username"]
            st.rerun()
        else:
            st.error("Invalid account configuration credentials.")
    st.stop()

USER_SESSION = st.session_state.auth

# Shared Sign-out Utility Inside Sidebar
with st.sidebar:
    st.markdown(f"👤 **Account:** {USER_SESSION['name']}  \n🔑 **Role Profile:** {USER_SESSION['role'].upper()}")
    if st.button("Log Out System", use_container_width=True):
        st.session_state.auth = None
        st.query_params.clear()
        st.rerun()
    st.divider()

# =============================================================================
# 4. NAVIGATION GEOMETRY GRAPHICS OPERATIONS (MAPBOX API)
# =============================================================================
def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return R * (2.0 * np.arcsin(np.sqrt(a)))

def get_mapbox_matrix_itinerary(coords, travel_mode):
    # Maps user selections to Mapbox Profile specifications
    profile = "mapbox/walking" if travel_mode == "Walking" else "mapbox/driving"
    coord_string = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"https://api.mapbox.com/directions-matrix/v1/{profile}/{coord_string}"
    params = {"annotations": "distance,duration", "access_token": MAPBOX_TOKEN}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") == "Ok":
                return res_data.get("durations"), res_data.get("distances")
    except Exception:
        pass
    
    # Fallback Geometry Matrix (Haversine Grid Model)
    n = len(coords)
    fallback_matrix = [[0.0]*n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            fallback_matrix[i][j] = calculate_haversine_distance(coords[i][0], coords[i][1], coords[j][0], coords[j][1])
    return fallback_matrix, fallback_matrix

def fetch_mapbox_maneuver_vectors(start_lat, start_lng, end_lat, end_lng, travel_mode):
    profile = "mapbox/walking" if travel_mode == "Walking" else "mapbox/driving"
    url = f"https://api.mapbox.com/directions/v5/{profile}/{start_lng},{start_lat};{end_lng},{end_lat}"
    params = {"geometries": "geojson", "overview": "full", "steps": "true", "access_token": MAPBOX_TOKEN}
    steps_itinerary = []
    try:
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                geometry_coordinates = [[coord[1], coord[0]] for coord in route["geometry"]["coordinates"]]
                for leg in route.get("legs", []):
                    for step in leg.get("steps", []):
                        instruction_text = step.get("maneuver", {}).get("instruction", "")
                        step_distance = step.get("distance", 0.0)
                        if instruction_text:
                            steps_itinerary.append({"instruction": instruction_text, "meters": step_distance})
                return geometry_coordinates, steps_itinerary
    except Exception:
        pass
    return [[start_lat, start_lng], [end_lat, end_lng]], [{"instruction": "Head toward target storefront drop direction", "meters": 0.0}]

def optimize_itinerary_sequence(start_coord, store_df, travel_mode):
    # Dynamic Node Mapping: Node 0 is ALWAYS the Sales Person's exact live location
    all_coordinates = [start_coord] + list(zip(store_df["Latitude"], store_df["Longitude"]))
    durations, _ = get_mapbox_matrix_itinerary(all_coordinates, travel_mode)
    
    cost_matrix = np.round(np.array(durations)).astype(int).tolist()
    manager = pywrapcp.RoutingIndexManager(len(cost_matrix), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def travel_cost_callback(from_index, to_index):
        return cost_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_callback_index = routing.RegisterTransitCallback(travel_cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    
    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return None
        
    optimized_order = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        optimized_order.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))
    return optimized_order

# =============================================================================
# 5. INTEGRATED INTERACTIVE MAP VIEWPORT COMPONENT WITH OVERLAY BANNER
# =============================================================================
def draw_navigation_map(current_coords, target_store, travel_mode, map_height):
    m = folium.Map(location=[current_coords[0], current_coords[1]], zoom_start=15, tiles="OpenStreetMap", zoom_control=False)
    
    # 1. Plot Sales Person Live Node Position
    folium.Marker([current_coords[0], current_coords[1]], popup="My Location",
                  icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(m)
                  
    # 2. Plot Active Target Hardware Store Target Node Position
    folium.Marker([target_store["lat"], target_store["lng"]], popup=target_store["name"],
                  icon=folium.Icon(color="red", icon="shop", prefix="fa")).add_to(m)
                  
    # 3. Retrieve Live Route Vector Geometry Paths and Turn Directions
    geo_path, maneuver_steps = fetch_mapbox_maneuver_vectors(current_coords[0], current_coords[1], target_store["lat"], target_store["lng"], travel_mode)
    folium.PolyLine(geo_path, color="#1A73E8", weight=6, opacity=0.85).add_to(m)
    
    return m, maneuver_steps

def run_itinerary_map_system(stores_list, current_step_index, travel_mode, map_height, user_coords):
    active_target_store = stores_list[current_step_index]
    
    # Extract live origin node (Salesperson position matrix overrides everything)
    origin_point = [user_coords["latitude"], user_coords["longitude"]] if user_coords else [DEFAULT_LAT, DEFAULT_LNG]
    
    # Compile dynamic graphic interface layouts
    map_object, text_vector_steps = draw_navigation_map(origin_point, active_target_store, travel_mode, map_height)
    
    # --- FLOATING WAZE-LIKE GRAPHICS OVERLAY GENERATION BANNERS ---
    if text_vector_steps:
        immediate_maneuver = text_vector_steps[0]
        formatted_distance = f"{immediate_maneuver['meters']:.0f} m" if immediate_maneuver['meters'] < 1000 else f"{immediate_maneuver['meters']/1000:.1f} km"
        
        waze_banner_html = f"""
        <div style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%); 
                    width: 92%; max-width: 500px; z-index: 9999; pointer-events: none;">
            <div style="background-color: #1976D2; color: white; padding: 12px 16px; 
                        border-radius: 10px; box-shadow: 0px 4px 10px rgba(0,0,0,0.25);
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; pointer-events: auto;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 24px;">🧭</span>
                        <div>
                            <div style="font-size: 14px; font-weight: 600; line-height: 1.2;">{immediate_maneuver['instruction']}</div>
                            <div style="font-size: 11px; opacity: 0.8; margin-top: 1px;">Navigation Vector Track Overlay</div>
                        </div>
                    </div>
                    <div style="font-size: 18px; font-weight: 700; border-left: 1px solid rgba(255,255,255,0.25); padding-left: 10px; white-space: nowrap;">
                        {formatted_distance}
                    </div>
                </div>
            </div>
        </div>
        """
        st.components.v1.html(waze_banner_html, height=0)

    # Render Active System Map Grid Interface Canvas Layout Frame
    col_map_layout, col_directions_layout = st.columns([2, 1])
    
    with col_map_layout:
        st_folium(map_object, width=850, height=map_height, key=f"nav_view_{current_step_index}_{origin_point[0]}_{origin_point[1]}", returned_objects=[])
    
    with col_directions_layout:
        st.markdown(f"### 🎯 Active Destination:\n**{active_target_store['name']}**")
        if active_target_store.get("instruction"):
            st.info(f"📋 **Manager Instructions:** {active_target_store['instruction']}")
            
        st.markdown("#### 📜 Full Turn-by-Turn Matrix Guidance")
        if text_vector_steps:
            steps_html = "<div style='max-height: 240px; overflow-y: auto; padding: 10px; border: 1px solid #E0E0E0; border-radius: 6px; background-color: #FAFAFA;'>"
            for idx, item in enumerate(text_vector_steps):
                d_lbl = f"{item['meters']:.0f}m" if item['meters'] < 1000 else f"{item['meters']/1000:.1f}km"
                steps_html += f"<div style='font-size:12px; margin-bottom:8px; border-bottom:1px solid #EEE; padding-bottom:4px;'><b>{idx+1}.</b> {item['instruction']} ({d_lbl})</div>"
            steps_html += "</div>"
            st.components.v1.html(steps_html, height=260)
        else:
            st.caption("Calculating itinerary layout directions...")

# =============================================================================
# 6. PORTAL VIEW INTERFACE A: BRAND MANAGER (ADMIN DASHBOARD)
# =============================================================================
def run_brand_manager_dashboard():
    st.title("💼 Brand Manager — Command Dashboard Portal")
    
    tab_assign, tab_monitor = st.tabs(["📌 Assign Sales Itinerary Loops", "📡 Live Team Field Tracking"])
    
    with tab_assign:
        st.subheader("Build Efficient Field Loops")
        
        col_setup_1, col_setup_2 = st.columns(2)
        with col_setup_1:
            target_salesperson = st.selectbox("Assign Field Representative Account", options=["sales1", "sales2"])
            transit_modality = st.radio("Assumed Transit Infrastructure Modality Mode", options=["Commute / Driving", "Walking"])
        with col_setup_2:
            assigned_store_selections = st.multiselect("Select Target Hardware Store Accounts to Visit", options=HARDWARE_STORES["Store Name"].tolist())
            
        st.markdown("#### Custom Directives & Instructions Management")
        instruction_dictionary = {}
        for store_selection_node in assigned_store_selections:
            instruction_dictionary[store_selection_node] = st.text_input(f"Directives for Account: {store_selection_node}", placeholder="E.g., Check shelf inventory levels, pitch new product lineup.")
            
        if st.button("⚡ Generate & Optimize Loop Run Manifest", type="primary", disabled=not assigned_store_selections):
            filtered_store_manifest = HARDWARE_STORES[HARDWARE_STORES["Store Name"].isin(assigned_store_selections)].reset_index(drop=True)
            
            # Extract current known position vectors from sales tracking database
            tracking_records = get_all_live_locations()
            user_last_known_node = next((item for item in tracking_records if item["username"] == target_salesperson), None)
            
            if user_last_known_node:
                origin_coordinate_pair = (user_last_known_node["last_lat"], user_last_known_node["last_lng"])
            else:
                origin_coordinate_pair = (DEFAULT_LAT, DEFAULT_LNG)
                
            with st.spinner("Executing traveling salesperson optimization computations via mapping layer vectors..."):
                optimal_index_path = optimize_itinerary_sequence(origin_coordinate_pair, filtered_store_manifest, transit_modality)
                
            if optimal_index_path:
                ordered_stores_payload = []
                for sequence_index in optimal_index_path:
                    if sequence_index == 0:
                        continue  # Skip initial salesperson placeholder position vector row index
                    data_row = filtered_store_manifest.iloc[sequence_index - 1]
                    ordered_stores_payload.append({
                        "name": data_row["Store Name"],
                        "lat": float(data_row["Latitude"]),
                        "lng": float(data_row["Longitude"]),
                        "instruction": instruction_dictionary.get(data_row["Store Name"], ""),
                        "arrival_time": None,
                        "visited": False
                    })
                    
                itinerary_id_node = save_itinerary(target_salesperson, ordered_stores_payload, transit_modality)
                st.success(f"Successfully calculated optimal loop sequence path. Saved under Manifest ID Entry #{itinerary_id_node} assigned to user `{target_salesperson}`.")
            else:
                st.error("Optimization execution matrix error encountered.")
                
    with tab_monitor:
        st.subheader("Field Personnel Positioning Tracking System Monitor")
        
        tracking_data_matrix = get_all_live_locations()
        if not tracking_data_matrix:
            st.info("No field reps are currently online transmitting GPS packets.")
        else:
            monitoring_map = folium.Map(location=[DEFAULT_LAT, DEFAULT_LNG], zoom_start=12, tiles="OpenStreetMap")
            for rep_node in tracking_data_matrix:
                folium.Marker(
                    [rep_node["last_lat"], rep_node["last_lng"]],
                    popup=f"Rep: {rep_node['name']}<br>Synced: {rep_node['last_sync']}",
                    icon=folium.Icon(color="purple", icon="user-tie", prefix="fa")
                ).add_to(monitoring_map)
                
            st_folium(monitoring_map, width=1100, height=500, key="admin_global_monitor_view", returned_objects=[])
            
        st.markdown("### Operational Manifest Log History Matrix")
        all_logs = get_itineraries()
        for log_entry in all_logs:
            with st.expander(f"Manifest Run ID #{log_entry['id']} · Rep Assigned: `{log_entry['salesperson']}` · Status Index: [{log_entry['status']}]"):
                parsed_stores = json.loads(log_entry["stores_json"])
                for idx, item in enumerate(parsed_stores):
                    visit_status_string = f"✅ Visited At Timestamp: {item['arrival_time']}" if item.get("visited") else "⏳ Pending Visit Route State"
                    st.write(f"**Stop {idx+1}:** {item['name']} — {visit_status_string}")

# =============================================================================
# 7. PORTAL VIEW INTERFACE B: SALES PERSON (FIELD DEVICE TERMINAL)
# =============================================================================
def run_sales_person_terminal():
    st.title("📱 Sales Representative Field Navigation Workspace")
    
    # --- HARDWARE LOCATION GPS SIGNAL EXTRACTION CORE ENGINE RUNNERS ---
    gps_packet_json = streamlit_js_eval(
        data_string="""
        (async function() {
            if (!navigator.geolocation) return null;
            return new Promise((resolve) => {
                navigator.geolocation.getCurrentPosition(
                    function(pos) { resolve(JSON.stringify({latitude: pos.coords.latitude, longitude: pos.coords.longitude})); },
                    function(err) { resolve(null); },
                    {enableHighAccuracy: true, timeout: 5000}
                );
            });
        })()
        """,
        key="field_gps_receiver"
    )
    
    user_location_coordinates = None
    if gps_packet_json:
        try:
            user_location_coordinates = json.loads(gps_packet_json)
            update_live_location(USER_SESSION["username"], user_location_coordinates["latitude"], user_location_coordinates["longitude"])
        except Exception:
            pass
            
    my_assigned_itineraries = get_itineraries(salesperson=USER_SESSION["username"])
    if not my_assigned_itineraries:
        st.info("No active storefront itineraries have been compiled for your account profile by the Brand Manager.")
        return
        
    active_itinerary_manifest = my_assigned_itineraries[0]
    manifest_stores_list = json.loads(active_itinerary_manifest["stores_json"])
    
    # Determine the target active stop index logically
    target_stop_index = None
    for index, data_node in enumerate(manifest_stores_list):
        if not data_node.get("visited"):
            target_stop_index = index
            break
            
    if target_stop_index is None:
        st.success("🏁 Outstanding manifest updates complete! All scheduled hardware store visits are resolved.")
        if st.button("Reset Manifest Loop For Testing"):
            for s in manifest_stores_list:
                s["visited"] = False
                s["arrival_time"] = None
            update_itinerary(active_itinerary_manifest["id"], manifest_stores_list, "Pending")
            st.rerun()
        return
        
    # --- AUTOMATIC DATA GEOFENCING LOGIC LAYER ---
    if user_location_coordinates:
        active_store_node = manifest_stores_list[target_stop_index]
        radial_distance_delta = calculate_haversine_distance(
            user_location_coordinates["latitude"], user_location_coordinates["longitude"],
            active_store_node["lat"], active_store_node["lng"]
        )
        
        if radial_distance_delta <= GEOFENCE_RADIUS_METERS:
            manifest_stores_list[target_stop_index]["visited"] = True
            manifest_stores_list[target_stop_index]["arrival_time"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
            
            is_completely_done = all(item.get("visited") for item in manifest_stores_list)
            new_status_string = "Completed" if is_completely_done else "In Progress"
            
            update_itinerary(active_itinerary_manifest["id"], manifest_stores_list, new_status_string)
            st.toast(f"🤖 GPS Auto-Checkin Success at: {active_store_node['name']}!", icon="✅")
            st.rerun()

    st.subheader(f"Current Target Stop Profile ({target_stop_index + 1} of {len(manifest_stores_list)})")
    
    run_itinerary_map_system(
        manifest_stores_list, 
        target_stop_index, 
        active_itinerary_manifest["travel_mode"], 
        500, 
        user_location_coordinates
    )
    
    st.divider()
    st.markdown("### 📋 Itinerary Route Progress State Verification")
    for idx, item in enumerate(manifest_stores_list):
        if item.get("visited"):
            st.markdown(f"✅ **Stop {idx+1}:** ~~{item['name']}~~ (Checked In: `{item['arrival_time']}`)")
        elif idx == target_stop_index:
            st.markdown(f"🎯 **Stop {idx+1}: {item['name']}** *(Active Target Navigation Route View)*")
        else:
            st.markdown(f"⏳ **Stop {idx+1}:** {item['name']}")
            
    if st.button("Manual Override Check-In Target"):
        manifest_stores_list[target_stop_index]["visited"] = True
        manifest_stores_list[target_stop_index]["arrival_time"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        is_completely_done = all(item.get("visited") for item in manifest_stores_list)
        new_status_string = "Completed" if is_completely_done else "In Progress"
        update_itinerary(active_itinerary_manifest["id"], manifest_stores_list, new_status_string)
        st.rerun()

# =============================================================================
# 8. CORE APP EXECUTIVE CONTROL ENVIRONMENT
# =============================================================================
if USER_SESSION["role"] == "admin":
    run_brand_manager_dashboard()
else:
    run_sales_person_terminal()
