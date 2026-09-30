from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ANALYZER = "clap-relative-mood-v5"
MOODS = ('calm', 'understated', 'melancholic', 'warm', 'intimate',
         'dreamy', 'nostalgic', 'lonely', 'hopeful', 'bright')
TABLES = {
    'ventures': 'id', 'assets': 'id', 'music_recordings': 'asset_id',
    'audio_assets': 'asset_id', 'asset_venture_links': 'asset_id,venture_id',
    'asset_files': 'asset_id,file_id', 'files': 'id', 'file_locations': 'id',
    'recording_annotations': 'id', 'audio_analyses': 'id',
}


class RadioError(Exception):
    pass


def secret(name: str, service: str, *, keychain_only=False) -> str:
    value = None if keychain_only else os.environ.get(name)
    if value:
        return value
    if os.uname().sysname == 'Darwin':
        result = subprocess.run(['security', 'find-generic-password', '-s', service,
                                 '-a', 'slow-wednesday-radio', '-w'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    raise RadioError(f'{name} 환경변수 또는 Keychain {service} 항목이 필요합니다.')


class Supabase:
    def __init__(self):
        self.url = os.environ.get('SUPABASE_URL', '').rstrip('/')
        if not self.url.startswith('https://'):
            raise RadioError('SUPABASE_URL에 https 프로젝트 URL을 설정하세요.')
        self.key = secret('SUPABASE_SECRET_KEY', 'slow-wednesday-radio.supabase')

    def request(self, table, params=None, body=None):
        url = self.url + '/rest/v1/' + table + '?' + urllib.parse.urlencode(params or {})
        headers = {'apikey': self.key, 'Content-Type': 'application/json'}
        if self.key.startswith('eyJ'):
            headers['Authorization'] = 'Bearer ' + self.key
        if body is not None:
            headers['Prefer'] = 'resolution=ignore-duplicates,return=minimal'
        req = urllib.request.Request(url, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            # Do not include response bodies, request objects or credentials in errors.
            raise RadioError(f'Supabase {table}: HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise RadioError(f'Supabase {table}: 연결 또는 응답 오류') from None

    def snapshot(self):
        tables = {}
        for table, order in TABLES.items():
            rows = []
            while True:
                params = {'select': '*', 'order': order,
                          'offset': len(rows), 'limit': 500}
                if table == 'audio_analyses':
                    params.update(analyzer='eq.' + ANALYZER,
                                  select='id,audio_asset_id,analyzer,analyzer_version,analyzed_at,duration_seconds,metrics')
                page = self.request(table, params)
                if not isinstance(page, list):
                    raise RadioError(f'{table}: 목록 응답이 아닙니다.')
                if not page:
                    break
                rows.extend(page)  # Continue until empty, even if server caps below 500.
            tables[table] = rows
        if not tables['music_recordings']:
            raise RadioError('조회된 recording이 없습니다. 프로젝트와 DB 권한을 확인하세요.')
        return tables

    def append_usage(self, annotation):
        self.request('recording_annotations', {'on_conflict': 'id'}, annotation)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve_uri(uri: str, root: Path) -> Path:
    if not uri.startswith('sw-saved://'):
        raise RadioError('지원하지 않는 source URI입니다.')
    relative = urllib.parse.unquote(uri[len('sw-saved://'):], errors='strict')
    if not relative or '\x00' in relative or Path(relative).is_absolute():
        raise RadioError('잘못된 source URI입니다.')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RadioError('source URI가 library 밖을 가리킵니다.')
    return path


def probe(path: Path):
    try:
        p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                            '-show_entries', 'stream=duration,sample_rate,channels:format=duration',
                            '-of', 'json', str(path)], capture_output=True, timeout=30)
        data = json.loads(p.stdout)
        stream = data['streams'][0]
        duration = float(stream.get('duration') or data['format']['duration'])
        if p.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return duration
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
        raise RadioError(f'음원 정보를 읽을 수 없습니다: {path.name}') from None


def use_counts(annotations):
    counts = {}
    for annotation in annotations:
        m = annotation['metadata']
        if (annotation['annotation_type'] == 'playlist_usage'
                and m.get('history_event') == 'used'
                and not m.get('superseded_by_reconciliation')
                and m.get('venture_key', 'sw') == 'sw'):
            inc = m.get('use_count_increment', 1)
            if isinstance(inc, bool) or not isinstance(inc, int) or inc < 0:
                raise RadioError('사용 이력의 use_count_increment가 올바르지 않습니다.')
            rid = annotation['recording_id']
            counts[rid] = counts.get(rid, 0) + inc
    return counts


def rejected(metadata):
    return (metadata.get('usable_for_curation') is False
            or metadata.get('is_available') is False
            or metadata.get('status') in ('rejected', 'unavailable'))


def load_catalog(data, root: Path):
    for table in TABLES:
        if not isinstance(data.get(table), list):
            raise RadioError(f'catalog에 {table} 목록이 없습니다.')
    venture = next((v['id'] for v in data['ventures'] if v['stable_key'] == 'sw'), None)
    linked = {a['asset_id'] for a in data['asset_venture_links'] if a['venture_id'] == venture}
    assets = {a['id']: a for a in data['assets']}
    files = {f['id']: f for f in data['files']}
    counts = use_counts(data['recording_annotations'])
    blocked = {a['recording_id'] for a in data['recording_annotations']
               if a['annotation_type'] == 'curation_rejection' or rejected(a['metadata'])}
    analyses = {}
    for a in sorted(data['audio_analyses'], key=lambda a: (a['analyzed_at'], a['id'])):
        if a['analyzer'] == ANALYZER:
            analyses[a['audio_asset_id']] = a
    def grouped(rows, key):
        out = {}
        for row in rows:
            out.setdefault(row[key], []).append(row)
        return out
    audios = grouped(data['audio_assets'], 'recording_id')
    afs = grouped(data['asset_files'], 'asset_id')
    locations = grouped(data['file_locations'], 'file_id')
    tracks, excluded = [], []
    for r in sorted(data['music_recordings'], key=lambda r: r['asset_id']):
        rid = r['asset_id']
        asset = assets.get(rid, {})
        if rid not in linked:
            continue
        reason = '로컬 파일 또는 source 연결 없음'
        if rid in blocked or rejected(asset.get('metadata', {})):
            excluded.append({'recording_id': rid, 'title': asset.get('title'), 'reason': 'rejected/unavailable'})
            continue
        candidates = []
        for audio in audios.get(rid, []):
            analysis = analyses.get(audio['asset_id'])
            if not analysis:
                reason = 'CLAP v5 분석 없음'
                continue
            profile = analysis['metrics'].get('within_track_centered_profile', {})
            try:
                vector = [float(profile[k]) for k in MOODS]
                if not all(math.isfinite(v) for v in vector):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                reason = 'CLAP profile 오류'
                continue
            for af in afs.get(audio['asset_id'], []):
                if af['role'] != 'source':
                    continue
                f = files[af['file_id']]
                for loc in locations.get(f['id'], []):
                    if not loc['is_available'] or loc['location_type'] != 'source_relative':
                        continue
                    try:
                        path = resolve_uri(loc['uri'], root)
                        if not path.is_file():
                            continue
                        sha = digest(path)
                        if f.get('sha256') and sha != f['sha256']:
                            raise RadioError('파일 SHA256 불일치')
                        if f.get('byte_size') is not None and path.stat().st_size != f['byte_size']:
                            raise RadioError('파일 크기 불일치')
                        duration = probe(path)
                    except (RadioError, OSError) as exc:
                        reason = str(exc)
                        continue
                    group = ('batch:' + r['generation_batch_id'] if r.get('generation_batch_id')
                             else 'filename:' + re.sub(r'_\d+$', '', Path(f['original_filename']).stem).casefold())
                    candidates.append(dict(recording_id=rid, title=asset.get('title', f['original_filename']),
                        audio_asset_id=audio['asset_id'], file_id=f['id'], uri=loc['uri'],
                        sha256=sha, byte_size=path.stat().st_size, duration_seconds=duration,
                        samples=round(duration * 48000), sibling_group=group,
                        use_count=counts.get(rid, 0), mood=vector, analysis_id=analysis['id'],
                        analyzer_version=analysis['analyzer_version']))
        if candidates:
            tracks.append(sorted(candidates, key=lambda t: (t['audio_asset_id'], t['file_id'], t['uri']))[0])
        else:
            excluded.append({'recording_id': rid, 'title': asset.get('title'), 'reason': reason})
    return tracks, excluded
