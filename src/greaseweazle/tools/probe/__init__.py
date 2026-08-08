# greaseweazle/tools/probe/__init__.py
#
# Greaseweazle control script: Probe drive parameters and feature support.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

description = "Probe drive parameters and feature support."

import sys
from typing import Any, Callable, Dict, List, Sequence

from greaseweazle import usb as USB
from greaseweazle.tools import util
from greaseweazle.tools.probe import consent, core, profile
from greaseweazle.tools.probe import (
    double_step, head_count, index_sensor, max_track, max_track_write,
    multi_speed, pin34, spin_up, step_timing, trk0, write_verify)

# The registry. Adding a probe means writing its module and naming it here:
# order, dependencies, consent and reporting all come from the module itself,
# so nothing else in this file needs to know it exists.
#
# Dependencies decide the run order; this order breaks the ties, and so is
# not merely decorative. pin34 comes first because a disk-change latch is
# cleared by the first step, so it has to be read before anything moves the
# head. The rest are alphabetical.
PROBES: Sequence[core.Probe] = (
    pin34,          # type: ignore[assignment]
    double_step,
    head_count,
    index_sensor,
    max_track,
    max_track_write,
    multi_speed,
    spin_up,
    step_timing,
    trk0,
    write_verify,
)


def _finish(usb: USB.Unit, args, results: Dict[str, Any]) -> None:
    '''Assemble the profile, then save and compare as asked.'''

    built = profile.build(results, name=args.name,
                          device=profile.device_info(usb),
                          bus=args.drive.bus.name)
    print()
    profile.report(built, print)

    if args.save:
        profile.save(built, args.save)
        print('  Saved to %s' % args.save)

    if args.compare:
        earlier = profile.load(args.compare)
        print()
        print('Compared with %s (%s):'
              % (args.compare, earlier.get('created', 'undated')))
        profile.report_comparison(
            profile.compare(earlier, built, PROBES),
            profile.environment_changes(earlier, built), print)


def probe(usb: USB.Unit, args, selected: Sequence[core.Probe],
          motor: bool, results: Dict[str, Any]) -> None:
    """Run the selected probes, collecting results by probe name.

    Results are gathered into the caller's dict rather than returned, so that
    they survive a probe raising part-way through. The drive profile (a later
    task) is what will consume them.
    """

    print('Probing drive (this steps the head repeatedly)...')

    # --non-interactive DECLINES what it cannot ask about, where --yes
    # approves it. The two are opposites and the difference matters: a
    # scripted run which silently wrote to a disk because nobody was there
    # to object would be the worst of both.
    if args.non_interactive:
        confirm = lambda writers: False
    else:
        confirm = lambda writers: consent.confirm(
            ', '.join(p.title for p in writers), assume_yes=args.yes)

    def reselect() -> None:
        usb.set_bus_type(args.drive.bus.value)
        usb.drive_select(args.drive.unit_id)
        usb.drive_motor(args.drive.unit_id, motor)

    ctx = core.Context(
        usb, args, confirm=confirm, reselect=reselect,
        # Nothing to wait for when nobody is going to change the disk: --yes
        # has already approved proceeding, and --non-interactive says there
        # is no one to ask.
        pause=None if (args.yes or args.non_interactive) else input)
    try:
        core.run_all(ctx, selected)
    finally:
        results.update(ctx.as_dict())
        _finish(usb, args, results)


def print_plan_to(selected: Sequence[core.Probe],
                  out: Callable[[str], None]) -> None:
    """What is about to run, grouped by what the drive must hold."""

    out('')
    out('This run will ask for the drive to hold, in order:')
    shown = None
    for probe in selected:
        if probe.needs_media != shown:
            shown = probe.needs_media
            out('')
            out('  %s' % core.MEDIA_INSTRUCTIONS[shown])
        out('      %-18s%s%s'
            % (probe.name, probe.summary,
               ' [WRITES TO IT]' if probe.destructive else ''))
    out('')
    out('It stops and waits at each change, so the disks can be swapped.')
    out('Anything marked as writing will ask before it does so.')
    out('')


def print_plan(selected: Sequence[core.Probe]) -> None:
    print_plan_to(selected, print)


def main(argv) -> None:

    epilog = (util.drive_desc + '''
Probes measure the drive, not a disk, but they differ over whether a disk
needs to be loaded. Stepping probes want the drive EMPTY: repeatedly dragging
the head across stationary media can score it. The index probe wants a disk
LOADED, since on 5.25" and similar drives the index comes from a hole in the
media and reads as absent without one. Probes needing the spindle turning say
so, and the motor is switched on for the whole run when any of them is
selected. Use --list-probes to see what there is, and --only to pick.''')

    parser = util.ArgumentParser(usage='%(prog)s [options]', epilog=epilog)
    parser.add_argument("--device", help="device name (COM/serial port)")
    parser.add_argument("--drive", type=util.Drive(), default='A',
                        help="drive to probe")
    parser.add_argument("--max-cylinder", type=util.uint, metavar="N",
                        help="optional ceiling on how far the head is driven"
                        " (default: search until the drive stops it)")
    parser.add_argument("--motor-on", action="store_true",
                        help="probe with the drive motor running")
    parser.add_argument("--write-test", action="store_true",
                        help="also run probes which write to the disk"
                        " (DESTROYS the disk contents)")
    parser.add_argument("--yes", action="store_true",
                        help="approve destructive probes without prompting")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never ask anything: skip probes which would"
                        " need approval, and do not wait for disk changes")
    parser.add_argument("--only", action="append", metavar="PROBE",
                        help="run only this probe (repeatable); probes it"
                        " depends on are run too")
    parser.add_argument("--name", metavar="NAME",
                        help="label for the drive being probed, recorded in"
                        " the profile (never guessed)")
    parser.add_argument("--save", metavar="FILE",
                        help="write the drive profile to FILE as JSON")
    parser.add_argument("--compare", metavar="FILE",
                        help="compare this run against a saved profile")
    parser.add_argument("--allow-wear", action="store_true",
                        help="also run probes which wear the drive mechanism")
    parser.add_argument("--all", action="store_true",
                        help="run every probe: implies --allow-wear and"
                        " --write-test")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would run, and what the drive must"
                        " hold, without touching it")
    parser.add_argument("--list-probes", action="store_true",
                        help="list the available probes and exit")
    parser.description = description
    parser.prog += ' ' + argv[1]
    args = parser.parse_args(argv[2:])

    if args.all:
        args.allow_wear = args.write_test = True

    if args.list_probes:
        for p in core.ordered(PROBES):
            notes = ([' (wears the drive)'] if p.wears_drive else [])
            print('  %-18s%-10s%s%s'
                  % (p.name, p.needs_media, p.summary, ''.join(notes)))
        print()
        print('The middle column is what the drive must hold. Probes run in')
        print('that order, so a full run asks for as few disk changes as it')
        print('can: nothing, then any disk, then a formatted one, then a')
        print('scratch one which gets written over.')
        return

    # Selecting a destructive probe is itself a request to run it; the consent
    # prompt, not the flag, is what guards the disk.
    if args.only is not None:
        by_name = dict((p.name, p) for p in PROBES)
        if any(by_name[n].destructive for n in args.only if n in by_name):
            args.write_test = True

    selected = core.select(PROBES, args.only, destructive=args.write_test,
                           allow_wear=args.allow_wear)
    if args.only is not None:
        added = [p.name for p in selected if p.name not in args.only]
        if added:
            print('Also running %s, which the selection depends on.'
                  % ', '.join(added))

    # What the run will ask for, before it asks for anything. Somebody about
    # to spend several minutes feeding disks to a drive should be able to
    # collect them first, and should know which of them will be written over.
    print_plan(selected)
    if args.dry_run:
        return

    # Asked of the probes rather than inferred from the flags: a probe which
    # needs the spindle turning says so, and the motor is switched on for the
    # whole session because that is the granularity the drive offers.
    motor = args.motor_on or core.needs_motor(selected)

    results: Dict[str, Any] = {}
    try:
        usb = util.usb_open(args.device)
        util.with_drive_selected(
            lambda: probe(usb, args, selected, motor, results),
            usb, args.drive, motor=motor)
    except USB.CmdError as err:
        print("Command Failed: %s" % err)


if __name__ == "__main__":
    main(sys.argv)

# Local variables:
# python-indent: 4
# End:
