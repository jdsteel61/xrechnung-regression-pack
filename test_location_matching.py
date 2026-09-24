"""Adversarial comparison checks; existing acceptance tests remain unchanged."""
import copy
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

import location_matching as lm
import xreg
from test_xreg import finding


def result(data, findings=(), status='pass', legacy=True):
    xreg.safe_xml(data)
    index = lm.LocationIndex(data)
    return {'status': status, 'document_sha256': index.document_sha256,
            'findings': list(findings),
            'location_resolution': [index.resolve(f['location'], legacy) for f in findings]}


class LocationMatchingTests(unittest.TestCase):
    def test_compiler_template_exact_match_required(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'compiler.xsl'
            source = f'<xsl:stylesheet xmlns:xsl="{lm.XSL}">{lm.LEGACY_SELECTOR}{lm.LEGACY_TEMPLATE}</xsl:stylesheet>'
            p.write_text(source)
            self.assertTrue(lm.compiler_evidence(p, 0, 0)['legacy_name_path_verified'])
            p.write_text(source.replace('name(.)=name(current())', 'local-name(.)=local-name(current())'))
            self.assertFalse(lm.compiler_evidence(p, 1, 0)['legacy_name_path_verified'])

    def test_root_index_and_namespace_notation_same_node(self):
        data = b'<r:Invoice xmlns:r="urn:r" xmlns:a="urn:a"><a:Item/></r:Invoice>'
        a = result(data, [finding(loc='/r:Invoice/a:Item[1]')])
        b = result(data, [finding(loc='/Q{urn:r}Invoice[1]/Q{urn:a}Item[1]')])
        d = xreg.delta(a, b)
        self.assertTrue(d['comparable']); self.assertFalse(d['regression'])
        self.assertEqual(1, len(d['notation_only'])); self.assertFalse(d['added'] or d['removed'])
        self.assertEqual(a['document_sha256'], d['matches'][0]['document_sha256'])

    def test_default_namespace_preserved_from_actual_node(self):
        data = b'<Invoice xmlns="urn:invoice"><Item/></Invoice>'
        d = xreg.delta(result(data, [finding(loc='/Invoice/Item[1]')]),
                      result(data, [finding(loc='/Q{urn:invoice}Invoice[1]/Q{urn:invoice}Item[1]')]))
        self.assertEqual(1, len(d['notation_only']))
        self.assertEqual('unresolved', lm.LocationIndex(data).resolve('/Invoice/Item[1]', False)['status'])

    def test_namespace_collision_is_not_a_match(self):
        data = b'<R xmlns:a="urn:a" xmlns:b="urn:b"><a:Item/><b:Item/></R>'
        d = xreg.delta(result(data, [finding(loc='/R/a:Item[1]')]),
                      result(data, [finding(loc='/Q{}R[1]/Q{urn:b}Item[1]', severity='error')], 'fail'))
        self.assertFalse(d['matches']); self.assertEqual(1, len(d['added']))
        self.assertEqual(1, len(d['removed'])); self.assertTrue(d['regression'])

    def test_rebound_prefix_and_distinct_index_spaces(self):
        data = b'<R><p:I xmlns:p="urn:a"/><p:I xmlns:p="urn:b"/></R>'
        index = lm.LocationIndex(data)
        first = index.resolve('/R/p:I[1]', True)
        second = index.resolve('/R/p:I[2]', True)
        self.assertNotEqual(first['node'], second['node'])
        self.assertEqual('/Q{}R[1]/Q{urn:b}I[1]', second['node'])
        self.assertEqual('unresolved', index.resolve('/Q{}R[1]/Q{urn:b}I[2]')['status'])

    def test_two_prefixes_one_namespace_preserve_occurrences(self):
        data = b'<R xmlns:a="urn:same" xmlns:b="urn:same"><a:I/><b:I/></R>'
        index = lm.LocationIndex(data)
        self.assertEqual('/Q{}R[1]/Q{urn:same}I[2]', index.resolve('/R/b:I[1]', True)['node'])

    def test_index_changes_are_finding_moves_not_notation(self):
        data = b'<R><Item/><Item/></R>'
        d = xreg.delta(result(data, [finding(loc='/R/Item[1]')]),
                      result(data, [finding(loc='/Q{}R[1]/Q{}Item[2]')]))
        self.assertFalse(d['notation_only']); self.assertEqual(1, len(d['added']))
        self.assertEqual(1, len(d['removed']))

    def test_repeated_findings_multiplicity_and_report_reorder(self):
        data = b'<R/>'
        warning = finding(loc='/R'); error = finding(loc='/R', occ=2, severity='error')
        a = result(data, [warning, error, dict(warning, occurrence=3)], 'fail')
        b = result(data, [dict(error, occurrence=1), dict(warning, occurrence=2)], 'fail')
        d = xreg.delta(a, b)
        self.assertEqual(2, len(d['matches'])); self.assertEqual(1, len(d['removed']))
        self.assertFalse(d['severity_changes']); self.assertTrue(d['comparable'])

    def test_duplicate_occurrence_metadata_does_not_drop_findings(self):
        data = b'<R/>'; f = finding(loc='/R')
        d = xreg.delta(result(data, [f, f]), result(data, [f]))
        self.assertEqual(1, len(d['matches'])); self.assertEqual(1, len(d['removed']))

    def test_severity_reported_severity_and_message_changes(self):
        data = b'<R/>'; a = finding(loc='/R'); b = dict(a, location='/Q{}R[1]', severity='error', reported_severity='information', message_sha256='1'*64)
        d = xreg.delta(result(data, [a]), result(data, [b], 'fail'))
        for key in ['severity_changes', 'reported_severity_changes', 'message_changes']:
            self.assertEqual(1, len(d[key]))
        self.assertFalse(d['notation_only']); self.assertTrue(d['regression'])

    def test_ambiguous_repeated_findings_not_assigned_by_order(self):
        data = b'<R/>'; f = finding(loc='/R')
        a = result(data, [f, dict(f, message_sha256='1'*64)])
        b = result(data, [dict(f, message_sha256='2'*64), dict(f, message_sha256='3'*64)])
        d = xreg.delta(a, b)
        self.assertFalse(d['comparable']); self.assertIsNone(d['regression'])
        self.assertEqual('ambiguous_repeated_finding_correspondence', d['unresolved'][0]['reason'])

    def test_added_removed_nodes_different_outputs(self):
        old = b'<R><Item/></R>'; new = b'<R><Item/><New/></R>'
        d = xreg.delta(result(old), result(new, [finding(loc='/R/New[1]', severity='error')], 'fail'))
        self.assertTrue(d['regression']); self.assertEqual(1, len(d['added']))
        reverse = xreg.delta(result(new, [finding(loc='/R/New[1]')]), result(old))
        self.assertEqual(1, len(reverse['removed']))

    def test_reordered_business_items_explicitly_unresolved(self):
        old = b'<R><Item id="A"/><Item id="B"/></R>'
        new = b'<R><Item id="B"/><Item id="A"/></R>'
        for newloc in ['/R/Item[1]', '/R/Item[2]']:
            d = xreg.delta(result(old, [finding(loc='/R/Item[1]')]), result(new, [finding(loc=newloc)]))
            self.assertFalse(d['comparable']); self.assertIsNone(d['regression'])
            self.assertFalse(d['matches']); self.assertEqual('different_document_correspondence', d['unresolved'][0]['reason'])

    def test_changed_bytes_identical_structure_not_business_identity(self):
        d = xreg.delta(result(b'<R><Item>1</Item></R>', [finding(loc='/R/Item[1]')]),
                      result(b'<R><Item>2</Item></R>', [finding(loc='/R/Item[1]')]))
        self.assertFalse(d['comparable']); self.assertIsNone(d['regression'])

    def test_unresolvable_locations_never_green(self):
        data = b'<R/>'
        for loc in ['/R/Item[9]', 'line:1:column:1', '//R', '/R/@id', '/wrong:R']:
            d = xreg.delta(result(data, [finding(loc=loc)]), result(data, [finding(loc=loc)]))
            self.assertFalse(d['comparable']); self.assertIsNone(d['regression'])
            self.assertFalse(d['added'] or d['removed'])

    def test_unresolved_counterpart_cannot_be_called_an_addition(self):
        data = b'<R/>'
        d = xreg.delta(result(data, [finding(loc='/bad')]), result(data, [finding(loc='/R', severity='error')]))
        self.assertFalse(d['added']); self.assertIsNone(d['regression'])

    def test_verdict_transition_survives_unresolved_matching(self):
        data = b'<R/>'
        a = result(data, [finding(loc='/bad')]); b = result(data, [finding(loc='/bad')], 'fail')
        d = xreg.delta(a, b)
        self.assertEqual('pass->fail', d['outcome']); self.assertTrue(d['regression'])
        self.assertFalse(d['comparable']); self.assertEqual('fail', b['status'])
        root = ET.fromstring(xreg.junit({'corpora': {'old_app': {'x': {'old_rules': a, 'new_rules': b}}}, 'pair_comparisons': []}))
        self.assertEqual('false', root.find("testcase[@classname='old_app.rule_regression']/failure").get('matching_complete'))

    def test_missing_xml_and_execution_errors_are_unknown(self):
        d = xreg.delta({'status': 'pass', 'findings': []}, {'status': 'pass', 'findings': []})
        self.assertFalse(d['comparable']); self.assertIsNone(d['regression'])
        self.assertIsNone(xreg.delta(xreg.error('timeout'), result(b'<R/>'))['regression'])

    def test_compare_does_not_mutate_validator_records(self):
        a = result(b'<R/>', [finding(loc='/R')]); b = result(b'<R/>', [finding(loc='/Q{}R[1]')])
        before = copy.deepcopy((a, b)); xreg.delta(a, b)
        self.assertEqual(before, (a, b))

    def test_rules_only_notation_or_finding_change_is_not_app_interaction(self):
        data = b'<R/>'
        for old_findings in ([], [finding(loc='/R')]):
            a = result(data, old_findings)
            b = result(data, [finding(loc='/Q{}R[1]')])
            row = {'old_rules': a, 'new_rules': b}
            pair = xreg.paired_comparison({'x': row}, {'x': copy.deepcopy(row)})[0]
            self.assertTrue(pair['finding_interaction_complete'])
            self.assertFalse(pair['interaction'])


if __name__ == '__main__':
    unittest.main()
