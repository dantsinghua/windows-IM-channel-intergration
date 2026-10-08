"""Produce a static Sites candidate containing UI files only, never runtime data."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

root=Path(__file__).resolve().parent
output=Path('/workspace/.qtrade-linux-debug/artifacts/sites-dist')
output.mkdir(parents=True,exist_ok=True)
names=('index.html','app.js','video.js')
for name in names:
    shutil.copyfile(root/'web'/name,output/name)
manifest={name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in names}
archive=output.parent/'redroid-sites.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in names:bundle.write(output/name,name)
(output.parent/'sites-build.json').write_text(json.dumps({'deployed':False,'files':manifest},indent=2)+'\n')
print('Static deployment candidate:',archive)
print('Contains only index.html, app.js, video.js; no configured endpoint, credentials or device data.')
