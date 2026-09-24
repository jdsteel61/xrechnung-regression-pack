"""Deterministic synthetic app outputs, derived from an Apache-2.0 public fixture.
These are demonstrations, never evidence of an actual customer release.
"""
import json,xml.etree.ElementTree as ET
from pathlib import Path
import xreg
CBC='urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
CAC='urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
def variants(source):
    root=ET.fromstring(source)
    ET.register_namespace('',root.tag.split('}')[0][1:]); ET.register_namespace('cbc',CBC); ET.register_namespace('cac',CAC)
    def make(mode):
        invoice=ET.fromstring(source)
        if mode=='missing_buyer_reference':
            for e in list(invoice):
                if e.tag=='{'+CBC+'}BuyerReference': invoice.remove(e)
        if mode in ('absolute_url','relative_url'):
            reference=ET.Element('{'+CAC+'}AdditionalDocumentReference')
            ET.SubElement(reference,'{'+CBC+'}ID').text='SYNTHETIC-REFERENCE'
            attachment=ET.SubElement(reference,'{'+CAC+'}Attachment'); external=ET.SubElement(attachment,'{'+CAC+'}ExternalReference')
            ET.SubElement(external,'{'+CBC+'}URI').text='https://example.invalid/attachment.pdf' if mode=='absolute_url' else 'attachment.pdf'
            position=next(i for i,e in enumerate(invoice) if e.tag in {'{'+CAC+'}'+name for name in ('ProjectReference','Signature','AccountingSupplierParty')})
            invoice.insert(position,reference)
        return ET.tostring(invoice,encoding='utf-8',xml_declaration=True)
    return {mode:make(mode) for mode in ('unchanged','missing_buyer_reference','absolute_url','relative_url')}
def create(public_root,destination,manifest_dir,base_id='test/instances/ubl-inv/ubl-inv-br-de-1-test.xml'):
    source=(public_root/base_id).read_bytes(); modes=variants(source)
    specifications=[('unchanged','unchanged','unchanged'),('application_missing_buyer_reference','unchanged','missing_buyer_reference'),('interaction_bad_url','absolute_url','relative_url'),('rule_axis_bad_url','relative_url','relative_url'),('application_repairs_url','relative_url','absolute_url'),('missing_pair','unchanged',None),('added_pair',None,'unchanged')]
    result={}
    for axis,index in [('old_app',1),('new_app',2)]:
        folder=destination/axis; folder.mkdir(parents=True,exist_ok=True); rows=[]
        for spec in specifications:
            if spec[index] is None: continue
            data=modes[spec[index]]; name=spec[0]+'.xml'; (folder/name).write_bytes(data)
            rows.append({'id':'synthetic/'+spec[0],'source_path':name,'sha256':xreg.sha(data),'bytes':len(data),'mutation':spec[index],'parent_public_fixture':base_id,'parent_sha256':xreg.sha(source)})
        manifest={'schema':1,'label':'SYNTHETIC application-output demonstration, not customer releases','licence':'Apache-2.0 derivative; see licences/xrechnung-schematron-v2.6.0-LICENSE','generation':'synthetic.py variants(); parses/reserializes source; modifies only described elements; example.invalid is inert and never fetched','fixtures':sorted(rows,key=lambda r:r['id'])}
        manifest_dir.mkdir(parents=True,exist_ok=True); mp=manifest_dir/(axis+'.json')
        data=xreg.canonical(manifest)
        if mp.exists() and mp.read_bytes()!=data: raise RuntimeError('synthetic_manifest_changed')
        if not mp.exists(): mp.write_bytes(data)
        result[axis]=(mp,folder)
    return result
if __name__=='__main__':
    R=Path(__file__).resolve().parent
    create(R/'.deps/xrechnung-schematron-source-v2.6.0/xrechnung-schematron-2.6.0',R/'.synthetic',R/'manifests/synthetic')
