"""Offline regressions: real parser/allocator/executor, mocked admin I/O only."""
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import staffing_moves as sm
import staffing_targets as st
import team_maker_bp as bp
from staffing_moves_gui import StaffingMovesPanel
from gui import BrightPatternSyncGUI

A = 'X-001-1026pt X TX CD 5 Cell 611'
B = 'Y-002-1026pt Y CA Statewide Land 611'
S = 'Z-003-1026pt Z TX Spanish 611'

def fill(team=A, count=10, center='611', ts='1'):
    return sm.Move(ts, '', '', center, '', sm.Match('pool', sm.logged_in_team(center)),
                   sm.Match(team, team), count, kind='fill')

class AdminHarness:
    """Fresh counts, selection and real move effects over mutable rosters."""
    def __init__(self, roster, partial=False, missing=False, counts=None):
        self.roster = {k: list(v) for k,v in roster.items()}
        self.open = None; self.selection = []; self.moves = []; self.partial = partial
        self.missing = missing; self.counts = counts or {}
        self.session = mock.Mock(); self.session.page.return_value = self
    def count(self, page, team, **kw):
        return self.counts.get(team, len(self.roster.get(team, [])))
    def select(self, page, picked, listed, **kw):
        self.selection = list(picked[:-1] if self.partial else picked)
        return {'matched': self.selection, 'unmatched': list(picked[-1:]) if self.partial else []}
    def move(self, page, dest, dry_run=False, **kw):
        if dry_run: return
        self.moves.append((self.open, dest, list(self.selection)))
        for name in self.selection:
            self.roster[self.open].remove(name)
            self.roster.setdefault(dest, []).append(name)
    def verify(self, page, dest, names, **kw):
        return {'found': [] if self.missing else list(names), 'missing': list(names) if self.missing else []}
    def __enter__(self):
        self.stack = ExitStack()
        replacements = {'admin_teams.refresh_teams_section': lambda *a,**kw: None,
                        'admin_teams.dismiss_stuck_modal_if_any': lambda *a,**kw: None,
                        'admin_teams.open_team': lambda p,n,**kw: setattr(self,'open',n),
                        'admin_teams.open_members_tab': lambda *a,**kw: None,
                        'admin_teams.move_selected_members_to_team': self.move,
                        'admin_teams.verify_agents_present_in_team': self.verify,
                        'staffing_moves.team_member_count': self.count,
                        'staffing_moves.read_member_names': lambda *a,**kw: list(self.roster.get(self.open, [])),
                        'staffing_moves.select_members_fast': self.select,
                        'staffing_moves.ensure_campaign_running': lambda *a,**kw: 'running'}
        for key,value in replacements.items(): self.stack.enter_context(mock.patch(key,value))
        return self
    def __exit__(self,*args): self.stack.close()
    def execute(self, moves, **opts):
        return sm.execute_moves(moves,'url','u','p',opts.pop('dry_run',False),True,1000,
                                session=self.session, log=lambda _:None, **opts)

class ParserChecks(unittest.TestCase):
    def teams(self, *names): return [sm.parse_team_name(n) for n in names]
    def test_inline_center_and_agent_count(self):
        moves,_=sm.parse_message('611: 3 agents from X TX CD5 Cell to Y CA Land', self.teams(A,B))
        self.assertTrue(moves[0].ready); self.assertEqual(moves[0].count,3)
    def test_district_type_distinguishes_similar_names(self):
        hd=A.replace('CD 5','HD 5')
        self.assertEqual(sm.match_project('X TX CD5 Cell','611',self.teams(A,hd)).team,A)
        self.assertEqual(sm.match_project('X TX HD5 Cell','611',self.teams(A,hd)).team,hd)
        self.assertIsNone(sm.match_project('X TX 5 Cell','611',self.teams(A,hd)).team)
    def test_explicit_wrong_center_is_never_remapped(self):
        self.assertIsNone(sm.match_project(A.replace('611','Squad'),'611',self.teams(A)).team)
    def test_full_name_and_duplicate_index(self):
        self.assertEqual(sm.match_project(A,'611',sm.TeamIndex(self.teams(A,A))).team,A)
    def test_multiword_state_and_spaced_center(self):
        team=A.replace('TX','NC').replace('611','InfoConnect')
        moves,_=sm.parse_message('Info    C\n4 X North Carolina CD5 Cell', self.teams(team))
        self.assertTrue(moves[0].ready); self.assertEqual(moves[0].dest.team,team)
    def test_large_counts_and_zero(self):
        moves,_=sm.parse_message('611\n1,200 X TX CD 5 Cell\n0 Y CA Land',self.teams(A,B))
        self.assertEqual([m.count for m in moves],[1200])
    def test_channel_conflict_and_same_team(self):
        self.assertIsNone(sm.match_project('X TX CD5 Cell Land','611',self.teams(A)).team)
        self.assertFalse(sm.Move('','','','611','',sm.Match(A,A),sm.Match(A,A)).ready)
    def test_cross_center_route_is_invalid(self):
        move=fill(A.replace('611','Squad'))
        self.assertFalse(move.ready); self.assertIn('Squad',move.route_problem)

class RegistryChecks(unittest.TestCase):
    def arm(self, registry, move, supplied=0):
        move.status='done: moved' if supplied else 'nothing to move'
        move.planned_count=supplied; move.verified_count=supplied
        registry.record_results([move])
    def test_unapproved_and_dry_run_are_inactive(self):
        r=st.PlanRegistry(); m=fill(); r.received([m]); self.assertEqual(r.pending(),[])
        m.status='done: 1 moved'; r.record_results([m],True); self.assertEqual(r.pending(),[])
    def test_empty_initial_pool_arms_shortage(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m)
        self.assertEqual(len(r.pending()),1)
    def test_full_initial_move_needs_no_late_fill(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m,10);self.assertEqual(r.pending(),[])
    def test_failed_verification_never_arms(self):
        r=st.PlanRegistry();m=fill();r.received([m]);m.status='CHECK: 1 missing'
        r.record_results([m]);self.assertEqual(r.pending(),[])
    def test_new_plan_replaces_only_its_center(self):
        r=st.PlanRegistry();a=fill();b=fill(A.replace('611','Squad'),center='Squad')
        r.received([a,b]);self.arm(r,a);self.arm(r,b);old=r.pending()[0]
        r.received([fill(ts='2')]);self.assertFalse(r.active(old));self.assertEqual([t.center for t in r.pending()],['Squad'])
    def test_clear_revokes_queued_callback(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m);target=r.pending()[0]
        r.clear();self.assertFalse(r.active(target))
    def test_top_and_spanish_are_excluded(self):
        teams=[sm.parse_team_name(n) for n in (A,B,S)]
        moves,skipped=sm.parse_message('611\n10 X TX CD5 Cell\nTop 5 X TX CD5 Cell\n8 Z TX Spanish',teams)
        r=st.PlanRegistry();r.received(moves,skipped)
        for m in moves:self.arm(r,m)
        self.assertEqual(r.pending(),[])
    def test_old_top_message_cannot_replace_new_numeric_plan(self):
        r=st.PlanRegistry();m=fill(ts='2')
        skipped=sm.Skipped('1','','','Top 5 X TX Cell','top agents -- list required',center='611',excluded_team=A)
        r.received([m],[skipped]);self.arm(r,m);self.assertEqual(len(r.pending()),1)
    def test_group_project_and_names_reserved(self):
        r=st.PlanRegistry();m=fill();g=sm.Move('1','','','611','',sm.Match('',sm.logged_in_team('611')),
                                            sm.Match(A,A),kind='group',names=['Ann Lee'],group='Spanish')
        r.received([m,g]);self.arm(r,m);self.assertEqual(r.pending(),[])
        self.assertIn(sm._normalize('Ann Lee'),r.reserved('611'))
    def test_zero_only_plan_replaces_old_shortage(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m)
        _, skipped=sm.parse_message('611\n0 X TX CD5 Cell',[sm.parse_team_name(A)],msg_ts='2')
        r.received([],skipped);self.assertEqual(r.pending(),[])
    def test_group_only_plan_replaces_old_shortage(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m)
        group=sm.Move('2','','','611','',sm.Match('pool',sm.logged_in_team('611')),sm.Match(S,S),kind='group',names=['Ann Lee'])
        r.received([group]);self.assertEqual(r.pending(),[])
    def test_unknown_count_held_and_target_completion(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m);t=r.pending()[0]
        r.observed(t,None);self.assertIsNone(t.remaining)
        r.observed(t,10);self.assertEqual(r.pending(),[])
    def test_later_go_home_cancels_even_empty_project(self):
        r=st.PlanRegistry();m=fill();r.received([m]);self.arm(r,m)
        home=sm.Move('2','','','611','',sm.Match(A,A),sm.Match('',sm.agents_all_team('611')),kind='go home',status='nothing to move')
        r.record_results([home]);self.assertEqual(r.pending(),[])
    def test_duplicate_destination_targets_are_held(self):
        r=st.PlanRegistry();moves=[fill(count=10),fill(count=20)];r.received(moves)
        for m in moves:self.arm(r,m)
        self.assertEqual(r.pending(),[])
    def test_allocation_priority_caps_and_unknown(self):
        a=st.Target('611',A,20,1,approved=True);b=st.Target('611',B,10,1,approved=True)
        self.assertEqual(st.allocate([b,a],8,{A:15,B:0}),[(a,5),(b,3)])
        self.assertEqual(st.allocate([a,b],8,{A:None,B:10}),[])
    def test_allocation_never_exceeds_pool_or_target_across_100_sizes(self):
        targets=[st.Target('611',str(i),n,1,approved=True) for i,n in enumerate((7,31,18,2))]
        for pool in range(100):
            out=st.allocate(targets,pool,{t.team:1 for t in targets})
            self.assertLessEqual(sum(n for t,n in out),pool)
            self.assertEqual([t.requested for t,n in out],sorted([t.requested for t,n in out],reverse=True))
            for t,n in out:self.assertLessEqual(n,t.requested-1)

class ExecutionChecks(unittest.TestCase):
    def test_late_caps_to_fresh_destination_total(self):
        move=fill(count=8);move.target_count=10
        with AdminHarness({move.source.team:['a','b','c'],A:[str(i) for i in range(9)]}) as h:
            self.assertEqual(h.execute([move]),0);self.assertEqual(len(h.roster[A]),10)
            self.assertEqual(move.verified_count,1)
    def test_unknown_destination_count_holds(self):
        move=fill();move.target_count=10
        with AdminHarness({move.source.team:['a'],A:[]},counts={A:None}) as h:
            h.execute([move]);self.assertEqual(h.moves,[]);self.assertIn('HELD',move.status)
    def test_partial_selection_never_moves_and_remainder_is_held(self):
        first=sm.Move('1','','','611','',sm.Match(A,A),sm.Match(B,B),count=2)
        rest=sm.Move('1','','','611','',sm.Match(A,A),sm.Match(S,S),count='rest')
        with AdminHarness({A:['a','b','c']},partial=True) as h:
            self.assertEqual(h.execute([first,rest]),1);self.assertEqual(h.moves,[])
            self.assertTrue(rest.status.startswith('HELD'))
    def test_invalid_route_is_reported_without_opening_browser(self):
        move=fill(A.replace('611','Squad'))
        with AdminHarness({}) as h:
            self.assertEqual(h.execute([move]),1)
            self.assertTrue(move.status.startswith('HELD'))
            h.session.page.assert_not_called()
    def test_settling_numeric_badge_does_not_block_complete_members(self):
        move=fill(count=2)
        with AdminHarness({move.source.team:['a','b']}) as h:
            values=iter((3,2))
            with mock.patch.object(sm,'team_member_count',side_effect=lambda *a,**k:next(values)):
                self.assertEqual(h.execute([move]),0)
            self.assertEqual(move.verified_count,2)
    def test_reserved_list_names_stay_in_pool(self):
        move=fill(count=3);move.exclude_names={sm._normalize('Ann Lee')}
        with AdminHarness({move.source.team:['Ann Lee','Other Agent']}) as h:
            h.execute([move])
            self.assertEqual(h.roster[move.source.team],['Ann Lee'])
            self.assertEqual(h.roster[A],['Other Agent'])
    def test_incomplete_member_list_never_claims_everyone(self):
        move=fill()
        with AdminHarness({move.source.team:['a']},counts={move.source.team:3}) as h:
            self.assertEqual(h.execute([move]),1);self.assertEqual(h.moves,[])
    def test_generation_cancellation_before_dialog(self):
        move=fill();move.before_move=lambda:False
        with AdminHarness({move.source.team:['a']}) as h:
            h.execute([move]);self.assertEqual(h.moves,[])
    def test_new_logins_between_two_moves(self):
        moves=[fill(count=1),fill(B,count=1)]
        with AdminHarness({sm.logged_in_team('611'):['first']}) as h:
            def collect():
                if h.moves:h.roster[sm.logged_in_team('611')].append('late')
                return True
            h.execute(moves,between_moves=collect)
            self.assertEqual(h.roster[A],['first']);self.assertEqual(h.roster[B],['late'])
    def test_late_driver_uses_largest_project_and_excludes_list_names(self):
        r=st.PlanRegistry();moves=[fill(count=5),fill(B,count=10)]
        r.received(moves)
        for m in moves:m.status='nothing to move'
        r.record_results(moves)
        opts=dict(base_url='x',username='u',password='p',timeout_ms=1000,headless=True,dry_run=False)
        with AdminHarness({sm.logged_in_team('611'):['late1','late2'],A:['old'],B:['old']}) as h:
            out=st.check_late_arrivals(r,h.session,opts,threading.Event(),log=lambda _:None)
            self.assertEqual([m.dest.team for m in out],[B]);self.assertEqual(len(h.roster[B]),3)
    def test_late_driver_no_pool_skips_project_counts(self):
        r=st.PlanRegistry();m=fill();r.received([m]);m.status='nothing to move';r.record_results([m])
        opts=dict(base_url='x',username='u',password='p',timeout_ms=1000,headless=True,dry_run=False)
        with AdminHarness({sm.logged_in_team('611'):[]}) as h, mock.patch.object(sm,'team_member_count',wraps=h.count) as count:
            self.assertEqual(st.check_late_arrivals(r,h.session,opts,threading.Event()),[])
            self.assertEqual([call.args[1] for call in count.call_args_list],[sm.logged_in_team('611')])
    def test_late_driver_is_center_scoped_and_stops_at_target(self):
        r=st.PlanRegistry();a=fill(count=2);b=fill(A.replace('611','Squad'),3,'Squad')
        r.received([a,b])
        for m in (a,b):m.status='nothing to move'
        r.record_results([a,b]);opts=dict(base_url='x',username='u',password='p',timeout_ms=1000,headless=True,dry_run=False)
        with AdminHarness({sm.logged_in_team('611'):['c1','c2','c3'],sm.logged_in_team('Squad'):['s1'],A:[]}) as h:
            st.check_late_arrivals(r,h.session,opts,threading.Event(),log=lambda _:None)
            self.assertEqual(h.roster[A],['c1','c2']);self.assertEqual(h.roster[b.dest.team],['s1'])
            h.roster[sm.logged_in_team('611')].append('c4')
            st.check_late_arrivals(r,h.session,opts,threading.Event(),log=lambda _:None)
            self.assertEqual(len(h.roster[A]),2)

