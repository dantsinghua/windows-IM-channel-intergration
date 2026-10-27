"""Build a hash-pinned Python 3.12 compatibility wheel without changing cipher code."""
from pathlib import Path
import argparse,base64,csv,hashlib,io,json,zipfile
UPSTREAM_SHA256 = 'a39acd6fa6480232d159c29eb4d03025a10a794d60797250f1156c6121c66a0d'
VERSION = '0.3.0+xunjia.py312'

def build(source, output):
    source,output=Path(source),Path(output)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=UPSTREAM_SHA256:
        raise ValueError('UPSTREAM_WHEEL_HASH_MISMATCH')
    if output.exists():raise ValueError('OUTPUT_EXISTS')
    with zipfile.ZipFile(source) as archive:
        records={n:archive.read(n) for n in archive.namelist()}
    before_native={n:hashlib.sha256(b).hexdigest() for n,b in records.items() if n.endswith('.so')}
    assert before_native
    original=records['twofish.py'].decode()
    assert original.count('import imp\n')==1
    assert original.count("_twofish = cdll.LoadLibrary(imp.find_module('_twofish')[1])")==1
    changed=original.replace('import imp\n','from importlib.util import find_spec\n')
    changed=changed.replace("_twofish = cdll.LoadLibrary(imp.find_module('_twofish')[1])",
        "# Local compatibility change: resolve the unchanged native library without imp.\n"
        "_native_spec = find_spec('_twofish')\n"
        "if _native_spec is None or not _native_spec.origin:\n"
        "    raise ImportError('Twofish native library is unavailable')\n"
        "_twofish = cdll.LoadLibrary(_native_spec.origin)")
    compile(changed,'twofish.py','exec');records['twofish.py']=changed.encode()
    old_info='twofish-0.3.0.dist-info/'
    new_info='twofish-'+VERSION+'.dist-info/'
    payload={n.replace(old_info,new_info):b for n,b in records.items() if not n.endswith('/RECORD')}
    metadata=payload[new_info+'METADATA'].decode()
    assert metadata.count('Version: 0.3.0\n')==1
    payload[new_info+'METADATA']=metadata.replace('Version: 0.3.0\n','Version: '+VERSION+'\n').encode()
    provenance={'upstream_sha256':UPSTREAM_SHA256,'compatibility_version':VERSION,
        'change':'Replace removed imp.find_module with importlib.util.find_spec; preserve ctypes and cipher implementation',
        'unchanged_native_sha256':before_native,'license':'Original BSD-3-Clause license retained'}
    payload[new_info+'XUNJIA-COMPATIBILITY.json']=json.dumps(provenance,indent=2).encode()
    assert before_native=={n:hashlib.sha256(b).hexdigest() for n,b in payload.items() if n.endswith('.so')}
    listing=io.StringIO();writer=csv.writer(listing,lineterminator='\n')
    for n,b in sorted(payload.items()):
        writer.writerow([n,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(b).digest()).decode().rstrip('='),len(b)])
    writer.writerow([new_info+'RECORD','','']);payload[new_info+'RECORD']=listing.getvalue().encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(payload.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,10,27,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
    return {**provenance,'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'output':str(output)}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();print(json.dumps(build(args.source,args.output),indent=2))
