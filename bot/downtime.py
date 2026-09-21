"""Classify silent probe gaps: server off vs service not running.

Same rule as daily-stats vpn_charts.split_downtime_gap: host uptime at the
end of a gap shorter than the gap means the machine was powered off.
"""

from __future__ import annotations

from datetime import datetime, timedelta

SIGNAL_SERVER_OFF = "server_off"
SIGNAL_SERVICE_DOWN = "service_down"
GAP_FACTOR = 3.0


def split_downtime_gap(
    start: datetime,
    end: datetime,
    *,
    host_uptime_s: float | None,
    step: timedelta,
) -> list[tuple[datetime, datetime, str]]:
    duration = (end - start).total_seconds()
    if duration <= 0:
        return []
    tolerance = max(step.total_seconds() * GAP_FACTOR, 5.0)
    if host_uptime_s is None or host_uptime_s + tolerance >= duration:
        return [(start, end, SIGNAL_SERVICE_DOWN)]
    host_back = end - timedelta(seconds=host_uptime_s)
    if host_back <= start:
        return [(start, end, SIGNAL_SERVICE_DOWN)]
    spans: list[tuple[datetime, datetime, str]] = [(start, host_back, SIGNAL_SERVER_OFF)]
    if end > host_back:
        spans.append((host_back, end, SIGNAL_SERVICE_DOWN))
    return spans


def _gap_bounds(
    left: datetime,
    right: datetime,
    *,
    step: timedelta,
    skip_first_step: bool,
) -> tuple[datetime, datetime] | None:
    if right - left <= step * GAP_FACTOR:
        return None
    start = left + step if skip_first_step else left
    if right - start <= timedelta(0):
        return None
    return start, right


def downtime_ticks(
    heartbeats: list[tuple[datetime, float | None]],
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    interval_seconds: int,
    now_host_uptime_s: float | None,
) -> tuple[int, int]:
    """Return (service_down_ticks, server_off_ticks) for silent gaps."""
    step_sec = max(1, int(interval_seconds))
    step = timedelta(seconds=step_sec)
    samples = [(ts, host_up) for ts, host_up in heartbeats if ts is not None]
    spans: list[tuple[datetime, datetime, str]] = []

    def add(
        left: datetime,
        right: datetime,
        host_up: float | None,
        *,
        skip_first_step: bool,
    ) -> None:
        bounds = _gap_bounds(left, right, step=step, skip_first_step=skip_first_step)
        if bounds is None:
            return
        spans.extend(split_downtime_gap(bounds[0], bounds[1], host_uptime_s=host_up, step=step))

    if not samples:
        if window_start is None or window_end is None:
            return 0, 0
        spans.extend(
            split_downtime_gap(
                window_start,
                window_end,
                host_uptime_s=now_host_uptime_s,
                step=step,
            )
        )
    else:
        first_time, first_up = samples[0]
        if window_start is not None:
            add(window_start, first_time, first_up, skip_first_step=False)
        for (_left_time, _), (right_time, right_up) in zip(samples, samples[1:]):
            add(_left_time, right_time, right_up, skip_first_step=True)
        if window_end is not None:
            add(samples[-1][0], window_end, now_host_uptime_s, skip_first_step=True)

    down_sec = 0.0
    off_sec = 0.0
    for start, end, signal in spans:
        seconds = (end - start).total_seconds()
        if signal == SIGNAL_SERVER_OFF:
            off_sec += seconds
        else:
            down_sec += seconds
    return max(0, int(round(down_sec / step_sec))), max(0, int(round(off_sec / step_sec)))
