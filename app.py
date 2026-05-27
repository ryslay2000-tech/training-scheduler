import streamlit as st
import pandas as pd
from collections import defaultdict
import calendar
from datetime import datetime, timedelta
import random

# --- Helper Functions ---
def check_overlap(start1, end1, start2, end2):
    """Checks if two time intervals overlap."""
    return max(start1, start2) < min(end1, end2)

def generate_training_schedule(class_catalog_df, instructor_roster_df, time_off_df, locations_df, target_year, target_month):
    """Core scheduling logic with advanced distribution rules."""
    locations = locations_df['Locations'].unique().tolist()
    cal = calendar.Calendar()
    month_days = [d for d in cal.itermonthdates(target_year, target_month) if d.month == target_month]

    try:
        time_off_df['StartDate'] = pd.to_datetime(time_off_df['Start Date'], errors='coerce').dt.date
        time_off_df['EndDate'] = pd.to_datetime(time_off_df['End Date'], errors='coerce').dt.date
        time_off_df.dropna(subset=['StartDate', 'EndDate'], inplace=True)
    except Exception:
        return pd.DataFrame(), ["Error parsing dates in the Time Off & Holidays data. Please ensure the format is YYYY-MM-DD."]

    general_holidays = set()
    for _, row in time_off_df[time_off_df['Instructor'].isnull() | (time_off_df['Instructor'] == '')].iterrows():
        for i in range((row['EndDate'] - row['StartDate']).days + 1):
            general_holidays.add(row['StartDate'] + timedelta(days=i))

    workdays = [d for d in month_days if d.weekday() < 5 and d not in general_holidays]
    if not workdays:
        return pd.DataFrame(), ["No available workdays found for the selected month."]

    # --- Initialize trackers ---
    location_availability = defaultdict(list)
    instructor_availability = defaultdict(list)
    class_day_tracker = defaultdict(set)
    class_week_tracker = defaultdict(set)
    final_schedule = []
    
    # --- Create a more balanced list of classes to schedule ---
    # First, schedule one of each class, then the second, etc.
    class_dict = {}
    for _, row in class_catalog_df.iterrows():
        try:
            frequency = int(row['Frequency'])
        except (ValueError, TypeError):
            frequency = 0
        if frequency > 0:
            class_dict[row['Title']] = frequency

    max_freq = max(class_dict.values()) if class_dict else 0
    classes_to_schedule = []
    for i in range(max_freq):
        round_i = [title for title, freq in class_dict.items() if freq > i]
        random.shuffle(round_i)
        classes_to_schedule.extend(round_i)

    warnings = []
    for class_name in classes_to_schedule:
        session_scheduled = False
        try:
            class_details = class_catalog_df[class_catalog_df['Title'] == class_name].iloc[0]
            duration_hours = float(class_details['Duration'])
            default_location = class_details['Default Location']
            class_frequency = int(class_details['Frequency'])
        except (IndexError, ValueError):
            warnings.append(f"Could not find details or valid duration for class '{class_name}'.")
            continue
            
        qualified_instructors = instructor_roster_df[instructor_roster_df['QualifiedClasses'].str.contains(class_name, na=False)].copy()
        qualified_instructors = qualified_instructors.sample(frac=1).reset_index(drop=True)

        shuffled_workdays = workdays.copy()
        random.shuffle(shuffled_workdays)

        for test_date in shuffled_workdays:
            if session_scheduled: break

            # --- NEW RULE CHECKS ---
            # 1. Check if class is already scheduled on this day
            if test_date in class_day_tracker[class_name]:
                continue
            
            # 2. Check if class is scheduled this week (if frequency <= 4)
            if class_frequency <= 4:
                week_number = test_date.isocalendar()[1]
                if week_number in class_week_tracker[class_name]:
                    continue
            
            preferred_start_times = [(9, 0), (10, 0), (13, 0), (14, 0)]
            random.shuffle(preferred_start_times)

            for start_hour, start_minute in preferred_start_times:
                start_time = datetime.combine(test_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                end_time = start_time + timedelta(hours=duration_hours)

                if any(check_overlap(start_time, end_time, bs, be) for bs, be in location_availability.get(default_location, [])):
                    continue

                for _, instructor in qualified_instructors.iterrows():
                    instructor_name = instructor['Title']
                    instructor_full_name = instructor['Email Address'] 

                    is_on_leave = not time_off_df[
                        (time_off_df['Instructor'] == instructor_full_name) &
                        (time_off_df['StartDate'] <= test_date) &
                        (time_off_df['EndDate'] >= test_date)
                    ].empty
                    if is_on_leave: continue

                    if not any(check_overlap(start_time, end_time, bs, be) for bs, be in instructor_availability.get(instructor_name, [])):
                        # --- Schedule Found! Update all trackers ---
                        final_schedule.append({
                            'Date': test_date,
                            'Start Time': start_time.strftime('%I:%M %p'),
                            'End Time': end_time.strftime('%I:%M %p'),
                            'Class': class_name,
                            'Instructor': instructor_name,
                            'Location': default_location
                        })
                        instructor_availability[instructor_name].append((start_time, end_time))
                        location_availability[default_location].append((start_time, end_time))
                        class_day_tracker[class_name].add(test_date)
                        class_week_tracker[class_name].add(test_date.isocalendar()[1])
                        
                        session_scheduled = True
                        break 
                if session_scheduled: break
        if not session_scheduled:
            warnings.append(f"Could not find a non-conflicting slot for an instance of '{class_name}'.")

    if not final_schedule:
        return pd.DataFrame(), warnings if warnings else ["Could not generate a schedule with the given constraints."]

    df = pd.DataFrame(final_schedule)
    df['Date_sort'] = pd.to_datetime(df['Date'])
    df['Start Time sort'] = pd.to_datetime(df['Start Time'], format='%I:%M %p').dt.time
    df['Date'] = df['Date_sort'].dt.strftime('%Y-%m-%d')
    df = df.sort_values(by=['Date_sort', 'Start Time sort']).drop(columns=['Start Time sort', 'Date_sort'])
    
    return df, warnings

# --- Streamlit Web App Interface (No changes needed below this line) ---
st.set_page_config(page_title="TLC Training Scheduler", page_icon="📅", layout="wide")

st.title("📅 TLC Monthly Training Scheduler")
st.markdown("Edit your class requirements and instructor availability below, then click generate.")

if 'catalog_data' not in st.session_state:
    st.session_state.catalog_data = pd.DataFrame({
        "Title": ["CapCentral", "CMS", "TLIS", "Excel", "Word", "Teams", "Making Word Docs Accessible", "Making Adobe PDF Docs Accessible", "Outlook", "Excel Formulas", "Texas Leg Apps"],
        "Frequency": [2, 2, 2, 1, 1, 1, 2, 2, 1, 1, 1],
        "Duration": [1.0, 2.0, 2.0, 2.0, 1.5, 1.0, 1.5, 3.0, 1.5, 2.0, 0.5],
        "Default Location": ["SHB 865", "SHB 835", "SHB 835", "JHR G11", "SHB 835", "JHR G11", "SHB 835", "SHB 835", "SHB 865", "JHR G11", "SHB 835"]
    })

if 'roster_data' not in st.session_state:
    st.session_state.roster_data = pd.DataFrame({
        "Title": ["Jeb", "Joel", "Lisa", "Ryan", "Jamila"],
        "Email Address": ["Jeb.Callan@tlc.texas.gov", "Joel.Corral@tlc.texas.gov", "Lisa.Flores@tlc.texas.gov", "Ryan.Slaymaker@tlc.texas.gov", "Jamila.Shaw@tlc.texas.gov"],
        "QualifiedClasses": ["CapCentral, CMS, TLIS, Excel, Word, Teams, Outlook, Excel Formulas", "CapCentral, Texas Leg Apps", "CapCentral, TLIS, Word, Excel, Outlook", "Making Word Docs Accessible, Making Adobe PDF Docs Accessible", "TLIS, CMS, Texas Leg Apps"]
    })

if 'timeoff_data' not in st.session_state:
    st.session_state.timeoff_data = pd.DataFrame({
        "Title": ["Juneteenth (Example)", "Joel - Out", "Jamila - Maternity"],
        "Start Date": ["2026-06-19", "2026-06-04", "2026-05-19"],
        "End Date": ["2026-06-19", "2026-06-09", "2026-07-20"],
        "Instructor": ["", "Joel.Corral@tlc.texas.gov", "Jamila.Shaw@tlc.texas.gov"]
    })

if 'locations_data' not in st.session_state:
    st.session_state.locations_data = pd.DataFrame({"Locations": ["SHB 835", "SHB 865", "JHR G10", "JHR G11", "Online"]})

col1, col2 = st.columns(2)
with col1:
    st.subheader("📚 Class Catalog")
    st.markdown("Adjust frequency and duration (hours). Rule: Classes won't repeat in the same week unless Frequency > 4.")
    df_catalog = st.data_editor(st.session_state.catalog_data, num_rows="dynamic", use_container_width=True)
    
    st.subheader("🌴 Time Off & Holidays")
    st.markdown("Format: YYYY-MM-DD. Leave Instructor email blank for agency holidays.")
    df_timeoff = st.data_editor(st.session_state.timeoff_data, num_rows="dynamic", use_container_width=True, column_config={"Instructor": st.column_config.TextColumn("Instructor (Email)")})

with col2:
    st.subheader("👥 Instructor Roster")
    st.markdown("Classes must be separated by commas.")
    df_roster = st.data_editor(st.session_state.roster_data, num_rows="dynamic", use_container_width=True)
    
    st.subheader("🏢 Locations")
    df_locations = st.data_editor(st.session_state.locations_data, num_rows="dynamic", use_container_width=True)

st.markdown("---")

col3, col4, col5 = st.columns([1, 1, 2])
with col3:
    target_year = st.number_input("Target Year", min_value=2024, max_value=2050, value=2026)
with col4:
    target_month = st.selectbox("Target Month", range(1, 13), index=5, format_func=lambda x: calendar.month_name[x])
with col5:
    st.write("") 
    st.write("") 
    generate_btn = st.button("🚀 Generate Schedule", type="primary", use_container_width=True)

if generate_btn:
    with st.spinner("Calculating optimal schedule..."):
        st.session_state.catalog_data = df_catalog
        st.session_state.roster_data = df_roster
        st.session_state.timeoff_data = df_timeoff
        st.session_state.locations_data = df_locations

        schedule_df, warnings = generate_training_schedule(
            df_catalog, df_roster, df_timeoff, df_locations, target_year, target_month
        )
        
        if not schedule_df.empty:
            st.success(f"✅ Schedule successfully generated for {calendar.month_name[target_month]} {target_year}!")
            for w in warnings:
                st.warning(w)
                
            st.dataframe(schedule_df, use_container_width=True, hide_index=True)
            
            csv = schedule_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download Schedule as CSV",
                data=csv,
                file_name=f"Training_Schedule_{target_year}_{target_month}.csv",
                mime="text/csv",
            )
        else:
            for w in warnings:
                st.error(w)
