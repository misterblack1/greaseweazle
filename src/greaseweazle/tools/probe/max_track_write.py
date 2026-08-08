# greaseweazle/tools/probe/max_track_write.py
#
# Probe: confirm the drive's cylinder limit by writing and reading markers.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# An independent check on max_track.py, and a stronger one, because it never
# counts steps. Counting steps is what makes the /TRK0 method vulnerable to a
# stalled stepper slipping poles and inventing travel that never happened.
#
# Instead: write a marker identifying each cylinder across a window spanning
# the suspected stop, then read them back. Below the stop each cylinder is its
# own physical track and reads back its own marker. At the stop and beyond,
# every write lands on the same physical track, so each overwrites the last
# and only the final one survives. The first cylinder that reads back somebody
# else's marker is therefore the stop itself.
#
#     write pass    79 -> trk79   80 -> trk80  ...  83 -> trk83
#                   84 -> trk83   85 -> trk83  ...  91 -> trk83  (all pile up)
#     read pass     79 reads 79   80 reads 80  ...  83 reads 91  <-- stop is 83
#
# Reads happen while stepping outward from a recalibrated cylinder 0, so no
# reversal is involved and there is no slip to confuse.
#
# DESTRUCTIVE: this erases the window it writes. It runs only behind
# consent.confirm().

from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile, consent, markers, max_track

name = 'max-track-write'
title = 'Max Track (write confirmation)'
summary = 'Confirm the cylinder limit by writing and reading back markers'
# Confirms a limit that max-track must first find, by writing markers and
# reading them back -- which needs a disk the drive will read, hence the
# index sensor too. Without it this ran anyway and failed on a missing index,
# which reads as a fault in the drive rather than as a probe that was never
# in a position to measure anything.
depends_on = ('trk0-sensor', 'max-track', 'index-sensor')
destructive = True
needs_motor = True
needs_media = core.MEDIA_SCRATCH
wears_drive = False

tolerances = {
    'readings': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
OK = 'ok'                       # Found the stop.
BEYOND_WINDOW = 'beyond-window' # Every cylinder read back its own marker.
AT_WINDOW_START = 'at-start'    # Mismatched immediately: window started late.
UNREADABLE = 'unreadable'       # A marker could not be decoded.
WRPROT = 'write-protected'      # Disk is protected; nothing was measured.
SKIPPED = 'skipped'             # User declined, or not requested.

# Markers come from markers.py, one slot per cylinder in the window. The
# names below are kept as this probe's own vocabulary, in cylinders rather
# than slots.
MARKER_BASE_US = markers.BASE_US
MARKER_STEP_US = markers.STEP_US

MARKER_TOLERANCE_US = markers.TOLERANCE_US

MARKER_MAX_US = markers.MAX_US

# How far either side of the suspected stop to write. Below it, enough
# cylinders to show markers reading back correctly; above it, enough to make
# the pile-up unmistakable.
WINDOW_BELOW = 4
WINDOW_ABOVE = 6


class Result(NamedTuple):
    status: str
    max_cylinder: Optional[int]
    detail: str
    readings: Tuple[Tuple[int, Optional[int]], ...] = ()
    # What max-track concluded, so agreement between two methods with quite
    # different failure modes can be stated rather than left to the reader.
    stepping_answer: Optional[int] = None

    @property
    def cylinders(self) -> Optional[int]:
        if self.max_cylinder is None:
            return None
        return self.max_cylinder + 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'max_cylinder': self.max_cylinder,
            'cylinders': self.cylinders,
            'detail': self.detail,
            'readings': [[c, m] for c, m in self.readings],
            'stepping_answer': self.stepping_answer,
            'agrees': self.agrees,
        }

    @property
    def agrees(self) -> Optional[bool]:
        '''Whether the two methods reached the same answer, if both ran.'''
        if self.status != OK or self.stepping_answer is None:
            return None
        return self.max_cylinder == self.stepping_answer

    @property
    def ok(self) -> bool:
        return self.status == OK

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == OK:
            max_cylinder, cylinders = self.max_cylinder, self.cylinders
            assert max_cylinder is not None and cylinders is not None
            out('  %d cylinders (0-%d)' % (cylinders, max_cylinder))
            if self.agrees is True:
                out('  AGREES with the step-counting measurement.')
            elif self.agrees is False:
                # agrees is only False when both answers exist.
                assert self.stepping_answer is not None
                out('  DISAGREES with the step-counting measurement (%d).'
                    % self.stepping_answer)
                out('  Trust this one: it counts no steps, so a stalled'
                    ' stepper cannot inflate it.')
        elif self.status == SKIPPED:
            out('  Not run.')
        elif self.status == WRPROT:
            out('  Not measured - the disk is write protected.')
        else:
            out('  Inconclusive.')
        out('  (%s)' % self.detail)
        if self.readings:
            out('  Cylinder -> marker read back: %s'
                % ', '.join('%d->%s' % (c, 'none' if m is None else m)
                            for c, m in self.readings))


def marker_us(cylinder: int, lowest: int) -> float:
    '''Flux period that identifies this cylinder. Pure.'''
    return markers.period_us(cylinder - lowest)


def window_fits(lowest: int, highest: int) -> bool:
    '''True if every cylinder in the window gets a well-resolved marker.'''
    return markers.slots_fit(highest - lowest + 1)


def decode_marker(median_us: float, lowest: int, highest: int) -> Optional[int]:
    '''Recover a cylinder number from a measured flux period. Pure.

    Returns None if the period matches no marker in the window, which is the
    honest answer for an unwritten or unreadable track.
    '''
    slot = markers.decode(median_us, highest - lowest + 1)
    return None if slot is None else lowest + slot


def interpret(readings: List[Tuple[int, Optional[int]]]) -> Result:
    '''Find the stop from marker read-backs. Pure.

    'readings' is (cylinder, marker read back) in ascending cylinder order.
    '''
    error.check(len(readings) > 0, 'max-track-write: no readings')

    for position, (cylinder, marker) in enumerate(readings):
        if marker is None:
            return Result(
                UNREADABLE, None,
                'Cylinder %d read back no recognisable marker, so the disk '
                'or drive could not carry the test.' % cylinder,
                tuple(readings))
        if marker == cylinder:
            continue
        # Somebody else's marker: every write from here out landed on this
        # same physical track.
        if position == 0:
            return Result(
                AT_WINDOW_START, cylinder,
                'The very first cylinder tested already showed the pile-up, '
                'so the stop is at or below cylinder %d.' % cylinder,
                tuple(readings))
        return Result(
            OK, cylinder,
            'Cylinder %d read back a marker that was not its own (decoded as '
            '%d): writes beyond it all landed on the same track.'
            % (cylinder, marker),
            tuple(readings))

    return Result(
        BEYOND_WINDOW, None,
        'Every cylinder tested read back its own marker, so the stop lies '
        'beyond cylinder %d.' % readings[-1][0],
        tuple(readings))


def _write_marker(usb: USB.Unit, cylinder: int, lowest: int,
                  rev_ticks: float) -> None:
    markers.write(usb, cylinder - lowest, rev_ticks)


def _read_marker(usb: USB.Unit, lowest: int,
                 highest: int) -> Optional[int]:
    slot = markers.read(usb, highest - lowest + 1)
    return None if slot is None else lowest + slot


def run(ctx) -> Result:
    # The orchestrator has already established that max-track produced a
    # usable limit, and has already taken consent for writing.
    stepping_answer = ctx.result(max_track).max_cylinder
    result = confirm(ctx.usb, stepping_answer, ctx.report)
    return result._replace(stepping_answer=stepping_answer)


def confirm(usb: USB.Unit, suspected_stop: int,
            report: Callable[[str], None] = print) -> Result:
    '''Confirm a suspected cylinder limit by writing markers around it.'''

    error.check(suspected_stop >= 0, 'suspected stop must not be negative')

    lowest = max(0, suspected_stop - WINDOW_BELOW)
    highest = suspected_stop + WINDOW_ABOVE
    error.check(window_fits(lowest, highest),
                'max-track-write: cylinders %d-%d need markers beyond %.1fus, '
                'which will not read back reliably'
                % (lowest, highest, MARKER_MAX_US))

    try:
        usb.seek(0, 0)
        flux = usb.read_track(1)
        error.check(len(flux.index_list) > 0,
                    'No index pulse: cannot time a track to write markers.')
        rev_ticks = flux.index_list[-1]

        report('  Writing markers to cylinders %d-%d...' % (lowest, highest))
        for cylinder in range(lowest, highest + 1):
            usb.seek(cylinder, 0, check_trk0=False)
            _write_marker(usb, cylinder, lowest, rev_ticks)

        report('  Reading markers back...')
        usb.seek(0, 0)
        readings: List[Tuple[int, Optional[int]]] = []
        for cylinder in range(lowest, highest + 1):
            usb.seek(cylinder, 0, check_trk0=False)
            readings.append((cylinder, _read_marker(usb, lowest, highest)))
            # Once a cylinder shows the pile-up there is nothing further to
            # learn, and every cylinder past it reads the same track.
            if readings[-1][1] != cylinder:
                break

        return interpret(readings)

    except USB.CmdError as err:
        if consent.is_write_protected(err):
            return Result(
                WRPROT, None,
                'The disk is write protected, so nothing was measured. Open '
                'the write-protect tab and re-run.')
        raise
    finally:
        try:
            usb.seek(0, 0)
        except (USB.CmdError, error.Fatal):
            report('  Warning: could not return the head to cylinder 0.')

# Local variables:
# python-indent: 4
# End:
