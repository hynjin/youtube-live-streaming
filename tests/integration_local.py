"""Real-media transport tests. Never contacts YouTube or writes production Supabase."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slow_wednesday_radio.catalog import RadioError, digest
from slow_wednesday_radio.history import Journal
from slow_wednesday_radio.planning import read_manifest
from slow_wednesday_radio import streaming

root = Path(__file__).resolve().parents[1]
manifest = read_manifest(root/'.local/session.json')
music = Path(os.environ['SW_SAVED_DIR'])
source_visual = root/'.local/test-visuals/unused/test-card.png'

class FakeDB:
    def __init__(self): self.rows = {}
    def request(self, *args, **kwargs): return []
    def append_usage(self, payload): self.rows.setdefault(payload['id'], payload)

with tempfile.TemporaryDirectory() as directory:
    base=Path(directory)
    visuals=base/'visuals';(visuals/'unused').mkdir(parents=True)
    (visuals/'unused/test-card.png').write_bytes(source_visual.read_bytes())
    m=copy.deepcopy(manifest)
    m['session_id']=str(uuid.uuid4())
    m['tracks']=m['tracks'][:2]
    m['duration_seconds']=sum(t['samples'] for t in m['tracks'])/48000
    client=FakeDB()
    original=streaming.encoder_command
    def local_command(visual,seconds,destination,**kwargs):
        return original(visual,seconds,str(base/'local-live.flv'),**kwargs)
    with patch.object(streaming,'secret',return_value='fake-test-key'), \
         patch.object(streaming,'Supabase',return_value=client), \
         patch.object(streaming,'encoder_command',side_effect=local_command):
        result=streaming.run(m,music,visuals,base/'state',limit=6)
    assert result['status']=='completed',result
    assert len(client.rows)==1,client.rows
    assert next(iter(client.rows.values()))['recording_id']==m['tracks'][0]['recording_id']
    assert (visuals/'used/test-card.png').exists()
    j=Journal(base/'state')
    assert j.status()['pending_usage']==0
    assert j.status()['sessions'][0]['status']=='completed'
    j.db.close()
    # Refused output: visual and use counts must remain untouched.
    (visuals/'unused/test-card.png').write_bytes(source_visual.read_bytes())
    (visuals/'used/test-card.png').unlink()
    failed=copy.deepcopy(m);failed['session_id']=str(uuid.uuid4())
    def fail_command(visual,seconds,destination,**kwargs):
        return original(visual,seconds,str(base/'missing-directory/out.flv'),**kwargs)
    with patch.object(streaming,'secret',return_value='fake-test-key'), \
         patch.object(streaming,'Supabase',return_value=client), \
         patch.object(streaming,'encoder_command',side_effect=fail_command):
        try: streaming.run(failed,music,visuals,base/'failure-state',limit=6)
        except RadioError: pass
        else: raise AssertionError('Expected output failure')
    assert (visuals/'unused/test-card.png').exists()
    j=Journal(base/'failure-state')
    assert j.status()['pending_usage']==0
    assert j.status()['sessions'][0]['status']=='failed'
    j.db.close()
    assert len(client.rows)==1
    print(json.dumps({'real_ffmpeg_output':result,'confirmed_recordings':len(client.rows),
                      'failure_did_not_consume_visual_or_usage':True}))
