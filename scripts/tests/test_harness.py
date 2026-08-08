# scripts/tests/test_harness.py
#
# The orchestrator, and the acquisition each probe does, against a drive
# which exists only in memory.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import unittest

import fake

from greaseweazle.tools.probe import core, max_track, trk0


def context(usb=None, confirm=None, pause=None, reselect=None, **options):
    class Options:
        pass
    opts = Options()
    opts.max_cylinder = None
    for key, value in options.items():
        setattr(opts, key, value)
    return core.Context(usb or fake.FakeUnit(), opts,
                        confirm=confirm or (lambda p: True),
                        out=fake.Recorder(), pause=pause, reselect=reselect)


class TestOrchestrator(unittest.TestCase):
    """run_all: what runs, what is skipped, and what is recorded."""

    def test_a_probe_with_its_prerequisite_met_runs(self):
        first = fake.StubProbe('first')
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertTrue(first.ran)
        self.assertTrue(second.ran)

    def test_a_probe_whose_prerequisite_failed_is_skipped(self):
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertTrue(first.ran)
        self.assertFalse(second.ran)
        self.assertEqual(ctx.results['second'].status, 'skipped')

    def test_a_skip_records_which_prerequisite_failed(self):
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertIn('first', ctx.results['second'].as_dict()['reason'])

    def test_a_destructive_probe_is_not_run_without_consent(self):
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda writers: False)
        core.run_all(ctx, [probe])
        self.assertFalse(probe.ran)
        self.assertEqual(ctx.results['writes'].status, 'skipped')

    def test_consent_is_asked_once_for_the_whole_run(self):
        # Three probes writing to one disk is one question, not three.
        # Asking per probe teaches people to type Yes without reading.
        asked = []
        writers = [fake.StubProbe('w%d' % n, destructive=True)
                   for n in range(3)]
        ctx = context(confirm=lambda ws: asked.append([p.name for p in ws])
                      or True)
        core.run_all(ctx, writers)
        self.assertEqual(len(asked), 1)
        self.assertTrue(all(p.ran for p in writers))

    def test_the_question_names_every_probe_it_covers(self):
        asked = []
        writers = [fake.StubProbe('w%d' % n, destructive=True)
                   for n in range(3)]
        ctx = context(confirm=lambda ws: asked.append([p.name for p in ws])
                      or True)
        core.run_all(ctx, writers)
        self.assertEqual(asked[0], ['w0', 'w1', 'w2'])

    def test_a_refusal_is_remembered_rather_than_re_litigated(self):
        asked = []
        writers = [fake.StubProbe('w%d' % n, destructive=True)
                   for n in range(3)]
        ctx = context(confirm=lambda ws: asked.append(1) or False)
        core.run_all(ctx, writers)
        self.assertEqual(len(asked), 1)
        self.assertFalse(any(p.ran for p in writers))

    def test_a_non_destructive_probe_is_never_asked_about(self):
        def refuse(writers):
            raise AssertionError('asked about a probe which writes nothing')
        ctx = context(confirm=refuse)
        core.run_all(ctx, [fake.StubProbe('reads')])

    def test_every_probe_leaves_a_result_behind(self):
        # The profile distinguishes "not measured" from "measured and
        # failed", which it can only do if skips are recorded rather than
        # omitted.
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertEqual(sorted(ctx.results), ['first', 'second'])
        self.assertIn('status', ctx.as_dict()['second'])


class TestAProbeFailing(unittest.TestCase):
    """One probe meeting the unexpected must not end the run."""

    def test_the_run_continues_past_a_probe_which_raises(self):
        from greaseweazle import error as gw_error
        bad = fake.StubProbe('bad', raises=gw_error.Fatal('no index'))
        after = fake.StubProbe('after')
        ctx = context()
        core.run_all(ctx, [bad, after])
        self.assertTrue(after.ran)

    def test_the_failure_is_recorded_where_the_result_would_be(self):
        from greaseweazle import error as gw_error
        bad = fake.StubProbe('bad', raises=gw_error.Fatal('no index'))
        ctx = context()
        core.run_all(ctx, [bad])
        self.assertEqual(ctx.results['bad'].status, 'error')
        self.assertIn('no index', ctx.results['bad'].as_dict()['error'])

    def test_a_usb_error_is_caught_too(self):
        from greaseweazle import usb as gw_usb
        bad = fake.StubProbe(
            'bad', raises=gw_usb.CmdError(b'x', gw_usb.Ack.NoIndex))
        after = fake.StubProbe('after')
        ctx = context()
        core.run_all(ctx, [bad, after])
        self.assertEqual(ctx.results['bad'].status, 'error')
        self.assertTrue(after.ran)

    def test_a_failure_is_not_a_skip(self):
        # The profile must tell "this probe broke" from "this probe was not
        # run", since only one of them is a fault.
        from greaseweazle import error as gw_error
        bad = fake.StubProbe('bad', raises=gw_error.Fatal('boom'))
        ctx = context()
        core.run_all(ctx, [bad])
        self.assertNotEqual(ctx.results['bad'].status, 'skipped')
        self.assertFalse(ctx.results['bad'].ok)

    def test_probes_depending_on_a_failed_one_are_skipped(self):
        from greaseweazle import error as gw_error
        bad = fake.StubProbe('bad', raises=gw_error.Fatal('boom'))
        after = fake.StubProbe('after', depends_on=('bad',))
        ctx = context()
        core.run_all(ctx, [bad, after])
        self.assertFalse(after.ran)
        self.assertEqual(ctx.results['after'].status, 'skipped')


class TestMediaAnnouncements(unittest.TestCase):

    def test_the_requirement_is_announced_when_it_rises(self):
        ctx = context()
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_NONE),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertIn('No disk needed', ctx.report.text)
        self.assertIn('Load ANY disk', ctx.report.text)

    def test_it_is_announced_once_not_per_probe(self):
        ctx = context()
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertEqual(ctx.report.text.count('Load ANY disk'), 1)

    def test_the_run_pauses_for_the_disk_to_be_changed(self):
        asked = []
        ctx = context(pause=lambda prompt: asked.append(prompt))
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_NONE),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertEqual(len(asked), 2)

    def test_a_destructive_probe_is_not_paused_for_twice(self):
        # The consent gate already tells the user to load a scratch disk and
        # waits, so pausing again for the media change would ask twice.
        asked = []
        ctx = context(pause=lambda prompt: asked.append(prompt))
        core.run_all(ctx, [fake.StubProbe('w', destructive=True,
                                          needs_media=core.MEDIA_SCRATCH)])
        self.assertEqual(asked, [])


class TestWatchdogRecovery(unittest.TestCase):
    """Anything which waits for a person outlasts the firmware watchdog,
    which drops every drive after ten seconds of silence."""

    def test_the_drive_is_taken_back_after_a_media_pause(self):
        taken = []
        ctx = context(pause=lambda prompt: None,
                      reselect=lambda: taken.append('reselect'))
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY)])
        self.assertEqual(len(taken), 1)

    def test_the_drive_is_taken_back_after_asking_to_write(self):
        taken = []
        ctx = context(confirm=lambda ws: True,
                      reselect=lambda: taken.append('reselect'))
        core.run_all(ctx, [fake.StubProbe('w', destructive=True)])
        self.assertEqual(len(taken), 1)

    def test_it_is_taken_back_even_when_writing_is_declined(self):
        # Declining still took a person's time, and the probes after it need
        # the drive.
        taken = []
        ctx = context(confirm=lambda ws: False,
                      reselect=lambda: taken.append('reselect'))
        core.run_all(ctx, [fake.StubProbe('w', destructive=True)])
        self.assertEqual(len(taken), 1)

    def test_it_is_taken_back_once_not_per_writing_probe(self):
        # The question is asked once, so the recovery happens once too.
        taken = []
        ctx = context(confirm=lambda ws: True,
                      reselect=lambda: taken.append('reselect'))
        core.run_all(ctx, [fake.StubProbe('a', destructive=True),
                           fake.StubProbe('b', destructive=True)])
        self.assertEqual(len(taken), 1)

    def test_nothing_is_taken_back_when_nobody_was_asked(self):
        # A scripted run never waits, so there is nothing to recover from.
        taken = []
        ctx = context(pause=None, reselect=lambda: taken.append('reselect'))
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY)])
        self.assertEqual(taken, [])


class TestTrk0Acquisition(unittest.TestCase):
    """The walk itself, against sensors which cannot be bought."""

    def test_a_healthy_sensor(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_WORKING))
        self.assertEqual(result.status, trk0.OK)

    def test_a_sensor_stuck_asserted(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_STUCK))
        self.assertEqual(result.status, trk0.STUCK_ASSERTED)

    def test_a_dead_sensor(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_DEAD))
        self.assertEqual(result.status, trk0.ABSENT_AT_HOME)

    def test_a_sensor_which_answers_only_on_the_way_out(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_ONE_WAY))
        self.assertEqual(result.status, trk0.NO_REASSERT)

    def test_the_head_is_left_at_cylinder_zero(self):
        usb = fake.FakeUnit()
        trk0.measure(usb)
        self.assertEqual(usb.head_cylinder, 0)
        self.assertEqual(usb.firmware_cylinder, 0)


class TestMaxTrackAcquisition(unittest.TestCase):
    """The search, against drives of known size and known bad habits."""

    def test_a_forty_cylinder_drive(self):
        usb = fake.FakeUnit(cylinders=40)
        result = max_track.measure(usb, 48)
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 39)

    def test_a_drive_the_probe_never_reaches_the_end_of(self):
        usb = fake.FakeUnit(cylinders=90)
        result = max_track.measure(usb, 48)
        self.assertTrue(result.saturated)
        self.assertEqual(result.max_cylinder, 48)

    def test_step_loss_inflates_a_single_measurement(self):
        # The pathology which made this probe report a cylinder too many.
        # A drive stopping at 39, probed at 42, slips three steps and hands
        # back a travel of 42 -- indistinguishable from having arrived.
        usb = fake.FakeUnit(cylinders=40, step_loss_period=4)
        result = max_track.measure(usb, 42)
        self.assertEqual(result.max_cylinder, 42)
        self.assertTrue(result.saturated)

    def test_the_full_search_defeats_step_loss(self):
        # Four consecutive probes cover every residue, so the minimum lands
        # on the truth however the slipping falls.
        for period in (2, 4):
            usb = fake.FakeUnit(cylinders=40, step_loss_period=period)
            result = max_track.search(usb, 60, report=lambda line: None)
            self.assertEqual(result.max_cylinder, 39,
                             'loss period %d' % period)
            self.assertEqual(result.cylinders, 40)

    def test_a_firmware_limit_is_not_a_drive_limit(self):
        usb = fake.FakeUnit(cylinders=90, firmware_limit=50)
        result = max_track.measure(usb, 60)
        self.assertEqual(result.status, max_track.FW_LIMIT)

    def test_the_search_starts_low_enough_for_a_small_drive(self):
        # A 37-cylinder drive must not be slammed into its stop by the first
        # pass. Nothing may go more than one escalation past the stop.
        usb = fake.FakeUnit(cylinders=37)
        max_track.search(usb, 60, report=lambda line: None)
        furthest = max(cylinder for cylinder, _ in usb.seeks)
        self.assertLessEqual(furthest - 36, max_track.ESCALATE_STEP * 2)


class TestNonInteractive(unittest.TestCase):
    """--non-interactive declines; --yes approves. They are opposites."""

    def test_declining_leaves_a_destructive_probe_unrun(self):
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: False)
        core.run_all(ctx, [probe])
        self.assertFalse(probe.ran)
        self.assertEqual(ctx.results['writes'].status, 'skipped')

    def test_the_skip_is_recorded_rather_than_omitted(self):
        # A profile must be able to tell "nobody approved this" from
        # "this failed".
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: False)
        core.run_all(ctx, [probe])
        self.assertIn('approved', ctx.results['writes'].as_dict()['reason'])

    def test_nothing_waits_when_there_is_nobody_to_wait_for(self):
        ctx = context(pause=None)
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY)])
        self.assertIn('Load ANY disk', ctx.report.text)


if __name__ == '__main__':
    unittest.main()

# Local variables:
# python-indent: 4
# End:
