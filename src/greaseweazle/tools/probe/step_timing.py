# greaseweazle/tools/probe/step_timing.py
#
# Probe: how fast can this drive be stepped, and how long does it need to
# settle afterwards?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# The step delay and settle time in "gw delays" are usually copied off a
# datasheet, or left at a default chosen to suit everything. This measures
# them instead, for the drive actually attached.
#
# STEP RATE. Drive the head outward at the candidate rate, then count the way
# home ONE CYLINDER AT A TIME at the known-good rate, watching /TRK0. If the
# head really made every step it was given, /TRK0 arrives after exactly as
# many steps as were issued. Fewer means the head fell behind, which is what
# stepping too fast does.
#
# Testing it the obvious way -- seek out, seek back, see whether /TRK0 is
# asserted -- does not work, and it is worth saying why. Steps lost on the way
# OUT leave the head short, so the return drives it into the track 0 stop and
# /TRK0 asserts anyway. The test passes on a drive that lost half its steps.
# Counting the way home measures the position instead of asking a question
# whose answer is yes either way.
#
# SETTLE TIME. Set a candidate settle, seek in from elsewhere, read at once,
# and compare what comes back against a settled reference read of that track.
# A head still moving reads the wrong thing, so agreement means it had
# arrived.
#
# COMPARING BY TRANSITION COUNT DOES NOT WORK, and the wrong turn is worth
# recording. read_track returns everything from the read starting to two
# index pulses having passed, so its total count depends on where the disk
# happens to be when the read begins -- and the settle delay shifts exactly
# that. Counted that way the first read after a seek came back thirty percent
# short and appeared to prove a settling problem, and settle times of 15, 30,
# 60, 120 and 250 ms gave shortfalls of 30, 38, 6, 42 and 20 percent: no
# order at all, because it was measuring rotational phase.
#
# Counted over exactly one revolution, index to index, the first read after a
# seek is 37,192 transitions against 37,191 settled. There was never a
# shortfall. So the comparison is by CONTENT, which is what being off-track
# would actually spoil.
#
# NOTHING IS ASSUMED ABOUT THE DRIVE. The search starts from whatever the
# device is currently set to -- which works, or nothing else would have got
# this far -- and looks for the smallest value that still holds. No datasheet
# figure appears as a bound, a default or an expectation.
#
# AND NOTHING IS CONCLUDED BEYOND IT. What comes out belongs to the drive that
# was measured and to nothing else: another drive of the same model, or the
# same drive after a decade of use, will answer differently. That is the point
# of measuring rather than looking it up, and it is why the figures go into a
# profile to be compared against that same drive later rather than against a
# specification. No number this probe produces should be carried to another
# drive, and none of the measurements quoted in these comments is a threshold
# -- they are there to show where the thresholds came from.
#
# A MARGINAL SETTING PASSES ONCE BY LUCK, so each candidate must hold over
# several attempts before it is believed. The whole point is to find where
# reliability ends, and one success proves nothing there.
#
# DEVICE STATE IS RESTORED. This writes the delay parameters, which persist
# beyond the probe and affect every later command; leaving them at some value
# a search happened to stop on would break seeking for the rest of the
# session. The originals are read up front and put back in a finally, on the
# way out of an exception or an interrupt alike.
#
# A finally does not survive the process being KILLED, though, and that is
# not hypothetical: a debugging run cut short left the drive with no settle
# delay at all, which then quietly became the starting point of the next
# measurement. So the original values are printed before anything is changed,
# and can be put back by hand with "gw delays --step N --settle N".
#
# It steps a good deal -- something over a thousand steps for a full search --
# but never into the stop, which is the difference between this and max-track.
# That is ordinary seeking, of the sort reading a disk does repeatedly.

from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.delays import Delays
from greaseweazle.tools.probe import core, fluxcmp, profile
from greaseweazle.tools.probe.pins import trk0_asserted

name = 'step-timing'
title = 'Step Rate and Settle Time'
summary = 'The fastest stepping and shortest settle this drive tolerates'
depends_on = ('trk0-sensor',)
destructive = False
needs_media = core.MEDIA_ANY
needs_motor = True
# Unlike every other probe, this one finds its answer by GOING OVER the edge:
# it is looking for where the drive stops keeping up, which means making it
# fail. On an older drive a failed trial can leave the head far enough out of
# step that /TRK0 is lost, and once that happens the firmware will not move
# the head at all -- no seek of any kind, positive or negative, is accepted --
# so nothing at this end can recover it. That cost a physical reset once. It
# is marked as wearing not for wear but so that it is never run on anybody's
# behalf, only when asked for by name.
wears_drive = True

tolerances = {
    # Where reliability ends is not a sharp line, and a search lands a step
    # either side of it from one run to the next.
    'step_us': profile.Tolerance(relative=0.35),
    'settle_ms': profile.Tolerance(relative=0.35),
    'step_trials': profile.IGNORED,
    'settle_trials': profile.IGNORED,
    'detail': profile.IGNORED,
}

# Outcomes.
OK = 'ok'                      # Both measured.
STEP_ONLY = 'step-only'        # Settle could not be measured.
NO_MARGIN = 'no-margin'        # Even the current setting failed.
UNREADABLE = 'unreadable'      # Nothing to read, so no settle measurement.
ABORTED = 'aborted'            # Position was lost; the search stopped.

# How far out to send the head. Comfortably inside the smallest drive there
# is -- they run from about 37 cylinders upward -- and far enough that a
# dropped step has somewhere to hide.
TRAVEL = 20

# How many times a candidate must hold before it is believed.
ATTEMPTS = 2

# How the step search descends, and where it gives up.
#
# GRADUALLY, never by bisection. Bisection is right for a cheap predicate and
# wrong when a failed trial has physical consequences: it jumps to extremes,
# and after the floor was added as its first probe it tried 200us on a drive
# working at 10000 -- a twentieth of the rate it was managing. That
# desynchronised the head badly enough to lose /TRK0 entirely. Stepping down
# by a modest factor from a value that just worked means the worst trial ever
# attempted is one small increment past the edge.
DESCENT = 0.85

# The floor follows the drive rather than being invented. A rate the drive is
# already running at is the only value known to work, so the search will not
# go below a tenth of it whatever else happens. This is a backstop; the
# gradual descent is what keeps the search safe.
FLOOR_FRACTION = 0.10

MIN_SETTLE_MS = 0

# Settle is sampled at a few values rather than searched for a boundary.
# Unlike a step rate it is not a cliff -- a head either has arrived or has
# not -- so the useful question is whether the drive needs any settling at
# all and roughly how much, which these answer. Descending geometrically
# instead tried eleven values, each with a long seek and an expensive
# comparison, and spent minutes of continuous seeking to say the same thing.
SETTLE_FRACTIONS = (1.0, 0.5, 0.25, 0.0)

# Far enough that the head must move and arrive, no further. The step search
# needs distance because distance is where lost steps hide; this does not,
# and twenty cylinders each way was simply copied from it.
SETTLE_TRAVEL = 4

# How much of a hurried read must match the settled reference.
#
# Two reads of one track share about 95% of their transitions, and different
# tracks about 56%, so anywhere between the two separates an arrived head
# from a moving one. Where exactly matters more than it looks: repeated reads
# of the SAME track ranged from 89.7% to 97.4% here, so a bar at 85% sits
# close enough to that spread for trials to fail by luck -- which they did,
# producing a settle requirement of 4ms on a drive that reads perfectly with
# no settle delay at all. At 80% the noise cannot reach it while an off-track
# read, well below 70%, still cannot clear it.
SETTLE_MATCH = 0.80


# What a single trial came to.
HELD = 'held'          # Every step arrived.
FELL_SHORT = 'short'   # Steps were lost, but the head found its way home.
LOST = 'lost'          # /TRK0 could not be found afterwards: stop everything.


class Trial(NamedTuple):
    value: int
    passed: bool


class Result(NamedTuple):
    status: str
    detail: str
    # Smallest values which held, in microseconds and milliseconds.
    step_us: Optional[int] = None
    settle_ms: Optional[int] = None
    # What the device was set to when the probe started.
    was_step_us: Optional[int] = None
    was_settle_ms: Optional[int] = None
    step_trials: Tuple[Trial, ...] = ()
    settle_trials: Tuple[Trial, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (OK, STEP_ONLY)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'step_us': self.step_us,
            'settle_ms': self.settle_ms,
            'was_step_us': self.was_step_us,
            'was_settle_ms': self.was_settle_ms,
            'step_trials': [[t.value, t.passed] for t in self.step_trials],
            'settle_trials': [[t.value, t.passed] for t in self.settle_trials],
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == NO_MARGIN:
            out('  UNKNOWN - the drive would not step reliably even at its'
                ' current setting.')
        elif self.status == UNREADABLE:
            out('  Step rate measured; settle time could not be.')
        elif self.status == STEP_ONLY:
            out('  Step rate measured; settle time could not be.')
        elif self.status == ABORTED:
            out('  INCOMPLETE - the head was lost and the search stopped.')
        else:
            out('  Measured.')
        if self.step_us is not None:
            out('  Step delay:  %d us (currently set to %s)'
                % (self.step_us,
                   '%d us' % self.was_step_us if self.was_step_us else '?'))
        if self.settle_ms is not None:
            out('  Settle time: %d ms (currently set to %s)'
                % (self.settle_ms,
                   '%d ms' % self.was_settle_ms if self.was_settle_ms else '?'))
        for label, trials in (('step', self.step_trials),
                              ('settle', self.settle_trials)):
            if trials:
                out('  %-7s tried: %s'
                    % (label, ', '.join('%d%s' % (t.value,
                                                  '' if t.passed else '(no)')
                                        for t in trials)))
        out('  (%s)' % self.detail)


def minimum_passing(trials: Sequence[Trial]) -> Optional[int]:
    '''Smallest value which held. Pure.'''
    passed = [t.value for t in trials if t.passed]
    return min(passed) if passed else None


def contradictions(trials: Sequence[Trial]) -> List[Tuple[int, int]]:
    '''Pairs where a larger value failed while a smaller one held. Pure.

    More time can only help, so this should be empty. It is checked rather
    than assumed: a drive which fails at a generous setting and passes at a
    tight one has not been measured, it has been guessed at, and reporting a
    number from that would be worse than reporting none.
    '''
    out = []
    for bigger in trials:
        if bigger.passed:
            continue
        for smaller in trials:
            if smaller.passed and smaller.value < bigger.value:
                out.append((smaller.value, bigger.value))
    return out


def interpret(step_trials: Sequence[Trial],
              settle_trials: Sequence[Trial],
              was_step_us: Optional[int] = None,
              was_settle_ms: Optional[int] = None,
              aborted: bool = False) -> Result:
    '''Turn the two searches into a result. Pure.'''

    error.check(len(step_trials) > 0, 'step-timing: no step trials')

    step = minimum_passing(step_trials)

    def result(status: str, detail: str, step_us: Optional[int] = None,
               settle_ms: Optional[int] = None) -> Result:
        return Result(status, detail, step_us, settle_ms,
                      was_step_us, was_settle_ms,
                      tuple(step_trials), tuple(settle_trials))

    if step is None:
        return result(
            NO_MARGIN,
            'The drive did not step reliably even at the delay it was '
            'already set to, so there is no margin to measure. Suspect the '
            'drive or the cable rather than the timing.')

    if aborted:
        held = minimum_passing(step_trials)
        return result(
            ABORTED,
            'The head was lost part-way through and the search stopped. '
            'Stepping held down to %s before that. A drive which falls far '
            'enough behind loses track 0 altogether, after which the '
            'firmware refuses to move it at all and only power-cycling the '
            'drive will recover it -- so nothing more was attempted.'
            % ('%d us' % held if held else 'nothing measurable'),
            step_us=held)

    quarrel = contradictions(step_trials) + contradictions(settle_trials)
    if quarrel:
        return result(
            NO_MARGIN,
            'The trials contradict themselves -- %s -- so nothing here was '
            'measured reliably. More time cannot make stepping worse, so a '
            'result like this means the drive is behaving erratically.'
            % '; '.join('%d held but %d did not' % pair for pair in quarrel))

    settle = minimum_passing(settle_trials) if settle_trials else None
    if settle is None:
        return result(
            STEP_ONLY,
            'Stepping held down to %d us. The settle time could not be '
            'measured, which needs a track the drive can read.' % step,
            step_us=step)

    if settle <= MIN_SETTLE_MS:
        return result(
            OK,
            'Stepping held down to %d us. Reads were correct even with no '
            'settle delay at all, so this drive has no settle requirement '
            'that can be measured from here -- it arrives faster than the '
            'shortest delay there is to ask for.' % step,
            step_us=step, settle_ms=settle)

    return result(
        OK,
        'Stepping held down to %d us and settling to %d ms, each over %d '
        'attempts.' % (step, settle, ATTEMPTS),
        step_us=step, settle_ms=settle)


def _descend(trial: Callable[[int], str], start: int, floor: int,
             trials: List[Trial]) -> bool:
    '''Step down from a working value until one does not hold.

    Returns False if the head was lost, in which case nothing further should
    be attempted. Each candidate is a modest fraction below one which just
    worked, so the furthest this ever goes past the edge is one increment --
    unlike a bisection, which reaches the extremes immediately and did.
    '''
    outcome = trial(start)
    trials.append(Trial(start, outcome == HELD))
    if outcome == LOST:
        return False
    if outcome != HELD:
        return True

    candidate = int(start * DESCENT)
    while candidate >= floor:
        outcome = trial(candidate)
        trials.append(Trial(candidate, outcome == HELD))
        if outcome == LOST:
            return False
        if outcome != HELD:
            # First failure ends it. Confirming the edge from below would
            # mean crossing it again for no more information.
            return True
        candidate = int(candidate * DESCENT)
    return True


def _steps_home(usb: USB.Unit, delays: Delays, safe_step_us: int,
                travel: int) -> int:
    '''Count single steps back to /TRK0, at a rate known to work.

    Stops at cylinder 0 rather than stepping past it, so the firmware's idea
    of where the head is never goes negative -- from which it cannot be
    brought back into agreement, there being no way to tell the firmware
    where the head actually is.
    '''
    delays.step = safe_step_us
    delays.update()
    found = travel + 1
    for stepped in range(1, travel + 1):
        usb.seek(travel - stepped, 0, check_trk0=False)
        if trk0_asserted(usb):
            found = stepped
            break

    # Bring the two back into agreement before leaving. A trial which lost
    # steps finds /TRK0 EARLY, so the firmware believes the head is at some
    # positive cylinder while it is really at zero; seeking to zero steps it
    # inward against the stop, which costs nothing and leaves both at zero.
    #
    # Skipping this is what made the drive appear to have an intermittent
    # Track 0 sensor: every run after a failed trial started from a position
    # the firmware had wrong, and reported no signal at cylinder 0.
    try:
        usb.seek(0, 0, check_trk0=False)
    except USB.CmdError:
        return travel + 1
    return found


def _step_trial(usb: USB.Unit, delays: Delays, safe_step_us: int,
                candidate: int, travel: int = TRAVEL) -> str:
    '''Try one step rate. HELD, FELL_SHORT, or LOST.

    LOST means /TRK0 was not found on the way home, so where the head is can
    no longer be established. Nothing further should be attempted after that:
    the firmware refuses every seek once it cannot find track 0, so carrying
    on only makes the position worse without measuring anything.
    '''
    outcome = HELD
    for _ in range(ATTEMPTS):
        delays.step = safe_step_us
        delays.update()
        try:
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            return LOST
        delays.step = candidate
        delays.update()
        try:
            usb.seek(travel, 0, check_trk0=False)
        except USB.CmdError:
            return LOST
        stepped = _steps_home(usb, delays, safe_step_us, travel)
        if stepped > travel:
            return LOST
        if stepped != travel:
            outcome = FELL_SHORT
    return outcome


def _one_revolution(usb: USB.Unit) -> List[float]:
    '''Transition times from the index pulse, for the current track.'''
    flux = usb.read_track(revs=1)
    if len(flux.index_list) < 2:
        return []
    start, span = flux.index_list[0], flux.index_list[1]
    out: List[float] = []
    total = 0.0
    for interval in flux.list:
        total += interval
        if total < start:
            continue
        if total - start > span:
            break
        out.append((total - start) / flux.sample_freq * 1e6)
    return out


def _settle_attempt(usb: USB.Unit, delays: Delays, candidate: int,
                    reference: Sequence[float], cylinder: int,
                    away: int) -> bool:
    '''One try: seek away, seek back, and see whether the read matches.'''
    delays.seek_settle = candidate
    delays.update()
    usb.seek(away, 0, check_trk0=False)
    usb.seek(cylinder, 0, check_trk0=False)
    return (fluxcmp.similarity(reference, _one_revolution(usb))
            >= SETTLE_MATCH)


def _settle_holds(usb: USB.Unit, delays: Delays, candidate: int,
                  reference: Sequence[float], cylinder: int,
                  away: int) -> bool:
    '''True if a read taken straight after a seek matches a settled one.

    One attempt when it succeeds, a second only to confirm a failure. Every
    attempt costs a seek out and back, a full revolution read and a
    comparison which is not cheap, so paying twice for the common answer is
    most of the probe's running time for nothing. A single failure is worth
    checking, since the comparison is noisy enough to dip on its own.
    '''
    if _settle_attempt(usb, delays, candidate, reference, cylinder, away):
        return True
    return _settle_attempt(usb, delays, candidate, reference, cylinder, away)


def settle_candidates(current: int) -> List[int]:
    '''The settle values worth trying, largest first. Pure.'''
    out: List[int] = []
    for fraction in SETTLE_FRACTIONS:
        value = max(MIN_SETTLE_MS, int(current * fraction))
        if value not in out:
            out.append(value)
    return out

def measure(usb: USB.Unit) -> Result:
    """Search for the smallest step delay and settle time which hold."""

    delays = Delays(usb)
    was_step, was_settle = delays.step, delays.seek_settle

    try:
        step_trials: List[Trial] = []
        floor = max(1, int(was_step * FLOOR_FRACTION))
        intact = _descend(
            lambda v: _step_trial(usb, delays, was_step, v),
            was_step, floor, step_trials)
        if not intact:
            return interpret(step_trials, [], was_step, was_settle,
                             aborted=True)

        # Back to the setting the drive arrived with before measuring the
        # other thing: a settle search run at a step rate the drive cannot
        # manage would be measuring both at once.
        delays.step = was_step
        delays.update()

        settle_trials: List[Trial] = []
        cylinder, away = 4, 4 + SETTLE_TRAVEL
        # Settling is searched the same way, though nothing here provokes a
        # failure the head has to recover from: a short settle spoils a read,
        # it does not lose the head.
        delays.seek_settle = was_settle
        delays.update()
        usb.seek(cylinder, 0, check_trk0=False)
        try:
            usb.read_track(revs=1)  # discard: cheap insurance, see below
            reference = _one_revolution(usb)
        except USB.CmdError:
            # No index, so no track to read: an empty drive, or one whose
            # media it will not read. The step rate is already measured and
            # is worth keeping; the settle time simply cannot be.
            reference = []
        if reference:
            for candidate in settle_candidates(was_settle):
                settle_trials.append(Trial(candidate, _settle_holds(
                    usb, delays, candidate, reference, cylinder, away)))

        return interpret(step_trials, settle_trials, was_step, was_settle)

    finally:
        # These persist beyond the probe and affect every later command, so
        # they go back whatever happened -- exception, interrupt or success.
        delays.step, delays.seek_settle = was_step, was_settle
        try:
            delays.update()
            # Twice: the first may find the head already at the stop with the
            # firmware believing otherwise, and the second settles it.
            usb.seek(0, 0, check_trk0=False)
            usb.seek(0, 0, check_trk0=False)
        except USB.CmdError:
            pass


def run(ctx) -> Result:
    # Printed before anything is touched: if this run is killed rather than
    # merely failing, the finally which puts these back never happens, and
    # this line is what says how to restore them.
    was = Delays(ctx.usb)
    ctx.report('  Delays before this probe: step %d us, settle %d ms.'
               ' Restore with "gw delays --step %d --settle %d" if this run'
               ' is interrupted.'
               % (was.step, was.seek_settle, was.step, was.seek_settle))
    ctx.report('  Searching for the fastest stepping and shortest settle'
               ' this drive holds...')
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
