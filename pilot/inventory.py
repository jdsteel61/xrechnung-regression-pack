"""Write an additive release/licence inventory; never changes pinned inputs."""
import argparse
import json
from pathlib import Path
import platform
import sys
import zipfile

import xreg
from .worker import ROOT, ENGINE, OLD, NEW, JAVA


def inventory():
    jars = []
    for path in sorted(ENGINE.rglob('*.jar')):
        with zipfile.ZipFile(path) as archive:
            notices = [name for name in archive.namelist() if
                       'license' in name.lower() or 'licence' in name.lower() or 'notice' in name.lower()]
            jars.append({'component':path.name,'sha256':xreg.sha(path.read_bytes()),
                         'licence_entries':[{'path':name,'sha256':xreg.sha(archive.read(name))} for name in notices],
                         'licence_clearance':'UNREVIEWED; see existing POM audit for missing notices'})
    refs = ['manifests/downloads.json','manifests/provenance.json','manifests/third-party-licences.json',
            'manifests/dependency-licence-audit.json','manifests/licence-files.json',
            'manifests/maintenance-2025-07-10/provenance.json']
    return {'status':'INVENTORY_NOT_LEGAL_CLEARANCE','created_by':'pilot.inventory',
        'runtime':{'python':sys.version,'platform':platform.platform(),'java_executable_sha256':xreg.sha(JAVA.read_bytes()),
                   'python_licence':'PSF; runtime includes bundled third-party components, review required',
                   'java_licence':'Temurin/OpenJDK GPLv2 with Classpath Exception; verify notices in runtime distribution'},
        'first_party_licence':'UNASSIGNED; owner must confirm rights and distribution terms',
        'jars':jars,'pinned_bundles':{'old':xreg.tree_identity(OLD),'new':xreg.tree_identity(NEW)},
        'reference_manifests':[{'path':r,'sha256':xreg.sha((ROOT/r).read_bytes())} for r in refs],
        'licence_files':[{'path':p.relative_to(ROOT).as_posix(),'sha256':xreg.sha(p.read_bytes())}
            for folder in (ROOT/'licences',ROOT/'manifests/maintenance-2025-07-10')
            for p in sorted(folder.iterdir()) if p.is_file() and ('LICENSE' in p.name or 'NOTICE' in p.name or p.suffix=='.pom')],
        'pilot_source_files':[{'path':p.relative_to(ROOT).as_posix(),'sha256':xreg.sha(p.read_bytes())}
            for p in sorted((ROOT/'pilot').rglob('*')) if p.is_file() and '__pycache__' not in p.parts],
        'deployment_image':'NOT_BUILT; base image digest, Linux JRE, OS packages and their notices unresolved',
        'external_services':['Stripe hosted Checkout sandbox','Google Cloud Run, Firestore, Storage, Scheduler, Logging, Artifact Registry, Secret Manager','OpenAI Responses API'],
        'review_flags':['CEN EUPL-1.2 communication/source obligations', 'KoSIT and bundled dependency notices',
                        'Saxon licence and version-specific obligations', 'Peppol/CEN/UBL/CII schema provenance',
                        'Public fixture reuse and report publication', 'No right to redistribute is inferred from download availability']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Choose a fresh output file')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    xreg.write_json(args.out,inventory())
