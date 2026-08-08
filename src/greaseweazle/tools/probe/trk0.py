# greaseweazle/tools/probe/trk0.py
#
# Probe: does the drive's Track 0 sensor actually work?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# /TRK0 is the only positional feedback a floppy interface offers, and the
# max-track probe is built entirely on it. A sensor stuck asserted makes the
# drive look as though its head never moves; one stuck clear makes cylinder 0
# unfindable. Either way the fault masquerades as a different one, so this
# runs first and every measurement resting on /TRK0 is qualified by it.
#
# The test is simply to walk the head out a few cylinders and back, watching
# the signal:
#
#     cylinder    0    1    2    3    4    3    2    1    0
#     /TRK0       *    .    .    .    .    .    .    .    *      healthy
#     /TRK0       *    *    *    *    *    *    *    *    *      stuck asserted
#     /TRK0       .    .    .    .    .    .    .    .    .      no signal
#     /TRK0       *    .    .    .    .    .    .    .    .      never returns
#
# Both directions are walked deliberately. usb.py already documents drives
# which fail to assert /TRK0 when stepping inward, so a sensor that answers
# going out but not coming back is a real fault, not a hypothetical one.
#
# Every seek here passes check_trk0=False. The usual consistency check in
# usb.seek() would raise on precisely the faults being tested, and a probe
# cannot lean on the signal it exists to validate.
#
# Measures the DRIVE: no disk is required, and none should be present.

from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile
from greaseweazle.tools.probe.pins import trk0_asserted

name = 'trk0-sensor'
title = 'Track 0 Sensor'
summary = 'Whether the Track 0 sensor reports position correctly'
depends_on: Tuple[str, ...] = ()
destructive = False
needs_motor = False
needs_media = core.MEDIA_NONE
wears_drive = False

# Nothing here is a measurement: the sensor either behaves or it does not.
tolerances = {
    'outward': profile.IGNORED,
    'homeward': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
OK = 'ok'                          # Asserts at cylinder 0, clears elsewhere.
ABSENT_AT_HOME = 'absent-at-home'  # No signal at cylinder 0 at all.
STUCK_ASSERTED = 'stuck-asserted'  # Asserted everywhere, including away.
NO_REASSERT = 'no-reassert'        # Cleared going out, never came back.
INCONSISTENT = 'inconsistent'      # Asserted at some cylinders, not others.

# How far out to walk. Far enough that a sensor with a wide aperture cannot
# still be triggering, close enough to be safe on the smallest drive there
# is -- nothing here may assume a cylinder count.
WALK_CYLINDERS = 4


class Result(NamedTuple):
    status: str
    detail: str
    outward: Tuple[Tuple[int, bool], ...] = ()
    homeward: Tuple[Tuple[int, bool], ...] = ()

    @property
    def ok(self) -> bool:
        '''True if measurements resting on /TRK0 can be believed.'''
        return self.status == OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'outward': [[c, t] for c, t in self.outward],
            'homeward': [[c, t] for c, t in self.homeward],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == OK:
            out('  Working.')
        elif self.status == ABSENT_AT_HOME:
            out('  NO SIGNAL at cylinder 0.')
        elif self.status == STUCK_ASSERTED:
            out('  FAULTY - stuck asserted.')
        elif self.status == NO_REASSERT:
            out('  FAULTY - does not re-assert on return.')
        else:
            out('  FAULTY - intermittent.')
        out('  (%s)' % self.detail)
        if self.outward:
            out('  Out:  %s' % _trace(self.outward))
            out('  Back: %s' % _trace(self.homeward))


def interpret(outward: List[Tuple[int, bool]],
              homeward: List[Tuple[int, bool]]) -> Result:
    '''Judge the sensor from a walk out and back. Pure.

    'outward' is (cylinder, asserted) ascending from cylinder 0; 'homeward'
    is the same descending back to it.
    '''
    error.check(len(outward) >= 2,
                'trk0: need at least cylinder 0 and one cylinder away')
    error.check(len(homeward) >= 1, 'trk0: no homeward observations')
    error.check(outward[0][0] == 0, 'trk0: outward walk must start at 0')
    error.check(homeward[-1][0] == 0, 'trk0: homeward walk must end at 0')

    samples = tuple(outward), tuple(homeward)

    if not outward[0][1]:
        # Cannot tell a dead sensor from a head that is not where the
        # firmware believes, and saying so is better than picking one.
        return Result(
            ABSENT_AT_HOME,
            'No Track 0 signal at cylinder 0. Either the sensor is dead or '
            'the head is not where the firmware thinks it is; try "gw reset" '
            'to recalibrate, and if the signal is still absent suspect the '
            'sensor.', *samples)

    # Away from cylinder 0 the signal must clear, in both directions.
    away = list(outward[1:]) + list(homeward[:-1])
    asserted_away = [cyl for cyl, trk0 in away if trk0]
    if asserted_away:
        if len(asserted_away) == len(away):
            return Result(
                STUCK_ASSERTED,
                'Track 0 stayed asserted at every cylinder out to %d. The '
                'sensor is stuck on, so head position cannot be trusted.'
                % outward[-1][0], *samples)
        return Result(
            INCONSISTENT,
            'Track 0 asserted away from cylinder 0 at %s, but not at every '
            'cylinder. An intermittent sensor, or steps being lost.'
            % ','.join(str(c) for c in asserted_away), *samples)

    if not homeward[-1][1]:
        return Result(
            NO_REASSERT,
            'Track 0 cleared on the way out but never returned on the way '
            'back. The sensor answers in one direction only.', *samples)

    return Result(
        OK,
        'Asserted at cylinder 0, clear out to cylinder %d, and asserted '
        'again on return.' % outward[-1][0], *samples)


def measure(usb: USB.Unit, walk: int = WALK_CYLINDERS) -> Result:
    '''Walk the head out and back, sampling /TRK0 at each cylinder.'''

    error.check(walk >= 1, 'trk0: walk must cover at least one cylinder')

    try:
        outward: List[Tuple[int, bool]] = []
        for cylinder in range(0, walk + 1):
            usb.seek(cylinder, 0, check_trk0=False)
            outward.append((cylinder, trk0_asserted(usb)))

        homeward: List[Tuple[int, bool]] = []
        for cylinder in range(walk - 1, -1, -1):
            usb.seek(cylinder, 0, check_trk0=False)
            homeward.append((cylinder, trk0_asserted(usb)))

        return interpret(outward, homeward)

    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def _trace(samples: Tuple[Tuple[int, bool], ...]) -> str:
    return ' '.join('%d:%s' % (cyl, 'ASSERT' if asserted else '-')
                    for cyl, asserted in samples)


def run(ctx) -> Result:
    ctx.report('  Walking the head out to cylinder %d and back...'
               % WALK_CYLINDERS)
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
