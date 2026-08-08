# greaseweazle/tools/probe/markers.py
#
# Writing identifiable marks on a track, and reading them back.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Several probes need to tell one written track from another without caring
# what is on it. A marker is simply a track written at a uniform flux period,
# with a different period for each slot, which avoids dragging in any sector
# codec just to write an identifier: the median interval read back names the
# slot.
#
# Long periods read back short. On the bench drive a track written at 14us
# came back near 12us, and narrowing the span did not cure it -- 10us still
# came back near 8.8us, consistently around 12% low. The likely cause is the
# read channel's gain control manufacturing transitions in the long gaps
# between real ones, which drags the median down.
#
# That is survivable where a probe only asks whether a track holds its OWN
# marker, since a misdecoded foreign marker answers that just as well. What
# would NOT be survivable is a period drifting so far it decodes as nothing,
# which reads as unreadable media. Hence MAX_US, and hence the caution
# against trusting WHICH marker came back.

import statistics
from typing import Optional

from greaseweazle import error
from greaseweazle import usb as USB

# Slot zero's period, and the step between slots.
BASE_US = 4.0
STEP_US = 0.6

# How far a read-back period may drift and still be accepted. Deliberately
# well under half a step: a period landing between two markers belongs to
# neither, and unwritten or damaged media must decode to nothing rather than
# to whichever marker happens to be nearest.
TOLERANCE_US = 0.2

# Keep every marker inside a span drives resolve comfortably.
MAX_US = 12.0


def period_us(slot: int) -> float:
    '''Flux period identifying a slot. Pure.'''
    return BASE_US + slot * STEP_US


def slots_fit(count: int) -> bool:
    '''True if this many slots all get well-resolved periods. Pure.'''
    return count >= 1 and period_us(count - 1) <= MAX_US


def decode(median_us: float, count: int) -> Optional[int]:
    '''Recover a slot number from a measured period. Pure.

    Returns None if the period matches no slot, which is the honest answer
    for unwritten or unreadable media.
    '''
    slot = round((median_us - BASE_US) / STEP_US)
    if not 0 <= slot < count:
        return None
    if abs(median_us - period_us(slot)) > TOLERANCE_US:
        return None
    return slot


def revolution_ticks(usb: USB.Unit) -> float:
    '''Length of one revolution, for sizing a write.'''
    flux = usb.read_track(revs=1)
    error.check(len(flux.index_list) > 0,
                'markers: no index pulse, so a track cannot be timed')
    return flux.index_list[-1]


def write(usb: USB.Unit, slot: int, rev_ticks: float) -> None:
    '''Fill the current track with the marker for this slot.'''
    period = round(period_us(slot) * 1e-6 * usb.sample_freq)
    # Overfill by a margin: the write is cut off at the index pulse, and
    # coming up short would leave the tail of whatever was there before.
    count = int(rev_ticks / period) + 64
    usb.write_track([period] * count, terminate_at_index=True)


def read(usb: USB.Unit, count: int) -> Optional[int]:
    '''Read the current track and name the marker on it, if any.'''
    flux = usb.read_track(revs=1)
    if len(flux.list) < 2:
        return None
    median_us = statistics.median(flux.list) / usb.sample_freq * 1e6
    return decode(median_us, count)

# Local variables:
# python-indent: 4
# End:
