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
    """Core scheduling logic."""
    locations = locations_df['Locations'].unique().tolist()
    cal = calendar.Calendar()
    month_days = [d for d in cal.itermonthdates(target_year, target_month) if d.month == target_month]
    
    time_off_df['StartDate'] = pd.to_datetime(time_off_df['Start Date']).dt.date
    time_off_df['EndDate'] = pd.to_datetime(time_off_df['End Date']).dt.date

    general_holidays = set()
    for _, row in time_off_df[time_off_df['Instructor'].isnull()].iterrows():
        for i in range((row['EndDate'] - row['StartDate']).days + 1):
            general_holidays.add(row['StartDate'] + timedelta(days=i))

    workdays = [d for d in month_days if d.weekday() < 5 and d not in general_holidays]
    if not workdays:
        return pd.DataFrame(), ["No available workdays found for the selected month."]

    location_availability = defaultdict(list)
    instructor_availability = defaultdict(list)
    final_schedule = []
    
    classes_to_schedule = []
    for _, row in class_catalog_df.iterrows():
        try:
            frequency = int(row['Frequency'])
        except (ValueError, TypeError):
            frequency = 0
        for _ in range(frequency):
            classes_to_schedule.append(row['Title'])
    
    random.shuffle(classes_to_schedule)

    warnings = []
    for class_name in classes_to_schedule:
        session_scheduled = False
        try:
            class_details = class_catalog_df[class_catalog_df['Title'] == class_name].iloc[0]
            duration_hours = float(class_details['Duration'])
            default_location = class_details['Default Location']
        except (IndexError, ValueError):
            warnings.append(f"Could not find details or valid duration for class '{class_name}'.")
            continue
            
        qualified_instructors = instructor_roster_df[instructor_roster_df['QualifiedClasses'].str.contains(class_name, na=False)]

        shuffled_workdays = workdays.copy()
        random.shuffle(shuffled_workdays)

        for test_date in shuffled_workdays:
            if session_scheduled: break
            
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
                        session_scheduled = True
                        break 
                if session_scheduled: break
        if not session_scheduled:
            warnings.append(f"Could not find an available slot for '{class_name}'.")

    if not final_schedule:
        return pd.DataFrame(), warnings if warnings else ["Could not generate a schedule with the given constraints."]

    df = pd.DataFrame(final_schedule)
    df['Date_sort'] = pd.to_datetime(df['Date'])
    df['Start Time sort'] = pd.to_datetime(df['Start Time'], format='%I:%M %p').dt.time
    df['Date'] = df['Date_sort'].dt.strftime('%Y-%m-%d')
    df = df.sort_values(by=['Date_sort', 'Start Time sort']).drop(columns=['Start Time sort', 'Date_sort'])
    
    return df, warnings

# --- Streamlit Web App Interface ---
st.set_page_config(page_title="TLC Training Scheduler", page_icon="📅", layout="wide")

st.title("📅 TLC Monthly Training Scheduler")
st.markdown("Upload your current SharePoint lists, select the target month, and instantly generate a conflict-free schedule.")

# Sidebar Configuration
st.sidebar.header("1. Configuration")
target_year = st.sidebar.number_input("Year", min_value=2024, max_value=2050, value=2026)
target_month = st.sidebar.selectbox("Month", range(1, 13), index=5, format_func=lambda x: calendar.month_name[x])

st.sidebar.markdown("---")
st.sidebar.header("2. Upload SharePoint Data")
st.sidebar.markdown("Export these lists as CSV from SharePoint and upload them here.")

class_file = st.sidebar.file_input("Upload Class Catalog.csv", type="csv")
roster_file = st.sidebar.file_input("Upload Instructor Roster.csv", type="csv")
timeoff_file = st.sidebar.file_input("Upload Time Off and Holidays.csv", type="csv")
locations_file = st.sidebar.file_input("Upload Locations.csv", type="csv")

# Main Area Logic
if st.button("🚀 Generate Schedule", type="primary"):
    if not all([class_file, roster_file, timeoff_file, locations_file]):
        st.error("⚠️ Please upload all four CSV files in the sidebar before generating the schedule.")
    else:
        with st.spinner("Calculating optimal schedule..."):
            try:
                df_catalog = pd.read_csv(class_file)
                df_roster = pd.read_csv(roster_file)
                df_timeoff = pd.read_csv(timeoff_file)
                df_locations = pd.read_csv(locations_file)
                
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
            except Exception as e:
                st.error(f"An error occurred while processing the files: {e}")
else:
    st.info("👈 Upload your files in the sidebar and click 'Generate Schedule' to begin.")
