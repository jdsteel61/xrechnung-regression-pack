"""Small local KoSIT regression CLI. Python standard library only."""
from __future__ import annotations
import argparse, base64, collections, hashlib, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
import xml.etree.ElementTree as ET
import location_matching

NS={'r':'http://www.xoev.de/de/validator/varl/1','i':'http://www.xoev.de/de/validator/framework/1/createreportinput','s':'http://www.xoev.de/de/validator/framework/1/scenarios','v':'http://purl.oclc.org/dsdl/svrl'}
MAX_INVOICE=5*1024*1024
MAX_REPORT=32*1024*1024
LEVEL={'information':0,'info':0,'warning':1,'error':2,'fatal':3}
class InputError(Exception):
    def __init__(self,code,details=None):
        super().__init__(code); self.details=details
def sha(data): return hashlib.sha256(data).hexdigest()
def canonical(data): return (json.dumps(data,ensure_ascii=True,sort_keys=True,indent=2)+'\n').encode()
def write_json(path,data): Path(path).write_bytes(canonical(data))
def tree_identity(path):
    return [{'path':p.relative_to(path).as_posix(),'sha256':sha(p.read_bytes())} for p in sorted(path.rglob('*')) if p.is_file()]
def error(code): return {'status':'execution_error','error':code,'findings':[]}
def safe_xml(data,limit=MAX_INVOICE):
    if len(data)>limit: raise InputError('file_size_limit')
    # Restrict to UTF-8/ASCII. Reject DTD/entities before any parser sees bytes.
    try: text=data.decode('utf-8-sig')
    except UnicodeDecodeError: raise InputError('unsupported_xml_encoding') from None
    if '\x00' in text: raise InputError('unsupported_xml_encoding')
    declaration=re.match(r'\s*<\?xml\s[^?]*\bencoding\s*=\s*[\"\x27]([^\"\x27]+)',text)
    if declaration and declaration[1].lower() not in ('utf-8','utf8','us-ascii','ascii'): raise InputError('unsupported_xml_encoding')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)',text,re.I): raise InputError('dtd_or_entity_forbidden')
    try: return ET.fromstring(data)
    except ET.ParseError: raise InputError('malformed_xml') from None
def read_report(p):
    if not p.is_file(): raise InputError('missing_report')
    if p.stat().st_size>MAX_REPORT: raise InputError('report_size_limit')
    return safe_xml(p.read_bytes(),MAX_REPORT)
