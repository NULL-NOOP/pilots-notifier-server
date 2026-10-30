"""Pure flight profile and phase calculations."""


def analyze_flight_profile(trk: list[float], total_sec: float) -> tuple[dict, list[int]]:
    """Find climb/cruise/descent bounds and generate the five segment percentages."""
    points = []
    for index in range(0, len(trk), 4):
        if index + 3 < len(trk):
            points.append((trk[index], trk[index + 3]))

    if not points or total_sec <= 60.0:
        return {
            "t_takeoff": 30.0,
            "t_cruise_start": 30.0 + (total_sec - 60.0) * 0.25,
            "t_cruise_end": 30.0 + (total_sec - 60.0) * 0.75,
            "t_landed": max(30.0, total_sec - 30.0),
        }, [5, 22, 46, 22, 5]

    min_altitude = min(point[1] for point in points)
    max_altitude = max(point[1] for point in points)
    altitude_range = max_altitude - min_altitude
    cruise_threshold = min_altitude + (0.95 * altitude_range) if altitude_range > 50 else min_altitude

    cruise_start = next((point[0] for point in points if point[1] >= cruise_threshold), 30.0)
    cruise_end = next(
        (point[0] for point in reversed(points) if point[1] >= cruise_threshold),
        total_sec - 30.0,
    )
    cruise_start = max(30.0, min(cruise_start, total_sec - 30.0))
    cruise_end = max(cruise_start, min(cruise_end, total_sec - 30.0))

    ascent_duration = cruise_start - 30.0
    cruise_duration = cruise_end - cruise_start
    descent_duration = (total_sec - 30.0) - cruise_end
    middle_duration = max(1.0, total_sec - 60.0)

    ascent_pct = max(5.0, 90.0 * (ascent_duration / middle_duration))
    cruise_pct = max(10.0, 90.0 * (cruise_duration / middle_duration))
    descent_pct = max(5.0, 90.0 * (descent_duration / middle_duration))
    scale = 90.0 / (ascent_pct + cruise_pct + descent_pct)

    ascent_segment = max(5, int(round(ascent_pct * scale)))
    cruise_segment = max(10, int(round(cruise_pct * scale)))
    descent_segment = max(5, 90 - (ascent_segment + cruise_segment))
    bounds = {
        "t_takeoff": 30.0,
        "t_cruise_start": cruise_start,
        "t_cruise_end": cruise_end,
        "t_landed": max(30.0, total_sec - 30.0),
    }
    return bounds, [5, ascent_segment, cruise_segment, descent_segment, 5]


def get_stage_from_bounds(elapsed_sec: float, total_sec: float, bounds: dict) -> tuple[int, str]:
    """Map elapsed flight time to stages 1 through 5."""
    if elapsed_sec <= bounds["t_takeoff"]:
        return 1, "Takeoff"
    if elapsed_sec <= bounds["t_cruise_start"]:
        return 2, "Ascent"
    if elapsed_sec <= bounds["t_cruise_end"]:
        return 3, "Cruise"
    if elapsed_sec < bounds["t_landed"]:
        return 4, "Descent"
    return 5, "Landed"
