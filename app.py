import streamlit as st
import pandas as pd
from collections import defaultdict
import calendar
from datetime import datetime, timedelta
import random
import os

# --- Helper Functions ---
def check_overlap(start1, end1, start2, end2):
    """Checks if two time intervals overlap."""
    return max(start1, start2) < min(end1, end2)

def generate_training_schedule(class_catalog_df, instructor_roster_df, time_off_df, locations_df, wfh_df, target_year, target_month, is_session_mode):
    """Core scheduling logic with time-interval, WFH, and course-conflict resolution."""
    locations = locations_df['Locations'].unique().tolist()
    cal = calendar.Calendar()
    month_days = [d for d in cal.itermonthdates(target_year, target_month) if d.month == target_month]

    try:
        time_off_df['StartDate'] = pd.to_datetime(time_off_df['Start Date'], errors='coerce').dt.date
        time_off_df['EndDate'] = pd.to_datetime(time_off_df['End Date'], errors='coerce').dt.date
        time_off_df['Start Time'] = time_off_df['Start Time'].astype(str)
        time_off_df['End Time'] = time_off_df['End Time'].astype(str)
        time_off_df.dropna(subset=['StartDate', 'EndDate'], inplace=True)
    except Exception:
        return pd.DataFrame(), ["Error parsing dates in the Time Off & Holidays data. Please ensure format is YYYY-MM-DD."]

    general_holidays = set()
    for _, row in time_off_df[time_off_df['Instructor'].isnull() | (time_off_df['Instructor'] == '')].iterrows():
        for i in range((row['EndDate'] - row['StartDate']).days + 1):
            general_holidays.add(row['StartDate'] + timedelta(days=i))

    if is_session_mode:
        allowed_weekdays =  # Monday to Friday
    else:
        allowed_weekdays =        # Tuesday to Thursday

    workdays = [d for d in month_days if d.weekday() in allowed_weekdays and d not in general_holidays]

    if not workdays:
        return pd.DataFrame(), ["No available workdays found for the selected mode and month."]

    wfh_days_map = {0: 'MONDAY', 1: 'TUESDAY', 2: 'WEDNESDAY', 3: 'THURSDAY', 4: 'FRIDAY'}

    location_availability = defaultdict(list)
    instructor_availability = defaultdict(list)
    instructor_day_classes = defaultdict(lambda: defaultdict(set))
    class_day_tracker = defaultdict(set)
    class_week_tracker = defaultdict(set)
    final_schedule = []

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
            warnings.append(f"Could not find details for class '{class_name}'.")
            continue

        qualified_instructors = instructor_roster_df[instructor_roster_df['QualifiedClasses'].str.contains(class_name, na=False)].copy()
        qualified_instructors = qualified_instructors.sample(frac=1).reset_index(drop=True)

        shuffled_workdays = workdays.copy()
        random.shuffle(shuffled_workdays)

        for test_date in shuffled_workdays:
            if session_scheduled:
                break

            if test_date in class_day_tracker[class_name]:
                continue
            if class_frequency <= 4 and test_date.isocalendar() in class_week_tracker[class_name]:
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

                    # Check LMS / CMS mutual exclusion rule
                    restricted_set = {"LMS", "CMS"}
                    if class_name in restricted_set:
                        already_assigned = instructor_day_classes[instructor_name][test_date]
                        if bool(already_assigned & (restricted_set - {class_name})):
                            continue

                    # WFH Policy Check for Online Classes
                    if str(default_location).lower() == 'online':
                        day_of_week = test_date.weekday()
                        if day_of_week in wfh_days_map:
                            day_name = wfh_days_map[day_of_week]
                            try:
                                instructor_wfh_row = wfh_df[wfh_df.iloc[:, 0] == instructor_name]
                                if not instructor_wfh_row.empty:
                                    wfh_status = instructor_wfh_row.iloc[0][day_name]
                                    if wfh_status != 'WFH':
                                        continue
                            except (KeyError, IndexError):
                                continue

                    # Time-off check
                    is_busy = False
                    if any(check_overlap(start_time, end_time, bs, be) for bs, be in instructor_availability.get(instructor_name, [])):
                        is_busy = True
                        continue

                    instructor_time_off = time_off_df[time_off_df['Instructor'] == instructor_full_name]
                    for _, leave in instructor_time_off.iterrows():
                        if leave['StartDate'] <= test_date <= leave['EndDate']:
                            if pd.isna(leave['Start Time']) or leave['Start Time'] in ['nan', '']:
                                is_busy = True
                                break
                            try:
                                leave_start = datetime.strptime(leave['Start Time'], '%I:%M %p').time()
                                leave_end = datetime.strptime(leave['End Time'], '%I:%M %p').time()
                                leave_start_dt = datetime.combine(test_date, leave_start)
                                leave_end_dt = datetime.combine(test_date, leave_end)
                                if check_overlap(start_time, end_time, leave_start_dt, leave_end_dt):
                                    is_busy = True
                                    break
                            except (ValueError, TypeError):
                                continue

                    if not is_busy:
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
                        instructor_day_classes[instructor_name][test_date].add(class_name)
                        class_day_tracker[class_name].add(test_date)
                        class_week_tracker[class_name].add(test_date.isocalendar())

                        session_scheduled = True
                        break
                if session_scheduled:
                    break
        if not session_scheduled:
            warnings.append(f"Could not find a non-conflicting slot for an instance of '{class_name}'.")

    if not final_schedule:
        return pd.DataFrame(), warnings if warnings else ["Could not generate a schedule."]

    df = pd.DataFrame(final_schedule)
    df['Date_sort'] = pd.to_datetime(df['Date'])
    df['Start Time sort'] = pd.to_datetime(df['Start Time'], format='%I:%M %p').dt.time
    df['Date'] = df['Date_sort'].dt.strftime('%Y-%m-%d')
    df = df.sort_values(by=['Date_sort', 'Start Time sort']).drop(columns=['Start Time sort', 'Date_sort'])

    return df, warnings


# --- Streamlit Web App Interface ---
st.set_page_config(page_title="TLC Training Scheduler", page_icon="📅", layout="wide")
st.title("📅 TLC Monthly Training Scheduler")
st.markdown("Edit your data, select your scheduling mode, then click generate.")

# --- Default Data Definitions ---
if 'catalog_data' not in st.session_state:
    st.session_state.catalog_data = pd.DataFrame({
        "Title": ["CapCentral", "CMS", "TLIS", "Excel", "Word", "Teams", "Making Word Docs Accessible", "Making Adobe PDF Docs Accessible", "Outlook", "Excel Formulas", "Texas Leg Apps", "LMS"],
        "Frequency":,
        "Duration": [1.0, 2.0, 2.0, 2.0, 1.5, 1.0, 1.5, 3.0, 1.5, 2.0, 0.5, 1.5],
        "Default Location": ["SHB 865", "SHB 835", "SHB 835", "JHR G11", "SHB 835", "JHR G11", "SHB 835", "SHB 835", "SHB 865", "JHR G11", "Online", "Online"]
    })

if 'roster_data' not in st.session_state:
    st.session_state.roster_data = pd.DataFrame({
        "Title": ["Jeb", "Joel", "Lisa", "Ryan", "Jamila"],
        "Email Address": ["Jeb.Callan@tlc.texas.gov", "Joel.Corral@tlc.texas.gov", "Lisa.Flores@tlc.texas.gov", "Ryan.Slaymaker@tlc.texas.gov", "Jamila.Shaw@tlc.texas.gov"],
        "QualifiedClasses": ["CapCentral, CMS, TLIS, Excel, Word, Teams, Outlook, Excel Formulas, LMS", "CapCentral, Texas Leg Apps", "CapCentral, TLIS, Word, Excel, Outlook", "Making Word Docs Accessible, Making Adobe PDF Docs Accessible", "TLIS, CMS, Texas Leg Apps, LMS"]
    })