def parse_reports(report,report_input,expected_hash):
    """Use assessment for verdict; report-input proves all required stages completed."""
    try:
        r=read_report(report); ri=read_report(report_input)
        if r.tag!='{'+NS['r']+'}report' or ri.tag!='{'+NS['i']+'}createReportInput': raise InputError('unexpected_report_type')
        if ri.find('i:processingError',NS) is not None: raise InputError('validator_processing_error')
        if ri.find('i:validationResultsWellformedness',NS) is None: raise InputError('missing_wellformedness_stage')
        if ri.findall('i:validationResultsWellformedness/i:xmlSyntaxError',NS): raise InputError('validator_malformed_xml')
        scenario=ri.find('s:scenario',NS)
        if scenario is None: raise InputError('unmatched_scenario')
        xs=ri.find('i:validationResultsXmlSchema',NS)
        if xs is None: raise InputError('missing_schema_stage')
        schema_invalid=bool(xs.findall('i:xmlSyntaxError',NS))
        expected=len(scenario.findall('s:validateWithSchematron',NS))
        actual=ri.findall('i:validationResultsSchematron',NS)
        if not schema_invalid and (len(actual)!=expected or any(x.find('i:results/v:schematron-output',NS) is None for x in actual)):
            raise InputError('incomplete_schematron')
        decisions=[x for x in ('accept','reject') if r.find('r:assessment/r:'+x,NS) is not None]
        if len(decisions)!=1: raise InputError('missing_or_ambiguous_assessment')
        engine=r.findtext('r:engine/r:name',namespaces=NS)
        if not engine: raise InputError('missing_engine_identity')
        if engine!=ri.findtext('i:engine/i:name',namespaces=NS): raise InputError('engine_report_mismatch')
        for node,prefix in ((r,'r'),(ri,'i')):
            alg=node.findtext(f'{prefix}:documentIdentification/{prefix}:documentHash/{prefix}:hashAlgorithm',namespaces=NS)
            val=node.findtext(f'{prefix}:documentIdentification/{prefix}:documentHash/{prefix}:hashValue',namespaces=NS)
            if alg not in ('SHA-256','SHA256') or base64.b64decode(val or '').hex()!=expected_hash: raise InputError('report_document_hash_mismatch')
        overrides={}
        for custom in scenario.findall('s:createReport/s:customLevel',NS):
            for code in (custom.text or '').split():
                if code in overrides: raise InputError('ambiguous_custom_severity')
                overrides[code]=custom.get('level')
        findings=[]; counts=collections.Counter()
        for step in r.findall('r:scenarioMatched/r:validationStepResult',NS):
            for msg in step.findall('r:message',NS):
                rule=msg.get('code') or 'XML-SCHEMA'
                location=msg.get('xpathLocation') or ('line:'+msg.get('lineNumber','')+':column:'+msg.get('columnNumber',''))
                key=(step.get('id'),rule,location); counts[key]+=1
                reported=msg.get('level','unknown')
                effective=overrides.get(rule,reported) if rule!='PROCESSING_ERROR' else reported
                if reported not in LEVEL or effective not in LEVEL: raise InputError('unknown_report_severity')
                findings.append({'stage':key[0],'rule_id':rule,'location':location,'occurrence':counts[key],'severity':effective,'reported_severity':reported,'message_sha256':sha(''.join(msg.itertext()).strip().encode())})
        findings.sort(key=finding_key)
        resources={step.get('id'):[loc.text for loc in step.findall('s:resource/s:location',NS)] for step in r.findall('r:scenarioMatched/r:validationStepResult',NS)}
        return {'status':'pass' if decisions[0]=='accept' else 'fail','engine':engine,'scenario':scenario.findtext('s:name',namespaces=NS),'stage_resources':resources,'schema_valid':not schema_invalid,'schematron_stages':len(actual),'findings':findings}
    except (InputError,ValueError,TypeError) as e:
        return error(str(e) if isinstance(e,InputError) else 'malformed_report_metadata')
def finding_key(f): return (f['stage'],f['rule_id'],f['location'],f['occurrence'])
def delta(a,b):
    if a['status']=='execution_error' or b['status']=='execution_error': return {'comparable':False,'regression':None,'unresolved':[{'reason':'validator_execution_error'}]}
    if 'document_sha256' in a and 'document_sha256' in b:
        return location_matching.compare(a,b,LEVEL)
    aa={finding_key(f):f for f in a['findings']}; bb={finding_key(f):f for f in b['findings']}
    added=[bb[k] for k in sorted(bb.keys()-aa.keys())]; removed=[aa[k] for k in sorted(aa.keys()-bb.keys())]
    severity=[]; reported_severity=[]; messages=[]
    for k in sorted(aa.keys()&bb.keys()):
        if aa[k]['severity']!=bb[k]['severity']: severity.append({'identity':list(k),'old':aa[k]['severity'],'new':bb[k]['severity']})
        if aa[k].get('reported_severity',aa[k]['severity'])!=bb[k].get('reported_severity',bb[k]['severity']): reported_severity.append({'identity':list(k),'old':aa[k].get('reported_severity',aa[k]['severity']),'new':bb[k].get('reported_severity',bb[k]['severity'])})
        if aa[k]['message_sha256']!=bb[k]['message_sha256']: messages.append({'identity':list(k),'old':aa[k]['message_sha256'],'new':bb[k]['message_sha256']})
    regression=(a['status']=='pass' and b['status']=='fail') or any(LEVEL.get(f['severity'],0)>=2 for f in added) or any(LEVEL.get(f['new'],0)>LEVEL.get(f['old'],0) for f in severity)
    # Compatibility for callers supplying hand-built/raw report records. These
    # literal deltas are diagnostic only; without XML there is no complete proof.
    return {'comparable':False,'outcome':a['status']+'->'+b['status'],'added':added,'removed':removed,'severity_changes':severity,'reported_severity_changes':reported_severity,'message_changes':messages,'regression':True if a['status']=='pass' and b['status']=='fail' else None,'interpretation':'literal_diagnostics_without_xml_proof','unresolved':[{'reason':'invoice_xml_evidence_missing'}]}
def paired_comparison(old,new):
    rows=[]
    for fid in sorted(old.keys()|new.keys()):
        if fid not in old or fid not in new:
            rows.append({'id':fid,'pair_status':'added' if fid not in old else 'missing'}); continue
        a,b=old[fid]['old_rules'],old[fid]['new_rules']; c,d=new[fid]['old_rules'],new[fid]['new_rules']
        ac,bd=delta(a,c),delta(b,d)
        def effect(diff,before_result,after_result):
            # Compare changes, not proof metadata or unchanged finding matches.
            # Otherwise a rules-only notation change can manufacture an app/rule
            # interaction even when the two application outputs are identical.
            before,after=diff['outcome'].split('->')
            value={'outcome':'unchanged' if before==after else diff['outcome']}
            for key,result in (('added',after_result),('removed',before_result)):
                changes=[]
                for f in diff[key]:
                    node=result['location_resolution'][f['finding_index']]['node']
                    changes.append((f['stage'],f['rule_id'],node,*location_matching.signature(f)))
                value[key]=sorted(changes)
            for key in ('severity_changes','reported_severity_changes','message_changes'):
                value[key]=sorted((tuple(c['identity'][:3]),c['old'],c['new']) for c in diff[key])
            return value
        # Outcome interaction is independently observable even when finding
        # correspondence is incomplete. Do not hide verdicts behind matching.
        valid=all(x['status']!='execution_error' for x in (a,b,c,d))
        outcome_interaction=((a['status']==c['status'])!=(b['status']==d['status']) or (a['status']!=c['status'] and b['status']!=d['status'] and (a['status'],c['status'])!=(b['status'],d['status']))) if valid else None
        interaction=effect(ac,a,c)!=effect(bd,b,d) if ac['comparable'] and bd['comparable'] else (True if outcome_interaction else None)
        rows.append({'id':fid,'pair_status':'paired','A_vs_B':delta(a,b),'C_vs_D':delta(c,d),'A_vs_C':ac,'B_vs_D':bd,'interaction':interaction,'outcome_interaction':outcome_interaction,'finding_interaction_complete':ac['comparable'] and bd['comparable']})
    return rows
def load_manifest(path,root):
    try: m=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,OSError): raise InputError('malformed_manifest') from None
    if not isinstance(m,dict) or not isinstance(m.get('fixtures'),list): raise InputError('malformed_manifest')
    rows=[]; ids=collections.Counter()
    for row in m['fixtures']:
        if not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ('id','source_path','sha256')): raise InputError('malformed_manifest_row')
        if not re.fullmatch('[0-9a-f]{64}',row['sha256']): raise InputError('malformed_fixture_hash')
        if not row['id'] or len(row['id'])>300 or any(ord(c)<32 for c in row['id']): raise InputError('invalid_fixture_id')
        p=(root/row['source_path']).resolve()
        if not p.is_relative_to(root.resolve()): raise InputError('fixture_path_outside_root')
        ids[row['id']]+=1; rows.append(dict(row,path=p))
    dup=sorted(k for k,v in ids.items() if v>1)
    if dup: raise InputError('ambiguous_fixture_ids',{'ambiguous_ids':dup})
    return m,sorted(rows,key=lambda r:r['id'])
def java_command(java,engine,bundle,output):
    cp=str(engine/'*')+os.pathsep+str(engine/'libs/*')
    return [str(java),'-Xmx512m','-Djavax.xml.accessExternalDTD=','-Djavax.xml.accessExternalSchema=file','-Djavax.xml.accessExternalStylesheet=file','-cp',cp,'de.kosit.validationtool.cmd.CommandLineApplication','-s',str(bundle/'scenarios.xml'),'-r',str(bundle),'-o',str(output),'--serialize-report-input']
def run_batch(rows,java,engine,bundle,timeout=120,temp_root=None,runner=subprocess.run):
    outcomes={}; start=time.perf_counter(); rc=None
    with tempfile.TemporaryDirectory(prefix='xreg-',dir=temp_root) as temp:
        work=Path(temp); targets=[]
        for index,row in enumerate(rows):
            try:
                if row['path'].stat().st_size>MAX_INVOICE: raise InputError('file_size_limit')
                data=row['path'].read_bytes()
                if sha(data)!=row['sha256']: raise InputError('fixture_hash_mismatch')
                safe_xml(data)
                name=f'f{index:04d}'; target=work/(name+'.xml'); target.write_bytes(data)
                targets.append((row,name,target))
            except (OSError,InputError) as e: outcomes[row['id']]=error(str(e) if isinstance(e,InputError) else 'fixture_unreadable')
        if targets:
            try:
                # Suppress raw stdout/stderr: official diagnostics may quote invoice values.
                proc=runner(java_command(java,engine,bundle,work)+[str(t[2]) for t in targets],cwd=work,input=b'',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
                rc=proc.returncode
                for row,name,target in targets:
                    outcomes[row['id']]=parse_reports(work/(name+'-report.xml'),work/(name+'-reportInput.xml'),row['sha256'])
                    location_matching.attach(outcomes[row['id']],target.read_bytes(),bundle,safe_xml)
                if rc<0 or rc>len(targets):
                    for row,_,_ in targets: outcomes[row['id']]=error('validator_process_failure')
                elif rc!=sum(x['status']=='fail' for x in outcomes.values()) and not any(x['status']=='execution_error' for x in outcomes.values()):
                    for row,_,_ in targets: outcomes[row['id']]=error('exit_assessment_disagreement')
            except subprocess.TimeoutExpired:
                for row,_,_ in targets: outcomes[row['id']]=error('validator_timeout')
            except OSError:
                for row,_,_ in targets: outcomes[row['id']]=error('validator_launch_failure')
    return outcomes,{'duration_seconds':time.perf_counter()-start,'process_exit_code':rc,'attempted':len(rows),'temporary_directory_removed':not work.exists()}
def junit(results):
    cases=[]
    for app,rows in sorted(results['corpora'].items()):
        for fid,row in sorted(rows.items()):
            for rule in ('old_rules','new_rules'):
                result=row[rule]; c=ET.Element('testcase',{'classname':app+'.'+rule,'name':fid})
                if result['status']=='execution_error': ET.SubElement(c,'error',{'type':'execution_error','message':result['error']})
                elif result['status']=='fail': ET.SubElement(c,'failure',{'type':'validation_failure','message':'Official KoSIT assessment: reject'})
                cases.append(c)
            diff=delta(row['old_rules'],row['new_rules']); c=ET.Element('testcase',{'classname':app+'.rule_regression','name':fid})
            if diff['regression']: ET.SubElement(c,'failure',{'type':'new_regression','matching_complete':str(diff['comparable']).lower()})
            elif not diff['comparable']: ET.SubElement(c,'error',{'type':'incomplete_comparison'})
            cases.append(c)
    for pair in results['pair_comparisons']:
        if pair['pair_status']!='paired':
            c=ET.Element('testcase',{'classname':'application_pairing','name':pair['id']})
            ET.SubElement(c,'skipped',{'message':pair['pair_status']+' application output'}); cases.append(c)
        else:
            for axis in ('A_vs_C','B_vs_D'):
                c=ET.Element('testcase',{'classname':'application_regression.'+axis,'name':pair['id']})
                d=pair[axis]
                if d['regression']: ET.SubElement(c,'failure',{'type':'new_regression','matching_complete':str(d['comparable']).lower()})
                elif not d['comparable']: ET.SubElement(c,'error',{'type':'incomplete_comparison'})
                cases.append(c)
    root=ET.Element('testsuite',{'name':'xrechnung-regression','tests':str(len(cases)),'failures':str(sum(c.find('failure') is not None for c in cases)),'errors':str(sum(c.find('error') is not None for c in cases))})
    root.extend(cases); ET.indent(root)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)+b'\n'
