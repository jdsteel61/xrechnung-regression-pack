"""Evidence-backed matching of KoSIT element locations; no business-item inference."""
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import xml.etree.ElementTree as ET
from xml.dom import minidom

XSL = 'http://www.w3.org/1999/XSL/Transform'
LEGACY_SELECTOR = f'''<xsl:template xmlns:xsl="{XSL}" match="*" mode="schematron-select-full-path"><xsl:apply-templates select="." mode="schematron-get-full-path-3"/></xsl:template>'''
LEGACY_TEMPLATE = f'''<xsl:template xmlns:xsl="{XSL}" match="node() | @*" mode="schematron-get-full-path-3"><xsl:for-each select="ancestor-or-self::*"><xsl:text>/</xsl:text><xsl:value-of select="name(.)"/><xsl:if test="parent::*"><xsl:text>[</xsl:text><xsl:value-of select="count(preceding-sibling::*[name(.)=name(current())])+1"/><xsl:text>]</xsl:text></xsl:if></xsl:for-each><xsl:if test="not(self::*)"><xsl:text/>/@<xsl:value-of select="name(.)"/></xsl:if></xsl:template>'''


def shape(e):
    return (e.tag, tuple(sorted(e.attrib.items())), (e.text or '').strip(),
            tuple((shape(c), (c.tail or '').strip()) for c in e))


@lru_cache(maxsize=64)
def compiler_evidence(path, mtime_ns, size):
    data = path.read_bytes()
    root = ET.fromstring(data)
    templates = root.findall('{'+XSL+'}template')
    expected = [ET.fromstring(LEGACY_SELECTOR), ET.fromstring(LEGACY_TEMPLATE)]
    supported = all(len([t for t in templates if t.get('mode') == e.get('mode')]) == 1
                    and any(shape(t) == shape(e) for t in templates) for e in expected)
    return {'resource_sha256': hashlib.sha256(data).hexdigest(),
            'legacy_name_path_verified': supported}


class LocationIndex:
    """Generate paths from actual nodes, then require a unique reverse lookup.

    Legacy indices count lexical QNames, exactly as the verified compiler does.
    Canonical indices count expanded QNames. They need not have the same value.
    Namespace rebinding therefore cannot silently collapse distinct nodes.
    """
    def __init__(self, data):
        self.document_sha256 = hashlib.sha256(data).hexdigest()
        self.canonical = defaultdict(list)
        self.legacy = defaultdict(list)
        with minidom.parseString(data) as doc:
            def walk(parent, canonical_parent='', legacy_parent=''):
                expanded_counts, lexical_counts = Counter(), Counter()
                for node in parent.childNodes:
                    if node.nodeType != node.ELEMENT_NODE:
                        continue
                    qname = 'Q{' + (node.namespaceURI or '') + '}' + node.localName
                    expanded_counts[qname] += 1
                    lexical_counts[node.nodeName] += 1
                    canonical = canonical_parent + '/' + qname + '[' + str(expanded_counts[qname]) + ']'
                    legacy = legacy_parent + '/' + node.nodeName
                    if parent.nodeType == parent.ELEMENT_NODE:
                        legacy += '[' + str(lexical_counts[node.nodeName]) + ']'
                    self.canonical[canonical].append(canonical)
                    self.legacy[legacy].append(canonical)
                    walk(node, canonical, legacy)
            walk(doc)

    def resolve(self, location, legacy_verified=False):
        # Deliberately only element paths emitted by these compilers. Unsupported
        # attributes, line/column positions and arbitrary XPath stay unresolved.
        candidates = self.canonical.get(location, [])
        method = 'expanded_qname_path'
        if not candidates and legacy_verified:
            candidates = self.legacy.get(location, [])
            method = 'verified_schematron_name_path_3'
        if len(candidates) != 1:
            return {'status': 'unresolved', 'reason': 'location_not_uniquely_resolved',
                    'candidate_count': len(candidates)}
        return {'status': 'resolved', 'node': candidates[0], 'method': method,
                'document_sha256': self.document_sha256}


def attach(result, data, bundle, safe_xml):
    """Keep raw validator findings intact; attach parallel node-resolution evidence."""
    if result['status'] == 'execution_error':
        return result
    safe_xml(data)
    index = LocationIndex(data)
    compiler = {}
    for stage, resources in result.get('stage_resources', {}).items():
        records = []
        for resource in resources:
            path = (bundle / resource).resolve()
            if not path.is_relative_to(bundle.resolve()) or not path.is_file() or path.suffix != '.xsl':
                continue
            st = path.stat()
            records.append(dict(compiler_evidence(path, st.st_mtime_ns, st.st_size), resource=resource))
        compiler[stage] = records
    result['document_sha256'] = index.document_sha256
    result['location_compilers'] = compiler
    result['location_resolution'] = [index.resolve(f['location'],
        len(compiler.get(f['stage'], [])) == 1 and compiler[f['stage']][0]['legacy_name_path_verified'])
        for f in result['findings']]
    return result


def signature(f):
    return (f['severity'], f.get('reported_severity', f['severity']), f['message_sha256'])


def compare(a, b, levels):
    outcome = a['status'] + '->' + b['status']
    d = {'comparable': True, 'outcome': outcome, 'document_transition': a['status'] != b['status'],
         'added': [], 'removed': [], 'severity_changes': [], 'reported_severity_changes': [],
         'message_changes': [], 'notation_only': [], 'matches': [], 'unresolved': [],
         'matching_mode': 'xml_node_identity' if a['document_sha256'] == b['document_sha256']
                          else 'different_document_no_business_identity_assumed'}
    same = a['document_sha256'] == b['document_sha256']
    groups = [defaultdict(list), defaultdict(list)]
    for side, value in enumerate((a, b)):
        resolutions = value.get('location_resolution', [])
        for i, f in enumerate(value['findings']):
            r = resolutions[i] if i < len(resolutions) else {'status': 'unresolved', 'reason': 'missing_resolution'}
            if r.get('status') != 'resolved' or r.get('document_sha256') != value['document_sha256']:
                d['unresolved'].append({'reason': r.get('reason', 'invalid_resolution_proof'),
                                        'side': 'old' if side == 0 else 'new', 'finding_index': i, 'finding': f})
            else:
                key = (f['stage'], f['rule_id'], r['node'] if same else '')
                groups[side][key].append(i)

    def match(i, j, key):
        old, new = a['findings'][i], b['findings'][j]
        record = {'old_index': i, 'new_index': j, 'node': key[2],
                  'document_sha256': a['document_sha256'], 'old_location': old['location'],
                  'new_location': new['location'], 'stage': key[0], 'rule_id': key[1]}
        d['matches'].append(record)
        identity = [*key, len(d['matches'])]
        for field, label in [('severity', 'severity_changes'), ('reported_severity', 'reported_severity_changes'),
                             ('message_sha256', 'message_changes')]:
            ov, nv = old.get(field, old['severity']), new.get(field, new['severity'])
            if ov != nv:
                d[label].append({'identity': identity, 'old': ov, 'new': nv,
                                 'old_index': i, 'new_index': j})
        if signature(old) == signature(new) and old['location'] != new['location']:
            d['notation_only'].append(record)

    for key in sorted(groups[0].keys() | groups[1].keys()):
        aa, bb = list(groups[0][key]), list(groups[1][key])
        if aa and bb and not same:
            d['unresolved'].append({'reason': 'different_document_correspondence', 'stage': key[0],
                                    'rule_id': key[1], 'old_indices': aa, 'new_indices': bb})
            continue
        # Cancel identical signatures as a multiset, without trusting report order.
        for i in aa[:]:
            j = next((j for j in bb if signature(a['findings'][i]) == signature(b['findings'][j])), None)
            if j is not None:
                match(i, j, key); aa.remove(i); bb.remove(j)
        if aa and bb:
            if len({signature(a['findings'][i]) for i in aa}) == 1 and len({signature(b['findings'][j]) for j in bb}) == 1:
                count = min(len(aa), len(bb))
                for i, j in zip(aa[:count], bb[:count]):
                    match(i, j, key)
                aa, bb = aa[count:], bb[count:]
            else:
                d['unresolved'].append({'reason': 'ambiguous_repeated_finding_correspondence',
                                       'identity': list(key), 'old_indices': aa, 'new_indices': bb})
                continue
        # If a location failed elsewhere for this rule, an apparent unmatched
        # occurrence could be its counterpart. Never turn that into add/remove.
        blocked = any(u.get('finding', {}).get('stage') == key[0] and
                      u.get('finding', {}).get('rule_id') == key[1] for u in d['unresolved'])
        if blocked and (aa or bb):
            d['unresolved'].append({'reason': 'counterpart_location_unresolved', 'identity': list(key),
                                   'old_indices': aa, 'new_indices': bb})
        else:
            d['removed'].extend(dict(a['findings'][i], finding_index=i) for i in aa)
            d['added'].extend(dict(b['findings'][j], finding_index=j) for j in bb)
    regression = outcome == 'pass->fail' or any(levels[f['severity']] >= 2 for f in d['added']) or any(
        levels[c['new']] > levels[c['old']] for c in d['severity_changes'])
    d['comparable'] = not d['unresolved']
    d['regression'] = True if regression else (None if d['unresolved'] else False)
    d['interpretation'] = 'incomplete_matching' if d['unresolved'] else 'rule_result_comparison'
    return d
