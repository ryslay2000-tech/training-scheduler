import streamlit as st
import pandas as pd
from collections import defaultdict
import calendar
from datetime import datetime, timedelta
import random
import os

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

def generate_training_schedule(class_catalog_df, instructor_roster_df, time_off_df, locations_df, wfh_df, target_year, target_month, is_session_mode):
    """Core scheduling logic with workload balancing, time-interval, bidirectional WFH, and LMS/CMS daily limits."""
    locations = locations_df['Locations'].unique().tolist()
    cal = calendar.Calendar()
    month_days = [d for d in cal.itermonthdates(target_year, target_month) if d.month == target_month]

    try:
        time_off_df = time_off_df.copy()
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
        allowed_weekdays =
    else:
        allowed_weekdays =

    workdays = [d for d in month_days if d.weekday() in allowed_weekdays and d not in general_holidays]

    if not workdays:
        return pd.DataFrame(), ["No available workdays found for the selected mode and month."]

    wfh_days_map = {0: 'MONDAY', 1: 'TUESDAY', 2: 'WEDNESDAY', 3: 'THURSDAY', 4: 'FRIDAY'}

    # Trackers
    location_availability = defaultdict(list)
    instructor_availability = defaultdict(list)
    class_day_tracker = defaultdict(set)
    class_week_tracker = defaultdict(set)
    RESTRICTED_CMS_LMS_CLASSES = {"LMS-S", "LMS-H", "LMS-C", "LMS-C Online", "LMS Online", "CMS Online", "CMS"}
    instructor_restricted_tracker = defaultdict(set)

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

        shuffled_workdays = workdays.copy()
        random.shuffle(shuffled_workdays)

        for strict_balance in [True, False]:
            if session_scheduled:
                break
            for test_date in shuffled_workdays:
                if session_scheduled:
                    break
                if test_date in class_day_tracker[class_name]:
                    continue
                if class_frequency <= 4 and test_date.isocalendar() in class_week_tracker.get(class_name, []):
                    continue

                preferred_start_times = [(9, 30), 
