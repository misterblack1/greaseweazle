# greaseweazle/tools/probe/head_count.py
#
# Probe: does the drive have a second head?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Reading cannot answer this, which was established the hard way. Two things
# defeat it.
#
# A single-sided DRIVE generally ignores the side-select line, so selecting
# head 1 hands back head 0's data rather than silence. Flux on head 1 is not
# evidence of a second head. A single-sided DISK in a double-sided drive
# reads blank on head 1, so silence on head 1 is not evidence of a missing
# head either. Comparing the two reads does not rescue it: on the bench drive
# head 1 came back within a few percent of head 0 at every cylinder, which is
# exactly what an ignored side-select looks like AND exactly what two
# similarly-formatted surfaces look like.
#
# So this probe writes. Put a different marker on each head at the same
# cylinder, then read head 0 back:
#
#     write head 0 <- marker 0        write head 1 <- marker 1
#     read head 0  -> marker 0        two surfaces, so two heads
#     read head 0  -> marker 1        one surface: the second write landed on
#                                     it, so side-select does nothing
#
# Definitive either way, and it settles the single-sided MEDIA case too: an
# unformatted second surface still takes a write, so a double-sided drive
# reports two heads whatever disk is in it. Reading could never do that.
#
# DESTRUCTIVE: it overwrites one cylinder on both surfaces. Runs only behind
# the consent gate, which the orchestrator applies from destructive = True.
#
# Needs a disk the drive will read: see index_sensor.py, and note that on a
# drive gating reads on READY an unreadable disk looks like a single-sided
# one. Hence the dependency.

from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile, consent, markers

name = 'head-count'
title = 'Head Count'
summary = 'Whether the drive has a second head'
depends_on = ('index-sensor',)
destructive = True
needs_motor = True
needs_media = core.MEDIA_SCRATCH
wears_drive = False

tolerances = {
    'read_back': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
DOUBLE = 'double-sided'      # The two heads reached different surfaces.
SINGLE = 'single-sided'      # Both writes landed on the same surface.
UNREADABLE = 'unreadable'    # Markers did not come back at all.
WRPROT = 'write-protected'   # Disk is protected; nothing was measured.

# Which cylinder to scribble on. Low, because nothing here may assume how
# many cylinders the drive has, and drives run from about 37 upward.
TEST_CYLINDER = 2

# One marker per head.
HEADS = 2


class Result(NamedTuple):
    status: str
    detail: str
    heads: Optional[int] = None
    # Which marker each head read back, by head number.
    read_back: Tuple[Optional[int], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (DOUBLE, SINGLE)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'heads': self.heads,
            'read_back': list(self.read_back),
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == DOUBLE:
            out('  2 heads (double-sided).')
        elif self.status == SINGLE:
            out('  1 head (single-sided).')
        elif self.status == WRPROT:
            out('  Not measured - the disk is write protected.')
        else:
            out('  Inconclusive.')
        if self.read_back:
            out('  Marker read back: %s'
                % ', '.join('head %d -> %s' % (h, 'none' if m is None else m)
                            for h, m in enumerate(self.read_back)))
        out('  (%s)' % self.detail)


def interpret(read_back: Tuple[Optional[int], ...]) -> Result:
    '''Decide from what each head read back after the writes. Pure.

    'read_back' is the marker each head returned, indexed by head. Head n
    was written with marker n, so head 0 returning marker 1 means the second
    write landed on the surface the first one did.
    '''
    error.check(len(read_back) == HEADS,
                'head-count: expected one reading per head')

    if any(marker is None for marker in read_back):
        return Result(
            UNREADABLE,
            'A marker did not read back. Nothing can be concluded about the '
            'heads, since the disk or drive could not carry the test.',
            read_back=read_back)

    if read_back[0] == 0:
        return Result(
            DOUBLE,
            'Head 0 kept its own marker after head 1 was written, so the '
            'two heads are on different surfaces.',
            heads=2, read_back=read_back)

    return Result(
        SINGLE,
        'Head 0 came back carrying the marker written to head 1, so both '
        'writes reached the same surface and side-select does nothing. The '
        'drive has one head.',
        heads=1, read_back=read_back)


def measure(usb: USB.Unit, cylinder: int = TEST_CYLINDER) -> Result:
    '''Write a distinct marker on each head, then read them back.'''

    error.check(markers.slots_fit(HEADS),
                'head-count: markers do not fit the readable span')

    try:
        usb.seek(cylinder, 0, check_trk0=False)
        rev_ticks = markers.revolution_ticks(usb)

        for head in range(HEADS):
            usb.seek(cylinder, head, check_trk0=False)
            markers.write(usb, head, rev_ticks)

        read_back = []
        for head in range(HEADS):
            usb.seek(cylinder, head, check_trk0=False)
            read_back.append(markers.read(usb, HEADS))

        return interpret(tuple(read_back))

    except USB.CmdError as err:
        if consent.is_write_protected(err):
            return Result(
                WRPROT,
                'The disk is write protected, so nothing was measured. Open '
                'the write-protect tab and re-run.')
        raise
    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def run(ctx) -> Result:
    ctx.report('  Writing a marker to each head at cylinder %d...'
               % TEST_CYLINDER)
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
