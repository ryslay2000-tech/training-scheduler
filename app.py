import streamlit as st
import pandas as pd
from collections import defaultdict
import calendar
from datetime import datetime, timedelta
import random
import os

# --- Page Configuration (Must be first Streamlit command) ---
st.set_page_config(page_title="TLC Training Scheduler", page_icon="📅", layout="wide")

# --- Persistence Setup ---
DATA_DIR = "app_data"
os.makedirs(DATA_DIR, exist_ok=True)

def load_or_init_data(filename: str, default_df: pd.DataFrame) -> pd.DataFrame:
    """Loads a DataFrame from a CSV file, or saves and returns the default if absent."""
    filepath = os.path.join(DATA_DIR, filename)
    if os.path.exists(filepath):
        try:
            return pd.read_csv(filepath)
        except Exception:
            pass
    default_df.to_csv(filepath, index=False)
    return default_df

def save_data(filename: str, df: pd.DataFrame):
    """Saves DataFrame changes to the persistent CSV file."""
    filepath = os.path.join(DATA_DIR, filename)
    df.to_csv(filepath, index=False)

# --- Helper Functions ---
def check_overlap(start1, end1, start2, end2):
    """Checks if two time intervals overlap."""
    return max(start1, start2) < min(end1, end2)

def generate_training_schedule(class_catalog_df, instructor_roster_df, time_off_df, locations_df, wfh_df, target_year, target_month, is_session_mode, max_daily_cap=3):
    """Core scheduling logic with workload balancing, time-interval, bidirectional WFH, LMS/CMS limits, and daily class balancing."""
    locations = locations_df['Locations'].unique().tolist()
    cal = calendar.Calendar()
    month_days = [d for d in cal.itermonthdates(target_year, target_month) if d.month == target_month]

    # --- Safe parsing of the Time Off table ---
    try:
        time_off_clean = time_off_df.dropna(how='all').copy()
        if not time_off_clean.empty:
            time_off_clean['StartDate'] = pd.to_datetime(time_off_clean['Start Date'], errors='coerce').dt.date
            time_off_clean['EndDate'] = pd.to_datetime(time_off_clean['End Date'], errors='coerce').dt.date
            
            # Standardize time strings (strip, uppercase, replace potential comma mistakes)
            time_off_clean['Start Time'] = time_off_clean['Start Time'].astype(str).str.strip().str.upper().str.replace(',', ':')
            time_off_clean['End Time'] = time_off_clean['End Time'].astype(str).str.strip().str.upper().str.replace(',', ':')
            time_off_clean.dropna(subset=['StartDate', 'EndDate'], inplace=True)
        else:
            time_off_clean = pd.DataFrame(columns=['Title', 'Start Date', 'End Date', 'Start Time', 'End Time', 'Instructor', 'StartDate', 'EndDate'])
    except Exception as e:
        return pd.DataFrame(), [f"Error parsing dates in the Time Off & Holidays data: {str(e)}. Please ensure format is YYYY-MM-DD."]

    # --- Separate General Holidays (All-Day Blocks) and General Partial-Day Meetings ---
    general_holidays = set()
    general_partial_conflicts = [] # Will hold (date, start_dt, end_dt) for team-wide partial events

    for _, row in time_off_clean.iterrows():
        # Check if it has no instructor assigned (applies to everyone)
        inst_val = str(row['Instructor']).strip().lower()
        is_general_event = pd.isna(row['Instructor']) or inst_val in ['', 'nan', 'none']
        
        if is_general_event:
            has_times = pd.notna(row['Start Time']) and row['Start Time'] not in ['', 'NAN'] and \
                        pd.notna(row['End Time']) and row['End Time'] not in ['', 'NAN']
            
            if not has_times:
                # All-day general holiday
                if pd.notna(row['StartDate']) and pd.notna(row['EndDate']):
                    for i in range((row['EndDate'] - row['StartDate']).days + 1):
                        general_holidays.add(row['StartDate'] + timedelta(days=i))
            else:
                # Partial-day general conflict (e.g. "Team Meeting")
                for i in range((row['EndDate'] - row['StartDate']).days + 1):
                    current_date = row['StartDate'] + timedelta(days=i)
                    try:
                        # Helper to normalize e.g. "9:00AM" to parseable formats
                        def parse_custom_time(t_str):
                            t_str = t_str.replace(" ", "")
                            if t_str.endswith("AM") or t_str.endswith("PM"):
                                time_part = t_str[:-2]
                                ampm = t_str[-2:]
                                if ":" not in time_part:
                                    time_part = f"{int(time_part)}:00"
                                return datetime.strptime(f"{time_part} {ampm}", "%I:%M %Y" if len(time_part.split(':')[-1]) > 2 else "%I:%M %p").time()
                            raise ValueError
                        
                        s_time = parse_custom_time(row['Start Time'])
                        e_time = parse_custom_time(row['End Time'])
                        
                        s_dt = datetime.combine(current_date, s_time)
                        e_dt = datetime.combine(current_date, e_time)
                        general_partial_conflicts.append((current_date, s_dt, e_dt))
                    except Exception:
                        warnings.append(f"Failed to parse time '{row['Start Time']}' or '{row['End Time']}' for general meeting '{row['Title']}'. Use format '09:00 AM'.")


    if is_session_mode:
        allowed_weekdays = [0, 1, 2, 3, 4]
    else:
        allowed_weekdays = [1, 2, 3]

    workdays = [d for d in month_days if d.weekday() in allowed_weekdays and d not in general_holidays]

    if not workdays:
        return pd.DataFrame(), ["No available workdays found for the selected mode and month."]

    wfh_days_map = {0: 'MONDAY', 1: 'TUESDAY', 2: 'WEDNESDAY', 3: 'THURSDAY', 4: 'FRIDAY'}

    # Trackers
    location_availability = defaultdict(list)
    instructor_availability = defaultdict(list)
    class_day_tracker = defaultdict(set)
    class_week_counter = defaultdict(lambda: defaultdict(int)) # Track occurrences per week
    RESTRICTED_CMS_LMS_CLASSES = {"LMS-S", "LMS-H", "LMS-C", "LMS-C Online","LMS Online", "CMS Online", "CMS"}
    instructor_restricted_tracker = defaultdict(set)
    
    # --- Daily Workload Tracker ---
    daily_class_tracker = {d: 0 for d in workdays}

    # --- Workload Balancing Tracker ---
    all_instructors = instructor_roster_df['Title'].dropna().unique().tolist()
    instructor_load_count = {name: 0 for name in all_instructors}

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

        qualified_instructors_base = instructor_roster_df[instructor_roster_df['QualifiedClasses'].str.contains(class_name, na=False)].copy()
        if qualified_instructors_base.empty:
            warnings.append(f"No qualified instructors found for '{class_name}'.")
            continue

        # Sort candidate days dynamically by current load, adding a random tie-breaker
        candidate_workdays = sorted(workdays, key=lambda d: (daily_class_tracker[d], random.random()))

        for strict_balance in [True, False]:
            if session_scheduled:
                break

        for test_date in candidate_workdays:
                if session_scheduled:
                    break

                # 1. Exact same day check
                if test_date in class_day_tracker[class_name]:
                    continue

                # 2. Consecutive days check (prevents scheduling back-to-back days)
                # If a class is on Tuesday, this prevents scheduling it on Monday or Wednesday
                has_consecutive_conflict = any(
                    abs((test_date - scheduled_date).days) <= 1 
                    for scheduled_date in class_day_tracker[class_name]
                )
                if has_consecutive_conflict:
                    continue

                # 3. Same-week distribution check
                week_key = (test_date.year, test_date.isocalendar().week)
                # Dynamic cap: e.g., freq <= 4 gets max 1/week; freq 8 gets max 2/week
                max_classes_per_week = max(1, (class_frequency + 3) // 4)
                if class_week_counter[class_name][week_key] >= max_classes_per_week:
                    continue

                # 4. Hard Cap & Strict Daily Balance
                if daily_class_tracker[test_date] >= max_daily_cap:
                    continue
                if strict_balance:
                    min_daily_load = min(daily_class_tracker.values()) if daily_class_tracker else 0
                    if daily_class_tracker[test_date] >= (min_daily_load + 1):
                        continue


                preferred_start_times = [(9, 30), (10, 0), (13, 0), (14, 0)]
                random.shuffle(preferred_start_times)

                for start_hour, start_minute in preferred_start_times:
                    if session_scheduled:
                        break

                    start_time = datetime.combine(test_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                    end_time = start_time + timedelta(hours=duration_hours)

                    if any(check_overlap(start_time, end_time, bs, be) for bs, be in location_availability.get(default_location, [])):
                        continue

                    qualified_instructors = qualified_instructors_base.copy()
                    qualified_instructors['current_load'] = qualified_instructors['Title'].map(instructor_load_count).fillna(0)
                    qualified_instructors['random_tie'] = [random.random() for _ in range(len(qualified_instructors))]
                    qualified_instructors = qualified_instructors.sort_values(by=['current_load', 'random_tie']).reset_index(drop=True)

                    for _, instructor in qualified_instructors.iterrows():
                        instructor_name = instructor['Title']
                        instructor_full_name = instructor['Email Address']

                        if strict_balance:
                            min_load = min(instructor_load_count.values()) if instructor_load_count else 0
                            if instructor_load_count.get(instructor_name, 0) >= (min_load + 3):
                                continue

                        if class_name in RESTRICTED_CMS_LMS_CLASSES:
                            if test_date in instructor_restricted_tracker.get(instructor_name, []):
                                continue

                        # --- Bidirectional WFH Policy Check ---
                        day_of_week = test_date.weekday()
                        if day_of_week in wfh_days_map:
                            day_name = wfh_days_map[day_of_week]
                            try:
                                instructor_wfh_row = wfh_df[wfh_df.iloc[:, 0] == instructor_name]
                                if not instructor_wfh_row.empty:
                                    wfh_status = str(instructor_wfh_row.iloc[0][day_name]).strip().upper()
                                    is_online_class = (str(default_location).strip().lower() == 'online')
                                    if wfh_status == 'WFH' and not is_online_class:
                                        continue
                                    if wfh_status != 'WFH' and is_online_class:
                                        continue
                            except (KeyError, IndexError):
                                if str(default_location).strip().lower() == 'online':
                                    continue

                                                # --- Instructor Availability Check with 30-Min Buffer ---
                        candidate_start_buffered = start_time - timedelta(minutes=30)
                        candidate_end_buffered = end_time + timedelta(minutes=30)

                        is_busy = False
                        
                        # 1. Check general/group-wide partial-day conflicts first (e.g., Team Meetings)
                        for conflict_date, s_dt, e_dt in general_partial_conflicts:
                            if test_date == conflict_date:
                                # Apply buffer to the team meeting as well for instructor sanity
                                if check_overlap(candidate_start_buffered, candidate_end_buffered, s_dt - timedelta(minutes=30), e_dt + timedelta(minutes=30)):
                                    is_busy = True
                                    break
                        
                        if is_busy:
                            continue

                        # 2. Check individual busy times
                        if any(check_overlap(candidate_start_buffered, candidate_end_buffered, bs, be) for bs, be in instructor_availability.get(instructor_name, [])):
                            is_busy = True
                            continue

                        # 3. Check individual time off conflicts
                        instructor_time_off = time_off_clean[time_off_clean['Instructor'] == instructor_full_name]
                        for _, leave in instructor_time_off.iterrows():
                            if leave['StartDate'] <= test_date <= leave['EndDate']:
                                if pd.isna(leave['Start Time']) or leave['Start Time'] in ['nan', '']:
                                    is_busy = True
                                    break
                                try:
                                    # Fallback helper parsing for individual leave
                                    leave_start = datetime.strptime(leave['Start Time'].replace(" ", ""), '%I:%M%p').time()
                                    leave_end = datetime.strptime(leave['End Time'].replace(" ", ""), '%I:%M%p').time()
                                    leave_start_dt = datetime.combine(test_date, leave_start)
                                    leave_end_dt = datetime.combine(test_date, leave_end)
                                    if check_overlap(candidate_start_buffered, candidate_end_buffered, leave_start_dt, leave_end_dt):
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
                            instructor_availability[instructor_name].append((candidate_start_buffered, candidate_end_buffered))
                            location_availability[default_location].append((start_time, end_time))
                            class_day_tracker[class_name].add(test_date)
                            class_week_tracker[class_name].add(test_date.isocalendar())
                            instructor_load_count[instructor_name] += 1
                            daily_class_tracker[test_date] += 1

                            if class_name in RESTRICTED_CMS_LMS_CLASSES:
                                instructor_restricted_tracker[instructor_name].add(test_date)

                            session_scheduled = True
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

# --- UI Title ---
st.title("📅 TLC Monthly Training Scheduler")
st.markdown("Edit your data, select your scheduling mode, then click generate.")

# --- Default Fallback Data Definitions ---
default_catalog = pd.DataFrame({
    "Title": ["CMS", "CMS Online", "TLIS", "TLIS Online", "LMS-H", "LMS-S", "LMS-C", "LMS Online", "LMS-C Online", "LDR-S", "LDR-H", "LDR Online", "LASSO", "LASSO Online", "Word ADA", "Word ADA Online", "CapCentral", "CapCentral Online", "Adobe ADA", "Adobe ADA Online"],
    "Frequency": [3,2,3,2,3,3,2,2,1,3,3,2,3,3,3,1,1,1,1,1],
    "Duration": [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 0.5, 0.5, 0.5, 0.5, 0.5, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0],
    "Default Location": ["SHB 835", "Online", "SHB 865", "Online", "JHR G11", "SHB 835", "SHB 865", "Online", "Online", "SHB 865", "JHR G10", "Online", "JHR G11", "Online", "JHR G10", "Online", "SHB 835", "Online", "SHB 835", "Online"]
})

default_roster = pd.DataFrame({
    "Title": ["Jeb", "Joel", "Lisa", "Ryan", "Jamila"],
    "Email Address": ["Jeb.Callan@tlc.texas.gov", "Joel.Corral@tlc.texas.gov", "Lisa.Flores@tlc.texas.gov", "Ryan.Slaymaker@tlc.texas.gov", "Jamila.Shaw@tlc.texas.gov"],
    "QualifiedClasses": ["CMS, CMS Online, TLIS, TLIS Online, LMS-H, LMS-S, LMS-C, LMS Online, LMS-C Online, LDR-S, LDR-H, LDR Online, LASSO, LASSO Online, Word ADA, Word ADA Online, CapCentral, CapCentral Online", "TLIS, TLIS Online, LDR-S, LDR-H, LDR Online, LASSO, LASSO Online, Word ADA, Word ADA Online, CapCentral, CapCentral Online", "TLIS, TLIS Online, LMS-H, LMS-S, LMS-C, LMS Online, LMS-C Online, LDR-S, LDR-H, LDR Online, LASSO, LASSO Online, CapCentral, CapCentral Online", "Adobe ADA, Adobe ADA Online", "CMS, CMS Online, TLIS, TLIS Online, LDR-S, LDR-H, LDR Online, LASSO, LASSO Online, Word ADA, Word ADA Online, CapCentral, CapCentral Online"]
})

default_timeoff = pd.DataFrame({
    "Title": ["Christmas", "Team Meeting", "Team Meeting", "Team Meeting"],
    "Start Date": ["2026-12-21", "2026-12-01", "2026-12-08", "2026-12-15"],
    "End Date": ["2026-12-31", "2026-12-01", "2026-12-08", "2026-12-15"],
    "Start Time": ["", "09:00 AM", "09:00 AM", "09:00 AM"],
    "End Time": ["", "10:30 AM", "10:30 AM", "10:30 AM"],
    "Instructor": ["", "", "", ""]
})

default_locations = pd.DataFrame({"Locations": ["SHB 835", "SHB 865", "JHR G10", "JHR G11", "Online"]})

default_wfh = pd.DataFrame({
    "Instructor": ["Jamila", "Jeb", "Joel", "Lisa", "Ryan"],
    "MONDAY": ["Office", "WFH", "WFH", "Office", "WFH"],
    "TUESDAY": ["Office", "Office", "Office", "Office", "Office"],
    "WEDNESDAY": ["Office", "Office", "Office", "Office", "WFH"],
    "THURSDAY": ["WFH", "Office", "WFH", "WFH", "Office"],
    "FRIDAY": ["WFH", "WFH", "Office", "WFH", "Office"]
})

# --- Persistent Loading into Session State ---
if 'catalog_data' not in st.session_state:
    st.session_state.catalog_data = load_or_init_data("catalog.csv", default_catalog)
if 'roster_data' not in st.session_state:
    st.session_state.roster_data = load_or_init_data("roster.csv", default_roster)
if 'timeoff_data' not in st.session_state:
    st.session_state.timeoff_data = load_or_init_data("timeoff.csv", default_timeoff)
if 'locations_data' not in st.session_state:
    st.session_state.locations_data = load_or_init_data("locations.csv", default_locations)
if 'wfh_data' not in st.session_state:
    st.session_state.wfh_data = load_or_init_data("wfh.csv", default_wfh)

# --- UI Layout & Auto-Saving Editors ---
colA, colB = st.columns(2)

with colA:
    st.subheader("📚 Class Catalog")
    df_catalog = st.data_editor(st.session_state.catalog_data, num_rows="dynamic", use_container_width=True, key="catalog_editor")
    if not df_catalog.equals(st.session_state.catalog_data):
        st.session_state.catalog_data = df_catalog
        save_data("catalog.csv", df_catalog)

    st.subheader("🌴 Time Off & Holidays")
    st.markdown("Add specific times for partial-day conflicts. Leave times blank for all-day events.")
    df_timeoff = st.data_editor(st.session_state.timeoff_data, num_rows="dynamic", use_container_width=True, key="timeoff_editor")
    if not df_timeoff.equals(st.session_state.timeoff_data):
        st.session_state.timeoff_data = df_timeoff
        save_data("timeoff.csv", df_timeoff)

    st.subheader("🏠 Work From Home Schedule")
    df_wfh = st.data_editor(st.session_state.wfh_data, num_rows="dynamic", use_container_width=True, key="wfh_editor")
    if not df_wfh.equals(st.session_state.wfh_data):
        st.session_state.wfh_data = df_wfh
        save_data("wfh.csv", df_wfh)

with colB:
    st.subheader("👥 Instructor Roster")
    df_roster = st.data_editor(st.session_state.roster_data, num_rows="dynamic", use_container_width=True, key="roster_editor")
    if not df_roster.equals(st.session_state.roster_data):
        st.session_state.roster_data = df_roster
        save_data("roster.csv", df_roster)

    st.subheader("🏢 Locations")
    df_locations = st.data_editor(st.session_state.locations_data, num_rows="dynamic", use_container_width=True, key="locations_editor")
    if not df_locations.equals(st.session_state.locations_data):
        st.session_state.locations_data = df_locations
        save_data("locations.csv", df_locations)

# --- Sidebar & Generation ---
st.sidebar.header("🗓️ Scheduling Controls")
session_mode = st.sidebar.toggle("Session Mode (Mon-Fri)", value=True, help="ON = Session (Mon-Fri). OFF = Interim (Tue-Thu).")
target_year = st.sidebar.number_input("Target Year", min_value=2024, max_value=2050, value=2026)
target_month = st.sidebar.selectbox("Target Month", range(1, 13), index=11, format_func=lambda x: calendar.month_name[x])

# Max Daily Class Cap Slider
max_daily_cap = st.sidebar.slider("Max Classes per Day", min_value=1, max_value=6, value=3, help="Enforces a maximum threshold of classes scheduled on any single day.")

generate_btn = st.sidebar.button("🚀 Generate Schedule", type="primary", use_container_width=True)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Reset to Default Tables", use_container_width=True):
    save_data("catalog.csv", default_catalog)
    save_data("roster.csv", default_roster)
    save_data("timeoff.csv", default_timeoff)
    save_data("locations.csv", default_locations)
    save_data("wfh.csv", default_wfh)

    st.session_state.catalog_data = default_catalog.copy()
    st.session_state.roster_data = default_roster.copy()
    st.session_state.timeoff_data = default_timeoff.copy()
    st.session_state.locations_data = default_locations.copy()
    st.session_state.wfh_data = default_wfh.copy()
    st.success("All tables have been reset to their defaults!")
    st.rerun()

if generate_btn:
    with st.spinner("Calculating optimal schedule..."):
        schedule_df, warnings = generate_training_schedule(
            st.session_state.catalog_data,
            st.session_state.roster_data,
            st.session_state.timeoff_data,
            st.session_state.locations_data,
            st.session_state.wfh_data,
            target_year,
            target_month,
            session_mode,
            max_daily_cap=max_daily_cap
        )
        st.subheader(f"Generated Schedule for {calendar.month_name[target_month]} {target_year}")

        if not schedule_df.empty:
            mode_text = "Session Mode (Mon-Fri)" if session_mode else "Interim Mode (Tues-Thur)"
            st.success(f"✅ Schedule successfully generated in **{mode_text}**!")
            for w in warnings:
                st.warning(w)

            st.dataframe(schedule_df, use_container_width=True, hide_index=True)
            st.download_button("📥 Download as CSV", schedule_df.to_csv(index=False).encode('utf-8'), f"Training_Schedule_{target_year}_{target_month}.csv", "text/csv")

            # --- Distribution Summaries ---
            sum_col1, sum_col2 = st.columns(2)
            with sum_col1:
                st.markdown("### ⚖️ Instructor Workload Distribution")
                workload_summary = schedule_df['Instructor'].value_counts().reset_index()
                workload_summary.columns = ['Instructor', 'Classes Scheduled']
                st.dataframe(workload_summary, hide_index=True)

            with sum_col2:
                st.markdown("### 📊 Daily Class Distribution")
                daily_summary = schedule_df['Date'].value_counts().reset_index()
                daily_summary.columns = ['Date', 'Classes Scheduled']
                daily_summary = daily_summary.sort_values(by='Date')
                st.dataframe(daily_summary, hide_index=True)
        else:
            if warnings:
                for w in warnings:
                    st.error(w)
            else:
                st.error("Could not generate a schedule with the current constraints.")
