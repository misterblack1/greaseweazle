# greaseweazle/tools/probe/write_verify.py
#
# Probe: does the write path work end to end?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Write a track, read it back, and see whether what comes back is what went
# down. That exercises the whole chain -- the write-enable line, the write
# current, the head, and the media -- and a drive which reads perfectly well
# can still fail all of it.
#
# The pattern is a run of blocks at different cell periods rather than one
# uniform period, so that a drive which writes SOMETHING but not what it was
# asked cannot pass. A single period would be recovered by any track holding
# any regular flux at all.
#
#     pass 0   3.0   4.5   6.0   3.0   4.5   6.0  us
#     pass 1   6.0   3.0   4.5   6.0   3.0   4.5  us
#
# Each block occupies a known slice of the revolution, so reading back and
# taking the median interval in each slice says whether that block arrived.
# Blocks repeat within a pass so that a drive writing only the start of a
# track, or only the end, shows as partial rather than clean.
#
# Two passes, because one cannot tell a working write path from a dead one. A
# drive which writes nothing leaves whatever the track already held, and on
# the second run of this probe that is the pattern this probe wrote the time
# before -- which would read back perfectly and pass. Rotating the periods
# means no stale track can satisfy both passes.
#
# TOLERANCE WAS EXPECTED TO BE THE DIFFICULTY AND IS NOT. markers.py records
# long periods reading some 12% short, so an exact comparison looked certain
# to fail healthy drives. At the periods used here it does not: on scratch
# high-density media in its own drive every block of every pass came back at
# 0.0000%, three runs running. A written period is a whole number of sample
# ticks and the median lands back on it exactly. The allowance that remains
# is for media and drives which are not at their best, and not for
# measurement noise, of which there is none to speak of.
#
# WRITE PROTECTION IS NOT A FAULT. A protected disk says nothing whatever
# about the write path, so it is reported as not measured rather than as a
# failure -- the distinction the drive profile depends on.
#
# DESTRUCTIVE: it overwrites the cylinder it tests. Runs only behind the
# consent gate, which the orchestrator applies from destructive = True.

import statistics
from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, consent, profile

name = 'write-verify'
title = 'Write and Read Back'
summary = 'Whether a written track reads back as written'
depends_on = ('index-sensor',)
destructive = True
needs_motor = True
needs_media = core.MEDIA_SCRATCH
wears_drive = False

tolerances = {
    'blocks': profile.IGNORED,
    'detail': profile.IGNORED,
    'worst_error': profile.Tolerance(absolute=0.05),
}

# Outcomes.
OK = 'ok'                    # Every block came back as written.
PARTIAL = 'partial'          # Some blocks arrived, some did not.
FAILED = 'failed'            # Nothing came back as written.
UNREADABLE = 'unreadable'    # The track read back as nothing at all.
WRPROT = 'write-protected'   # Disk is protected; nothing was measured.

# Which cylinder to scribble on. Low, because nothing may assume how many
# cylinders the drive has, and drives run from about 37 upward.
TEST_CYLINDER = 2

# Cell periods to write, in microseconds, one per block. Kept inside the span
# a drive resolves comfortably -- see markers.py on long periods reading
# short -- and repeated so a drive writing only part of a track is caught.
#
# TWO PASSES, with the periods rotated between them, and both must come back
# right. One pass cannot tell a working write path from a dead one: a drive
# which writes nothing leaves whatever was on the track, and on the second
# run of this probe that is the pattern this probe wrote the time before. It
# would read back perfectly and pass. No stale track can satisfy both of
# these, because matching one guarantees failing the other.
PASSES_US = ((3.0, 4.5, 6.0, 3.0, 4.5, 6.0),
             (6.0, 3.0, 4.5, 6.0, 3.0, 4.5))

# How far a block's recovered period may sit from what was written.
#
# Measured on both sides, not guessed. A healthy write is exact and a failed
# one is wildly out, so the bar has a wide gap to sit in:
#
#     high-density media in its own drive     0.00%   (three runs)
#     double-density media in that drive      0.31%   (the marginal pairing)
#     a stale track from the other pass       33% or more
#
# The last line is the failure this catches. A drive which writes nothing
# leaves the previous pass's pattern, so a block expecting 4.5us reads 3.0 or
# 6.0 -- a third out at least. Five percent sits sixteen times above the worst
# honest reading and six times below the mildest dishonest one.
PERIOD_TOLERANCE = 0.05


class Block(NamedTuple):
    # Which pass wrote this block: both must come back correct.
    pass_index: int
    written_us: float
    read_us: Optional[float]

    @property
    def error(self) -> Optional[float]:
        '''How far the recovered period sits from the written one.'''
        if self.read_us is None or self.written_us <= 0:
            return None
        return abs(self.read_us - self.written_us) / self.written_us

    @property
    def arrived(self) -> bool:
        return self.error is not None and self.error <= PERIOD_TOLERANCE


class Result(NamedTuple):
    status: str
    detail: str
    blocks: Tuple[Block, ...] = ()

    @property
    def worst_error(self) -> Optional[float]:
        errors = [b.error for b in self.blocks if b.error is not None]
        return max(errors) if errors else None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'worst_error': self.worst_error,
            'blocks': [{'pass': b.pass_index, 'written_us': b.written_us,
                        'read_us': b.read_us, 'error': b.error,
                        'arrived': b.arrived} for b in self.blocks],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == OK:
            out('  Writes and reads back correctly.')
        elif self.status == PARTIAL:
            out('  FAULTY - only part of the track came back as written.')
        elif self.status == FAILED:
            out('  FAULTY - what was read back is not what was written.')
        elif self.status == WRPROT:
            out('  Not measured - the disk is write protected.')
        else:
            out('  UNKNOWN - the track read back as nothing.')
        for b in self.blocks:
            if b.read_us is None:
                out('  pass %d: wrote %.1f us, read nothing'
                    % (b.pass_index, b.written_us))
            else:
                out('  pass %d: wrote %.1f us, read %.3f us (%+.2f%%) %s'
                    % (b.pass_index, b.written_us, b.read_us,
                       (b.read_us / b.written_us - 1) * 100,
                       'ok' if b.arrived else 'WRONG'))
        out('  (%s)' % self.detail)


def interpret(blocks: Sequence[Block]) -> Result:
    '''Judge the read-back against what was written. Pure.'''

    error.check(len(blocks) > 0, 'write-verify: no blocks')

    if all(b.read_us is None for b in blocks):
        return Result(
            UNREADABLE,
            'The track read back as nothing at all. Either nothing was '
            'written or nothing can be read, and this probe cannot tell '
            'which; the index and head probes speak to the read side.',
            tuple(blocks))

    arrived = [b for b in blocks if b.arrived]

    if len(arrived) == len(blocks):
        worst = max(b.error or 0.0 for b in blocks)
        return Result(
            OK,
            'All %d blocks across both passes read back at the period they '
            'were written, the furthest out by %.2f%%. Two passes with the '
            'periods rotated, so a track left over from an earlier run '
            'cannot have produced this.' % (len(blocks), worst * 100),
            tuple(blocks))

    if not arrived:
        return Result(
            FAILED,
            'No block read back at the period it was written. The drive is '
            'not writing what it is given: check the write-enable line, and '
            'that the media suits this drive.', tuple(blocks))

    return Result(
        PARTIAL,
        'Only %d of %d blocks read back as written. A drive writing part of '
        'a track is worse than one writing none, since what it produces '
        'looks valid.' % (len(arrived), len(blocks)), tuple(blocks))


def _pattern(usb: USB.Unit, rev_ticks: float,
             periods: Sequence[float]) -> List[int]:
    '''Flux for one revolution: equal blocks at the chosen periods.'''
    per_block = rev_ticks / len(periods)
    out: List[int] = []
    for period_us in periods:
        period = round(period_us * 1e-6 * usb.sample_freq)
        error.check(period > 0, 'write-verify: period rounds to nothing')
        out += [period] * int(per_block / period)
    # Overfill: the write is cut off at the index pulse, and coming up short
    # would leave whatever was on the track before.
    out += [out[-1]] * 64
    return out


def _read_blocks(usb: USB.Unit, pass_index: int,
                 periods: Sequence[float]) -> List[Block]:
    '''Read the track back and recover each block's period.'''
    flux = usb.read_track(revs=1)
    error.check(len(flux.index_list) >= 2,
                'write-verify: need a full revolution between index pulses')

    start, span = flux.index_list[0], flux.index_list[1]
    per_block = span / len(periods)
    gathered: List[List[float]] = [[] for _ in periods]

    total = 0.0
    for interval in flux.list:
        total += interval
        if total < start:
            continue
        offset = total - start
        if offset >= span:
            break
        which = int(offset / per_block)
        if which < len(gathered):
            gathered[which].append(interval / flux.sample_freq * 1e6)

    blocks = []
    for period_us, intervals in zip(periods, gathered):
        # A median: a handful of spurious transitions in a block must not
        # drag its recovered period away from what the bulk of it says.
        read_us = statistics.median(intervals) if len(intervals) > 8 else None
        blocks.append(Block(pass_index, period_us, read_us))
    return blocks


def measure(usb: USB.Unit, cylinder: int = TEST_CYLINDER) -> Result:
    '''Write the pattern, read it back, and compare.'''

    try:
        usb.seek(cylinder, 0, check_trk0=False)
        flux = usb.read_track(revs=1)
        error.check(len(flux.index_list) > 0,
                    'write-verify: no index pulse, so a track cannot be timed')
        rev_ticks = flux.index_list[-1]
        blocks: List[Block] = []
        for pass_index, periods in enumerate(PASSES_US):
            usb.write_track(_pattern(usb, rev_ticks, periods), True)
            blocks += _read_blocks(usb, pass_index, periods)
        return interpret(blocks)

    except USB.CmdError as err:
        if consent.is_write_protected(err):
            return Result(
                WRPROT,
                'The disk is write protected, so the write path was not '
                'exercised at all. That is not a fault in the drive.')
        raise
    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def run(ctx) -> Result:
    ctx.report('  Writing a test pattern to cylinder %d and reading it back...'
               % TEST_CYLINDER)
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
