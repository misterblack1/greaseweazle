# greaseweazle/tools/probe/max_track.py
#
# Probe: how far outward can the drive's head actually step?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# A floppy drive's stepper is open loop: nothing reports that the head has
# hit the outer stop, and the firmware goes on counting steps it never made.
# /TRK0 is the only position feedback in the interface, so we use it as one.
#
# Step the head outward to a probe cylinder. If the drive's stop is nearer
# than that, the head stalls there while the firmware believes it arrived.
# Now step back inward one cylinder at a time, watching /TRK0: it asserts
# when the *head* reaches cylinder 0, at some firmware cylinder c. The head
# apparently travelled (probe - c) cylinders outward.
#
# "Apparently", because a single such measurement is not trustworthy. Driving
# a stepper into its stop makes it slip poles, and on reversal it takes some
# steps to re-engage before the head moves again. Those steps are counted as
# travel that never happened, so a measurement can only ever OVERSTATE how far
# the head got. Measured on one 5.25" drive whose stop is at cylinder 83, the
# inflation was (overdrive mod 4) -- a four-phase stepper artifact -- and a
# probe 3 cylinders past the stop reported the head as having arrived exactly:
#
#     probe cylinder   86   88   90  100
#     travel measured  86   84   86   84     <-- stop is 83, not 84
#
# So we probe several cylinders beyond the stop and take the MINIMUM travel.
# That relies only on the inflation being non-negative, not on the mod-4
# pattern, which was only ever observed on one drive.
#
# The probes must be CONSECUTIVE, though, and that is what the four readings
# above lack: every one of them was inflated, and their minimum of 84 is a
# cylinder too high. Four consecutive probe cylinders cover every residue mod
# 4, so on a drive with periodic loss at least one reading comes back clean.
#
# The PERIOD is drive-specific, which is why four consecutive rather than four
# of anything. A second drive showed a period of 2, its readings alternating
# 41, 42, 41, 42 rather than cycling through four values. Four consecutive
# probes cover residues mod 2 and mod 4 alike.
#
# AND ON THAT SECOND DRIVE THIS METHOD CAME OUT A CYLINDER SHORT. It reported
# a stop at 41 while the marker probe -- which counts no steps at all --
# showed 40 and 41 holding their own marks and 42, 43 and 44 all holding the
# same one, so the stop is 42. The step-loss model here says travel is the
# stop plus some non-negative loss, so a reading BELOW the stop should be
# impossible, and 41 was read repeatedly. Something on that drive makes the
# count come up short and it is not understood; see the open question in the
# task list rather than a guess here. The write confirmation exists for
# precisely this, and it flagged the disagreement rather than either figure
# passing silently.
#
# Note this measures the DRIVE, not the media: no disk is required, and none
# should be present, since stepping repeatedly across stationary media can
# score it.

from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile
from greaseweazle.tools.probe.pins import trk0_asserted

name = 'max-track'
title = 'Max Track'
summary = 'Highest cylinder the drive head can reach'
# Head position is measured against /TRK0, so a figure taken without
# validating that sensor would be unqualified.
depends_on = ('trk0-sensor',)
destructive = False
needs_motor = False
# Deliberately drives the head into its outer stop, repeatedly, which is the
# only way to find where that stop is. Never run on anyone else's behalf.
needs_media = core.MEDIA_NONE
wears_drive = True

