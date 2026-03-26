import streamlit as st
import googlemaps
import pandas as pd
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import urllib.parse
import requests

gmaps = googlemaps.Client(key=st.secrets.GOOGLE_MAPS_API_KEY)

# 1. DATA PREPARATION - using OSRM demo server
def create_data_model(addresses, num_vehicles, gmaps_client, depot_index=0):
    data = {}
    data['addresses'] = addresses
    data['num_vehicles'] = num_vehicles
    data['depot'] = depot_index

    # 1. GEOCODING: Get [lon, lat] for every address
    # OSRM requires coordinates, so we use Google once per address to get them.
    coords_list = []
    st.info("Geocoding addresses...")
    for addr in addresses:
        geocode_result = gmaps_client.geocode(addr)
        if geocode_result:
            loc = geocode_result[0]['geometry']['location']
            # IMPORTANT: OSRM uses [longitude, latitude]
            coords_list.append(f"{loc['lng']},{loc['lat']}")
        else:
            st.error(f"Could not find coordinates for: {addr}")
            return None

    # 2. OSRM TABLE CALL: Get the full matrix in one shot
    # We join the coordinates with semicolons as per OSRM requirements
    coords_string = ";".join(coords_list)
    osrm_url = f"http://router.project-osrm.org/table/v1/driving/{coords_string}"
    
    # We request 'duration' for time-based optimization
    params = {"annotations": "distance"} #params = {"annotations": "duration"}
    
    st.info("Fetching travel times from OSRM...")
    try:
        response = requests.get(osrm_url, params=params)
        response_data = response.json()

        if response_data.get('code') == 'Ok':
            # OSRM returns duration in seconds. 
            # This becomes your distance_matrix for OR-Tools.
            data['distance_matrix'] = response_data['distances'] #data['distance_matrix'] = response_data['durations']
            
            
            st.success("OSRM Matrix generated successfully.")
        else:
            st.error(f"OSRM API Error: {response_data.get('code')}")
            return None

    except Exception as e:
        st.error(f"Failed to connect to OSRM: {e}")
        return None
        
    st.write(data) #debug
    return data
    
    # 2. THE SOLVER (The "Brain")
    def solve_routing(data, service_time_mins=10):
        """
        Optimizes for total travel time + service time at each stop.
        service_time_mins: The time spent at each delivery location (default 10 mins).
        """
        # Convert service time to seconds to match OSRM duration units
        service_time_seconds = service_time_mins * 60
    
        manager = pywrapcp.RoutingIndexManager(
            len(data['distance_matrix']),
            data['num_vehicles'], 
            data['depot']
        )
        routing = pywrapcp.RoutingModel(manager)

        # 1. Define the Time Callback
        def time_callback(from_index, to_index):
            """Returns the total time (travel + service) between two nodes."""
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            
            # Get driving duration from OSRM matrix
            driving_duration = data['distance_matrix'][from_node][to_node]
            
            # Add service time if the 'from' node is a delivery stop (not the depot)
            if from_node != data['depot']:
                return int(driving_duration + service_time_seconds)
            
            return int(driving_duration)
    
        transit_callback_index = routing.RegisterTransitCallback(time_callback)
        
        # 2. Set the Cost (Objective) to Time
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    
        # 3. Add Time Dimension for Balancing
        # We set a high upper bound (e.g., 12 hours = 43,200 seconds) 
        # to ensure all routes are feasible.
        max_time_per_team = 43200 
        
        routing.AddDimension(
            transit_callback_index,
            0,                # No waiting time allowed at stops
            max_time_per_team,
            True,             # Start cumulative time at 0
            'Time'
        )
    
        # 4. Global Span Cost: This is what "Balances" the load.
        # It forces the solver to minimize the difference between the longest and shortest route.
        time_dimension = routing.GetDimensionOrDie('Time')
        time_dimension.SetGlobalSpanCostCoefficient(100)
    
        # 5. Search Parameters
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        # Give the solver a few seconds to find a better balanced solution
        search_params.time_limit.seconds = 5 
    
        return routing, manager, routing.SolveWithParameters(search_params)

# 3. THE FORMATTER (The "Output")
def get_readable_output(data, manager, routing, solution):
    results = {}
    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)
        route_for_team = []
        route_distance = 0
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            route_for_team.append(data['addresses'][node_index])
            previous_index = index
            index = solution.Value(routing.NextVar(index))
            route_distance += routing.GetArcCostForVehicle(previous_index, index, vehicle_id)

        # Add the final return to depot
        route_for_team.append(data['addresses'][manager.IndexToNode(index)])
        results[f"Team {vehicle_id + 1}"] = {
            'route': route_for_team,
            'distance': route_distance / 1000 # Distance in the unit returned by callback (meters in this case)
        }
    return results

def print_final_manifests(route_results):
    for team, details in route_results.items():
        st.divider()
        stops = details['route']
        distance = details['distance']
        st.write(f"\n=== {team.upper()} MANIFEST ===")
        st.write(f"Total Distance: {distance} km") # Display in kilometers
        # Generate Google Maps Link
        encoded_waypoints = [urllib.parse.quote_plus(stop) for stop in stops[1:-1]] # URL encode waypoints
        waypoints_str = "|".join(encoded_waypoints)
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={urllib.parse.quote_plus(stops[0])}&destination={urllib.parse.quote_plus(stops[-1])}&waypoints={waypoints_str}"
        st.markdown(f"[🔗 Open in Google Maps]({gmaps_url})")
        st.table(stops)

# --- CONFIGURATION & UI ---
st.set_page_config(page_title="CCRT Route Planner", layout="wide")
st.title("🚚 CCRT Route Optimizer")

# --- HELP & INSTRUCTIONS SECTION ---
with st.expander("📖 How to use the CCRT Route Planner", expanded=False):
    st.markdown("""
    ### 🚀 Getting Started
    Follow these steps to optimize your delivery routes and balance the workload.

    1. **Prepare Your File**
        * Upload a **CSV** or **Excel** file.
        * Ensure your file has a column titled exactly **'Address'**.
        * *Tip: Use full addresses (Street, City, State, Zip) for best results.*

    2. **Configure Settings (Sidebar)**
        * **Number of Teams:** Select how many drivers are working today.

    3. **Optimize**
        * Click **'🚀 Optimize Routes'**. The app will calculate the fastest paths and balance the time between all teams.

    4. **Dispatch**
        * **Digital Link:** Click the 'Open in Google Maps' link to send the route to a driver's phone.
        * **Printable Manifest:** Use the table below each team to verify stops and distances.
    
    ---
    **Note:** The first and last stop is always CCRT HQ.
    """)

st.logo("CCRT_logo.png", size="large", link="https://www.ccrt.org/", icon_image=None)

# Sidebar for Settings
with st.sidebar:
    st.header("Settings")
    num_teams = st.number_input("Number of Teams", min_value=1, max_value=20, value=3)
    st.info("Upload a CSV/Excel file with an 'Address' column.")

# File Uploader
uploaded_file = st.file_uploader("Drag and drop address file here", type=["csv", "xlsx"])

# --- WEB APP MAIN LOGIC ---
if uploaded_file:
    # Read file
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    if 'Address' not in df.columns:
        st.error("Error: Your file must have a column named 'Address'")
    else:
        addresses = df['Address'].tolist()
        addresses.insert(0, "49 W Huron St, Pontiac, MI 48342")
        st.success(f"Loaded {len(addresses)} addresses.")

        if st.button("🚀 Optimize Routes"):
            with st.spinner("Calculating optimal paths..."):

                main_data = create_data_model(addresses, num_teams, gmaps)

                if 'distance_matrix' in main_data and main_data['distance_matrix']:
                    routing_model, routing_manager, solution_obj = solve_routing(main_data)

                    if solution_obj:
                        readable_routes = get_readable_output(main_data, routing_manager, routing_model, solution_obj)
                        print_final_manifests(readable_routes)
                    else:
                        print("No solution found!")
                else:
                    print("Could not create data model due to missing distance matrix. Check API key and network connection.")
                
                # --- DISPLAY RESULTS ---
                #st.divider()
                #cols = st.columns(num_teams)
                
                #for i in range(num_teams):
                    #with cols[i]:
                        #st.subheader(f"Team {i+1}")
                        # Display Digital Link
                        #st.markdown(f"[🔗 Open in Google Maps](http://googleusercontent.com/maps.google.com/5)")
                        
                        # Display Printable Table
                        #st.table(df.head(5)) # Replace with optimized stop list
