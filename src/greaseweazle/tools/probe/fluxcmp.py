# greaseweazle/tools/probe/fluxcmp.py
#
# Telling one track from another by where its flux falls.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Comparing tracks by matching flux intervals position by position does not
# work: MFM intervals take a few quantised values with one of them dominant,
# so a same-track control shifted along by ONE interval scores as highly as an
# honest reread. Comparing transition COUNTS does not work either, since a
# formatted disk carries the same density on every track whatever the data.
#
# What works is asking where the individual transitions SIT around the
# revolution, and how many of one track's have a partner on the other within a
# fraction of a microsecond. The same track answers about 95%; different
# tracks answer about 56%, which is the rate at which transitions coincide by
# luck rather than any likeness -- so the bar for "different" belongs above
# that floor, not near zero.
#
# Alignment is per segment. Two reads are cued to the index, but a spindle
# holding speed to a hundredth of a percent still drifts tens of microseconds
# across a revolution, far more than transitions are matched to.
#
# This lived in double_step.py, which no longer needs it: that probe now asks
# the sector headers what cylinder they are on, which needs no comparison at
# all. The step-timing probe still uses it, to tell whether a head which has
# just been moved is reading the track it was sent to.

from typing import List, Sequence

from greaseweazle import error
from greaseweazle import usb as USB

# How close two transitions must fall to count as the same one, and how far
# apart the two reads may have drifted within a segment.
MATCH_US = 0.6
DRIFT_US = 40.0

# Segments of the revolution to sample, and how long each is. A handful spread
# around the track is plenty and costs far less than comparing all of it, since
# each segment pays for its own alignment search.
SEGMENT_US = 5000.0
SEGMENTS_SAMPLED = 8

# Fraction of intervals which must differ from the track's median before it
# counts as carrying data. Blank media is a regular grid and scores zero, as
# does a uniformly written test track; real data mixes interval lengths.
VARIETY_MIN = 0.05


def _transitions(usb: USB.Unit, cylinder: int) -> List[float]:
    """Transition times in microseconds from the index pulse, one revolution."""
    usb.seek(cylinder, 0, check_trk0=False)
    # A discarded read after each seek. It was added believing the first
    # read came back short, which turned out to be an artifact of counting a
    # rotational-phase-dependent window: counted per revolution there is no
    # shortfall at all. Kept as cheap insurance for drives which genuinely do
    # need a moment, not for the reason it was written.
    usb.read_track(revs=1)
    flux = usb.read_track(revs=1)
    error.check(len(flux.index_list) >= 2,
                'double-step: need a full revolution between index pulses')

    start, span = flux.index_list[0], flux.index_list[1]
    out: List[float] = []
    total = 0.0
    for interval in flux.list:
        total += interval
        if total < start:
            continue
        if total - start > span:
            break
        out.append((total - start) / flux.sample_freq * 1e6)
    return out


def variety(times: Sequence[float]) -> float:
    """Fraction of intervals differing from the median interval. Pure.

    Near zero for blank media, which reads as a regular grid of synthesised
    flux, and for a uniformly written track. Real data mixes interval
    lengths. A track without variety cannot be told from any other track
    without variety, so it must not be compared at all.
    """
    if len(times) < 3:
        return 0.0
    intervals = [b - a for a, b in zip(times, times[1:])]
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if median <= 0:
        return 0.0
    return sum(1 for i in intervals
               if abs(i - median) > MATCH_US) / len(intervals)


def _best_overlap(here: Sequence[float], there: Sequence[float]) -> int:
    """Most of 'here' that can be paired with 'there' at any offset.

    Two passes: a coarse sweep to find roughly where the segment sits, then a
    fine one around it. Searching the whole range finely would cost twenty
    times as much for the same answer.
    """
    best = 0
    for coarse in range(-int(DRIFT_US), int(DRIFT_US) + 1, 2):
        best = max(best, _overlap(here, there, coarse))
    centre = 0.0
    for coarse in range(-int(DRIFT_US), int(DRIFT_US) + 1, 2):
        if _overlap(here, there, coarse) == best:
            centre = coarse
            break
    fine = centre - 2.0
    while fine <= centre + 2.0:
        best = max(best, _overlap(here, there, fine))
        fine += MATCH_US / 3
    return best


def _overlap(here: Sequence[float], there: Sequence[float],
             offset: float) -> int:
    """How many of 'here' have a partner in 'there', shifted by 'offset'."""
    buckets = set()
    for t in there:
        buckets.add(int((t + offset) / MATCH_US))
    return sum(1 for t in here
               if int(t / MATCH_US) in buckets
               or int(t / MATCH_US) - 1 in buckets
               or int(t / MATCH_US) + 1 in buckets)


def similarity(here: Sequence[float], there: Sequence[float]) -> float:
    """Fraction of transitions shared between two tracks. Pure.

    Each segment is aligned separately: a spindle holding speed to a
    hundredth of a percent still drifts tens of microseconds across a
    revolution, far more than transitions are being matched to.
    """
    if not here or not there:
        return 0.0
    span = min(here[-1], there[-1])
    if span <= 0:
        return 0.0

    matched = counted = 0
    for n in range(SEGMENTS_SAMPLED):
        low = span * n / SEGMENTS_SAMPLED
        high = low + SEGMENT_US
        if high > span:
            break
        mine = [t for t in here if low <= t < high]
        theirs = [t for t in there if low - DRIFT_US <= t < high + DRIFT_US]
        if len(mine) < 20 or not theirs:
            continue
        matched += _best_overlap(mine, theirs)
        counted += len(mine)

    return matched / counted if counted else 0.0

# Local variables:
# python-indent: 4
# End:
