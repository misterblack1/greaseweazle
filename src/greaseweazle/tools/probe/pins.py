# greaseweazle/tools/probe/pins.py
#
# Interface signals shared by the probes.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from greaseweazle import usb as USB
from greaseweazle.tools.diag.pinmap import TK0_PIN


def trk0_asserted(usb: USB.Unit) -> bool:
    '''True if /TRK0 says the head is at cylinder 0.

    The signal is active low, so a low pin level is an assertion. Worth
    having in one place: reading the polarity backwards inverts the meaning
    of every measurement built on it.
    '''
    return not usb.get_pin(TK0_PIN)

# Local variables:
# python-indent: 4
# End:
