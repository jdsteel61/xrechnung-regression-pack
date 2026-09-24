"""Hash-verified local download of pinned public runtime dependencies (no pip)."""
import hashlib,io,json,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DEPENDENCIES=('java','validator-v1.6.3','validator-configuration-xrechnung-v2026-01-31','validator-configuration-xrechnung-v2026-08-31')
def fetch(artifact):
    with urllib.request.urlopen(urllib.request.Request(artifact['url'],headers={'User-Agent':'xrechnung-local-benchmark'}),timeout=120) as response:
        data=response.read(160*1024*1024+1)
    if len(data)>160*1024*1024 or hashlib.sha256(data).hexdigest()!=artifact['sha256']: raise RuntimeError('download_size_or_hash_mismatch: '+artifact['name'])
    return data
def extract(data,destination,members=None):
    destination=destination.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries=[i for i in z.infolist() if members is None or i.filename in members]
        if sum(i.file_size for i in entries)>400*1024*1024: raise RuntimeError('archive_size_limit')
        for info in entries:
            target=(destination/info.filename).resolve()
            if not target.is_relative_to(destination) or (info.external_attr>>16)&0o170000==0o120000: raise RuntimeError('unsafe_archive_member')
            if info.is_dir(): target.mkdir(parents=True,exist_ok=True)
            else:
                data=z.read(info)
                if target.exists():
                    if target.read_bytes()!=data: raise RuntimeError('existing_dependency_differs: '+info.filename)
                else: target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
def ensure():
    artifacts=json.loads((ROOT/'manifests/downloads.json').read_text())
    for item in artifacts:
        if item['name'] not in DEPENDENCIES: continue
        destination=ROOT/'.deps'/item['name']
        # Always verify directory contents against pinned archive; downloads held only in memory.
        data=fetch(item); extract(data,destination)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            expected={i.filename.rstrip('/') for i in z.infolist() if not i.is_dir()}
        actual={p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
        if actual!=expected: raise RuntimeError('unexpected_dependency_files: '+item['name'])
        print('Verified dependency '+item['name'],flush=True)
def materialise_corpus(destination):
    m=json.loads((ROOT/'manifests/corpus.json').read_text())
    item=next(a for a in json.loads((ROOT/'manifests/downloads.json').read_text()) if a['name']==m['archive'])
    needed={m['source_prefix']+'/'+f['source_path'] for f in m['fixtures']}
    needed.add(m['source_prefix']+'/test/instances/ubl-inv/ubl-inv-br-de-1-test.xml')
    extract(fetch(item),destination,needed)
    root=destination/m['source_prefix']
    for row in m['fixtures']:
        if hashlib.sha256((root/row['source_path']).read_bytes()).hexdigest()!=row['sha256']: raise RuntimeError('frozen_corpus_hash_mismatch')
    return root
if __name__=='__main__':ensure()
