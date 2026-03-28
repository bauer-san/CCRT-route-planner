import streamlit as st
import googlemaps
import pandas as pd
import requests
import urllib.parse
import io
import datetime
from fpdf import FPDF
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# --- INITIALIZATION ---
# Assumes GOOGLE_MAPS_API_KEY is in your Streamlit Secrets
gmaps = googlemaps.Client(key=st.secrets.GOOGLE_MAPS_API_KEY)

CCRTHQ_ADDRESS = "49 W Huron St, Pontiac, MI 48342"

# --- PDF GENERATION LOGIC ---
def generate_pdf_manifest(team_name, stops, map_url):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    
    # Header
    pdf.cell(0, 10, f"Route Manifest: {team_name}", ln=True, align='C')
    pdf.ln(5)

    # Embed Static Map
    try:
        response = requests.get(map_url)
        img_data = io.BytesIO(response.content)
        pdf.image(img_data, x=10, y=30, w=190) 
        pdf.ln(130) 
    except:
        pdf.cell(0, 10, "(Map image unavailable)", ln=True)

    # Table Header
    pdf.set_font("Arial", "B", 12)
    pdf.cell(20, 10, "Stop", 1)
    pdf.cell(170, 10, "Address", 1, ln=True)
    
    # Table Content
    pdf.set_font("Arial", size=10)
    for i, addr in enumerate(stops):
        label = "START" if i == 0 else "END" if i == len(stops)-1 else str(i)
        pdf.cell(20, 10, label, 1)
        pdf.cell(170, 10, str(addr), 1, ln=True)

    return bytes(pdf.output())

# --- MAP & DATA LOGIC ---
def get_static_map_url(osrm_coords, api_key=st.secrets.GOOGLE_MAPS_API_KEY):
    base_url = "https://maps.googleapis.com/maps/api/staticmap?"
    markers = []
    for i, coord in enumerate(osrm_coords):
        label = "S" if i == 0 else "E" if i == len(osrm_coords)-1 else str(i)
        color = "green" if i == 0 else "red" if i == len(osrm_coords)-1 else "blue"
        markers.append(f"markers=color:{color}|label:{label}|{coord[1]},{coord[0]}")
    
    path_points = "|".join([f"{c[1]},{c[0]}" for c in osrm_coords])
    path = f"path=color:0x0000ff|weight:5|{path_points}"
    params = {"size": "600x400", "maptype": "roadmap", "key": api_key}
    return base_url + urllib.parse.urlencode(params) + "&" + "&".join(markers) + "&" + path

def create_data_model(addresses, num_vehicles, gmaps_client, depot_index=0):
    data = {'addresses': addresses, 'num_vehicles': num_vehicles, 'depot': depot_index}
    addr_to_coords = {}
    coords_list = []

    st.info("Geocoding addresses...")
    for addr in addresses:
        res = gmaps_client.geocode(addr)
        if res:
            loc = res[0]['geometry']['location']
            coords_list.append(f"{loc['lng']},{loc['lat']}")
            addr_to_coords[addr] = [loc['lng'], loc['lat']]
        else:
            st.error(f"Geocode failed: {addr}")
            return None

    osrm_url = f"http://router.project-osrm.org/table/v1/driving/{';'.join(coords_list)}"
    try:
        response = requests.get(osrm_url, params={"annotations": "duration"}).json()
        if response.get('code') == 'Ok':
            data['distance_matrix'] = response['durations']
            data['addr_to_coords'] = addr_to_coords
            return data
    except Exception as e:
        st.error(f"OSRM Error: {e}")
    return None

def solve_routing(data, service_time_mins=10):
    service_time_seconds = service_time_mins * 60
    manager = pywrapcp.RoutingIndexManager(len(data['distance_matrix']), data['num_vehicles'], data['depot'])
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node, to_node = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        driving = data['distance_matrix'][from_node][to_node]
        return int(driving + service_time_seconds) if from_node != data['depot'] else int(driving)

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    routing.AddDimension(transit_idx, 0, 43200, True, 'Time')
    routing.GetDimensionOrDie('Time').SetGlobalSpanCostCoefficient(100)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 10
    return routing, manager, routing.SolveWithParameters(search_params)

# --- UI & APP ---
st.set_page_config(page_title="CCRT Route Planner", layout="wide")

st.logo("CCRT_logo.png", size="large", link="https://www.ccrt.org/")

st.title("🚚 CCRT Route Optimizer - OSRM")

with st.expander("📖 How to use the CCRT Route Planner", expanded=False):
    st.markdown("""
    ### 🚀 Getting Started
    1. **Prepare Your File**: Upload a CSV or Excel file with a column titled **'Address'**.
    2. **Configure Teams**: Select how many delivery teams are working today in the sidebar.
    3. **Optimize**: Click **'Optimize Routes'**. The app calculates the fastest paths and balances work between teams.
    4. **Dispatch**: Click the **'Open Navigation'** link to launch Google Maps on a driver's phone.
    
    *Note: All routes start and end at CCRT HQ (49 W Huron St).*
    """)

with st.sidebar:
    st.header("Settings")
    num_teams = st.number_input("Number of Teams", 1, 20, 3)
    st.info("Upload a file to begin.")
    
service_time = 10 #minutes

uploaded_file = st.file_uploader("Upload address file", type=["csv", "xlsx"])

if uploaded_file:
    df = pd.read_csv(uploaded_file) if uploaded_file.name.endswith('.csv') else pd.read_excel(uploaded_file)
    if 'Address' not in df.columns:
        st.error("Missing 'Address' column.")
    else:
        addresses = df['Address'].tolist()
        addresses.insert(0, CCRTHQ_ADDRESS) # Depot

        if st.button("🚀 Optimize Routes"):
            with st.spinner("Calculating..."):
                main_data = create_data_model(addresses, num_teams, gmaps)
                if main_data:
                    model, manager, solution = solve_routing(main_data, service_time)
                    if solution:
                        for vid in range(main_data['num_vehicles']):
                            idx = model.Start(vid)
                            route = []
                            while not model.IsEnd(idx):
                                route.append(main_data['addresses'][manager.IndexToNode(idx)])
                                idx = solution.Value(model.NextVar(idx))
                            route.append(main_data['addresses'][manager.IndexToNode(idx)])

                            # Output
                            st.divider()
                            st.subheader(f"Team {vid + 1}")
                            
                            t_coords = [main_data['addr_to_coords'][addr] for addr in route]
                            m_url = get_static_map_url(t_coords)
                            
                            c1, c2 = st.columns(2)
                            with c1:
                                st.image(m_url)
                                #pdf_data = generate_pdf_manifest(f"Team {vid+1}", route, m_url)
                                #st.download_button("📄 Download PDF", pdf_data, f"Team_{vid+1}.pdf", "application/pdf")
                            with c2:
                                # --- GOOGLE MAPS LINK GENERATION ---
                                origin = urllib.parse.quote_plus(route[0])
                                destination = urllib.parse.quote_plus(route[-1])
                                waypoints = "|".join([urllib.parse.quote_plus(s) for s in route[1:-1]])
                                
                                gmaps_nav_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}&travelmode=driving"
                                
                                st.markdown(f"### [🔗 Open Navigation in Google Maps]({gmaps_nav_url})")
            
                                st.table(route)
                    else:
                        st.error("No solution found.")
