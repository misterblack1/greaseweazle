# greaseweazle/tools/probe/spin_up.py
#
# Probe: how long after the motor starts is the drive usable?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Stop the spindle, wait for it to actually stop, start it again and time the
# index pulses. The intent was to watch the gaps shorten as the disk gathers
# speed and then level off, and to report the moment they levelled off, which
# is what the motor delay in "gw delays" exists to cover.
#
# The bench drive does not permit that. It emits NO index at all until it is
# already at speed: the very first pulse arrives with the revolution period
# already settled, and every reading behaves the same way.
#
#     motor on ..................|-----|-----|-----|-----|     nothing, then
#              ^                 ^                             steady at once
#              t=0               first pulse, already at speed
#
# So there is no acceleration transient to be seen from out here, and what
# gets measured is the time until the drive starts delivering index -- which
# is the more useful figure anyway, being exactly what a host has to wait for
# before the drive is any use. The report says which of the two it got, since
# a drive that does not gate its index this way would show the transient and
# the numbers would then mean something different.
#
# QUANTISATION, and it is large. The first pulse cannot arrive until the index
# hole next passes the sensor, so a reading is late by however far past the
# sensor the hole happened to be when the drive came ready -- anything from
# nothing to a full revolution. Five runs on the bench drive:
#
#     909.8  913.5  914.7  750.4  750.5 ms      spread 164.3 ms
#                                               one revolution is 166.9 ms
#
# Two clusters a revolution apart, not noise. A reading can only ever be LATE,
# never early, so the minimum across several runs is the honest one -- the
# same argument the max-track probe makes about lost steps, arriving from a
# quite different direction. Anything comparing this field between profiles
# needs a tolerance wider than one revolution, or every re-probe will look
# like a change.
#
# Timing is done by polling the index line from the host rather than by
# capturing flux. Polling resolves about a fifth of a millisecond, ample
# against revolutions of a hundred and fifty or more, and it avoids hauling
# several million flux transitions across the wire to learn a few dozen pulse
# times. It also starts the clock at the motor command itself rather than at
# whenever a flux capture happens to begin.
#
# Needs a disk whose index the drive will read: see index_sensor.py, and note
# that on drives which gate reads on READY there is nothing to time without
# one. Hence the dependency, rather than discovering the same absence again
# in this probe's own vocabulary.

import statistics
import time
from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile

name = 'spin-up'
title = 'Motor Spin-Up'
summary = 'How long after motor-on the drive starts delivering index'
depends_on = ('index-sensor',)
destructive = False
needs_motor = True
needs_media = core.MEDIA_ANY
wears_drive = False

# How these fields compare between profiles. The quantisation is the whole
# reason this probe needs a stated tolerance: the first pulse cannot arrive
# until the index hole comes round, so a reading is late by up to a whole
# revolution. Anything tighter than a revolution reports a change on every
# re-probe. 200ms covers the 166.9ms revolution measured here with room to
# spare; a drive turning more slowly would want more.
tolerances = {
    'first_pulse_ms': profile.Tolerance(absolute=200.0),
    'steady_at_ms': profile.Tolerance(absolute=200.0),
    'period_ms': profile.Tolerance(relative=0.02),
    'rpm': profile.Tolerance(relative=0.02),
    'spread_ms': profile.IGNORED,
    'readings_ms': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
OK = 'ok'                        # Timed it.
NO_PULSES = 'no-pulses'          # Motor started but no index ever arrived.
NEVER_STEADY = 'never-steady'    # Still varying when the clock ran out.
TOO_FEW = 'too-few'              # Not enough revolutions to judge.
NEVER_STOPPED = 'never-stopped'  # Spindle would not come to rest first.

# The index line. Not among the signals tools/diag/pinmap.py lists, since that
# covers only what the interactive diagnostic displays, so it is named here
# rather than imported.
INDEX_PIN = 8

# How long to allow for the spindle to stop before starting a run, and how
# long a silence counts as stopped. The silence has to outlast any plausible
# revolution, including the very slow ones a spindle passes through on its
# way down.
STOP_TIMEOUT = 8.0
STOP_QUIET = 1.2

# How long to watch after the motor is commanded on before giving up.
SPIN_UP_TIMEOUT = 6.0

# How many run-ups to time. The reading is quantised by where the index hole
# sits when the drive comes ready, so one run is not enough to trust; these
# are averaged by taking the smallest, never the mean.
RUNS = 3

# Fewest revolutions worth drawing a conclusion from.
MIN_INTERVALS = 6

# The drive's own final speed is taken as the median of this many of the last
# revolutions seen, and the spindle counts as steady once this many
# consecutive revolutions sit within TOLERANCE_PCT of it.
STEADY_WINDOW = 4
STEADY_RUN = 3
TOLERANCE_PCT = 2.0


class Result(NamedTuple):
    status: str
    detail: str
    # Seconds from the motor command to the first index pulse, best of RUNS.
    first_pulse: Optional[float] = None
    # Seconds to the first steady revolution. Equal to first_pulse on a drive
    # which delivers no index until it is already at speed.
    steady_at: Optional[float] = None
    # The steady revolution period the drive settled at, in seconds.
    period: Optional[float] = None
    # True if the speed was still changing when index pulses began, ie. an
    # acceleration transient was actually visible.
    transient_seen: bool = False
    # Time to first pulse from every run, and the spread across them.
    readings: Tuple[float, ...] = ()
    spread: Optional[float] = None

    @property
    def rpm(self) -> Optional[float]:
        if not self.period:
            return None
        return 60.0 / self.period

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'first_pulse_ms': (None if self.first_pulse is None
                               else self.first_pulse * 1e3),
            'steady_at_ms': (None if self.steady_at is None
                             else self.steady_at * 1e3),
            'period_ms': None if self.period is None else self.period * 1e3,
            'rpm': self.rpm,
            'transient_seen': self.transient_seen,
            'readings_ms': [t * 1e3 for t in self.readings],
            'spread_ms': None if self.spread is None else self.spread * 1e3,
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status != OK:
            out({NO_PULSES: '  UNKNOWN - no index pulses after motor-on.',
                 NEVER_STEADY: '  UNSTEADY - the speed never settled.',
                 NEVER_STOPPED: '  UNKNOWN - the spindle would not stop.',
                 }.get(self.status, '  Inconclusive.'))
            out('  (%s)' % self.detail)
            return

        first, period = self.first_pulse, self.period
        assert first is not None and period is not None
        out('  Index from %.0f ms after motor-on.' % (first * 1e3))
        if self.transient_seen:
            steady = self.steady_at
            assert steady is not None
            out('  Speed steady from %.0f ms.' % (steady * 1e3))
        else:
            out('  Already at speed on the first pulse: this drive delivers'
                ' no index')
            out('  until it is up to speed, so no run-up was visible.')
        rpm = self.rpm
        assert rpm is not None
        out('  Settled at %.3f ms per revolution (%.2f rpm as measured).'
            % (period * 1e3, rpm))
        if self.readings:
            out('  Best of %d runs: %s ms'
                % (len(self.readings),
                   ', '.join('%.0f' % (t * 1e3) for t in self.readings)))
        out('  (%s)' % self.detail)


def reconcile(readings: Sequence[float]) -> Tuple[float, float]:
    '''Best estimate of time-to-index, and the spread across runs. Pure.

    A reading is late by however far past the sensor the index hole sat when
    the drive came ready, so it can be up to a revolution too long and can
    never be too short. The smallest reading is therefore the closest to the
    truth, and averaging would bias the answer high by half a revolution.
    '''
    error.check(len(readings) > 0, 'spin-up: no readings')
    return min(readings), max(readings) - min(readings)


def interpret(pulse_times: Sequence[float]) -> Result:
    '''Judge one run-up from its index pulse times. Pure.

    'pulse_times' is the time in seconds from the motor command to each index
    pulse observed.
    '''
    if not pulse_times:
        return Result(
            NO_PULSES,
            'The motor was started but no index pulse ever arrived, so there '
            'was nothing to time. Check a disk is loaded whose index hole '
            'the drive can see.')

    first = pulse_times[0]
    intervals = [b - a for a, b in zip(pulse_times, pulse_times[1:])]

    if len(intervals) < MIN_INTERVALS:
        return Result(
            TOO_FEW,
            'Only %d revolution(s) seen, too few to say when the speed '
            'settled.' % len(intervals),
            first_pulse=first)

    # The drive's own final speed, not a figure any drive family ought to hit.
    steady = statistics.median(intervals[-STEADY_WINDOW:])
    error.check(steady > 0, 'spin-up: steady period is not positive')
    tolerance = steady * TOLERANCE_PCT / 100.0

    for start in range(len(intervals) - STEADY_RUN + 1):
        if all(abs(i - steady) <= tolerance
               for i in intervals[start:start + STEADY_RUN]):
            return Result(
                OK,
                'Timed from the motor command to the first index pulse.',
                first_pulse=first, steady_at=pulse_times[start],
                period=steady,
                # Nothing before the first steady revolution means the drive
                # was already at speed when it began emitting index.
                transient_seen=start > 0)

    return Result(
        NEVER_STEADY,
        'The revolution period was still varying by more than %.1f%% when '
        'the clock ran out, across %d revolutions.'
        % (TOLERANCE_PCT, len(intervals)),
        first_pulse=first, period=steady)


def _wait_for_standstill(usb: USB.Unit) -> bool:
    '''Wait until the index line has been quiet long enough to call it stopped.

    Returns False if it never went quiet. A spindle on its way down passes
    through arbitrarily long revolutions, so the silence has to outlast those
    rather than merely outlast a revolution at speed.
    '''
    deadline = time.monotonic() + STOP_TIMEOUT
    last_pulse = time.monotonic()
    asserted = False
    while time.monotonic() < deadline:
        now_asserted = not usb.get_pin(INDEX_PIN)
        if now_asserted and not asserted:
            last_pulse = time.monotonic()
        asserted = now_asserted
        if time.monotonic() - last_pulse >= STOP_QUIET:
            return True
    return False


def _time_pulses(usb: USB.Unit, started: float) -> List[float]:
    '''Collect index pulse times, relative to the motor command.'''
    times: List[float] = []
    deadline = started + SPIN_UP_TIMEOUT
    asserted = False
    while time.monotonic() < deadline:
        now = time.monotonic()
        now_asserted = not usb.get_pin(INDEX_PIN)
        if now_asserted and not asserted:
            times.append(now - started)
        asserted = now_asserted
        # Stop early once the speed has plainly settled, rather than holding
        # the drive at speed for the whole timeout on every run.
        if len(times) >= MIN_INTERVALS + 2:
            recent = [b - a for a, b in zip(times[-STEADY_RUN - 1:],
                                            times[-STEADY_RUN:])]
            if recent and (max(recent) - min(recent)
                           <= statistics.median(recent)
                           * TOLERANCE_PCT / 100.0):
                break
    return times


def measure(usb: USB.Unit, unit: int) -> Result:
    '''Stop the spindle, start it again, and time one run-up.'''

    usb.drive_motor(unit, False)
    if not _wait_for_standstill(usb):
        return Result(
            NEVER_STOPPED,
            'The index line was still pulsing %.0f seconds after the motor '
            'was switched off, so a run-up could not be timed from rest.'
            % STOP_TIMEOUT)

    # Start the clock at the command itself. The command costs a USB round
    # trip, so the reading is long by that much -- well under a millisecond,
    # against a run-up measured in hundreds.
    started = time.monotonic()
    usb.drive_motor(unit, True)

    try:
        return interpret(_time_pulses(usb, started))
    finally:
        # Leave the spindle running: the session turned it on, and later
        # probes expect to find it that way.
        usb.drive_motor(unit, True)


def run(ctx) -> Result:
    '''Time several run-ups and keep the smallest, which is the honest one.'''

    results: List[Result] = []
    for attempt in range(RUNS):
        ctx.report('  Stopping the spindle and timing run-up %d of %d...'
                   % (attempt + 1, RUNS))
        result = measure(ctx.usb, ctx.options.drive.unit_id)
        if result.status != OK:
            return result
        results.append(result)

    readings = [r.first_pulse for r in results if r.first_pulse is not None]
    best, spread = reconcile(readings)
    winner = min(results, key=lambda r: r.first_pulse or 0.0)
    return winner._replace(
        detail=winner.detail + ' Smallest of %d runs, which vary by where the'
        ' index hole sits when the drive comes ready.' % RUNS,
        readings=tuple(readings), spread=spread)

# Local variables:
# python-indent: 4
# End:
