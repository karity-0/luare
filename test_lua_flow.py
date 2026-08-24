import tempfile
import unittest
import shutil
import subprocess
import sys
import json
import threading
import time
import urllib.request
from pathlib import Path

from lua_flow import (
    normalize_source, annotate, render_lua, render_html, build_cfg,
    build_xrefs, build_project, collect_trace, LiveDebugger, LiveDebugSession, make_debug_server,
)


class FlowTests(unittest.TestCase):
    def analyze(self, src):
        stmts = normalize_source(src)
        meta = annotate(stmts)
        return stmts, meta, render_lua(stmts)

    def test_inline_if_is_split_and_linked(self):
        stmts, meta, out = self.analyze('if x then foo() else bar() end')
        self.assertEqual([s.kind for s in stmts], ['if', 'stmt', 'else', 'stmt', 'end'])
        self.assertIn('-- branch true -> @0x000002 | false -> @0x000003', out)

    def test_local_function_call_uses_same_symbol_id(self):
        stmts, meta, out = self.analyze('local function foo(x)\n return x\nend\nfoo(1)')
        fid = meta['functions']['foo']
        self.assertIn(f'-- {fid} def foo', out)
        self.assertIn(f'-- {fid} call foo -> @0x000001 | return -> <exit>', out)

    def test_assigned_function_gets_scope_and_runtime_order(self):
        stmts, meta, out = self.analyze('local decode = function(x)\n return x\nend\ndecode(1)')
        self.assertEqual(stmts[0].kind, 'function')
        self.assertIn('decode', meta['functions'])
        self.assertIn('-- @0x000001 [src:1]', out)
        self.assertIn('-- @0x000002 [src:4]', out)
        self.assertIn('-- @0x000003 [src:2]', out)
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('"scope": "decode"', page)

    def test_function_definition_skips_body_in_runtime_flow(self):
        _, _, out = self.analyze('function foo()\n print("body")\nend\nprint("after")')
        self.assertIn('-- next -> @0x000002', out)
        self.assertIn('-- return -> <caller>\nend', out)

    def test_statement_ids_follow_first_runtime_visit_across_function_call(self):
        _, _, out = self.analyze('a = 1\nfunction b()\n\nend\nc = 3\nb()')
        self.assertIn('-- @0x000001 [src:1]\n-- next -> @0x000002\na = 1', out)
        self.assertIn('-- @0x000002 [src:2]', out)
        self.assertIn('-- @0x000003 [src:5]\n-- next -> @0x000004\nc = 3', out)
        self.assertIn('-- @0x000004 [src:6]', out)
        self.assertIn('-- @0x000005 [src:4]\n-- return -> <caller>\nend', out)

    def test_called_function_body_is_numbered_before_later_top_level_code(self):
        _, _, out = self.analyze('function b()\nwork()\nend\nb()\nafter = true')
        self.assertIn('-- @0x000002 [src:4]', out)
        self.assertIn('-- @0x000003 [src:2]', out)
        self.assertIn('-- @0x000004 [src:3]', out)
        self.assertIn('-- @0x000005 [src:5]', out)

    def test_return_marks_dynamic_caller(self):
        _, _, out = self.analyze('function foo()\n return 1\nend')
        self.assertIn('-- return -> <caller>', out)

    def test_break_targets_after_loop(self):
        _, _, out = self.analyze('while x do\n if y then break end\n x = x - 1\nend\nprint("done")')
        self.assertIn('-- break -> @0x000007', out)

    def test_call_at_if_arm_tail_returns_after_if(self):
        _, _, out = self.analyze('if x then foo() else bar() end\nprint("after")')
        self.assertIn('call foo -> <external> | return -> @0x000006', out)
        self.assertIn('call bar -> <external> | return -> @0x000006', out)

    def test_call_at_loop_tail_returns_to_loop_header(self):
        _, _, out = self.analyze('while x do\n foo()\nend\nprint("after")')
        self.assertIn('call foo -> <external> | return -> @0x000001', out)

    def test_call_in_condition_resumes_condition_before_branch(self):
        _, _, out = self.analyze('if ready() then foo() end')
        self.assertIn('call ready -> <external> | return -> <resume:@0x000001>', out)

    def test_goto_resolves_label_in_same_scope(self):
        _, meta, out = self.analyze('goto done\nprint("skip")\n::done::\nprint("ok")')
        self.assertIn('-- goto L0x', out)
        self.assertIn('done -> @0x000003', out)
        self.assertIn('-- L0x', out)
        self.assertIn('label done', out)
        self.assertTrue(meta['labels'])

    def test_goto_does_not_cross_function_scope(self):
        _, _, out = self.analyze('::done::\nfunction f()\n goto done\nend')
        self.assertIn('-- goto done -> <label:unresolved>', out)

    def test_and_condition_describes_short_circuit(self):
        _, _, out = self.analyze('if a() and b() then ok() end')
        self.assertIn('cond-step E0x', out)
        self.assertRegex(out, r'\[1\] a\(\) \| true -> E0x[0-9A-F]+ \| false -> <exit>')
        self.assertIn('[2] b() | true -> @0x000002 | false -> <exit>', out)

    def test_or_condition_describes_short_circuit(self):
        _, _, out = self.analyze('while cached or refresh() do break end')
        self.assertRegex(out, r'\[1\] cached \| true -> @0x000002 \| false -> E0x[0-9A-F]+')
        self.assertIn('[2] refresh() | true -> @0x000002 | false -> <exit>', out)

    def test_mixed_and_or_respects_lua_precedence(self):
        _, _, out = self.analyze('if a() and b() or c() then ok() end')
        self.assertRegex(out, r'\[1\] a\(\) \| true -> E0x[0-9A-F]+ \| false -> E0x[0-9A-F]+')
        self.assertRegex(out, r'\[2\] b\(\) \| true -> @0x000002 \| false -> E0x[0-9A-F]+')
        self.assertIn('[3] c() | true -> @0x000002 | false -> <exit>', out)

    def test_while_has_back_edge(self):
        stmts, meta, out = self.analyze('while x > 0 do\nx=x-1\nend')
        self.assertIn('-- loop -> @0x000001', out)

    def test_semicolon_split_respects_call_args(self):
        stmts, _, _ = self.analyze('foo("a;b"); bar()')
        self.assertEqual(len(stmts), 2)

    def test_nested_calls_are_emitted_as_eval_dependencies(self):
        _, meta, out = self.analyze('result = foo(bar(), baz(qux()))')
        self.assertIn('eval-call A0x', out)
        self.assertIn('bar -> <external> | return -> <eval:A0x', out)
        self.assertIn('qux -> <external> | return -> <eval:A0x', out)
        self.assertIn('baz waits <- [A0x', out)
        self.assertIn('foo waits <- [A0x', out)
        self.assertIn('sibling-order:unspecified', out)
        self.assertEqual(len(meta['call_sites'][0]), 4)

    def test_duplicate_calls_keep_distinct_eval_sites(self):
        _, meta, out = self.analyze('foo(bar(), bar())')
        sites = meta['call_sites'][0]
        bars = [site for site in sites if site['name'] == 'bar']
        self.assertEqual(len(bars), 2)
        self.assertNotEqual(bars[0]['eid'], bars[1]['eid'])
        self.assertIn('sibling-order:unspecified', out)

    def test_html_contains_jump_logic(self):
        stmts, meta, _ = self.analyze('function foo()\nend\nfoo()')
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('Backspace', page)
        self.assertIn('symbolMap', page)
        self.assertIn('class="jump jump-', page)

    def test_html_visually_separates_code_and_typed_annotations(self):
        stmts, meta, _ = self.analyze('if ready() then return "yes" end')
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('class="statement-unit kind-if"', page)
        self.assertIn('annotation annotation-anchor', page)
        self.assertIn('annotation annotation-branch', page)
        self.assertIn('annotation annotation-symbol', page)
        self.assertIn('<span class="tok-keyword">if</span>', page)
        self.assertIn('<span class="tok-string">&quot;yes&quot;</span>', page)

        statement = page[page.index('class="statement-unit kind-if"'):]
        self.assertLess(statement.index('annotation-anchor'), statement.index('class="line code"'))
        self.assertLess(statement.index('class="line code"'), statement.index('annotation-branch'))

    def test_html_has_source_execution_flow_and_graph_views(self):
        stmts, meta, _ = self.analyze('a = 1\nfunction b()\nend\nc = 3\nb()')
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('data-view="source"', page)
        self.assertIn('data-view="flow"', page)
        self.assertIn('data-view="graph"', page)
        self.assertIn('id="graph-world"', page)
        self.assertIn('id="graph-prev"', page)
        self.assertIn('id="graph-next"', page)
        self.assertIn('pageSize:36', page)
        self.assertIn("viewport.addEventListener('wheel'", page)
        self.assertIn("element.addEventListener('pointerdown',startNodeDrag)", page)

        flow = page[page.index('id="flow-view"'):page.index('id="graph-view"')]
        ordered_ids = [f'id="flow-stmt-0x{number:06X}"' for number in range(1, 6)]
        positions = [flow.index(anchor) for anchor in ordered_ids]
        self.assertEqual(positions, sorted(positions))

    def test_graph_data_contains_function_scopes_and_call_edges(self):
        stmts, meta, _ = self.analyze('function b()\nwork()\nend\nb()')
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('"scope": "b"', page)
        self.assertIn('"scope": "\\u003Cmain\\u003E"', page)
        self.assertIn('"label": "call b"', page)

    def test_graph_coalesces_straight_line_statements_into_basic_blocks(self):
        stmts, _, _ = self.analyze('a = 1\nb = 2\nc = 3')
        cfg = build_cfg(stmts)
        self.assertEqual(len(cfg['nodes']), 1)
        self.assertEqual(cfg['nodes'][0]['statements'], ['0x000001', '0x000002', '0x000003'])
        self.assertEqual(cfg['nodes'][0]['text'], 'a = 1\nb = 2\nc = 3')

    def test_xrefs_index_definitions_reads_writes_calls_and_strings(self):
        stmts, _, _ = self.analyze('local value = "seed"\nvalue = value .. "!"\nprint(value)')
        xrefs = build_xrefs(stmts)
        value = next(item for item in xrefs['symbols'] if item['name'] == 'value')
        self.assertEqual(value['definitions'], ['0x000001'])
        self.assertEqual(value['writes'], ['0x000001', '0x000002'])
        self.assertEqual(value['reads'], ['0x000002', '0x000003'])
        printer = next(item for item in xrefs['symbols'] if item['name'] == 'print')
        self.assertEqual(printer['calls'], ['0x000003'])
        self.assertEqual([item['value'] for item in xrefs['strings']], ['"!"', '"seed"'])

    def test_html_has_searchable_analysis_xref_view(self):
        stmts, meta, _ = self.analyze('local value = 1\nprint(value)')
        page = render_html(stmts, meta, 'x.lua')
        self.assertIn('data-view="analysis"', page)
        self.assertIn('id="analysis-search"', page)
        self.assertIn('const xrefData = {', page)
        self.assertIn('function renderAnalysisDetail(item)', page)

    def test_project_refresh_preserves_user_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project_case.lua'
            path.write_text('local value = 1\nprint(value)\n', encoding='utf-8')
            stmts = normalize_source(path.read_text(encoding='utf-8'))
            annotate(stmts)
            existing = {'user': {'renames': {'value': 'decodedValue'}, 'bookmarks': ['0x000002']}}
            project = build_project(path, stmts, existing)
        self.assertEqual(project['user']['renames']['value'], 'decodedValue')
        self.assertEqual(project['user']['bookmarks'], ['0x000002'])
        self.assertTrue(project['source_hash'])
        self.assertTrue(project['analysis']['xrefs']['symbols'])

    def test_cli_project_writes_luare_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'database.lua'
            path.write_text('local value = 1\nprint(value)\n', encoding='utf-8')
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('lua_flow.py')), str(path), '--project'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
            )
            project_path = path.with_name('database.luare.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(project_path.exists())
            project = json.loads(project_path.read_text(encoding='utf-8'))
            self.assertEqual(project['version'], 1)
            self.assertIn('xrefs', project['analysis'])

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_runtime_trace_maps_lines_and_captures_locals(self):
        source = 'local function add(a, b)\n local total = a + b\n return total\nend\nlocal answer = add(2, 3)\nprint(answer)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            trace = collect_trace(path, stmts, timeout=5, max_events=500)

        self.assertEqual(trace['exit_code'], 0)
        self.assertFalse(trace['timed_out'])
        self.assertIn('5', trace['console'])
        line_events = [event for event in trace['events'] if event['event'] == 'line']
        self.assertTrue(any(event.get('sid') for event in line_events))
        answer_event = next(event for event in line_events if event.get('line') == 6)
        answer_local = next(item for item in answer_event['locals'] if item['name'] == 'answer')
        self.assertEqual(answer_local['value']['value'], 5)

    def test_html_embeds_debug_trace_timeline_and_inspectors(self):
        stmts, meta, _ = self.analyze('local answer = 5\nprint(answer)')
        trace = {
            'exit_code': 0,
            'events': [{
                'seq': 1,
                'event': 'line',
                'line': 2,
                'sid': '0x000002',
                'statements': ['0x000002'],
                'locals': [{'name': 'answer', 'value': {'type': 'number', 'value': 5}}],
                'upvalues': [],
                'stack': [{'name': '<chunk>', 'line': 2}],
            }],
            'console': ['5'],
            'stderr': [],
        }
        page = render_html(stmts, meta, 'x.lua', trace)
        self.assertIn('data-view="debug"', page)
        self.assertIn('id="trace-events"', page)
        self.assertIn('id="debug-source"', page)
        self.assertIn('id="debug-inspector"', page)
        self.assertIn('const traceData = {', page)
        self.assertIn('"answer"', page)
        self.assertIn('function selectTrace(index)', page)
        self.assertIn('Run to Cursor F4', page)
        self.assertIn("event.key==='F7'", page)
        self.assertIn('Current Locals · editable', page)
        self.assertIn('trace.liveSnapshot=state.last_pause', page)
        self.assertIn('Restart F2', page)
        self.assertIn("event.key==='F2'", page)

    def test_html_patch_workspace_embeds_exact_source_and_downloads_copy(self):
        source = '-- keep blank lines\n\nlocal value = "</script>"\n'
        stmts, meta, _ = self.analyze(source)
        page = render_html(stmts, meta, 'sample.lua', source_text=source)
        self.assertIn('data-view="patch"', page)
        self.assertIn('id="patch-draft"', page)
        self.assertIn('Save .patched.lua', page)
        self.assertIn('function applyPatchPreview()', page)
        self.assertIn('Source/Flow/Graph preview updated', page)
        self.assertIn("+'.patched.lua'", page)
        self.assertIn('\\u003C/script\\u003E', page)
        self.assertNotIn('const originalSource = "-- keep blank lines\\n\\nlocal value = "</script>', page)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_cli_trace_writes_json_and_embeds_debug_view(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cli_trace.lua'
            path.write_text('local value = 7\nprint(value)\n', encoding='utf-8')
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name('lua_flow.py')),
                    str(path),
                    '--trace',
                    '--html',
                    '--trace-timeout', '5',
                    '--max-events', '500',
                ],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False,
            )
            trace_path = path.with_name('cli_trace.trace.json')
            html_path = path.with_name('cli_trace.flow.html')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(trace_path.exists())
            self.assertIn('"event": "line"', trace_path.read_text(encoding='utf-8'))
            self.assertIn('Debug Trace', html_path.read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_steps_and_continues_target(self):
        source = 'local value = 1\nvalue = value + 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'live_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                first = debugger.read_until_stop()
                self.assertTrue(first['paused'])
                self.assertEqual(first['line'], 1)
                second = debugger.command('step')
                self.assertTrue(second['paused'])
                self.assertEqual(second['line'], 2)
                value = next(item for item in second['locals'] if item['name'] == 'value')
                self.assertEqual(value['value']['value'], 1)
                end = debugger.command('continue')
                self.assertEqual(end['event'], 'session_end')
                self.assertIn('2', debugger.console)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_honors_breakpoints(self):
        source = 'local value = 1\nvalue = value + 1\nvalue = value + 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'break_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                first = debugger.read_until_stop()
                self.assertEqual(first['line'], 1)
                acknowledgement = debugger.command('break 4')
                self.assertEqual(acknowledgement['event'], 'breakpoint_set')
                stopped = debugger.command('continue')
                self.assertTrue(stopped['paused'])
                self.assertEqual(stopped['line'], 4)
                end = debugger.command('continue')
                self.assertEqual(end['event'], 'session_end')
                self.assertIn('3', debugger.console)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_runs_to_cursor_once(self):
        source = 'local value = 1\nvalue = value + 1\nvalue = value + 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runto_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                first = debugger.read_until_stop()
                self.assertEqual(first['line'], 1)
                stopped = debugger.command('runto 3')
                self.assertTrue(stopped['paused'])
                self.assertEqual(stopped['line'], 3)
                end = debugger.command('continue')
                self.assertEqual(end['event'], 'session_end')

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_restarts_after_exit(self):
        source = 'local value = 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'restart_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                first = debugger.read_until_stop()
                self.assertEqual(first['line'], 1)
                self.assertEqual(debugger.command('continue')['event'], 'session_end')
                debugger.restart()
                restarted = debugger.read_until_stop()
                self.assertTrue(restarted['paused'])
                self.assertEqual(restarted['line'], 1)
                debugger.command('continue')

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_session_restart_restores_breakpoints(self):
        source = 'local value = 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'restart_break_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            session = LiveDebugSession(LiveDebugger(path, stmts, max_events=500))

            def wait_stopped():
                state = {}
                for _ in range(100):
                    state = session.state()
                    if not state['running']:
                        return state
                    time.sleep(0.01)
                self.fail('live session did not stop')

            try:
                self.assertEqual(wait_stopped()['last_pause']['line'], 1)
                session.command('break 2')
                wait_stopped()
                session.command('continue')
                self.assertEqual(wait_stopped()['last_pause']['line'], 2)
                session.command('continue')
                self.assertEqual(wait_stopped()['current']['event'], 'session_end')
                session.command('restart')
                self.assertEqual(wait_stopped()['last_pause']['line'], 1)
                session.command('continue')
                self.assertEqual(wait_stopped()['last_pause']['line'], 2)
            finally:
                session.close()

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_hides_and_protects_its_hook(self):
        source = 'assert(debug.gethook() == nil, "hook detected")\ndebug.sethook()\nlocal value = 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'anti_hook_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                first = debugger.read_until_stop()
                self.assertEqual(first['line'], 1)
                end = debugger.command('continue')
                self.assertEqual(end['event'], 'session_end')
                self.assertIn('1', debugger.console)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_changes_a_paused_local(self):
        source = 'local value = 1\nvalue = value + 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'setlocal_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                debugger.read_until_stop()
                paused = debugger.command('step')
                value = next(item for item in paused['locals'] if item['name'] == 'value')
                changed = debugger.command(f"setlocal {value['index']} lua 3430")
                self.assertEqual(changed['event'], 'value_set')
                updated = next(item for item in changed['locals'] if item['name'] == 'value')
                self.assertEqual(updated['value']['value'], 40)
                end = debugger.command('continue')
                self.assertEqual(end['event'], 'session_end')
                self.assertIn('41', debugger.console)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_supplies_program_input(self):
        source = 'io.write("input your name: ")\nio.flush()\nlocal name = io.read("l")\nprint("hello " .. name)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)
            with LiveDebugger(path, stmts, max_events=500) as debugger:
                debugger.read_until_stop()
                waiting = debugger.command('continue')
                self.assertEqual(waiting['event'], 'input_request')
                end = debugger.command('input 416C696365')
                self.assertEqual(end['event'], 'session_end')
                self.assertIn('input your name: ', debugger.console)
                self.assertIn('hello Alice', debugger.console)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debugger_step_over_and_step_out(self):
        source = 'local function bump(value)\n local result = value + 1\n return result\nend\nlocal answer = bump(1)\nprint(answer)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'depth_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            annotate(stmts)

            with LiveDebugger(path, stmts, max_events=500) as debugger:
                debugger.read_until_stop()
                call_line = debugger.command('step')
                self.assertEqual(call_line['line'], 5)
                after_call = debugger.command('over')
                self.assertEqual(after_call['line'], 6)
                debugger.command('continue')

            with LiveDebugger(path, stmts, max_events=500) as debugger:
                debugger.read_until_stop()
                debugger.command('step')
                inside = debugger.command('step')
                self.assertEqual(inside['line'], 2)
                after_return = debugger.command('out')
                self.assertEqual(after_return['line'], 6)
                debugger.command('continue')

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_live_debug_http_bridge_serves_state_and_commands(self):
        source = 'local value = 1\nvalue = value + 1\nprint(value)\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'web_case.lua'
            path.write_text(source, encoding='utf-8')
            stmts = normalize_source(source)
            meta = annotate(stmts)
            debugger = LiveDebugger(path, stmts, max_events=500)
            session = LiveDebugSession(debugger)
            page = render_html(stmts, meta, 'web_case.lua', live_debug=True)
            self.assertIn("if (liveDebug) switchView('debug')", page)
            server = make_debug_server(page, session, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_address[1]}'
            try:
                self.assertIn('const liveDebug = true', urllib.request.urlopen(base + '/', timeout=2).read().decode('utf-8'))
                state = {}
                for _ in range(40):
                    state = json.loads(urllib.request.urlopen(base + '/api/state', timeout=2).read())
                    if state.get('last_pause'):
                        break
                    time.sleep(0.025)
                self.assertEqual(state['last_pause']['line'], 1)

                request = urllib.request.Request(
                    base + '/api/command',
                    data=json.dumps({'command': 'step'}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                self.assertEqual(urllib.request.urlopen(request, timeout=2).status, 202)
                for _ in range(40):
                    state = json.loads(urllib.request.urlopen(base + '/api/state', timeout=2).read())
                    if state.get('last_pause', {}).get('line') == 2:
                        break
                    time.sleep(0.025)
                self.assertEqual(state['last_pause']['line'], 2)
            finally:
                server.shutdown()
                server.server_close()
                session.close()
                thread.join(timeout=2)

    def test_html_links_analysis_anchors_to_definition_rows(self):
        stmts, meta, _ = self.analyze('::again::\nif a() and b() then foo(bar()) end')
        page = render_html(stmts, meta, 'x.lua')
        for prefix in ('A0x', 'C0x', 'E0x', 'L0x'):
            self.assertIn(f'href="#nav-{prefix}', page)
            self.assertIn(f'id="nav-{prefix}', page)

    def test_short_circuit_routes_use_real_eval_anchor_ids(self):
        _, _, out = self.analyze('if a and b or c then ok() end')
        self.assertNotIn('<expr:', out)
        self.assertRegex(out, r'cond-step E0x[0-9A-F]+ \[1\].*true -> E0x[0-9A-F]+')


if __name__ == '__main__':
    unittest.main()
