import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from slow_wednesday_radio.catalog import RadioError, Supabase, resolve_uri, use_counts
from slow_wednesday_radio.history import Journal, lock
from slow_wednesday_radio.planning import ALGORITHM, feasible, read_manifest, select, sequence
from slow_wednesday_radio.streaming import consume_visual, encoder_command


def track(i, group=None, use=0):
    return dict(recording_id=str(i), sibling_group=group or str(i), use_count=use,
                samples=48000*180, mood=[random.Random(i*10+k).random() for k in range(10)],
                start_sample=i*48000*180)


class CatalogTests(unittest.TestCase):
    def test_usage_keeps_recording_identity_and_ignores_superseded(self):
        def annotation(rid, kind='playlist_usage', **extra):
            return dict(recording_id=rid,annotation_type=kind,
                        metadata=dict(history_event='used',use_count_increment=1,**extra))
        rows=[annotation('song'),annotation('song',usage_kind='radio_live'),
              annotation('song_1'),annotation('song','playlist_usage_superseded'),
              annotation('song',superseded_by_reconciliation=True),
              annotation('song',venture_key='ssc')]
        self.assertEqual(use_counts(rows),{'song':2,'song_1':1})

    def test_uri(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            self.assertEqual(resolve_uri('sw-saved://A%20B_1.m4a',root),root/'A B_1.m4a')
            for bad in ('sw-saved://../x','sw-saved://%2Fetc/passwd','file:///tmp/x'):
                with self.assertRaises(RadioError):resolve_uri(bad,root)
            (root/'link').symlink_to('/etc')
            with self.assertRaises(RadioError):resolve_uri('sw-saved://link/passwd',root)

    def test_pagination_continues_under_server_cap(self):
        from slow_wednesday_radio import catalog
        client=object.__new__(Supabase)
        calls=[]
        def request(table,params):
            calls.append(params['offset'])
            return [{'asset_id':str(params['offset'])}] if params['offset']<3 else []
        client.request=request
        with patch.dict(catalog.TABLES,{'music_recordings':'asset_id'},clear=True):
            self.assertEqual(len(client.snapshot()['music_recordings']),3)
        self.assertEqual(calls,[0,1,2,3])


class PlannerTests(unittest.TestCase):
    def test_weighting_without_replacement(self):
        pool=[track(0,use=0),track(1,use=9)]
        hits=sum(select(pool,1,random.Random(seed))[0]['recording_id']=='0' for seed in range(2000))
        self.assertGreater(hits,1750)
        self.assertLess(hits,1900)
        with self.assertRaises(RadioError):select(pool,400,random.Random(1))

    def test_sibling_adjacency_many_seeds(self):
        pool=[track(i,group=str(i//2)) for i in range(40)]
        for seed in range(100):
            chosen=select(pool,6000,random.Random(seed))
            result=sequence(chosen,random.Random(seed))
            self.assertEqual({t['recording_id'] for t in chosen},{t['recording_id'] for t in result})
            self.assertTrue(all(a['sibling_group']!=b['sibling_group'] for a,b in zip(result,result[1:])))
            self.assertEqual(result,sequence(chosen,random.Random(seed)))

    def test_feasibility_edge(self):
        self.assertTrue(feasible({'a':3,'b':2}))
        self.assertFalse(feasible({'a':3,'b':2},'a'))
        result=sequence([track(i,group='a' if i%2==0 else 'b') for i in range(5)],random.Random(1))
        self.assertEqual([t['sibling_group'] for t in result],list('ababa'))

    def test_nearby_sampling_varies_and_excludes_distant_candidates(self):
        class FixedStart(random.Random):
            def choice(self, candidates):
                return next(c for c in candidates if c[1] == '0')
        pool = [track(i) for i in range(12)]
        for i, t in enumerate(pool):
            t['mood'] = [float(i)] * 10
        seconds = [sequence(pool, FixedStart(seed))[1]['recording_id'] for seed in range(500)]
        self.assertTrue(set(seconds).issubset({str(i) for i in range(1, 9)}))
        self.assertGreaterEqual(len(set(seconds)), 5)
        self.assertGreater(seconds.count('1'), seconds.count('8'))

    def test_equal_profiles_and_input_order(self):
        pool = [track(i, group=str(i//2)) for i in range(20)]
        for t in pool:
            t['mood'] = [0.] * 10
        a = sequence(pool, random.Random(19))
        b = sequence(list(reversed(pool)), random.Random(19))
        self.assertEqual(a, b)
        self.assertEqual(len({t['recording_id'] for t in a}), 20)
        self.assertTrue(all(x['sibling_group'] != y['sibling_group'] for x,y in zip(a,a[1:])))

    def test_existing_manifest_remains_playable(self):
        manifest = dict(schema_version=1, session_id='11111111-1111-4111-8111-111111111111',
                        tracks=[track(0)], duration_seconds=180, visual={'filename':'test.png'})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'session.json'
            for algorithm in ('weighted-wave-v1', ALGORITHM):
                manifest['algorithm'] = algorithm
                path.write_text(json.dumps(manifest))
                self.assertEqual(read_manifest(path)['tracks'], manifest['tracks'])


class HistoryTests(unittest.TestCase):
    def test_partial_output_and_retry_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d))
            m=dict(session_id='11111111-1111-4111-8111-111111111111',tracks=[track(0),track(1)])
            j.begin(m)
            self.assertEqual(j.status()['pending_usage'],0)
            j.progress(m,1.9)
            self.assertEqual(j.status()['pending_usage'],0)
            j.progress(m,100)
            j.progress(m,181)
            self.assertEqual(j.status()['pending_usage'],1)
            j.progress(m,183)
            self.assertEqual(j.status()['pending_usage'],2)
            class Client:
                def __init__(self):self.rows={};self.fail=True
                def append_usage(self,a):
                    self.rows.setdefault(a['id'],a)
                    if self.fail:
                        self.fail=False
                        raise RadioError('response lost after DB commit')
            client=Client()
            with self.assertRaises(RadioError):j.sync(client)
            self.assertEqual(j.sync(client),2)
            self.assertEqual(len(client.rows),2)
            self.assertEqual(j.sync(client),0)
            with self.assertRaises(RadioError):j.begin(m)
            j.db.close()

    def test_visual_move_and_lock(self):
        from slow_wednesday_radio.catalog import digest
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'unused').mkdir();(root/'used').mkdir()
            p=root/'unused/x.png';p.write_bytes(b'image')
            m={'visual':{'filename':'x.png','sha256':digest(p)}}
            consume_visual(m,root);consume_visual(m,root)
            self.assertFalse(p.exists());self.assertTrue((root/'used/x.png').exists())
            with lock(root/'lock'):
                with self.assertRaises(RadioError):
                    with lock(root/'lock'):pass

    def test_encoder_key_is_only_runtime_destination(self):
        args=encoder_command(Path('image.png'),100,'rtmps://example/key')
        self.assertEqual(args[-1],'rtmps://example/key')
        self.assertIn('-re',args)
        self.assertIn('-progress',args)
        self.assertNotIn('-report',args)


if __name__=='__main__':unittest.main()
