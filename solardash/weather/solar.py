"""Solar geometry and clear-sky irradiance (no dependencies).

Solar position follows the NOAA / Astronomical Almanac low-precision
algorithm (good to ~0.01°). Clear-sky global horizontal irradiance uses the
Haurwitz model, which is plenty for sizing PV expectations.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def _julian_day(t: datetime) -> float:
    t = t.astimezone(timezone.utc)
    return t.timestamp() / 86400.0 + 2440587.5


def solar_position(lat: float, lon: float, t: datetime) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg) of the sun at ``t`` (aware datetime)."""
    n = _julian_day(t) - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    lst_deg = (gmst * 15.0 + lon) % 360.0
    ha = math.radians((lst_deg - math.degrees(ra) + 540.0) % 360.0 - 180.0)
    phi = math.radians(lat)
    sin_el = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)
    el = math.asin(max(-1.0, min(1.0, sin_el)))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(phi) - math.sin(phi) * math.cos(ha))
    return math.degrees(el), (math.degrees(az) + 360.0) % 360.0


def clear_sky_ghi(elevation_deg: float) -> float:
    """Haurwitz clear-sky GHI in W/m² for a solar elevation angle."""
    if elevation_deg <= 0:
        return 0.0
    cos_z = math.sin(math.radians(elevation_deg))
    return 1098.0 * cos_z * math.exp(-0.059 / cos_z)


def clear_sky_hourly(lat: float, lon: float, start: datetime, hours: int, substeps: int = 6) -> list[tuple[int, float]]:
    """Average clear-sky GHI (W/m²) for each hour from ``start`` (aligned to the hour).

    Returns ``[(hour_start_epoch, ghi_avg), ...]``.
    """
    start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for h in range(hours):
        t0 = start + timedelta(hours=h)
        acc = 0.0
        for k in range(substeps):
            tk = t0 + timedelta(minutes=(k + 0.5) * 60.0 / substeps)
            acc += clear_sky_ghi(solar_position(lat, lon, tk)[0])
        out.append((int(t0.timestamp()), acc / substeps))
    return out


def cloud_transmission(cloud_fraction: float) -> float:
    """Kasten & Czeplak: GHI/GHI_clear = 1 - 0.75 * c^3.4 (c in 0..1)."""
    c = max(0.0, min(1.0, cloud_fraction))
    return 1.0 - 0.75 * c**3.4


def sun_times(lat: float, lon: float, day: datetime) -> tuple[datetime | None, datetime | None]:
    """Approximate sunrise/sunset (UTC) for the local calendar day containing ``day``."""
    # local midnight in UTC terms (solar time offset from longitude)
    local = day.astimezone(timezone.utc) + timedelta(hours=lon / 15.0)
    base = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=lon / 15.0)
    prev = None
    rise = set_ = None
    for m in range(0, 24 * 60, 5):
        t = base + timedelta(minutes=m)
        el = solar_position(lat, lon, t)[0]
        if prev is not None:
            if prev <= 0 < el and rise is None:
                rise = t
            if prev > 0 >= el:
                set_ = t
        prev = el
    return rise, set_