if 'timeoff_data' not in st.session_state:
    st.session_state.timeoff_data = pd.DataFrame({
        "Title": ["Juneteenth (Example)", "Joel - Out (All Day)", "Jeb - Meeting"],
        "Start Date": ["2026-06-19", "2026-06-04", "2026-06-09"],
        "End Date": ["2026-06-19", "2026-06-09", "2026-06-10"],
        "Start Time": ["", "", "10:00 AM"],
        "End Time": ["", "", "11:00 AM"],
        "Instructor": ["", "Joel.Corral@tlc.texas.gov", "Jeb.Callan@tlc.texas.gov"]
    })

if 'locations_data' not in st.session_state:
    st.session_state.locations_data = pd.DataFrame({"Locations": ["SHB 835", "SHB 865", "JHR G10", "JHR G11", "Online"]})

if 'wfh_data' not in st.session_state:
    st.session_state.wfh_data = pd.DataFrame({
        "Instructor": ["Jamila", "Jeb", "Joel", "Lisa", "Ryan"],
        "MONDAY": ["Office", "WFH", "WFH", "Office", "WFH"],
        "TUESDAY": ["Office", "Office", "Office", "Office", "Office"],
        "WEDNESDAY": ["Office", "Office", "Office", "Office", "WFH"],
        "THURSDAY": ["WFH", "Office", "WFH", "WFH", "Office"],
        "FRIDAY": ["WFH", "WFH", "Office", "WFH", "Office"]
    })

# --- UI Layout ---
colA, colB = st.columns(2)
with colA:
    st.subheader("📚 Class Catalog")
    df_catalog = st.data_editor(st.session_state.catalog_data, num_rows="dynamic", use_container_width=True)

    st.subheader("🌴 Time Off & Holidays")
    st.markdown("Add specific times for partial-day conflicts. Leave times blank for all-day events.")
    df_timeoff = st.data_editor(st.session_state.timeoff_data, num_rows="dynamic", use_container_width=True, column_config={"Instructor": st.column_config.TextColumn("Instructor (Email)")})

    st.subheader("🏠 Work From Home Schedule")
    df_wfh = st.data_editor(st.session_state.wfh_data, num_rows="dynamic", use_container_width=True)

with colB:
    st.subheader("👥 Instructor Roster")
    df_roster = st.data_editor(st.session_state.roster_data, num_rows="dynamic", use_container_width=True)

    st.subheader("🏢 Locations")
    df_locations = st.data_editor(st.session_state.locations_data, num_rows="dynamic", use_container_width=True)

# --- Sidebar Controls ---
st.sidebar.header("🗓️ Scheduling Controls")
session_mode = st.sidebar.toggle("Session Mode (Mon-Fri)", value=True, help="ON = Session (Mon-Fri). OFF = Interim (Tue-Thu).")
target_year = st.sidebar.number_input("Target Year", min_value=2024, max_value=2050, value=2026)
target_month = st.sidebar.selectbox("Target Month", range(1, 13), index=5, format_func=lambda x: calendar.month_name[x])
generate_btn = st.sidebar.button("🚀 Generate Schedule", type="primary", use_container_width=True)

if generate_btn:
    with st.spinner("Calculating optimal schedule..."):
        st.session_state.catalog_data = df_catalog
        st.session_state.roster_data = df_roster
        st.session_state.timeoff_data = df_timeoff
        st.session_state.locations_data = df_locations
        st.session_state.wfh_data = df_wfh

        schedule_df, warnings = generate_training_schedule(
            df_catalog, df_roster, df_timeoff, df_locations, df_wfh, target_year, target_month, session_mode
        )

        st.subheader(f"Generated Schedule for {calendar.month_name[target_month]} {target_year}")
        if not schedule_df.empty:
            mode_text = "Session Mode (Mon-Fri)" if session_mode else "Interim Mode (Tues-Thur)"
            st.success(f"✅ Schedule successfully generated in **{mode_text}**!")
            for w in warnings:
                st.warning(w)
            st.dataframe(schedule_df, use_container_width=True, hide_index=True)
            st.download_button(
                "📥 Download as CSV",
                schedule_df.to_csv(index=False).encode('utf-8'),
                f"Training_Schedule_{target_year}_{target_month}.csv",
                "text/csv"
            )
        else:
            for w in warnings:
                st.error(w)