# The cylinder count is the finding and is compared exactly. The spread
# reflects how much step loss the stepper happened to show on the day, not
# anything about the drive, so comparing it would report noise as change.
tolerances = {
    'spread': profile.IGNORED,
    'observations': profile.IGNORED,
    'probe_cylinder': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Probe outcomes.
OK = 'ok'                    # We measured a limit.
NO_MOVEMENT = 'no-movement'  # /TRK0 never released: the head did not move.
NO_TRK0 = 'no-trk0'          # Stepped all the way home without /TRK0.
FW_LIMIT = 'firmware-limit'  # Firmware refused the cylinder as out of range.

# Where the outward search begins, and how far each pass reaches past the
# last. Deliberately low: drives run from about 37 cylinders upward, and
# opening anywhere near a large drive's count would drive a small drive's
# head well into its stop before the search has learned anything at all.
#
# Starting low is cheap as well as safe. A pass below the stop does no
# grinding -- the head simply arrives -- and costs only the steps out and
# back, so the early passes are the short ones. Whatever the drive, the head
# is never driven more than one escalation past its stop.
START_CYLINDER = 8

# How many adjacent cylinders to measure once the stop has been bracketed.
# Step loss was periodic on the drive it was characterised on, so a run of
# consecutive probes is more likely to include a clean one than repeats of
# the same probe would be.
REFINE_PROBES = 4

# How far each pass reaches past the last. Small enough that the head is
# never driven far past its stop, large enough not to spend a measurement on
# every cylinder. A search increment, not a claim about how many cylinders
# any drive has.
ESCALATE_STEP = 8

# Backstop against a search that never terminates. The Seek command carries a
# signed 16-bit cylinder, so nothing beyond this is expressible in any case.
PROTOCOL_MAX_CYLINDER = 0x7fff


class Result(NamedTuple):
    status: str
    probe_cylinder: int
    # Highest cylinder the head reached, or None if we could not tell.
    max_cylinder: Optional[int]
    # True if the head appeared to reach the probe cylinder, so the real
    # limit is at or above max_cylinder rather than exactly at it. Note a
    # measurement inflated by step loss can look saturated when it is not,
    # which is why a saturated observation is never used as evidence.
    saturated: bool
    detail: str
    # (probe cylinder, travel) for each measurement the answer rests on.
    observations: Tuple[Tuple[int, int], ...] = ()
    # Difference between the largest and smallest travel seen. Zero means
    # every probe agreed; larger means step loss was in play.
    spread: Optional[int] = None

    @property
    def cylinders(self) -> Optional[int]:
        '''Cylinder count, counting from zero.'''
        if self.max_cylinder is None:
            return None
        return self.max_cylinder + 1

    def as_dict(self) -> Dict[str, Any]:
        '''JSON-friendly form, for the drive profile.'''
        return {
            'status': self.status,
            'probe_cylinder': self.probe_cylinder,
            'max_cylinder': self.max_cylinder,
            'cylinders': self.cylinders,
            'saturated': self.saturated,
            'detail': self.detail,
            'observations': [list(o) for o in self.observations],
            'spread': self.spread,
        }

    @property
    def ok(self) -> bool:
        '''True if a cylinder limit was actually pinned down.

        A saturated result is a lower bound, not a limit, so probes which
        confirm the limit must not treat it as one.
        '''
        return (self.status == OK and self.max_cylinder is not None
                and not self.saturated)

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == OK:
            max_cylinder, cylinders = self.max_cylinder, self.cylinders
            assert max_cylinder is not None and cylinders is not None
            if self.saturated:
                out('  At least %d cylinders (0-%d)'
                    % (cylinders, max_cylinder))
                out('  The head reached every cylinder attempted, so the'
                    ' drive may go further.')
                out('  Re-run with a higher --max-cylinder to find the stop.')
            else:
                out('  %d cylinders (0-%d)' % (cylinders, max_cylinder))
                if self.observations:
                    out('  Measured at probe cylinders %s -> travel %s'
                        % (','.join(str(c) for c, _ in self.observations),
                           ','.join(str(t) for _, t in self.observations)))
        elif self.status == NO_MOVEMENT:
            out('  UNKNOWN - the head did not move.')
            out('  Check the drive select and step lines, and that the drive'
                ' is powered.')
        elif self.status == FW_LIMIT:
            out('  UNKNOWN - the firmware would not seek that far.')
            out('  This is a Greaseweazle firmware limit, not a drive limit.')
        elif self.status == NO_TRK0:
            out('  UNKNOWN - no Track 0 signal.')
            out('  This probe measures against the Track 0 sensor, so it'
                ' cannot report a limit without one.')
        out('  (%s)' % self.detail)


def reconcile(observations: List[Tuple[int, int]]) -> Tuple[int, int]:
    '''Best estimate of the outer stop, and the spread across measurements.

    Lost steps add travel that never happened and can never subtract it, so
    across several probes the SMALLEST travel is the one closest to the
    truth. This holds however the step loss is distributed; it does not
    assume the mod-4 pattern seen on one drive.

    Pure. Every observation must come from a probe cylinder beyond the stop
    (ie. a measurement that was not saturated), or a probe that the head
    genuinely reached would drag the minimum down below the real limit.
    '''
    error.check(len(observations) > 0, 'max-track: no usable observations')
    travels = [travel for _, travel in observations]
    return min(travels), max(travels) - min(travels)


def interpret(probe_cylinder: int,
              samples: List[Tuple[int, bool]]) -> Result:
    '''Turn a set of step-back observations into a result.

    'samples' is the /TRK0 state observed at each cylinder the firmware
    stepped through on the way home, in descending cylinder order, the
    first being the probe cylinder itself. True means /TRK0 asserted.

    This is pure: it performs no I/O, and is where the arithmetic that
    turns a stalled stepper into a cylinder count is tested.
    '''
    error.check(probe_cylinder > 0,
                'max-track: probe cylinder must be above zero')
    error.check(len(samples) > 0, 'max-track: no observations')
    error.check(samples[0][0] == probe_cylinder,
                'max-track: observations must start at the probe cylinder')

    for cyl, trk0 in samples:
        if not trk0:
            continue
        if cyl == probe_cylinder:
            return Result(
                NO_MOVEMENT, probe_cylinder, None, False,
                'Track 0 still asserted after stepping out to cylinder %d: '
                'the head did not move.' % probe_cylinder)
        # The head travelled this many cylinders before /TRK0 came back.
        reached = probe_cylinder - cyl
        if cyl == 0:
            return Result(
                OK, probe_cylinder, reached, True,
                'Head reached the probe cylinder, so this is a lower bound.')
        return Result(
            OK, probe_cylinder, reached, False,
            'Head stalled %d cylinder(s) short of the probe cylinder.'
            % cyl)

    return Result(
        NO_TRK0, probe_cylinder, None, False,
        'Stepped back to cylinder 0 without Track 0 ever asserting.')


def _recalibrate(usb: USB.Unit, report: Callable[[str], None]) -> None:
    '''Return the head to cylinder 0 and resync the firmware's position.

    On the way out of a measurement the head sits at cylinder 0 while the
    firmware believes it is at c, so this steps inward c times against the
    track 0 stop. That is what any drive recalibration does, and c is only
    ever as large as the overshoot past the drive's own limit.
    '''
    try:
        usb.seek(0, 0)
    except (USB.CmdError, error.Fatal):
        report('  Warning: could not return the head to cylinder 0.'
               ' Try "gw reset".')


def measure(usb: USB.Unit, probe_cylinder: int) -> Result:
    '''Run one outward-then-count-back measurement.'''

    usb.seek(0, 0)
    error.check(trk0_asserted(usb),
                'Track 0 signal absent at cylinder 0. The max-track probe '
                'needs a working Track 0 sensor to measure against.')

    try:
        try:
            usb.seek(probe_cylinder, 0, check_trk0=False)
        except USB.CmdError as err:
            if err.code == USB.Ack.BadCylinder:
                # A limit of the firmware, not of the drive. Reported rather
                # than raised, so the search can stop and say which one it hit.
                return Result(
                    FW_LIMIT, probe_cylinder, None, False,
                    'Firmware rejected cylinder %d as out of range.'
                    % probe_cylinder)
            raise

        samples = [(probe_cylinder, trk0_asserted(usb))]
        cyl = probe_cylinder
        while not samples[-1][1] and cyl > 0:
            cyl -= 1
            usb.seek(cyl, 0, check_trk0=False)
            samples.append((cyl, trk0_asserted(usb)))

        return interpret(probe_cylinder, samples)

    finally:
        _recalibrate(usb, print)


def run(ctx) -> Result:
    return search(ctx.usb, ctx.options.max_cylinder, ctx.report)


def search(usb: USB.Unit, max_cylinder: Optional[int] = None,
           report: Callable[[str], None] = print) -> Result:
    '''Measure the drive's cylinder limit.

    Two stages. First bracket the stop by working outward from a low cylinder
    until the head demonstrably fails to arrive -- never opening at a high
    cylinder, which would slam a small drive's head into its stop before
    anything is known about it. Then re-measure at a few adjacent cylinders
    and take the smallest travel, so that step loss at the stop cannot inflate
    the answer.

    max_cylinder is an optional ceiling for callers who want to bound how far
    the head is driven. There is deliberately no default: how many cylinders a
    drive has is what this probe exists to find out, so assuming a figure here
    would presume the answer.
    '''

    ceiling = PROTOCOL_MAX_CYLINDER
    if max_cylinder is not None:
        error.check(max_cylinder > 0, 'max-cylinder must be above zero')
        ceiling = min(ceiling, max_cylinder)

    bracket = None
    probe_cylinder = min(START_CYLINDER, ceiling)
    while True:
        report('  Stepping out to cylinder %d and counting back...'
               % probe_cylinder)
        result = measure(usb, probe_cylinder)
        if result.status not in (OK, FW_LIMIT):
            return result
        if result.status == OK:
            bracket = result
            if not result.saturated:
                break
            # The head arrived, so the stop is further out than this. Note
            # that an inflated reading can look like an arrival, which only
            # costs another pass further out.
        if probe_cylinder >= ceiling or result.status == FW_LIMIT:
            reason = ('the firmware would not accept a higher cylinder'
                      if result.status == FW_LIMIT
                      else 'that was the highest cylinder allowed')
            if bracket is None:
                return result._replace(
                    detail='Could not reach a measurable cylinder: %s.'
                           % reason)
            return bracket._replace(
                saturated=True,
                detail='The head reached every cylinder attempted, and %s.'
                       ' The stop is at or beyond this.' % reason)
        probe_cylinder = min(probe_cylinder + ESCALATE_STEP, ceiling)

    assert bracket is not None

    # The bracket probe is beyond the stop, so cylinders below it are too.
    # Measuring a few of them costs little and lets the minimum discard any
    # inflated readings.
    observations: List[Tuple[int, int]] = []
    lowest = max(1, bracket.probe_cylinder - (REFINE_PROBES - 1))
    for probe_cylinder in range(lowest, bracket.probe_cylinder + 1):
        if probe_cylinder == bracket.probe_cylinder:
            result = bracket
        else:
            report('  Confirming at cylinder %d...' % probe_cylinder)
            result = measure(usb, probe_cylinder)
            if result.status != OK:
                return result
        # A saturated reading means the head either genuinely reached this
        # cylinder or was inflated up to it. Either way it is not evidence
        # of where the stop is, so it is dropped rather than averaged in.
        if not result.saturated and result.max_cylinder is not None:
            observations.append((probe_cylinder, result.max_cylinder))

    if not observations:
        return bracket._replace(
            saturated=True,
            detail='No probe cleared the stop by enough to measure against.')

    best, spread = reconcile(observations)
    detail = 'Smallest travel across %d probe cylinders' % len(observations)
    detail += (' (all agreed).' if spread == 0
               else ' (readings spread by %d, step loss at the stop).'
               % spread)
    return Result(OK, bracket.probe_cylinder, best, False, detail,
                  tuple(observations), spread)

# Local variables:
# python-indent: 4
# End:
