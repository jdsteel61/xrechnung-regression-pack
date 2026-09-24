import base64,copy,json,subprocess,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import xreg

def finding(rule='R1',loc='/Invoice[1]',occ=1,severity='warning'):
    return {'stage':'val-sch.1','rule_id':rule,'location':loc,'occurrence':occ,'severity':severity,'message_sha256':'0'*64}
def verdict(status='pass',findings=None): return {'status':status,'findings':findings or []}
def output(a,b): return {'old_rules':a,'new_rules':b}

class ComparisonTests(unittest.TestCase):
    def test_unchanged(self):
        v=verdict(findings=[finding()]); d=xreg.delta(v,copy.deepcopy(v))
        self.assertFalse(d['regression']); self.assertEqual([],d['added']); self.assertEqual([],d['severity_changes'])
    def test_rule_axis(self):
        a=verdict(); b=verdict('fail',[finding(severity='error')]); p=xreg.paired_comparison({'x':output(a,b)},{'x':output(a,b)})[0]
        self.assertTrue(p['A_vs_B']['regression']); self.assertFalse(p['interaction'])
    def test_application_axis(self):
        a=verdict(); b=verdict('fail',[finding(severity='error')]); p=xreg.paired_comparison({'x':output(a,a)},{'x':output(b,b)})[0]
        self.assertTrue(p['A_vs_C']['regression']); self.assertFalse(p['interaction'])
    def test_interaction(self):
        a=verdict(); c=verdict(findings=[finding()]); d=verdict('fail',[finding(severity='error')]); p=xreg.paired_comparison({'x':output(a,a)},{'x':output(c,d)})[0]
        self.assertTrue(p['interaction']); self.assertEqual('pass->pass',p['A_vs_C']['outcome']); self.assertEqual('pass->fail',p['B_vs_D']['outcome'])
    def test_unmatched(self):
        v=output(verdict(),verdict()); p=xreg.paired_comparison({'lost':v},{'added':v})
        self.assertEqual([('added','added'),('lost','missing')],[(x['id'],x['pair_status']) for x in p])
    def test_occurrences_not_collapsed(self):
        a=verdict(findings=[finding(occ=1),finding(occ=2),finding(loc='/Invoice[2]')]); b=verdict(findings=[finding(occ=1)])
        self.assertEqual(2,len(xreg.delta(a,b)['removed']))
    def test_severity_is_not_add_remove(self):
        d=xreg.delta(verdict(findings=[finding()]),verdict('fail',[finding(severity='error')]))
        self.assertEqual(1,len(d['severity_changes'])); self.assertEqual([],d['added']); self.assertEqual([],d['removed']); self.assertTrue(d['regression'])
    def test_incomplete_never_comparable(self):
        self.assertFalse(xreg.delta(xreg.error('timeout'),verdict())['comparable'])

class InputAndCleanupTests(unittest.TestCase):
    def test_cli_malformed_manifest_nonzero_sanitised(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t); manifest=t/'manifest.json'; manifest.write_text('PRIVATE_SENTINEL')
            command=[sys.executable,str(Path(xreg.__file__))]
            for flag,value in [('corpus',manifest),('root',t),('old-rules',t),('new-rules',t),('java',t/'java'),('engine',t),('out',t/'out')]: command+=['--'+flag,str(value)]
            p=subprocess.run(command,capture_output=True,timeout=10)
            self.assertEqual(2,p.returncode); self.assertNotIn(b'PRIVATE_SENTINEL',p.stdout+p.stderr); self.assertIn(b'malformed_manifest',p.stderr)
    def test_dtd_entity_encodings_and_malformed(self):
        for data in [b'<Invoice>',b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///sentinel">]><x>&e;</x>', '<!DOCTYPE x><x/>'.encode('utf-16'),b'<?xml version="1.0" encoding="ISO-8859-1"?><x/>']:
            with self.subTest(data_hash=xreg.sha(data)),self.assertRaises(xreg.InputError): xreg.safe_xml(data)
    def test_size_limit(self):
        with self.assertRaisesRegex(xreg.InputError,'file_size_limit'): xreg.safe_xml(b'x'*20,10)
    def test_ambiguous_and_path_escape(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t); p=t/'manifest.json'; row={'id':'x','source_path':'x.xml','sha256':'0'*64}
            p.write_text(json.dumps({'fixtures':[row,row]}))
            with self.assertRaisesRegex(xreg.InputError,'ambiguous_fixture_ids'): xreg.load_manifest(p,t)
            row['source_path']='../elsewhere.xml'; p.write_text(json.dumps({'fixtures':[row]}))
            with self.assertRaisesRegex(xreg.InputError,'fixture_path_outside_root'): xreg.load_manifest(p,t)
    def test_timeout_launch_crash_and_missing_report_cleanup(self):
        for case in ['timeout','launch','crash','missing']:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as t:
                t=Path(t); original=t/'original.xml'; original.write_bytes(b'<Invoice/>'); temps=t/'temp'; temps.mkdir()
                row={'id':'x','path':original,'sha256':xreg.sha(original.read_bytes())}
                def runner(cmd,**kw):
                    work=Path(kw['cwd']); (work/'partial-report.xml').write_bytes(b'<secret>SENTINEL</secret>')
                    if case=='timeout': raise subprocess.TimeoutExpired('java',1,output=b'SENTINEL')
                    if case=='launch': raise OSError('SENTINEL')
                    return SimpleNamespace(returncode=99 if case=='crash' else 0)
                rows,ops=xreg.run_batch([row],Path('java'),t,t,1,temps,runner)
                self.assertEqual('execution_error',rows['x']['status']); self.assertNotIn('SENTINEL',json.dumps(rows)); self.assertTrue(ops['temporary_directory_removed']); self.assertEqual([],list(temps.iterdir())); self.assertEqual(b'<Invoice/>',original.read_bytes())
    def test_preflight_failure_no_java_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t); original=t/'invoice.xml'; original.write_bytes(b'<Invoice>SECRET'); runner=Mock()
            out,ops=xreg.run_batch([{'id':'x','path':original,'sha256':xreg.sha(original.read_bytes())}],t,t,t,runner=runner)
            runner.assert_not_called(); self.assertEqual('malformed_xml',out['x']['error']); self.assertEqual(b'<Invoice>SECRET',original.read_bytes()); self.assertTrue(ops['temporary_directory_removed'])

class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.r=self.root/'report.xml'; self.i=self.root/'input.xml'; self.digest=xreg.sha(b'<invoice/>')
        h=base64.b64encode(bytes.fromhex(self.digest)).decode()
        self.report=f'<report xmlns="{xreg.NS["r"]}" valid="true"><engine><name>KoSIT Validator 1.6.3</name></engine><documentIdentification><documentHash><hashAlgorithm>SHA-256</hashAlgorithm><hashValue>{h}</hashValue></documentHash></documentIdentification><scenarioMatched><validationStepResult id="val-sch.1" valid="true"/></scenarioMatched><assessment><accept/></assessment></report>'
        self.raw=f'<createReportInput xmlns="{xreg.NS["i"]}" xmlns:s="{xreg.NS["s"]}" xmlns:v="{xreg.NS["v"]}"><engine><name>KoSIT Validator 1.6.3</name></engine><documentIdentification><documentHash><hashAlgorithm>SHA-256</hashAlgorithm><hashValue>{h}</hashValue></documentHash></documentIdentification><s:scenario><s:validateWithSchematron/></s:scenario><validationResultsWellformedness/><validationResultsXmlSchema/><validationResultsSchematron><results><v:schematron-output/></results></validationResultsSchematron></createReportInput>'
    def parse(self,report=None,raw=None):
        self.r.write_text(self.report if report is None else report); self.i.write_text(self.raw if raw is None else raw)
        return xreg.parse_reports(self.r,self.i,self.digest)
    def test_complete_assessment(self): self.assertEqual('pass',self.parse()['status'])
    def test_effective_and_reported_severity_separate(self):
        raw=self.raw.replace('<s:validateWithSchematron/>','<s:validateWithSchematron/><s:createReport><s:customLevel level="information">R1 R2</s:customLevel></s:createReport>')
        report=self.report.replace('<validationStepResult id="val-sch.1" valid="true"/>','<validationStepResult id="val-sch.1" valid="true"><message code="R1" level="error" xpathLocation="/Invoice[1]">Synthetic test</message></validationStepResult>')
        result=self.parse(report,raw); self.assertEqual('information',result['findings'][0]['severity']); self.assertEqual('error',result['findings'][0]['reported_severity']); self.assertEqual('pass',result['status'])
    def test_reject_even_if_root_valid_true(self): self.assertEqual('fail',self.parse(self.report.replace('<accept/>','<reject/>'))['status'])
    def test_missing_transformation(self):
        raw=self.raw.replace('<v:schematron-output/>','')
        self.assertEqual('incomplete_schematron',self.parse(raw=raw)['error'])
    def test_processing_error_despite_accept(self):
        raw=self.raw.replace('</createReportInput>','<processingError><error>SECRET</error></processingError></createReportInput>')
        result=self.parse(raw=raw); self.assertEqual('validator_processing_error',result['error']); self.assertNotIn('SECRET',json.dumps(result))
    def test_missing_report_and_hash_mismatch(self):
        self.assertEqual('missing_report',xreg.parse_reports(self.r,self.i,self.digest)['error'])
        self.parse(); self.assertEqual('report_document_hash_mismatch',xreg.parse_reports(self.r,self.i,'0'*64)['error'])
    def test_complete_schema_invalid_is_verdict(self):
        raw=self.raw.replace('<validationResultsXmlSchema/>','<validationResultsXmlSchema><xmlSyntaxError><severityCode>SEVERITY_ERROR</severityCode></xmlSyntaxError></validationResultsXmlSchema>')
        raw=raw.replace('<validationResultsSchematron><results><v:schematron-output/></results></validationResultsSchematron>','')
        self.assertEqual('fail',self.parse(self.report.replace('<accept/>','<reject/>'),raw)['status'])
    def test_json_junit_stable_and_separate_errors(self):
        rows={'a':{'old_rules':verdict(),'new_rules':verdict('fail')},'b':{'old_rules':xreg.error('timeout'),'new_rules':verdict()}}
        result={'corpora':{'old_app':rows},'pair_comparisons':[]}
        self.assertEqual(xreg.canonical(result),xreg.canonical(copy.deepcopy(result)))
        self.assertEqual(xreg.junit(result),xreg.junit(copy.deepcopy(result)))
        root=ET.fromstring(xreg.junit(result)); self.assertEqual('2',root.get('errors')); self.assertEqual('2',root.get('failures'))

if __name__=='__main__': unittest.main()
