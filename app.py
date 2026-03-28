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

# --- CONFIGURATION & UI ---
st.set_page_config(page_title="CCRT Route Planner", layout="wide")

# Custom CSS to handle side-by-side printing
st.markdown("""
    <style>
    @media print {
        [data-testid="stSidebar"], header { display: none !important; }
        .stHorizontalBlock { display: flex !important; flex-direction: row !important; }
    }
    </style>
""", unsafe_allow_html=True)

st.title("🚚 CCRT Route Optimizer")

# Initialize Session State if not already present
if 'optimized_results' not in st.session_state:
    st.session_state.optimized_results = None
if 'main_data' not in st.session_state:
    st.session_state.main_data = None

# --- HELP & INSTRUCTIONS ---
with st.expander("📖 Instructions", expanded=False):
    st.markdown("""
    1. Upload your file. 2. Set the number of teams. 3. Click Optimize. 
    4. View results below and download individual PDFs for each driver.
    """)

# Sidebar
with st.sidebar:
    st.header("Settings")
    num_teams = st.number_input("Number of Teams", 1, 20, 3)
    service_time = st.slider("Minutes per stop", 1, 60, 10)
    
    if st.button("🗑️ Clear Results"):
        st.session_state.optimized_results = None
        st.session_state.main_data = None
        st.rerun()

# File Uploader
uploaded_file = st.file_uploader("Upload address file", type=["csv", "xlsx"])

# --- MAIN APP LOGIC ---
if uploaded_file:
    # Read the file
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    if 'Address' not in df.columns:
        st.error("Missing 'Address' column.")
    else:
        addresses = df['Address'].tolist()
        # Add Depot (CCRT HQ)
        addresses.insert(0, "49 W Huron St, Pontiac, MI 48342") 

        # Step 1: The "Trigger" - This only runs when the button is clicked
        if st.button("🚀 Optimize Routes"):
            with st.spinner("Geocoding and calculating optimal paths..."):
                data = create_data_model(addresses, num_teams, gmaps)
                
                if data:
                    model, manager, solution = solve_routing(data, service_time)
                    
                    if solution:
                        all_teams = {}
                        for vid in range(data['num_vehicles']):
                            idx = model.Start(vid)
                            route = []
                            while not model.IsEnd(idx):
                                node = manager.IndexToNode(idx)
                                route.append(data['addresses'][node])
                                idx = solution.Value(model.NextVar(idx))
                            # Final return to depot
                            route.append(data['addresses'][manager.IndexToNode(idx)])
                            
                            # Get coords for map
                            t_coords = [data['addr_to_coords'][addr] for addr in route]
                            m_url = get_static_map_url(t_coords)
                            
                            all_teams[f"Team {vid+1}"] = {
                                "route": route, 
                                "map_url": m_url
                            }
                        
                        # Save to session state so it persists during PDF downloads
                        st.session_state.optimized_results = all_teams
                        st.session_state.main_data = data
                    else:
                        st.error("No solution found. Try increasing the number of teams.")

# Step 2: The "Renderer" - This runs on every refresh if data exists in session state
if st.session_state.optimized_results:
    st.divider()
    st.header("Optimized Dispatch Manifests")
    
    for team_name, details in st.session_state.optimized_results.items():
        st.subheader(team_name)
        
        # Create the Side-by-Side layout
        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.write("**Route Map**")
            st.image(details['map_url'], use_container_width=True)
            
            # PDF Download Logic
            pdf_bytes = generate_pdf_manifest(
                team_name, 
                details['route'], 
                details['map_url']
            )
            
            st.download_button(
                label=f"📄 Download {team_name} PDF",
                data=pdf_bytes,
                file_name=f"{team_name.replace(' ', '_')}_Manifest.pdf",
                mime="application/pdf",
                key=f"pdf_{team_name}" # Unique key is required for each team
            )
            
        with col2:
            st.write("**Stop List**")
            # Format the list as a clean table for the UI
            route_df = pd.DataFrame({
                "Stop": [f"#{i}" if 0 < i < len(details['route'])-1 else "DEPOT" for i in range(len(details['route']))],
                "Address": details['route']
            })
            st.table(route_df)
        
        st.divider()
