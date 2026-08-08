# greaseweazle/tools/probe/pin34.py
#
# Probe: is pin 34 DISK-CHANGE or READY?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# tools/diag/pinmap.py marks pin 34 'ambiguous' and shows the raw level,
# because its meaning varies by drive family and jumper. This settles it for
# a particular drive.
#
# The two behave differently under stepping, which is the whole trick.
# DISK-CHANGE latches asserted when the media is swapped and clears on the
# first step with a disk loaded. READY reports whether the drive is up and
# usable, and stepping does not touch it. So: sample the line, step one
# cylinder, sample again.
#
#     asserted -> clear     the step cleared a latch, so DISK-CHANGE
#     asserted -> asserted  stepping did not touch it, so READY
#
# That much was measured on the bench drive, which went from asserted to
# clear across a single step and stayed clear.
#
# The hole in it is a latch that was ALREADY clear when the probe started,
# from any earlier stepping in the session. Then the line reads clear both
# times, which is also what a READY line looks like on a drive that is not
# ready. Knowing whether a readable disk is loaded rules READY out -- with one
# spinning it would be asserted -- but two causes remain: a latch already
# cleared, or a drive which does not drive pin 34 at all. The second is not
# hypothetical: a 360k drive on this bench read clear on a freshly inserted
# disk with nothing having stepped, which leaves only "not driven". Disk-
# change arrived with the later high-density drives and plenty of earlier
# ones simply leave the line alone.
#
# Both are reported as indeterminate, with the way to separate them: reinsert
# the disk and run this probe alone before anything steps. Clear even then
# means the line is not driven.
#
# Hence the dependency on the index sensor: without a disk the drive will
# read, none of the above holds. A DISK-CHANGE latch cannot be cleared with
# no media, so it sits asserted and looks exactly like a ready READY line.
#
# ORDERING. Any probe which steps the head clears the latch, so this one has
# to run before them or it finds nothing to clear and can only report
# indeterminate. The run order happens to oblige, since the probes it depends
# on do not step, but that is where the ordering falls out rather than
# anything declared -- a stepping probe added with no dependencies could sort
# ahead of this one and quietly cost it its answer. A test pins the order
# down. The alternative, a fourth flag on the probe contract to say a probe
# steps, buys one probe's ordering at everybody's expense.

import time
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile

name = 'pin34'
title = 'Pin 34 Mode'
summary = 'Whether pin 34 is DISK-CHANGE or READY'
depends_on = ('index-sensor',)
destructive = False
needs_motor = True
needs_media = core.MEDIA_ANY
wears_drive = False

# The duty figures are sampling artefacts; what pin 34 IS must match exactly.
tolerances = {
    'asserted_before': profile.IGNORED,
    'asserted_after': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
DISK_CHANGE = 'disk-change'      # Latched, and a step cleared it.
READY = 'ready'                  # Asserted and indifferent to stepping.
INDETERMINATE = 'indeterminate'  # Clear throughout; cannot tell from here.
UNSTABLE = 'unstable'            # The line would not hold a level.
UNEXPECTED = 'unexpected'        # Stepping asserted it, which fits neither.

# The line in question. tools/diag/pinmap.py lists it among the signals it
# displays, flagged ambiguous; the number is repeated here rather than picked
# back out of that table.
PIN = 34

# How long to watch the line for each sample, and how one-sided the reading
# must be to count as a level rather than something changing under us.
SAMPLE_SECONDS = 0.25
STEADY_FRACTION = 0.95

# Where to step to and back. One cylinder is enough to clear a latch, and
# there is no reason to move the head further than the test requires.
STEP_TO = 1


class Result(NamedTuple):
    status: str
    detail: str
    # Fraction of samples with the line asserted (low), before and after.
    before: Optional[float] = None
    after: Optional[float] = None

    @property
    def mode(self) -> Optional[str]:
        '''What pin 34 is, or None if this could not be settled.'''
        if self.status == DISK_CHANGE:
            return 'disk-change'
        if self.status == READY:
            return 'ready'
        return None

    @property
    def ok(self) -> bool:
        return self.mode is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'mode': self.mode,
            'detail': self.detail,
            'asserted_before': self.before,
            'asserted_after': self.after,
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == DISK_CHANGE:
            out('  DISK-CHANGE.')
        elif self.status == READY:
            out('  READY.')
        elif self.status == UNSTABLE:
            out('  UNKNOWN - the line would not hold a level.')
        elif self.status == UNEXPECTED:
            out('  UNKNOWN - stepping asserted the line.')
        else:
            out('  UNKNOWN - clear before and after stepping.')
        if self.before is not None and self.after is not None:
            out('  Asserted %.0f%% of samples before stepping, %.0f%% after.'
                % (self.before * 100, self.after * 100))
        out('  (%s)' % self.detail)


def _level(fraction: float) -> Optional[bool]:
    '''True if asserted, False if clear, None if it would not settle.'''
    if fraction >= STEADY_FRACTION:
        return True
    if fraction <= 1.0 - STEADY_FRACTION:
        return False
    return None


def interpret(before: float, after: float) -> Result:
    '''Decide what pin 34 is, from the level either side of a step. Pure.

    'before' and 'after' are the fraction of samples with the line asserted.
    A readable disk is assumed loaded and spinning: the caller guarantees it
    by depending on the index sensor probe, and without it none of these
    conclusions hold.
    '''
    error.check(0.0 <= before <= 1.0 and 0.0 <= after <= 1.0,
                'pin34: sample fractions must lie between 0 and 1')

    was, now = _level(before), _level(after)

    if was is None or now is None:
        return Result(
            UNSTABLE,
            'The line changed state while being sampled, so no level could '
            'be read from it. Neither meaning fits a line that will not '
            'hold still.', before, after)

    if was and not now:
        return Result(
            DISK_CHANGE,
            'The line was asserted and a single step cleared it, which is '
            'what a disk-change latch does and what a ready signal cannot.',
            before, after)

    if was and now:
        return Result(
            READY,
            'The line stayed asserted across a step. A disk-change latch '
            'would have cleared, so this reports readiness.', before, after)

    if not was and now:
        return Result(
            UNEXPECTED,
            'Stepping asserted the line, which fits neither meaning. Treat '
            'pin 34 on this drive as unidentified.', before, after)

    return Result(
        INDETERMINATE,
        'The line was clear before and after stepping, which leaves two '
        'causes. Either it is a disk-change latch that earlier stepping had '
        'already cleared -- any probe which moved the head first will have '
        'done so, as will any earlier run -- or the drive does not drive pin '
        '34 at all, which is common on drives predating disk-change. It is '
        'not READY: with a disk loaded and spinning that would be asserted. '
        'To tell the two apart, eject and reinsert the disk and run this '
        'probe on its own before anything steps. If it reads clear even '
        'then, the line is not driven.', before, after)


def _sample(usb: USB.Unit, seconds: float = SAMPLE_SECONDS) -> float:
    '''Fraction of samples over the window with the line asserted (low).'''
    asserted = total = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not usb.get_pin(PIN):
            asserted += 1
        total += 1
    error.check(total > 0, 'pin34: no samples taken')
    return asserted / total


def measure(usb: USB.Unit) -> Result:
    '''Sample pin 34, step one cylinder, and sample it again.'''
    before = _sample(usb)
    try:
        usb.seek(STEP_TO, 0, check_trk0=False)
        after = _sample(usb)
    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass
    return interpret(before, after)


def run(ctx) -> Result:
    ctx.report('  Sampling pin 34 either side of a single step...')
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
