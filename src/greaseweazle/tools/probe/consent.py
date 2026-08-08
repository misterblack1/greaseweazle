# greaseweazle/tools/probe/consent.py
#
# Approval gate for probes which write to the disk.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Some probes can only measure the drive by writing to media. Those destroy
# whatever is on the disk, so they are never run on the strength of a command
# line flag alone: the user is told what will happen, told to put in a disk
# they do not mind losing, and asked to agree. This lives in one place so that
# every destructive probe asks in the same way and none can forget to.

from typing import Callable, Optional

from greaseweazle import usb as USB


def confirm(what: str,
            assume_yes: bool = False,
            prompt: Optional[Callable[[str], str]] = None) -> bool:
    '''Instruct the user and ask permission to write to the disk.

    Returns True if the probe may proceed. 'prompt' is injectable so that
    tests can answer without a terminal; it defaults to input().
    '''

    print()
    print('*** %s writes to the disk and DESTROYS its contents. ***' % what)
    print('Insert a blank or expendable floppy disk before continuing.')
    print('Do not use a disk you care about, and check the write-protect')
    print('tab is open.')

    if assume_yes:
        print('Proceeding: approval was given on the command line.')
        return True

    if prompt is None:
        prompt = input

    answer = prompt('Type "Yes" to continue, anything else to skip: ')
    if answer != 'Yes':
        print('Skipped.')
        return False
    return True


def is_write_protected(err: USB.CmdError) -> bool:
    '''True if a failed command failed because the disk is write protected.

    Worth separating from other write failures: a protected disk says nothing
    about the drive, so a probe must report it as "not measured" rather than
    as a fault it has found.
    '''
    return err.code == USB.Ack.Wrprot

# Local variables:
# python-indent: 4
# End:
