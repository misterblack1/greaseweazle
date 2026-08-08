# scripts/tests/fake.py
#
# A drive and a Greaseweazle that exist only in memory.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# The probes' interpretation is pure and tested directly. What was left
# untested is everything around it: the orchestrator, and the acquisition in
# each probe which drives the head about and reads pins. This covers those.
#
# It MODELS a drive rather than replaying recordings of one. Fixtures of raw
# USB bytes were the original plan and would have been worse: they say what
# happened without saying why, they cannot be adjusted to ask "and if the
# sensor were stuck?", and every one of the faults worth testing is a fault
# no drive here has. A model can be told to have a dead sensor, forty
# cylinders, or a stepper which loses count, and the probe meets it exactly
# as it would meet the real thing.
#
# Nothing here is named after a drive model, and nothing should be. The
# parameters are behaviours -- how many cylinders, whether /TRK0 answers --
# because that is what a probe can actually observe.

from typing import Any, Dict, List, Optional, Tuple

from greaseweazle import usb as USB

# How /TRK0 behaves. The faults are the ones trk0.py exists to name, none of
# which can be produced on demand with real hardware.
TRK0_WORKING = 'working'
TRK0_STUCK = 'stuck-asserted'
TRK0_DEAD = 'dead'
TRK0_ONE_WAY = 'no-reassert'


class FakeUnit:
    '''Enough of USB.Unit for the probes which work from stepping and pins.

    The head has a real position of its own, which is the point: the
    firmware's idea of where it is and where it actually is come apart
    exactly as they do on a drive, when the head reaches its stop or the
    stepper loses count.
    '''

    def __init__(self, cylinders: int = 40,
                 trk0: str = TRK0_WORKING,
                 step_loss_period: int = 0,
                 firmware_limit: Optional[int] = None) -> None:
        self.sample_freq = 72e6
        self.version = (1, 6)
        self.hw_model, self.hw_submodel = 4, 0

        # The outermost cylinder the head can reach. Beyond it the carriage
        # is against the stop and further steps go nowhere.
        self.stop = cylinders - 1
        self.firmware_cylinder = 0
        self.head_cylinder = 0
        self.head = 0

        self.trk0 = trk0
        self.has_left_track_0 = False

        # Driving a stalled stepper makes it slip, and on reversal it takes
        # a few steps to re-engage. Measured on one drive as (overdrive mod
        # 4) and on another as (overdrive mod 2), so the period is a knob.
        self.step_loss_period = step_loss_period
        self._pending_loss = 0

        # Some firmwares refuse a cylinder outright, which is not the same
        # as a drive whose head will not go there.
        self.firmware_limit = firmware_limit

        self.seeks: List[Tuple[int, int]] = []
        self.pin_levels: Dict[int, bool] = {}
        self.motor_on = False
        self.params: Dict[int, bytes] = {}

    # -- the surface the probes use ------------------------------------

    def seek(self, cylinder: int, head: int = 0,
             check_trk0: bool = True) -> None:
        if (self.firmware_limit is not None
                and cylinder > self.firmware_limit):
            raise USB.CmdError(b'seek', USB.Ack.BadCylinder)

        self.seeks.append((cylinder, head))
        self.head = head
        delta = cylinder - self.firmware_cylinder
        self.firmware_cylinder = cylinder

        if delta > 0:
            self._step_out(delta)
        elif delta < 0:
            self._step_in(-delta)

    def get_pin(self, pin: int) -> bool:
        '''Pin levels are active low, so True means not asserted.'''
        if pin == 26:
            return not self._trk0_asserted()
        if pin in self.pin_levels:
            return self.pin_levels[pin]
        return True

    def set_pin(self, pin: int, level: int) -> None:
        self.pin_levels[pin] = bool(level)

    def drive_motor(self, unit: int, state: bool) -> None:
        self.motor_on = state

    def get_params(self, index: int, count: int) -> bytes:
        return self.params.get(index, bytes(count))[:count]

    def set_params(self, index: int, data: bytes) -> None:
        self.params[index] = data

    # -- the drive itself ----------------------------------------------

    def _step_out(self, steps: int) -> None:
        room = max(0, self.stop - self.head_cylinder)
        moved = min(steps, room)
        self.head_cylinder += moved
        if self.head_cylinder > 0:
            self.has_left_track_0 = True
        stalled = steps - moved
        if stalled and self.step_loss_period:
            # Slipped against the stop. Coming back, that many steps are
            # spent re-engaging before the head moves at all.
            self._pending_loss = stalled % self.step_loss_period

    def _step_in(self, steps: int) -> None:
        absorbed = min(steps, self._pending_loss)
        self._pending_loss -= absorbed
        steps -= absorbed
        self.head_cylinder = max(0, self.head_cylinder - steps)

    def _trk0_asserted(self) -> bool:
        if self.trk0 == TRK0_STUCK:
            return True
        if self.trk0 == TRK0_DEAD:
            return False
        if self.trk0 == TRK0_ONE_WAY:
            # Answers on the way out and never again.
            return self.head_cylinder == 0 and not self.has_left_track_0
        return self.head_cylinder == 0


class Recorder:
    '''Collects report lines instead of printing them.'''

    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return '\n'.join(self.lines)


class StubResult:
    '''A probe result which is whatever a test needs it to be.'''

    def __init__(self, status: str = 'ok', ok: bool = True) -> None:
        self.status = status
        self._ok = ok

    @property
    def ok(self) -> bool:
        return self._ok

    def as_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'ok': self._ok}

    def report(self, out) -> None:
        out('  stub: %s' % self.status)


class StubProbe:
    '''A probe which does nothing but record that it was asked to.'''

    def __init__(self, name: str, depends_on: Tuple[str, ...] = (),
                 destructive: bool = False, wears_drive: bool = False,
                 needs_media: str = 'none', needs_motor: bool = False,
                 ok: bool = True, raises: Optional[Exception] = None) -> None:
        self.name = name
        self.title = name.title()
        self.summary = 'stub'
        self.depends_on = depends_on
        self.destructive = destructive
        self.wears_drive = wears_drive
        self.needs_media = needs_media
        self.needs_motor = needs_motor
        self.tolerances: Dict[str, Any] = {}
        self._ok = ok
        self._raises = raises
        self.ran = False

    def run(self, ctx) -> StubResult:
        self.ran = True
        if self._raises is not None:
            raise self._raises
        return StubResult('ok' if self._ok else 'bad', self._ok)

# Local variables:
# python-indent: 4
# End:
