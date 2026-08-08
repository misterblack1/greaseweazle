# greaseweazle/tools/probe/profile.py
#
# A drive profile: what the probes found, saved, and compared over time.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# A profile is what every probe found in one run, with a timestamp, whatever
# name the user gave the drive, and enough about the Greaseweazle to know
# what was doing the measuring. Saved as JSON, it can be compared against a
# later one to see whether a drive has changed.
#
# COMPARING IS THE HARD PART, and most of the design is about not crying wolf.
# Three things would otherwise produce false alarms on every re-probe:
#
# A measurement never repeats exactly. Spin-up is quantised by where the index
# hole sits when the drive comes ready and varies by up to a whole revolution
# between runs, so comparing it exactly would report a change every time. Each
# probe therefore declares tolerances for its own fields -- it is the only
# thing that knows which are measurements, and why they move.
#
# A probe that did not run is not a probe that failed. Destructive probes are
# skipped without consent, wearing ones without being asked for, and any probe
# is skipped when something it depends on did not hold. Those are recorded
# with their reason and reported as not-measured rather than as change.
#
# The measuring apparatus is part of the measurement. A firmware upgrade
# between runs can move a number without anything about the drive having
# changed, so the firmware version and bus type are recorded and reported
# plainly when they differ, rather than left for someone to wonder about.
#
# The drive name is a LABEL THE USER SUPPLIES. Nothing here infers what drive
# is attached, and nothing should: identifying the drive is the user's to say.

import datetime
import json
from typing import (Any, Callable, Dict, List, NamedTuple, Optional,
                    Sequence, Tuple)

from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.tools.probe import core

# Bumped when the shape of a saved profile changes incompatibly. Comparing
# across a bump is refused rather than attempted: a field that has changed
# meaning would otherwise be diffed against its own past self.
SCHEMA_VERSION = 1


class Tolerance(NamedTuple):
    '''How much a numeric field may move between runs without it counting.

    Whichever of the two allowances is larger applies, so a relative figure
    can be given for fields spanning orders of magnitude while an absolute
    one keeps small values from being compared to a hair's breadth.
    '''

    absolute: float = 0.0
    relative: float = 0.0

    def accepts(self, old: float, new: float) -> bool:
        allowed = max(self.absolute, abs(old) * self.relative)
        return abs(new - old) <= allowed


class Ignored:
    '''Marks a field as not worth comparing at all.

    Raw sample lists and prose belong in a profile -- they are what makes an
    unexpected result diagnosable months later -- but diffing them would
    bury the fields that matter under noise.
    '''

    def __repr__(self) -> str:
        return 'IGNORED'


IGNORED = Ignored()

# How a field compares, when the probe has not said. Exact equality: a status
# or a head count that moves IS the news, and anything numeric a probe cares
# about should have been declared.
DEFAULT = None


class Change(NamedTuple):
    probe: str
    field: str
    old: Any
    new: Any
    # 'changed', 'not-measured', 'newly-measured', 'added' or 'removed'.
    kind: str

    def describe(self) -> str:
        if self.kind == 'not-measured':
            return '%s: not measured this time (was %r)' % (self.probe,
                                                            self.old)
        if self.kind == 'newly-measured':
            return '%s: measured this time (was not)' % self.probe
        if self.kind == 'added':
            return '%s: new in this profile' % self.probe
        if self.kind == 'removed':
            return '%s: absent from this profile' % self.probe
        return '%s.%s: %r -> %r' % (self.probe, self.field, self.old,
                                     self.new)


def now_iso() -> str:
    '''Creation time, to the second, in UTC.'''
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat())


def device_info(usb: USB.Unit) -> Dict[str, Any]:
    '''What was doing the measuring, since it can move the numbers.'''
    return {
        'firmware': '%d.%d' % usb.version,
        'hw_model': usb.hw_model,
        'hw_submodel': usb.hw_submodel,
        'sample_freq': usb.sample_freq,
    }


def build(results: Dict[str, Any],
          name: Optional[str] = None,
          device: Optional[Dict[str, Any]] = None,
          bus: Optional[str] = None,
          when: Optional[str] = None) -> Dict[str, Any]:
    '''Assemble a profile from what the probes returned.'''
    return {
        'schema': SCHEMA_VERSION,
        'created': when if when is not None else now_iso(),
        # None rather than a guess: naming the drive is the user's to do.
        'drive_name': name,
        'bus': bus,
        'device': device or {},
        'probes': results,
    }


def save(profile: Dict[str, Any], path: str) -> None:
    with open(path, 'w') as f:
        json.dump(profile, f, indent=2, sort_keys=True)
        f.write('\n')


def load(path: str) -> Dict[str, Any]:
    with open(path) as f:
        profile = json.load(f)
    error.check(isinstance(profile, dict) and 'probes' in profile,
                '%s is not a drive profile' % path)
    return profile


def _field_rule(tolerances: Dict[str, Any], field: str) -> Any:
    return tolerances.get(field, DEFAULT)


def _compare_fields(probe: str, old: Dict[str, Any], new: Dict[str, Any],
                    tolerances: Dict[str, Any]) -> List[Change]:
    changes = []
    for field in sorted(set(old) | set(new)):
        rule = _field_rule(tolerances, field)
        if isinstance(rule, Ignored):
            continue
        before, after = old.get(field), new.get(field)
        if before == after:
            continue
        if (isinstance(rule, Tolerance)
                and isinstance(before, (int, float))
                and isinstance(after, (int, float))
                and not isinstance(before, bool)
                and not isinstance(after, bool)):
            if rule.accepts(before, after):
                continue
        changes.append(Change(probe, field, before, after, 'changed'))
    return changes


def compare(old: Dict[str, Any], new: Dict[str, Any],
            probes: Sequence[core.Probe] = ()) -> List[Change]:
    '''What differs between two profiles.

    Probes supply the tolerances for their own fields; any probe not in
    'probes' is compared exactly, which errs towards reporting rather than
    towards silence.
    '''
    error.check(old.get('schema') == new.get('schema'),
                'Profiles use different schema versions (%r and %r) and '
                'cannot be compared. A field may have changed meaning '
                'between them.' % (old.get('schema'), new.get('schema')))

    tolerances = dict((p.name, getattr(p, 'tolerances', {})) for p in probes)
    was, now = old.get('probes', {}), new.get('probes', {})

    changes: List[Change] = []
    for probe in sorted(set(was) | set(now)):
        before, after = was.get(probe), now.get(probe)
        if before is None:
            changes.append(Change(probe, '', None, None, 'added'))
            continue
        if after is None:
            changes.append(Change(probe, '', None, None, 'removed'))
            continue
        # A probe that did not run this time is not a probe that failed, and
        # must not read as degradation.
        was_skipped = before.get('status') == 'skipped'
        now_skipped = after.get('status') == 'skipped'
        if now_skipped and not was_skipped:
            changes.append(Change(probe, 'status', before.get('status'),
                                  after.get('status'), 'not-measured'))
            continue
        if was_skipped and not now_skipped:
            changes.append(Change(probe, 'status', before.get('status'),
                                  after.get('status'), 'newly-measured'))
            continue
        if was_skipped and now_skipped:
            continue
        changes.extend(_compare_fields(probe, before, after,
                                       tolerances.get(probe, {})))
    return changes


def environment_changes(old: Dict[str, Any],
                        new: Dict[str, Any]) -> List[str]:
    '''Differences in what did the measuring, rather than in the drive.'''
    notes = []
    for label, key in (('Firmware', 'firmware'),
                       ('Sample frequency', 'sample_freq')):
        before = old.get('device', {}).get(key)
        after = new.get('device', {}).get(key)
        if before != after:
            notes.append('%s changed: %r -> %r. A measurement may have moved '
                         'with it rather than the drive.'
                         % (label, before, after))
    if old.get('bus') != new.get('bus'):
        notes.append('Bus type changed: %r -> %r.'
                     % (old.get('bus'), new.get('bus')))
    return notes


def report(profile: Dict[str, Any], out: Callable[[str], None]) -> None:
    '''Print the header a profile carries, wherever it is shown.'''
    out('Drive profile')
    out('  Name:     %s' % (profile.get('drive_name') or '(unnamed)'))
    out('  Created:  %s' % profile.get('created'))
    device = profile.get('device', {})
    if device:
        out('  Device:   Greaseweazle firmware %s'
            % device.get('firmware', '?'))
    if profile.get('bus'):
        out('  Bus:      %s' % profile['bus'])


def report_comparison(changes: Sequence[Change], notes: Sequence[str],
                      out: Callable[[str], None]) -> None:
    for note in notes:
        out('  %s' % note)
    if not changes:
        out('  No changes.')
        return
    for change in changes:
        out('  %s' % change.describe())

# Local variables:
# python-indent: 4
# End:
