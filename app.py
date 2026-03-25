import streamlit as st
import googlemaps
import pandas as pd
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import urllib.parse

gmaps = googlemaps.Client(key=st.secrets.GOOGLE_MAPS_API_KEY)

# 1. DATA PREPARATION
def create_data_model(addresses, num_vehicles, depot_index=0):
    data = {}
    data['addresses'] = addresses
    data['num_vehicles'] = num_vehicles
    data['depot'] = depot_index

    num_addresses = len(addresses)
    full_distance_matrix = [[0 for _ in range(num_addresses)] for _ in range(num_addresses)]
    chunk_size = 10 # To respect the 100 elements (origin * destination) limit per API call

    print("Fetching distance matrix from Google Maps API in chunks...")

    try:
        for i in range(0, num_addresses, chunk_size):
            origin_chunk = addresses[i:i + chunk_size]
            for j in range(0, num_addresses, chunk_size):
                destination_chunk = addresses[j:j + chunk_size]

                matrix_result = gmaps.distance_matrix(
                    origins=origin_chunk,
                    destinations=destination_chunk,
                    mode="driving",
                    units="metric"
                )

                if matrix_result['status'] == 'OK':
                    for idx_origin, origin in enumerate(matrix_result['rows']):
                        for idx_dest, element in enumerate(origin['elements']):
                            global_origin_index = i + idx_origin
                            global_destination_index = j + idx_dest

                            if element['status'] == 'OK':
                                # Distance is in meters, store directly
                                full_distance_matrix[global_origin_index][global_destination_index] = element['distance']['value']
                            else:
                                # Placeholder for unreachable or error
                                full_distance_matrix[global_origin_index][global_destination_index] = 999999999 # Using a large number for meters
                else:
                    print(f"Error fetching distance matrix for chunk (origins {i}-{i+len(origin_chunk)-1}, destinations {j}-{j+len(destination_chunk)-1}): {matrix_result['status']}")
                    # If a chunk fails, we might want to fill with a large value or raise an error
                    # For simplicity, filling the failed chunk area with 999999999 for all elements (large number in meters)
                    for idx_origin in range(len(origin_chunk)):
                        for idx_dest in range(len(destination_chunk)):
                            global_origin_index = i + idx_origin
                            global_destination_index = j + idx_dest
                            full_distance_matrix[global_origin_index][global_destination_index] = 999999999

        data['distance_matrix'] = full_distance_matrix
        print("Distance matrix fetched and compiled successfully.")

    except Exception as e:
        print(f"An unexpected error occurred during API calls: {e}")
        # Fallback to a dummy matrix or raise an error
        data['distance_matrix'] = [[0 for _ in addresses] for _ in addresses] # Dummy matrix

    return data

# 2. THE SOLVER (The "Brain")
def solve_routing(data):
    manager = pywrapcp.RoutingIndexManager(len(data['distance_matrix']),
                                           data['num_vehicles'], data['depot'])
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return data['distance_matrix'][manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # Balancing load (max distance per team). Adjust this value based on typical route lengths.
    # The distance is in meters. So 5000000 means 5000 km in meters.
    routing.AddDimension(transit_callback_index, 0, 5000000, True, 'Distance') # Capacity now in meters

    # Add objective: Minimize the maximum distance traveled by any vehicle.
    distance_dimension = routing.GetDimensionOrDie('Distance')
    # Add a global span constraint to the distance dimension (optional, but good practice)
    # distance_dimension.SetGlobalSpanCostCoefficient(100) # This is for minimizing span, not max

    # Minimize the maximum distance traveled by any vehicle.
    # We need to find the maximum cumul value among all vehicles at their respective end nodes.
    # The objective is to minimize this maximum value.
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    # Add a dimension for distance
    distance_dimension = routing.GetDimensionOrDie('Distance')
    distance_dimension.SetGlobalSpanCostCoefficient(100)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)

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

        st.table(stops, hide_index=True)

# --- CONFIGURATION & UI ---
st.set_page_config(page_title="Delivery Route Planner", layout="wide")
st.title("🚚 Team Route Optimizer")

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

                main_data = create_data_model(addresses, num_teams)

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