class CoordinatorChecks(unittest.TestCase):
    def panel(self):
        p=StaffingMovesPanel.__new__(StaffingMovesPanel)
        p.gui=mock.Mock(is_running=False,is_watching=False,env_error=None)
        p.resume_aa=True;p.watching=True;p.late_enabled=True;p.coordinated_watch=False
        p._collection_stop=None;p.log=mock.Mock();p.session=mock.Mock()
        p._logged_pairs=[{'sourceTeam':'All','destinationTeam':'Logged'}]
        p.gui.team_pairs=p._logged_pairs;p.gui.timeout_ms=1000
        p._logged_interval=30;p._logged_in_next=0;p._opts={'headless':True,'dry_run':False}
        return p
    def test_handoff_keeps_same_browser_and_visible_stop_control(self):
        p=self.panel();p._finish_watch_handoff()
        self.assertTrue(p.coordinated_watch);self.assertTrue(p.gui.is_watching)
        self.assertIs(p.gui._staffing_watch_owner,p);p.gui._on_start_watch.assert_not_called()
        p._release_for_active_agents();p.session.close.assert_not_called()
    def test_stop_logged_in_stops_collection_only(self):
        p=self.panel();p._finish_watch_handoff();p.stop_logged_in();p._finish_watch_handoff()
        self.assertFalse(p.gui.is_watching);self.assertFalse(p.resume_aa)
        p.gui._on_start_watch.assert_not_called()
    def test_stopping_slack_returns_original_logged_in_watch(self):
        p=self.panel();p._finish_watch_handoff();p.watching=False;p._release_for_active_agents()
        p._finish_watch_handoff();p.session.close.assert_called_once();p.gui._on_start_watch.assert_called_once()
    def test_collection_reuses_original_routine_and_throttles(self):
        p=self.panel();p._finish_watch_handoff()
        with mock.patch('main.process_pair') as process, mock.patch('admin_teams.refresh_teams_section'), mock.patch('login.login'):
            self.assertTrue(p._sync_logged_in(threading.Event()));self.assertFalse(p._sync_logged_in(threading.Event()))
            self.assertEqual(process.call_count,1)
            self.assertIs(process.call_args.args[0],p.session.page.return_value)
    def test_collection_stop_checked_between_centers(self):
        p=self.panel();p._logged_pairs*=2;p._finish_watch_handoff()
        with mock.patch('main.process_pair',side_effect=lambda *a,**k:p.stop_logged_in()) as process, mock.patch('admin_teams.refresh_teams_section'), mock.patch('login.login'):
            p._sync_logged_in(threading.Event());self.assertEqual(process.call_count,1)
    def test_due_late_check_takes_priority_over_collection(self):
        import collections, queue
        p=self.panel();p._finish_watch_handoff();p.jobs=collections.deque();p.q=queue.Queue()
        p.job_running=False;p._dry_live=False;p._late_next=0;p._logged_in_next=0
        p.targets=mock.Mock();p.targets.pending.return_value=[object()]
        p.parent=mock.Mock();p._pump=mock.Mock()
        p._poll();self.assertEqual(p.jobs[0][0],'late')
    def test_active_coordinator_does_not_close_idle_browser(self):
        p=self.panel();p._finish_watch_handoff();p._idle_tick()
        p.session.close.assert_not_called()
    def test_original_logged_in_stop_routes_to_coordinator(self):
        g=BrightPatternSyncGUI.__new__(BrightPatternSyncGUI);g._staffing_watch_owner=mock.Mock()
        g._paint_watch_toggle=mock.Mock();g._on_stop_watch();g._staffing_watch_owner.stop_logged_in.assert_called_once()

class CacheChecks(unittest.TestCase):
    def test_cache_is_tenant_scoped_and_malformed_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sm,'TEAMS_CACHE_PATH',Path(d)/'cache.json'):
            sm.save_team_cache([A],base_url='https://one/admin/')
            self.assertEqual(sm.load_team_cache(base_url='https://ONE/admin')[0][0].name,A)
            self.assertIsNone(sm.load_team_cache(base_url='https://two/admin')[0])
            for data in ('[]','{"saved":"bad"}','{"saved":99999999999,"teams":[]}'):
                sm.TEAMS_CACHE_PATH.write_text(data);self.assertIsNone(sm.load_team_cache()[0])

class TeamMakerChecks(unittest.TestCase):
    def test_project_formatting_equivalence_preserves_project_and_channel(self):
        self.assertEqual(bp._campaign_key('FLA – 078 Land'),bp._campaign_key('FLA-078 Land'))
        self.assertNotEqual(bp._campaign_key('FLA-078 Land'),bp._campaign_key('FLA-078 Cell'))
        self.assertNotEqual(bp._campaign_key('FLA-078 Land'),bp._campaign_key('FLA-079 Land'))
    def test_center_aliases_match_for_duplicate_detection(self):
        self.assertEqual(bp._team_center('Renamed project Tag'),'Tagbilaran')
        self.assertEqual(bp._team_center('Project Squad'),'Squad')
        self.assertEqual(bp._team_center('Project Info C'),'InfoConnect')
    def test_existing_team_result_preserves_actual_destination_name(self):
        result=bp.ExistingTeamResult('Renamed project Squad')
        self.assertEqual(result,['already existed']);self.assertEqual(result.existing_team,'Renamed project Squad')
    def test_existing_project_prevents_add_even_with_a_different_name(self):
        bot=bp.BrightPatternBot({},log=lambda _:None)
        for method in ('ensure_logged_in','open_teams','_dismiss_save_prompt','_recover','screenshot'):
            setattr(bot,method,mock.Mock())
        bot.team_count=mock.Mock(return_value=200);bot.team_exists=mock.Mock(return_value=False)
        bot.existing_project_team=mock.Mock(return_value='Renamed Squad');bot._open_new_team_form=mock.Mock()
        with self.assertRaises(bp.TeamAlreadyExists) as raised:bot.create_team(A.replace('611','Squad'),[],campaign=A.rsplit(' ',1)[0])
        self.assertEqual(raised.exception.team_name,'Renamed Squad');bot._open_new_team_form.assert_not_called()


class Clock:
    def __init__(self): self.now=0.0
    def monotonic(self): return self.now
    def sleep(self, value): self.now += value

class PickerUI:
    """Fake virtual grid; the bot still executes its real search and scan loops."""
    def __init__(self, campaigns=(), teams=(), assigned=None, page_size=2):
        self.campaigns=list(campaigns);self.teams=list(teams);self.assigned=assigned or {}
        self.query='';self.offset=0;self.team=None;self.page_size=page_size
        self.searches=[];self.clicks=[];self.first=self
    def locator(self, selector):
        self.team_mode='assign' not in selector
        return self
    def count(self): return 1
    def press(self, key): pass
    def visible_names(self, teams=False):
        names=self.teams if teams else self.campaigns
        return [n for n in names if self.query.casefold() in n.casefold()][self.offset:self.offset+self.page_size]
    def evaluate(self, script, arg=None):
        if script == bp.JS_TEAM_ROW_NAMES: return self.visible_names(True)
        if script == bp.JS_VIEW_HEADER: return f'Teams >> {self.team} >> Properties' if self.team else 'Teams >> Properties'
        if 'top:e.scrollTop' in script:
            total=len([n for n in (self.teams if self.team_mode else self.campaigns) if self.query.casefold() in n.casefold()])
            return dict(top=self.offset,height=total,view=self.page_size)
        if 'scrollTop = 0' in script or 'scrollTop=0' in script: self.offset=0
        elif 'scrollTop +=' in script: self.offset+=self.page_size
    def wait_for_selector(self,*a,**kw): pass
    def type(self, box, text):
        self.query=text;self.offset=0;self.searches.append(text)
    def names(self, kind, grid):
        if grid == bp.SEL_RIGHT_GRID:return [bp._norm(n) for n in self.assigned.get(self.team,[])]
        return [bp._norm(n) for n in self.visible_names()]

class PickerChecks(unittest.TestCase):
    def bot(self, ui):
        bot=bp.BrightPatternBot({},log=lambda _:None);bot.page=ui
        bot._type_into=ui.type;bot._names=ui.names
        bot._open_picker=mock.Mock();bot._open_form_tab=mock.Mock();bot._click_dialog_button=mock.Mock()
        return bot
    def clock(self):
        clock=Clock();stack=ExitStack()
        stack.enter_context(mock.patch('team_maker_bp.time.monotonic',clock.monotonic))
        stack.enter_context(mock.patch('team_maker_bp.time.sleep',clock.sleep))
        return stack
    def test_picker_scrolls_past_similar_projects_and_returns_exact_one(self):
        names=[A.rsplit(' ',1)[0].replace('001',str(n).zfill(3)) for n in range(1,8)]
        ui=PickerUI(names);bot=self.bot(ui)
        # Full search produces no match because the UI's filter treats typography literally.
        typed=names[6].replace('X-007','X – 007')
        with self.clock():idx,target=bot._find_campaign(typed)
        self.assertEqual(target,bp._norm(names[6]));self.assertIn('X',ui.searches)
        self.assertEqual(ui.names('services',bp.SEL_LEFT_GRID)[idx],target)
    def test_picker_does_not_select_similar_wrong_code(self):
        ui=PickerUI([A.rsplit(' ',1)[0]]);bot=self.bot(ui)
        with self.clock():self.assertEqual(bot._find_campaign(A.rsplit(' ',1)[0].replace('001','999'))[0],-1)
    def test_short_campaign_requires_unique_structured_match(self):
        names=[A.rsplit(' ',1)[0],A.rsplit(' ',1)[0].replace('CD 5','HD 5')]
        self.assertEqual(bp._resolve_campaign_phrase('X TX CD5 Cell',names),names[0])
        with self.assertRaises(bp.BPError):bp._resolve_campaign_phrase('X TX 5 Cell',names)
        self.assertIsNone(bp._resolve_campaign_phrase('X TX CD5',names))
    def test_existing_assignment_found_for_renamed_team_not_similar_project(self):
        project=A.rsplit(' ',1)[0]
        ui=PickerUI(teams=['Renamed one Squad','Renamed two Squad','Other center Tagbilaran'],
                    assigned={'Renamed one Squad':[project.replace('001','002')], 'Renamed two Squad':[project]})
        bot=self.bot(ui)
        with self.clock(), mock.patch('admin_teams.open_team',side_effect=lambda page,name,**k:setattr(ui,'team',name)):
            existing=bot.existing_project_team('New name Squad',project)
        self.assertEqual(existing,'Renamed two Squad')
        self.assertEqual(bot._click_dialog_button.call_count,2)
        self.assertNotIn('Other center Tagbilaran',bot._assignment_cache)
    def test_duplicate_assignments_are_reported_without_creation(self):
        project=A.rsplit(' ',1)[0]
        ui=PickerUI(teams=['One Squad','Two Squad'],assigned={'One Squad':[project],'Two Squad':[project]})
        bot=self.bot(ui)
        with self.clock(), mock.patch('admin_teams.open_team',side_effect=lambda page,name,**k:setattr(ui,'team',name)):
            with self.assertRaises(bp.BPError):bot.existing_project_team('New Squad',project)
    def test_second_create_in_batch_reuses_assignment_reads(self):
        project=A.rsplit(' ',1)[0]
        ui=PickerUI(teams=['Old Squad'],assigned={'Old Squad':[project]});bot=self.bot(ui)
        with self.clock(), mock.patch('admin_teams.open_team',side_effect=lambda page,name,**k:setattr(ui,'team',name)) as opened:
            self.assertEqual(bot.existing_project_team('New Squad',project),'Old Squad')
            self.assertEqual(bot.existing_project_team('Another Squad',project),'Old Squad')
            self.assertEqual(opened.call_count,1)


def run_all():
    suite=unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():raise AssertionError('Staffing accuracy checks failed')
if __name__=='__main__':run_all()