def execute(args):
    start=time.perf_counter(); old_m,old_rows=load_manifest(args.corpus,args.root)
    for name in ('results.json','junit.xml','operations.json'):
        if (args.out/name).exists(): raise InputError('output_already_exists')
    inputs={'old_app':(old_m,old_rows,args.corpus)}
    if args.new_corpus:
        if args.new_root is None: raise InputError('new_root_required')
        nm,nr=load_manifest(args.new_corpus,args.new_root); inputs['new_app']=(nm,nr,args.new_corpus)
    for path in (args.engine,args.old_rules,args.new_rules):
        if not path.is_dir(): raise InputError('missing_dependency_directory')
    if not args.java.is_file(): raise InputError('missing_java')
    provenance={'engine_files':tree_identity(args.engine),'java_executable_sha256':sha(args.java.read_bytes()),'bundles':{'old_rules':tree_identity(args.old_rules),'new_rules':tree_identity(args.new_rules)},'corpus_manifest_hashes':{k:sha(v[2].read_bytes()) for k,v in inputs.items()}}
    if args.provenance:
        provenance['release_manifest']=json.loads(args.provenance.read_text(encoding='utf-8'))
    results={'schema':2,'provenance':provenance,'corpora':{},'rule_comparisons':{}}
    ops={'batches':[]}
    for app,(manifest,rows,_) in inputs.items():
        out={row['id']:{'sha256':row['sha256']} for row in rows}
        for rule,bundle in (('old_rules',args.old_rules),('new_rules',args.new_rules)):
            for pos in range(0,len(rows),args.batch_size):
                batch,op=run_batch(rows[pos:pos+args.batch_size],args.java,args.engine,bundle,args.timeout,args.temp_root)
                for fid,value in batch.items(): out[fid][rule]=value
                ops['batches'].append(dict(op,app=app,rule=rule,first_index=pos))
                print(f'{app} {rule}: {min(pos+args.batch_size,len(rows))}/{len(rows)} attempted',flush=True)
        results['corpora'][app]=out
        results['rule_comparisons'][app]={fid:delta(row['old_rules'],row['new_rules']) for fid,row in out.items()}
    results['pair_comparisons']=paired_comparison(results['corpora']['old_app'],results['corpora']['new_app']) if 'new_app' in inputs else []
    values=[r[rule] for rows in results['corpora'].values() for r in rows.values() for rule in ('old_rules','new_rules')]
    errors=sum(r['status']=='execution_error' for r in values)
    regressions=sum(d['regression'] is True for rows in results['rule_comparisons'].values() for d in rows.values())
    app_regressions=sum(d[k]['regression'] is True for d in results['pair_comparisons'] if d['pair_status']=='paired' for k in ('A_vs_C','B_vs_D'))
    incomplete=sum(not d['comparable'] for rows in results['rule_comparisons'].values() for d in rows.values())+sum(not d[k]['comparable'] for d in results['pair_comparisons'] if d['pair_status']=='paired' for k in ('A_vs_C','B_vs_D'))
    results['summary']={'attempted':len(values),'processed':len(values)-errors,'execution_errors':errors,'passes':sum(r['status']=='pass' for r in values),'validation_failures':sum(r['status']=='fail' for r in values),'rule_regressions':regressions,'application_axis_regressions':app_regressions,'unmatched_pairs':sum(r['pair_status']!='paired' for r in results['pair_comparisons'])}
    results['summary']['incomplete_comparisons']=incomplete
    args.out.mkdir(parents=True,exist_ok=True)
    for name in ('results.json','junit.xml','operations.json'):
        if (args.out/name).exists(): raise InputError('output_already_exists')
    write_json(args.out/'results.json',results); (args.out/'junit.xml').write_bytes(junit(results))
    ops['duration_seconds']=time.perf_counter()-start; write_json(args.out/'operations.json',ops)
    print(json.dumps(results['summary'],sort_keys=True))
    return 2 if errors else (3 if results['summary']['unmatched_pairs'] or incomplete else (1 if regressions or app_regressions else 0))
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('corpus','root','old-rules','new-rules','engine','java','out'): p.add_argument('--'+flag,required=True,type=lambda x:Path(x).resolve())
    for flag in ('new-corpus','new-root','provenance','temp-root'): p.add_argument('--'+flag,type=lambda x:Path(x).resolve())
    p.add_argument('--batch-size',type=int,default=25); p.add_argument('--timeout',type=float,default=120)
    args=p.parse_args(argv)
    try:
        if not 1<=args.batch_size<=50 or not 0<args.timeout<=600: raise InputError('invalid_execution_bound')
        return execute(args)
    except InputError as e:
        message={'status':'execution_error','error':str(e)}
        if e.details: message['details']=e.details
        print(json.dumps(message),file=sys.stderr); return 2
    except (OSError,ValueError,KeyError,TypeError):
        print('{"status":"execution_error","error":"invalid_configuration_or_io"}',file=sys.stderr); return 2
if __name__=='__main__': sys.exit(main())
