# greaseweazle/tools/probe/double_step.py
#
# Probe: does this disk need double-stepping in this drive?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# A disk written at half the drive's track pitch -- 48tpi media in a 96tpi
# drive -- puts one written track under two of the drive's cylinder
# positions, so stepping two at a time is needed to read it.
#
# THE DISK SAYS SO ITSELF. Every sector header on an IBM-format disk carries
# the cylinder number the formatting drive believed it was writing. Read the
# drive's cylinder 4 and ask the headers what they are: on matched media they
# answer 4, and on half-pitch media they answer 2. No thresholds, no
# calibration, no noise floor -- the answer is written on the disk.
#
# Measured on a 360k disk formatted in its own 40-track drive and then read in
# an 80-track one:
#
#     drive cyl  0 -> headers say c=[0]     9 sectors
#     drive cyl  1 -> headers say c=[0, 1]  7 sectors
#     drive cyl  4 -> headers say c=[2]     9 sectors
#     drive cyl  5 -> headers say c=[2, 3]  7 sectors
#
# The odd cylinders are the geometry showing through: a 96tpi step is half a
# 48tpi pitch, so an even cylinder sits over one written track while an odd
# one straddles TWO and returns sectors from both. That is corroboration, and
# it is also the reason the previous approach failed.
#
# THREE FLUX-COMPARISON ATTEMPTS WERE DISCARDED BEFORE THIS, and the history
# is worth keeping so a fourth is not attempted.
#
#   1. Matching flux intervals position by position. A same-track control
#      shifted by ONE interval scored identically to an honest reread: MFM
#      intervals are quantised with one value dominant, so it was measuring
#      how often the common value coincided with itself.
#   2. Counting transitions per angular segment and comparing the density
#      profiles. Random data has constant density, so every track of a
#      formatted disk matched every other; it announced that a freshly
#      formatted disk in its own drive needed double-stepping.
#   3. Comparing where individual transitions fall. This one genuinely tells
#      tracks apart -- 95% shared for the same track against 56% for
#      different ones -- and still cannot answer the question, because
#      adjacent cylinders on half-pitch media are NOT alike. The odd cylinder
#      straddles two written tracks and shares about 70% with its neighbour,
#      against 60% for tracks two apart: a ten-point signal, far too weak,
#      and the premise that they would read identically was simply wrong.
#
# Each failed for its own reason, and each looked convincing on whatever
# media it had been calibrated against. The disk carrying its own answer
# needs no calibration at all.
#
# IT REQUIRES A PC-FORMATTED DISK, which is a requirement rather than an
# apology. It reads IBM MFM and FM -- PC, Atari ST, Amstrad, most CP/M --
# and not Amiga, Commodore or Apple GCR, whose track numbers are in
# encodings this does not decode. Asking for suitable media is a small thing
# to ask of somebody characterising a drive, and it buys an answer written
# on the disk instead of one inferred from a threshold. Given unsuitable
# media it says so and stops, rather than guessing.
#
# NON-DESTRUCTIVE, necessarily: writing would destroy the very thing being
# measured.

from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.codec import codec
from greaseweazle.tools.probe import core, profile

name = 'double-step'
title = 'Double-Step'
summary = 'Whether the loaded disk needs double-stepping in this drive'
depends_on = ('index-sensor',)
destructive = False
needs_motor = True
needs_media = core.MEDIA_FORMATTED
wears_drive = False

tolerances = {
    'pairs': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
MATCHED = 'matched'        # Headers name the cylinder they are on.
HALF_PITCH = 'half-pitch'  # Headers name half it: double-step.
NO_HEADERS = 'no-headers'  # Nothing decodable, so nothing to go on.
UNCLEAR = 'unclear'        # The cylinders disagreed.

# Cylinders to read. Both members of a pair, because the odd one corroborates:
# on half-pitch media it straddles two written tracks and returns headers from
# both, which nothing else explains. Kept low, since drives run from about 37
# cylinders upward and nothing may assume more.
SAMPLE_PAIRS = ((4, 5), (8, 9), (12, 13))

# The format-agnostic IBM decoder: finds sector headers without being told the
# layout, so no disk geometry is assumed either.
SCAN_FORMAT = 'ibm.scan'


class Reading(NamedTuple):
    cylinder: int
    # Cylinder numbers written in the headers found there.
    headers: Tuple[int, ...] = ()

    @property
    def verdict(self) -> Optional[str]:
        '''"matched", "half", or None if this cylinder could not say.'''
        if not self.headers:
            return None
        if set(self.headers) == {self.cylinder}:
            return 'matched'
        half = self.cylinder // 2
        if self.cylinder % 2 == 0:
            if set(self.headers) == {half}:
                return 'half'
        # An odd cylinder over half-pitch media sits between two written
        # tracks and returns headers from both.
        elif set(self.headers) <= {half, half + 1}:
            return 'half'
        return None


class Result(NamedTuple):
    status: str
    detail: str
    readings: Tuple[Reading, ...] = ()

    @property
    def double_step(self) -> Optional[bool]:
        if self.status == HALF_PITCH:
            return True
        if self.status == MATCHED:
            return False
        return None

    @property
    def ok(self) -> bool:
        return self.status in (MATCHED, HALF_PITCH)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'double_step': self.double_step,
            'readings': [{'cylinder': r.cylinder,
                          'headers': list(r.headers),
                          'verdict': r.verdict} for r in self.readings],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == HALF_PITCH:
            out('  Double-stepping needed for this disk.')
        elif self.status == MATCHED:
            out('  No double-stepping needed for this disk.')
        elif self.status == NO_HEADERS:
            out('  UNKNOWN - no sector headers could be read.')
        else:
            out('  UNCLEAR - the cylinders disagreed.')
        for r in self.readings:
            out('  cyl %2d: headers say %-12s -> %s'
                % (r.cylinder,
                   ','.join(str(c) for c in r.headers) if r.headers
                   else 'nothing',
                   r.verdict or 'unclear'))
        out('  (%s)' % self.detail)


def interpret(readings: Sequence[Reading]) -> Result:
    '''Decide from what the sector headers call themselves. Pure.'''

    error.check(len(readings) > 0, 'double-step: no cylinders read')

    verdicts = [r.verdict for r in readings if r.verdict is not None]

    if not verdicts:
        return Result(
            NO_HEADERS,
            'No sector headers could be read, so nothing is claimed. This '
            'needs a PC-formatted disk -- or any IBM MFM/FM one, such as '
            'Atari ST, Amstrad or most CP/M. A blank disk carries no '
            'headers, and Amiga, Commodore and Apple GCR are in encodings '
            'this does not decode.', tuple(readings))

    if all(v == 'half' for v in verdicts):
        return Result(
            HALF_PITCH,
            'Every cylinder read back headers naming half its number, so one '
            'written track covers two of this drive\'s cylinder positions. '
            'The disk was written at half this drive\'s pitch and needs '
            'stepping two cylinders at a time.', tuple(readings))

    if all(v == 'matched' for v in verdicts):
        return Result(
            MATCHED,
            'Every cylinder read back headers naming itself, so the disk was '
            'written at this drive\'s own pitch and wants single stepping.',
            tuple(readings))

    return Result(
        UNCLEAR,
        'The cylinders did not agree: %s. That fits neither a disk at this '
        'drive\'s pitch nor one at half it, so nothing is claimed.'
        % ', '.join('%d %s' % (r.cylinder, r.verdict or 'unclear')
                    for r in readings),
        tuple(readings))


def _headers(usb: USB.Unit, scan, cylinder: int) -> Tuple[int, ...]:
    """Cylinder numbers written in the sector headers found at this cylinder.

    The decoder is told nothing about the disk, so on the FIRST cylinder it
    searches every data rate and speed it knows, decoding the flux afresh for
    each -- which takes appreciable time. It caches what worked and tries that
    first thereafter, so later cylinders are quick. The caller says so before
    starting, since a minute of silence and a hang look identical.
    """
    usb.seek(cylinder, 0, check_trk0=False)
    # A discarded read, as elsewhere: cheap insurance for a drive which
    # wants a moment after a seek.
    usb.read_track(revs=1)
    track = scan.mk_track(cylinder, 0)
    track.decode_flux(usb.read_track(revs=2))
    sectors = getattr(track.track, 'sectors', [])
    return tuple(sorted(set(s.idam.c for s in sectors)))


def measure(usb: USB.Unit,
            sample_pairs: Sequence[Tuple[int, int]] = SAMPLE_PAIRS,
            report: Callable[[str], None] = lambda line: None) -> Result:
    """Read the sector headers and ask what cylinder they think they are."""

    error.check(len(sample_pairs) > 0, 'double-step: no cylinders to read')

    scan = codec.mk_trackdef(SCAN_FORMAT)
    cylinders = [c for pair in sample_pairs for c in pair]
    try:
        readings = []
        for n, cylinder in enumerate(cylinders):
            report('    cylinder %d of %d (cyl %d)...'
                   % (n + 1, len(cylinders), cylinder))
            found = _headers(usb, scan, cylinder)
            report('      headers say %s'
                   % (','.join(str(c) for c in found) if found
                      else 'nothing decodable here'))
            readings.append(Reading(cylinder, found))
        return interpret(readings)
    finally:
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def run(ctx) -> Result:
    ctx.report('  Reading sector headers from %d cylinders. The first has to'
               ' find the disk\'s data rate and speed by trying each in turn,'
               ' which takes a minute or two; the rest reuse what it found.'
               % (len(SAMPLE_PAIRS) * 2))
    return measure(ctx.usb, report=ctx.report)

# Local variables:
# python-indent: 4
# End:
