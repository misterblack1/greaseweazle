# greaseweazle/tools/probe/core.py
#
# The probe contract, and the orchestrator which runs probes to it.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Every probe is a self-contained module which declares what it needs and
# owns its own presentation. Nothing here knows about any particular probe,
# so adding one means writing its module and naming it in the registry --
# not editing the orchestrator in a dozen places.
#
# A probe module provides:
#
#     name         short identifier, used for selection and in the profile
#     title        heading for its section of the report
#     summary      one line, for --list-probes
#     depends_on   names of probes whose results qualify this one
#     destructive  True if it writes to the disk
#     needs_motor  True if it needs the spindle turning
#     wears_drive  True if running it measurably wears the mechanism
#     needs_media  what has to be in the drive: see MEDIA_* below
#     run(ctx)     perform the measurement, returning a Result
#
# and its Result satisfies the Result protocol below.

from typing import (Any, Callable, Dict, Iterable, List, NamedTuple,
                    Optional, Protocol, Sequence)

from greaseweazle import error
from greaseweazle import usb as USB


# What a probe needs in the drive, in increasing order of demand. Probes run
# in this order so that a session asks for as few disk changes as it can:
# everything needing nothing runs first, then everything needing a disk, and
# the ones which write come last, when a scratch disk is called for anyway.
#
# Dependencies never point the wrong way across this -- a probe needing a
# scratch disk may depend on one needing none, but not the reverse -- so
# ordering by demand and ordering by dependency do not fight.
MEDIA_NONE = 'none'
MEDIA_ANY = 'any'
MEDIA_FORMATTED = 'formatted'
MEDIA_SCRATCH = 'scratch'

MEDIA_ORDER = (MEDIA_NONE, MEDIA_ANY, MEDIA_FORMATTED, MEDIA_SCRATCH)

MEDIA_INSTRUCTIONS = {
    MEDIA_NONE: 'No disk needed. An EMPTY drive is best: these probes step '
                'the head repeatedly, and dragging it across a stationary '
                'disk can score the surface.',
    MEDIA_ANY: 'Load ANY disk the drive can read. Its contents do not '
               'matter, but there must be one: on drives taking the index '
               'from a hole in the media there is no index signal without.',
    MEDIA_FORMATTED: 'Load a PC-FORMATTED disk (or any IBM MFM/FM disk: '
                     'Atari ST, Amstrad, most CP/M). What follows reads the '
                     'sector headers, which carry the cylinder number the '
                     'formatting drive wrote -- a blank disk has none, and '
                     'Amiga, Commodore and Apple GCR are not decoded here.',
    MEDIA_SCRATCH: 'Load a SCRATCH disk. What follows writes to it and '
                   'DESTROYS the contents.',
}


def media_rank(level: str) -> int:
    error.check(level in MEDIA_ORDER, 'Unknown media requirement %r' % level)
    return MEDIA_ORDER.index(level)


class Result(Protocol):
    '''What every probe returns.

    A Protocol rather than a base class: each probe's Result carries quite
    different fields and they are all NamedTuples, so what matters is the
    shared surface, not a shared ancestor.
    '''

    # Read-only: every Result is a NamedTuple, whose fields cannot be
    # assigned. Declaring a plain attribute here would demand a settable one
    # and reject all of them.
    @property
    def status(self) -> str:
        '''Machine-readable outcome, recorded in the profile.'''
        ...

    @property
    def ok(self) -> bool:
        '''True if probes depending on this one may believe it.'''
        ...

    def as_dict(self) -> Dict[str, Any]:
        '''JSON-friendly form, for the drive profile.'''
        ...

    def report(self, out: Callable[[str], None]) -> None:
        '''Present this result to the user.'''
        ...


class Probe(Protocol):
    name: str
    title: str
    summary: str
    depends_on: Sequence[str]
    destructive: bool
    needs_motor: bool
    wears_drive: bool
    needs_media: str

    def run(self, ctx: 'Context') -> Result:
        ...


class Skipped(NamedTuple):
    '''Stands in for a probe which did not run.

    Recorded rather than simply omitted, because "not measured" and "measured
    and failed" mean entirely different things when two drive profiles are
    compared: an absent field must never read as a change.
    '''

    reason: str
    status: str = 'skipped'

    @property
    def ok(self) -> bool:
        return False

    def as_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'reason': self.reason}

    def report(self, out: Callable[[str], None]) -> None:
        out('  Not run.')
        out('  (%s)' % self.reason)


class Failed(NamedTuple):
    """Stands in for a probe which raised.

    A probe meeting something it did not expect must not take the rest of the
    run with it. Twelve probes over three disk changes is several minutes of
    somebody's attention, and losing the lot because the ninth met an empty
    drive would be a poor trade for a shorter traceback. The error is recorded
    where the result would have been, so the profile shows which probe failed
    and why.
    """

    reason: str
    status: str = 'error'

    @property
    def ok(self) -> bool:
        return False

    def as_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'error': self.reason}

    def report(self, out: Callable[[str], None]) -> None:
        out('  FAILED - %s' % self.reason)


class Context:
    '''What a probe is given: the drive, the options, and what ran before.'''

    def __init__(self, usb: USB.Unit, options: Any,
                 confirm: Callable[[Sequence['Probe']], bool],
                 out: Callable[[str], None] = print,
                 pause: Optional[Callable[[str], Any]] = None,
                 reselect: Optional[Callable[[], None]] = None) -> None:
        self.usb = usb
        self.options = options
        self._confirm = confirm
        self.report = out
        self._pause = pause
        # Anything which waits for a person outlasts the firmware watchdog,
        # which deselects every drive and stops the motors after ten seconds
        # of silence. Whatever runs next then fails with "no drive unit
        # selected" -- which is what a real run did, at the very first
        # prompt, while every scripted test sailed past because the answer
        # arrived instantly. So the drive is taken back afterwards.
        self._reselect = reselect
        # Asked once for the run, then remembered -- including a refusal, so
        # that declining is not re-litigated probe by probe.
        self._may_write: Optional[bool] = None
        self.results: Dict[str, Result] = {}
        # Nothing has been asked for yet, so the first probe announces
        # whatever it needs even if that is nothing.
        self.media: Optional[str] = None

    def require_media(self, probe: Probe) -> None:
        '''Announce what the drive needs, when it changes.

        Only ever increases within a run, because probes are ordered by what
        they demand. A probe which writes says so through the consent gate a
        moment later, so it is announced but not paused for twice.
        '''
        if self.media is not None and (media_rank(probe.needs_media)
                                       <= media_rank(self.media)):
            return
        self.media = probe.needs_media
        self.report('')
        self.report('*** %s' % MEDIA_INSTRUCTIONS[probe.needs_media])
        if self._pause is not None and not probe.destructive:
            self._pause('    Press Enter when the drive is ready: ')
            self.reselect()

    def record(self, probe: Probe, result: Result) -> None:
        self.results[probe.name] = result

    def result(self, probe: Any) -> Any:
        '''Result of an earlier probe, by module.

        Keyed by the module rather than by its name so that the caller keeps
        the concrete Result type: a probe reading another's measurement wants
        the field, not an opaque object.
        '''
        return self.results.get(probe.name)

    def ok(self, name: str) -> bool:
        result = self.results.get(name)
        return result is not None and result.ok

    def confirm(self, writers: Sequence[Probe]) -> bool:
        if self._may_write is None:
            self._may_write = self._confirm(writers)
            # Asking took a person's time, and the watchdog does not wait.
            self.reselect()
        return self._may_write

    def reselect(self) -> None:
        '''Take the drive back after the watchdog will have dropped it.'''
        if self._reselect is not None:
            self._reselect()

    def as_dict(self) -> Dict[str, Any]:
        return dict((name, result.as_dict())
                    for name, result in self.results.items())


def ordered(probes: Iterable[Probe]) -> List[Probe]:
    '''Probes in an order which satisfies their dependencies.

    Sorted from the declarations rather than maintained by hand, so a probe
    cannot be added in the wrong place, and a cycle is reported instead of
    silently producing an order that cannot be run.

    Orders whatever it is given. A dependency outside the set is not an
    error here: it means that probe was not selected, and run_all will skip
    whatever needed it, saying so. Registry validity is checked by select().
    '''
    # By what each probe needs in the drive, so a session asks for as few
    # disk changes as it can. Dependencies still decide the order; this only
    # breaks the ties, and cannot fight them because a probe never depends on
    # one needing MORE than it does.
    probes = sorted(probes, key=lambda p: media_rank(p.needs_media))
    by_name = dict((p.name, p) for p in probes)
    state: Dict[str, str] = {}
    result: List[Probe] = []

    def visit(probe: Probe) -> None:
        seen = state.get(probe.name)
        if seen == 'done':
            return
        error.check(seen != 'visiting',
                    'Probe dependency cycle involving %s' % probe.name)
        state[probe.name] = 'visiting'
        for dependency in probe.depends_on:
            if dependency in by_name:
                visit(by_name[dependency])
        state[probe.name] = 'done'
        result.append(probe)

    for probe in probes:
        visit(probe)
    return result


def select(probes: Sequence[Probe], only: Optional[List[str]],
           destructive: bool = False,
           allow_wear: bool = False) -> List[Probe]:
    '''Which probes to run, in order.

    'only' names the probes explicitly asked for, or None for all of them.
    Prerequisites are added automatically: running a probe without whatever
    qualifies its result would produce a number nobody should trust.

    A probe which wears the mechanism is never added on anyone's behalf --
    not by a plain run, and not as somebody else's prerequisite. It runs when
    it is named or when wear is allowed outright, and otherwise whatever
    depended on it is skipped with the reason given. Pulling a wearing probe
    in transitively is exactly the surprise this exists to prevent.
    '''
    by_name = dict((p.name, p) for p in probes)

    for probe in probes:
        for dependency in probe.depends_on:
            error.check(dependency in by_name,
                        'Probe %s depends on unknown probe %s'
                        % (probe.name, dependency))

    if only is None:
        named: Sequence[str] = ()
        chosen = set(p.name for p in probes
                     # Destructive probes are opt-in, never part of a plain
                     # run, however convenient it would be to include them.
                     if destructive or not p.destructive)
    else:
        unknown = [name for name in only if name not in by_name]
        error.check(not unknown,
                    'Unknown probe(s): %s\nAvailable: %s'
                    % (', '.join(unknown),
                       ', '.join(p.name for p in probes)))
        named = only
        chosen = set(only)
        pending = list(chosen)
        while pending:
            for dependency in by_name[pending.pop()].depends_on:
                if dependency not in chosen:
                    chosen.add(dependency)
                    pending.append(dependency)

    if not allow_wear:
        chosen = set(name for name in chosen
                     if not by_name[name].wears_drive or name in named)

    # Registry order, not set order. Iterating the set would hand ordered()
    # its input in an order that varies between processes, since Python
    # randomises string hashing -- so the run order, and with it a saved
    # profile, would differ run to run for no reason. Dependencies constrain
    # the order; the registry breaks the ties, and does so the same way every
    # time.
    return ordered([p for p in probes if p.name in chosen])


def needs_motor(probes: Iterable[Probe]) -> bool:
    '''True if any of these probes needs the spindle turning.

    Asked before the drive is selected, since the motor is switched on for
    the whole session rather than per probe.
    '''
    return any(p.needs_motor for p in probes)


def run_all(ctx: Context, probes: Sequence[Probe]) -> None:
    '''Run each probe, skipping any whose prerequisites did not hold.'''

    writers = [p for p in probes if p.destructive]

    for probe in probes:
        ctx.require_media(probe)
        unmet = [name for name in probe.depends_on if not ctx.ok(name)]
        if unmet:
            result: Result = Skipped(
                'depends on %s, which did not produce a usable result'
                % ', '.join(unmet))
        elif probe.destructive and not ctx.confirm(writers):
            # Consent is enforced here, once, rather than trusted to each
            # destructive probe to remember.
            result = Skipped('not approved')
        else:
            try:
                result = probe.run(ctx)
            except (USB.CmdError, error.Fatal) as exception:
                # Recorded, not raised. The probes which follow may want
                # nothing this one needed, and the ones already done have
                # answers worth keeping.
                result = Failed(str(exception))

        ctx.record(probe, result)
        ctx.report('')
        ctx.report('%s:' % probe.title)
        result.report(ctx.report)

# Local variables:
# python-indent: 4
# End:
