# greaseweazle/tools/probe/index_sensor.py
#
# Probe: is the drive's index signal present, and is it steady?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Sample the index signal for a while and look at the gaps between pulses.
# One healthy pulse per revolution gives a run of near-identical intervals;
# the two common faults each distort that run in their own way:
#
#     healthy      |-----|-----|-----|-----|-----|     even
#     dropped      |-----|-----------|-----|-----|     one gap ~2x
#     spurious     |-----|--|--|-----|-----|-----|     some gaps much shorter
#
# So the median interval is the revolution, and each gap is judged as a
# multiple of it. No nominal speed is assumed anywhere: the period is
# measured and reported, never checked against what some drive family ought
# to do. A drive spinning at an unusual speed is a fact about the drive, not
# an error, and only a comparison against that same drive's earlier profile
# can say whether it has changed.
#
# LIMIT, and it is a real one: a sensor which fires exactly twice per
# revolution, every revolution, produces perfectly even intervals at half
# the true period. That is indistinguishable from a drive spinning at twice
# the speed unless a nominal RPM is assumed, which is precisely what must
# not be done here. Intermittent double-triggering IS caught, because it
# makes the run uneven, and a consistent change in period will show up when
# profiles are compared. Steady doubling from the very first measurement
# will not.
#
# Needs the spindle turning. On drives which take the index from a hole in
# the media -- 5.25" among them -- it also needs a disk loaded.
#
# Absent pulses have several quite different causes. Four were measured on a
# 5.25" drive to find out which are separable, and the answer was humbling:
#
#                             pin 8 INDEX      pin 28   flux
#     disk loaded, normal     pulsing, 1.7%    high     688k
#     disk loaded, INVERTED   never asserted   LOW      ZERO
#     index hole TAPED OVER   never asserted   high     ZERO
#     no disk at all          never asserted   high     ZERO
#
# Note the write-protect column, and do not lean on it. An inverted disk reads
# protected only because its notch has swung out of view; a FLIPPY disk, cut
# with a second notch so the reverse side can be used, reads unprotected when
# inverted -- indistinguishable from an empty drive. Measured on one.
#
# Pin 34 is deliberately absent from that table. It read low in every one of
# these captures, and an earlier reading of high for a normal disk turned out
# to have been taken after a run which stepped the head hundreds of times.
# Pin 34 is DISK-CHANGE on this drive: it latches low when the disk is
# changed and clears on the first step with a disk loaded -- confirmed, one
# step took it from 100% low to 0%. So it reports nothing about readiness,
# and the apparent correlation was an artefact of comparing captures taken
# under different conditions.
#
# The inverted disk was DIRECTLY OBSERVED SPINNING and still returned no flux.
# The taped-over row then settled why, as a controlled experiment: same disk,
# same orientation, write-protect notch still in view, spinning as before, and
# the ONLY thing changed was a piece of tape over the index hole. Flux went
# from 688,000 transitions to zero, at three cylinders on both heads.
#
# So the drive is not failing to turn these disks, it is refusing to read
# them. READY here derives from seeing index pulses, and the read output is
# gated on READY: no index hole in view, therefore never ready, therefore no
# data at all, however fast the disk spins and however healthy the media.
#
# Which means a covered or damaged index hole is not a minor complaint on
# this drive -- it takes the disk from perfectly readable to entirely
# unreadable. Worth saying plainly to anyone who meets it.
#
# Note pin 28 DOES separate the two -- an inverted disk hides its write-protect
# notch, so it reads protected, while an empty drive reads unprotected. The
# drive can plainly tell the cases apart; it just declines to say so on any
# line this probe can use. Pin 28 is no help in general either, since a
# normally-loaded protected disk reads the same as an inverted one, and an
# unprotected one the same as an empty drive.
#
# MEDIA REQUIREMENT, and the table above is void without it: the disk must be
# genuinely double-sided, carrying ONE write-protect notch and ONE index hole.
# Commodore, Apple and Atari owners routinely cut a second notch -- and often
# punched a second index hole -- into single-sided disks so the media could be
# turned over and the reverse side used. Inverting such a "flippy" disk is not
# this experiment at all: the second index hole restores index pulses, so the
# drive comes ready and hands over flux, and the second notch makes the
# write-protect line read unprotected, which is precisely what an empty drive
# reads. Every distinction drawn above collapses. Anyone repeating this on
# another drive must check the media first.
#
# THE GATING IS NOT UNIVERSAL, which a second drive settled. Everything above
# came from a 5.25" HD drive. A 360k drive of the earlier era, given media
# with no index hole in view -- a flippy disk notched on both sides but with
# only one index hole, inserted so that hole faces away -- handed over flux
# perfectly happily:
#
#                     pin 8 INDEX   pin 28   flux
#     HD drive        none          high     ZERO       reads gated
#     360k drive      none          high     68k-94k    reads not gated
#
# So a drive which does not gate reads makes the loaded-but-no-index case
# reachable and separable, exactly as hoped, while one which does collapses
# it into the empty-drive case. Both behaviours are real and the probe has
# to serve both, which is why the no-flux branch names several causes rather
# than picking one.
#
# Two further ideas were tried and abandoned. Sampling pin 8 statically looked
# promising and distinguishes nothing, because the drive holds the interface
# line inactive whatever its own detector sees. Pin 34 looked like it tracked
# readiness and does not; see above.
#
# So no index plus no flux means only that the drive will not read whatever is
# in there -- NOT that nothing is turning, which the taped disk disproved by
# spinning throughout. The report must say that rather than announce an empty
# drive: telling somebody their drive is empty when their disk is merely in
# backwards, or has a damaged index hole, is worse than admitting that from
# out here the cases look alike.
#
# No index WITH flux is a different matter -- something is being read, so the
# index hole itself is the problem rather than the drive's willingness. That
# branch is unreachable on a drive which gates reads: the taped-hole case
# proved as much, arriving with zero flux rather than the flux the branch
# expects. On a drive which does not gate it fires exactly as intended, and
# it has now been seen doing so, on a 360k drive holding a flippy disk with
# its single index hole facing the wrong way.

import statistics
from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core, profile

name = 'index-sensor'
title = 'Index Sensor'
summary = 'Whether the index signal is present and steady'
depends_on: Tuple[str, ...] = ()
destructive = False
needs_motor = True
needs_media = core.MEDIA_ANY
wears_drive = False

# Speed and jitter are measurements and move a little; the status, and
# whether the signal is there at all, are not and must not.
tolerances = {
    'period_ms': profile.Tolerance(relative=0.02),
    'rpm': profile.Tolerance(relative=0.02),
    'jitter_pct': profile.Tolerance(absolute=0.5),
    'intervals_ms': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
OK = 'ok'                        # One steady pulse per revolution.
ABSENT = 'absent'                # No pulses, and no way to tell why.
NOT_READABLE = 'not-readable'    # No pulses and no flux: drive will not read.
NO_INDEX_HOLE = 'no-index-hole'  # No pulses but flux present: media is there.
SPURIOUS = 'spurious'            # Extra pulses: some gaps far too short.
DROPPED = 'dropped'              # Missed pulses: some gaps a multiple too long.
JITTERY = 'jittery'              # One per revolution, but unsteady.
TOO_FEW = 'too-few'              # Not enough revolutions to judge.

# How long to watch. Long enough for several revolutions at any plausible
# speed without assuming one, short enough to keep the flux capture modest.
SAMPLE_SECONDS = 1.5

# Fewest complete revolutions worth drawing a conclusion from.
MIN_INTERVALS = 4

# A gap this much shorter than the median is an extra pulse, and this much
# longer is a missed one. Set far outside any credible speed variation, so
# that ordinary wow and flutter is reported as jitter rather than misread as
# a structural fault.
SHORT_GAP = 0.75
LONG_GAP = 1.5

# Peak-to-peak variation, as a percentage of the period, beyond which the
# signal is called unsteady. This is about the spindle and the sensor, not
# about any drive model: a healthy drive of any vintage holds well inside it.
JITTER_LIMIT_PCT = 3.0


class Result(NamedTuple):
    status: str
    detail: str
    # Median interval between pulses, in seconds, or None if there were none.
    period: Optional[float] = None
    # Peak-to-peak spread of the good intervals, as a percentage of period.
    jitter_pct: Optional[float] = None
    intervals: Tuple[float, ...] = ()

    @property
    def rpm(self) -> Optional[float]:
        '''Measured speed. Reported, never checked against an expectation.'''
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
            'period_ms': None if self.period is None else self.period * 1e3,
            'rpm': self.rpm,
            'jitter_pct': self.jitter_pct,
            'intervals_ms': [i * 1e3 for i in self.intervals],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == OK:
            out('  Present and steady.')
        elif self.status == ABSENT:
            out('  NO INDEX SIGNAL.')
        elif self.status == NOT_READABLE:
            out('  No readable disk in the drive.')
        elif self.status == NO_INDEX_HOLE:
            out('  NO INDEX SIGNAL, but a disk is loaded.')
        elif self.status == SPURIOUS:
            out('  FAULTY - extra pulses.')
        elif self.status == DROPPED:
            out('  FAULTY - pulses being missed.')
        elif self.status == JITTERY:
            out('  UNSTEADY.')
        else:
            out('  Inconclusive.')
        if self.period is not None:
            rpm = self.rpm
            assert rpm is not None
            out('  Period: %.3f ms  (%.2f rpm as measured)'
                % (self.period * 1e3, rpm))
        if self.jitter_pct is not None:
            out('  Jitter: %.2f%% peak-to-peak' % self.jitter_pct)
        out('  (%s)' % self.detail)


def interpret(intervals: Sequence[float],
              flux_seen: Optional[bool] = None) -> Result:
    '''Judge the index signal from the gaps between pulses. Pure.

    'intervals' is the time in seconds between consecutive index pulses,
    complete revolutions only. 'flux_seen' says whether the head read
    anything at all, which is what tells an empty drive from a loaded one
    when there are no pulses; None if that was not established.
    '''
    if not intervals:
        if flux_seen is False:
            return Result(
                NOT_READABLE,
                'No index pulses and no flux at all, so there is nothing '
                'here the drive is willing to read. Either the drive is '
                'empty, or a disk is loaded whose index hole it cannot see '
                '-- upside down, or taped or damaged over. Measured on one '
                'drive: covering the index hole of a healthy disk took it '
                'from 688,000 flux transitions to none, because the drive '
                'gates its read output until index pulses arrive. Check a '
                'disk is loaded the right way up with its index hole clear, '
                'then run this again.')
        if flux_seen is True:
            return Result(
                NO_INDEX_HOLE,
                'No index pulses, but the head is reading flux, so a disk '
                'IS loaded and its index hole never passes the sensor. '
                'Check the disk is the right way up and its index hole is '
                'clear; failing that, either the sensor has failed or this '
                'drive has none, as Apple 5.25" drives do not.')
        return Result(
            ABSENT,
            'No index pulses seen, and whether a disk is loaded was not '
            'established. An empty drive, an inverted disk and a failed '
            'sensor all look like this from the index line alone.')

    if len(intervals) < MIN_INTERVALS:
        return Result(
            TOO_FEW,
            'Only %d complete revolution(s) seen, too few to judge the '
            'signal.' % len(intervals),
            intervals=tuple(intervals))

    period = statistics.median(intervals)
    error.check(period > 0, 'index: median interval is not positive')

    short = [i for i in intervals if i < period * SHORT_GAP]
    long_ = [i for i in intervals if i > period * LONG_GAP]
    good = [i for i in intervals if period * SHORT_GAP <= i <= period * LONG_GAP]

    jitter = ((max(good) - min(good)) / period * 100.0) if good else None

    def measured(status: str, detail: str) -> Result:
        return Result(status, detail, period, jitter, tuple(intervals))

    if short:
        return measured(
            SPURIOUS,
            '%d of %d gaps were far shorter than the revolution, so the '
            'sensor is firing more than once per turn.'
            % (len(short), len(intervals)))

    if long_:
        return measured(
            DROPPED,
            '%d of %d gaps were around a whole revolution too long, so '
            'pulses are being missed.'
            % (len(long_), len(intervals)))

    assert jitter is not None
    if jitter > JITTER_LIMIT_PCT:
        return measured(
            JITTERY,
            'One pulse per revolution, but the period varies by %.2f%%, '
            'beyond the %.1f%% a healthy spindle and sensor hold.'
            % (jitter, JITTER_LIMIT_PCT))

    return measured(
        OK,
        'One pulse per revolution across %d revolutions.'
        % len(intervals))


def measure(usb: USB.Unit, seconds: float = SAMPLE_SECONDS) -> Result:
    '''Watch the index signal for a fixed time.'''

    error.check(seconds > 0, 'index: sample time must be positive')

    # Read by TIME rather than by revolutions. A revolution-counted read
    # needs index pulses to know when to stop, so it cannot be used to find
    # out whether there are any: a drive without the signal would fail the
    # read instead of reporting an absent sensor.
    #
    # This hauls back every flux transition in the sample window -- some
    # 700,000 of them on a formatted disk -- purely to learn the handful of
    # index times buried in it. The firmware has a GetIndexTimes command
    # which would presumably answer directly, but usb.py implements no host
    # side for it, and guessing at its arguments and reply format is not
    # worth the second or so this costs.
    ticks = round(seconds * usb.sample_freq)
    flux = usb.read_track(revs=0, ticks=ticks)

    # Flux tells a disk the drive will read from one it will not. It does NOT
    # tell an empty drive from an inverted disk: measured, both return zero,
    # because the drive gates its read output when it is not ready.
    flux_seen = len(flux.list) > 0

    # index_list holds the gaps between pulses, but the first entry runs from
    # the start of the capture to the first pulse -- a part revolution, not a
    # whole one. Dropping it costs one revolution and saves a false jitter
    # reading. (rpm.py takes index_list[-1] for the same reason.)
    intervals = [t / flux.sample_freq for t in flux.index_list[1:]]

    return interpret(intervals, flux_seen)


def run(ctx) -> Result:
    ctx.report('  Watching the index signal for %.1f seconds...'
               % SAMPLE_SECONDS)
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
