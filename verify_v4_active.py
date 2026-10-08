"""Verify normalized release bytes. Does not access accounts or submit orders."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'V4_ACTIVE_FILES.json').read_text())
for name,expected in manifest.items():
    target=root/name
    if not target.is_file() or hashlib.sha256(target.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=expected:raise SystemExit('V4_ACTIVE_CHECKSUM_FAILED: '+name)
print('V4_ACTIVE_RELEASE_VERIFIED:',len(manifest),'files. No account access or orders.')
