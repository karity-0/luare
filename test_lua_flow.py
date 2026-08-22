import tempfile
import unittest
from pathlib import Path

from lua_flow import normalize_source, annotate, render_lua, render_html


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

    def test_function_definition_skips_body_in_runtime_flow(self):
        _, _, out = self.analyze('function foo()\n print("body")\nend\nprint("after")')
        self.assertIn('-- next -> @0x000004', out)
        self.assertIn('-- return -> <caller>\nend', out)

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
        self.assertIn('class="jump"', page)

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
