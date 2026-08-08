# greaseweazle/tools/probe/multi_speed.py
#
# Probe: does driving pin 2 change the spindle speed?
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Drive pin 2 one way, measure the revolution period; drive it the other way
# and measure again. If the two differ SUBSTANTIALLY, the drive changes speed
# on that line. If they do not, it does not -- which is an answer, not a
# failure. Plenty of drives are fixed-speed, plenty are jumpered to one speed,
# and plenty more use pin 2 only to change write current while the spindle
# keeps turning at one rate.
#
# Substantially matters. Any two measurements of a spinning disk differ a
# little, and a drive that wanders half a percent between readings has not
# got two speeds. The pair worth detecting is 300 and 360 rpm -- low-density
# media read at the slower rate, high-density at the faster -- a ratio of 1.2,
# so the bar is set well below that and far above any drift. Something in
# between is reported as neither rather than rounded to whichever is nearer.
#
# WHAT IS NOT ASSUMED. No nominal speeds appear anywhere here. The measured
# periods are reported as measured, and the verdict is simply whether they
# differ; nothing is checked against 300 or 360 rpm or any other figure a
# drive of some type ought to produce. A drive turning at an unexpected rate
# is a fact about the drive.
#
# WHAT PIN 2 MEANS IS ITSELF NOT ASSUMED. tools/diag/pinmap.py already
# records that pin 2 is not always density-select: on 8" drives, and on the
# 34-to-50 adapters which almost always carry it through, the same line is
# often TG43 instead, asserted past a given cylinder to enable write
# precompensation. So this probe reports what driving the line did, and does
# not claim to have found a density-select input. A drive whose speed does
# not change may be fixed-speed, or may simply have something else wired to
# pin 2.
#
# RESTORING IT. The line is an output with no read-back in this firmware, so
# its state before the probe ran cannot be recovered directly. Instead the
# period is measured once before anything is touched, and afterwards the line
# is left in whichever state reproduced that reading -- so a drive which does
# change speed is handed back turning at the rate it was found at. When
# neither state can be told from the baseline, which is every fixed-speed
# drive, the line is left high.
#
# Needs a disk the drive will read, since the measurement is of index timing.
# Hence the dependency.

import time
from typing import Any, Callable, Dict, NamedTuple, Optional

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.diag.pinmap import DENSITY_SELECT_PIN
from greaseweazle.tools.probe import core, index_sensor, profile

name = 'multi-speed'
title = 'Multi-Speed'
summary = 'Whether driving pin 2 changes the spindle speed'
depends_on = ('index-sensor',)
destructive = False
needs_motor = True
needs_media = core.MEDIA_ANY
wears_drive = False

tolerances = {
    'period_low_ms': profile.Tolerance(relative=0.02),
    'period_high_ms': profile.Tolerance(relative=0.02),
    'rpm_low': profile.Tolerance(relative=0.02),
    'rpm_high': profile.Tolerance(relative=0.02),
    'ratio': profile.Tolerance(relative=0.02),
    'detail': profile.IGNORED,
}

# Outcomes.
SWITCHES = 'switches'          # Two speeds, differing substantially.
FIXED = 'fixed'                # One speed: pin 2 does not change it.
MARGINAL = 'marginal'          # Something moved, but not by a speed's worth.
INCONCLUSIVE = 'inconclusive'  # A period could not be measured.

# How long to let the spindle settle after changing the line. Generous: a
# drive which does change speed has to accelerate or brake to the new one,
# and measuring across that transition would give a median belonging to
# neither state.
SETTLE_SECONDS = 2.0

# Thresholds on the RATIO between the two periods, slower over faster.
#
# A real speed change is a big one. The pair that matters is 300 and 360 rpm
# -- low-density media is read at the slower rate, high-density at the faster
# -- which is a ratio of 1.2, and the bench drive produced 1.200 to three
# figures once jumpered for it. So the bar for calling a drive two-speed sits
# at 1.10: far enough below 1.2 that another genuine pair still registers,
# far enough above the hundredth of a percent a drive holds its own speed to
# that drift cannot reach it.
#
# The two thresholds leave a gap on purpose. Speeds within SAME_RATIO are one
# speed measured twice. Beyond SWITCH_RATIO they are two speeds. In between
# something moved, but not by anything resembling a density change, and
# saying so is better than rounding it to either answer -- calling a 5%
# wobble "two speeds" would be as wrong as calling it "one".
SAME_RATIO = 1.02
SWITCH_RATIO = 1.10


class Result(NamedTuple):
    status: str
    detail: str
    period_low: Optional[float] = None
    period_high: Optional[float] = None

    @property
    def rpm_low(self) -> Optional[float]:
        return None if not self.period_low else 60.0 / self.period_low

    @property
    def rpm_high(self) -> Optional[float]:
        return None if not self.period_high else 60.0 / self.period_high

    @property
    def ratio(self) -> Optional[float]:
        '''Faster speed over slower, or None if either is missing.'''
        if not self.period_low or not self.period_high:
            return None
        return max(self.period_low, self.period_high) / min(self.period_low,
                                                            self.period_high)

    @property
    def ok(self) -> bool:
        return self.status in (SWITCHES, FIXED)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'ok': self.ok,
            'detail': self.detail,
            'period_low_ms': (None if self.period_low is None
                              else self.period_low * 1e3),
            'period_high_ms': (None if self.period_high is None
                               else self.period_high * 1e3),
            'rpm_low': self.rpm_low,
            'rpm_high': self.rpm_high,
            'ratio': self.ratio,
        }

    def report(self, out: Callable[[str], None]) -> None:
        if self.status == SWITCHES:
            out('  Two speeds: pin 2 changes it.')
        elif self.status == FIXED:
            out('  One speed: pin 2 does not change it.')
        elif self.status == MARGINAL:
            out('  UNCLEAR - the speeds differ, but not by a speed.')
        else:
            out('  UNKNOWN - the speed could not be measured.')
        for label, period, rpm in (('low ', self.period_low, self.rpm_low),
                                   ('high', self.period_high, self.rpm_high)):
            if period is not None and rpm is not None:
                out('  pin 2 %s: %.3f ms per revolution (%.2f rpm)'
                    % (label, period * 1e3, rpm))
        if self.ratio is not None:
            out('  Ratio between them: %.3f' % self.ratio)
        out('  (%s)' % self.detail)


def interpret(period_low: Optional[float],
              period_high: Optional[float]) -> Result:
    '''Decide from the period measured in each pin-2 state. Pure.

    Periods are in seconds; None means one could not be measured.
    '''
    if period_low is None or period_high is None:
        return Result(
            INCONCLUSIVE,
            'The revolution period could not be measured in both states, so '
            'nothing can be said about whether pin 2 changes the speed.',
            period_low, period_high)

    error.check(period_low > 0 and period_high > 0,
                'multi-speed: periods must be positive')

    ratio = max(period_low, period_high) / min(period_low, period_high)

    if ratio >= SWITCH_RATIO:
        faster = 'high' if period_high < period_low else 'low'
        return Result(
            SWITCHES,
            'The two states turn at rates a factor of %.3f apart, so this '
            'drive changes speed on pin 2, and runs faster with the line %s. '
            'What else that line may mean on a given cable is not '
            'established here.' % (ratio, faster),
            period_low, period_high)

    if ratio > SAME_RATIO:
        return Result(
            MARGINAL,
            'The two states differ by a factor of %.3f. Something changed, '
            'but far less than a density change moves a spindle -- 300 to '
            '360 rpm is a factor of 1.2 -- so this is not a two-speed drive '
            'on the strength of it. Worth measuring again before drawing any '
            'conclusion.' % ratio,
            period_low, period_high)

    return Result(
        FIXED,
        'Both states turned at the same rate, within a factor of %.3f, so '
        'pin 2 does not change this drive\'s speed. It may be a fixed-speed '
        'drive, or jumpered for one speed, or the line may carry something '
        'other than density-select on this cable, or it may change write '
        'current without touching the spindle.' % ratio,
        period_low, period_high)


def _period_with_pin(usb: USB.Unit, level: int) -> Optional[float]:
    usb.set_pin(DENSITY_SELECT_PIN, level)
    time.sleep(SETTLE_SECONDS)
    result = index_sensor.measure(usb)
    return result.period if result.status == index_sensor.OK else None


def measure(usb: USB.Unit) -> Result:
    '''Measure the period with pin 2 driven each way, and restore it.'''

    # As found, so the line can be put back the way the drive was running.
    baseline = index_sensor.measure(usb)
    as_found = baseline.period if baseline.status == index_sensor.OK else None

    low: Optional[float] = None
    high: Optional[float] = None
    try:
        low = _period_with_pin(usb, 0)
        high = _period_with_pin(usb, 1)
        return interpret(low, high)
    finally:
        # Runs even if a measurement raised part-way, so the line is never
        # left in whichever state the probe happened to stop in.
        _restore(usb, as_found, low, high)


def _restore(usb: USB.Unit, as_found: Optional[float],
             low: Optional[float], high: Optional[float]) -> None:
    '''Leave pin 2 in whichever state reproduces the period found at entry.'''
    level = 1
    if as_found is not None and low is not None and high is not None:
        if abs(low - as_found) < abs(high - as_found):
            level = 0
    try:
        usb.set_pin(DENSITY_SELECT_PIN, level)
    except USB.CmdError:
        pass


def run(ctx) -> Result:
    ctx.report('  Measuring the spindle with pin 2 driven each way...')
    return measure(ctx.usb)

# Local variables:
# python-indent: 4
# End:
