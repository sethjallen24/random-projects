"""Offline design and UI lifecycle regressions. No live services."""
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
import gui
import theme
import team_maker_gui as tm
import app
from staffing_moves_gui import StaffingMovesPanel
from ui_runtime import POLL_BATCH, UiDispatcher


class FakeRoot:
    def __init__(self):
        self.owner = threading.get_ident()
        self.callbacks = {}
        self.next_id = 0
    def after(self, delay, callback):
        assert threading.get_ident() == self.owner, 'worker accessed Tk'
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id
    def after_cancel(self, token):
        assert threading.get_ident() == self.owner, 'worker accessed Tk'
        self.callbacks.pop(token, None)
    def tick(self):
        token = next(iter(self.callbacks))
        self.callbacks.pop(token)()


class UiRegressionTests(unittest.TestCase):
    def test_navigation_hides_inactive_views_without_stopping_jobs(self):
        shell = app.PartnersToolboxApp.__new__(app.PartnersToolboxApp)
        shell.pages = {key: mock.Mock() for key, *_ in shell.WORKSPACES}
        shell.nav_buttons = {key: mock.Mock() for key in shell.pages}
        shell.page_title = mock.Mock(); shell.page_subtitle = mock.Mock()
        shell.bp_gui = mock.Mock(); shell.team_maker_gui = mock.Mock()
        shell.sheet_gui = mock.Mock(); shell.chat_gui = mock.Mock()
        for key in ('staffing', 'teams', 'slack', 'chat', 'staffing'):
            for page in shell.pages.values(): page.reset_mock()
            shell.show_workspace(key)
            shell.pages[key].grid.assert_called_once()
            shell.pages[key].grid_remove.assert_not_called()
            for other, page in shell.pages.items():
                if other != key:
                    page.grid_remove.assert_called_once(); page.grid.assert_not_called()
        for tool in (shell.bp_gui, shell.team_maker_gui, shell.sheet_gui, shell.chat_gui):
            self.assertEqual(tool.mock_calls, [])

    def test_modern_shell_keeps_one_instance_and_shared_coordinator(self):
        root = mock.Mock(); root.winfo_screenwidth.return_value = 1920; root.winfo_screenheight.return_value = 1080
        with mock.patch.object(app.theme, 'apply'), mock.patch.object(app.tk, 'StringVar'), \
             mock.patch.object(app.ttk, 'Frame'), mock.patch.object(app.ttk, 'Label'), \
             mock.patch.object(app.ttk, 'Button'), \
             mock.patch.object(app.gui, 'BrightPatternSyncGUI') as agents, \
             mock.patch.object(app.team_maker_gui, 'TeamMakerGUI') as teams, \
             mock.patch.object(app.sheet_slack_gui, 'SheetToSlackGUI') as slack, \
             mock.patch.object(app.sheet_chat_gui, 'SheetToChatGUI') as chat:
            shell = app.PartnersToolboxApp(root)
            for key, *_ in shell.WORKSPACES: shell.show_workspace(key)
            for factory in (agents, teams, slack, chat): self.assertEqual(factory.call_count, 1)
            self.assertIs(shell.bp_gui.staffing.team_maker, shell.team_maker_gui)
            self.assertIs(shell.team_maker_gui.coordinator, shell.bp_gui.staffing)
            self.assertEqual(root.bind.call_count, 5)

    def test_worker_submission_never_calls_tk(self):
        root = FakeRoot()
        dispatcher = UiDispatcher(root)
        results = []
        thread = threading.Thread(target=lambda: dispatcher.submit(lambda: results.append(threading.get_ident())))
        thread.start(); thread.join()
        self.assertEqual(results, [])
        self.assertEqual(len(root.callbacks), 1)
        root.tick()
        self.assertEqual(results, [root.owner])
        dispatcher.close()

    def test_callback_flood_yields_and_preserves_order(self):
        root = FakeRoot(); dispatcher = UiDispatcher(root); results = []
        for i in range(POLL_BATCH * 2 + 1):
            dispatcher.submit(lambda value=i: results.append(value))
        root.tick()
        self.assertEqual(results, list(range(POLL_BATCH)))
        self.assertEqual(len(root.callbacks), 1)
        root.tick(); root.tick()
        self.assertEqual(results, list(range(POLL_BATCH * 2 + 1)))
        dispatcher.close()

    def test_close_discards_pending_and_late_worker_callbacks(self):
        root = FakeRoot(); dispatcher = UiDispatcher(root); result = []
        dispatcher.submit(lambda: result.append('pending'))
        dispatcher.close()
        worker = threading.Thread(target=lambda: dispatcher.submit(lambda: result.append('late')))
        worker.start(); worker.join()
        dispatcher.close()
        self.assertFalse(root.callbacks)
        self.assertTrue(dispatcher._queue.empty())
        self.assertFalse(result)

    def test_close_during_callback_does_not_reschedule(self):
        root = FakeRoot(); dispatcher = UiDispatcher(root)
        dispatcher.submit(dispatcher.close)
        later = mock.Mock(); dispatcher.submit(later)
        root.tick()
        later.assert_not_called()
        self.assertFalse(root.callbacks)

    def test_callback_error_does_not_stop_future_dispatch(self):
        root = FakeRoot(); dispatcher = UiDispatcher(root); later = mock.Mock()
        dispatcher.submit(lambda: (_ for _ in ()).throw(ValueError('broken callback')))
        dispatcher.submit(later)
        with self.assertRaises(ValueError): root.tick()
        self.assertEqual(len(root.callbacks), 1)
        root.tick(); later.assert_called_once()
        dispatcher.close()




    def test_theme_applies_to_each_new_root(self):
        class Root:
            def winfo_toplevel(self): return self
            def configure(self, **kwargs): pass
            def option_add(self, pattern, value): pass
        first, second = Root(), Root()
        style = mock.MagicMock(); style.theme_names.return_value = ['clam']
        font = mock.MagicMock(); font.cget.side_effect = lambda name: 'Arial' if name == 'family' else 9
        with mock.patch.object(theme.ttk, 'Style', return_value=style) as styles, \
             mock.patch.object(theme.tkfont, 'nametofont', return_value=font) as fonts:
            theme.apply(first); theme.apply(first); theme.apply(second)
        self.assertEqual(styles.call_count, 2)
        self.assertTrue(all(call.kwargs['root'] in (first, second) for call in fonts.call_args_list))

    def test_failed_theme_application_can_be_retried(self):
        root = SimpleNamespace(winfo_toplevel=lambda: root)
        with mock.patch.object(theme.ttk, 'Style', side_effect=RuntimeError('style failed')):
            with self.assertRaises(RuntimeError): theme.apply(root)
        self.assertFalse(getattr(root, '_partners_theme_applied', False))




    def test_scrolling_supervisors_does_not_erase_global_wheel_bindings(self):
        panel = tm.TeamMakerGUI.__new__(tm.TeamMakerGUI)
        widget = mock.Mock(); panel.sup_canvas = mock.Mock()
        panel._bind_wheel(widget)
        widget.bind_all.assert_not_called(); widget.unbind_all.assert_not_called()
        self.assertEqual(panel._on_wheel(SimpleNamespace(delta=1)), 'break')
        panel.sup_canvas.yview_scroll.assert_called_once_with(-1, 'units')
        panel._on_wheel(SimpleNamespace(num=5))
        self.assertEqual(panel.sup_canvas.yview_scroll.call_args.args, (1, 'units'))

    def test_log_trimming_preserves_recent_lines_and_read_only_state(self):
        box = mock.Mock(); box.yview.return_value = (0.9, 1.0)
        box.index.return_value = '6000.0'
        theme.append_log(box, 'new result\n')
        box.insert.assert_called_once_with('end', 'new result\n')
        box.delete.assert_called_once_with('1.0', '1001.0')
        self.assertEqual(box.configure.call_args.kwargs, {'state': 'disabled'})
        box.see.assert_called_once_with('end')

    def test_log_does_not_jump_when_reading_older_results(self):
        box = mock.Mock(); box.yview.return_value = (0.1, 0.5); box.index.return_value = '30.0'
        theme.append_log(box, 'new\n')
        box.see.assert_not_called(); box.delete.assert_not_called()

    def test_log_error_still_restores_read_only_state(self):
        box = mock.Mock(); box.yview.return_value = (0, 1); box.insert.side_effect = RuntimeError('gone')
        with self.assertRaises(RuntimeError): theme.append_log(box, 'new')
        self.assertEqual(box.configure.call_args.kwargs, {'state': 'disabled'})

    def test_full_disk_log_receives_entries_independently_of_display_limit(self):
        panel = gui.BrightPatternSyncGUI.__new__(gui.BrightPatternSyncGUI)
        panel.log_widget = mock.Mock(); panel._log_file = mock.Mock()
        with mock.patch.object(theme, 'append_log'):
            panel._append_log('result\n')
        panel._log_file.write.assert_called_once_with('result\n')

    def test_active_agents_batches_logs_and_leaves_time_for_other_ui_events(self):
        panel = gui.BrightPatternSyncGUI.__new__(gui.BrightPatternSyncGUI)
        panel.root = FakeRoot(); panel._closed = False; panel.line_queue = queue.Queue()
        panel._append_log = mock.Mock()
        for i in range(POLL_BATCH * 2 + 1): panel.line_queue.put(('log', f'{i}\n'))
        panel._poll_queue()
        panel._append_log.assert_called_once_with(''.join(f'{i}\n' for i in range(POLL_BATCH)))
        self.assertEqual(panel.line_queue.qsize(), POLL_BATCH + 1)
        panel.root.tick(); panel.root.tick()
        self.assertEqual(''.join(call.args[0] for call in panel._append_log.call_args_list),
                         ''.join(f'{i}\n' for i in range(POLL_BATCH * 2 + 1)))
        panel._closed = True
        panel.root.tick()
        self.assertFalse(panel.root.callbacks)

    def test_team_maker_flushes_logs_before_completion_summary(self):
        panel = tm.TeamMakerGUI.__new__(tm.TeamMakerGUI)
        panel.root = FakeRoot(); panel._closed = False; panel.line_queue = queue.Queue()
        seen = []; panel._append_log = seen.append
        panel._finish_batch = lambda *args: seen.append('summary')
        for event in [('log', 'first\n'), ('log', 'second\n'), ('batch_done', ())]:
            panel.line_queue.put(event)
        panel._poll_queue()
        self.assertEqual(seen, ['first\nsecond\n', 'summary'])

    def test_active_agents_poll_survives_a_bad_completion_callback(self):
        panel = gui.BrightPatternSyncGUI.__new__(gui.BrightPatternSyncGUI)
        panel.root = FakeRoot(); panel._closed = False; panel.line_queue = queue.Queue()
        panel._append_log = mock.Mock()
        panel._set_buttons_enabled = mock.Mock(side_effect=ValueError('bad widget'))
        panel.line_queue.put(('done', True)); panel.line_queue.put(('log', 'later\n'))
        with self.assertRaises(ValueError): panel._poll_queue()
        panel.root.tick()
        panel._append_log.assert_called_once_with('later\n')


    def test_move_editor_reveals_on_selection_without_guessing_unresolved_teams(self):
        panel = StaffingMovesPanel.__new__(StaffingMovesPanel)
        panel.fix_rows = mock.Mock(); panel.problem_var = mock.Mock()
        panel._current = lambda: (None, None)
        panel._show_selected()
        panel.fix_rows.pack_forget.assert_called_once()
        panel.fix_rows.pack.assert_not_called()
        unresolved = SimpleNamespace(problem='ambiguous', phrase='project', candidates=['A', 'B'], team=None)
        move = SimpleNamespace(source=unresolved, dest=unresolved, center='611', kind='move', count=2, status='', route_problem='')
        panel._current = lambda: (0, move)
        panel._center_teams = lambda *args: ['A', 'B']
        class Combo(dict):
            def state(self, flags): pass
        panel.src_combo = Combo(); panel.dst_combo = Combo()
        panel.center_combo = mock.Mock(); panel.use_btn = mock.Mock(); panel.job_running = False
        panel.center_var = mock.Mock(); panel.src_var = mock.Mock(); panel.dst_var = mock.Mock()
        panel._show_selected()
        panel.fix_rows.pack.assert_called_once()
        self.assertEqual(panel.src_combo['values'], ['A', 'B'])
        panel.src_var.set.assert_called_once_with('')
        panel.dst_var.set.assert_called_once_with('')


def run_all():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    if not result.wasSuccessful(): raise AssertionError('UI regression checks failed')

if __name__ == '__main__': run_all()
