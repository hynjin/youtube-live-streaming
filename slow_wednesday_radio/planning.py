from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import uuid

from .catalog import ANALYZER, MOODS, RadioError, digest

ALGORITHM = 'weighted-neighbors-v2'


def feasible(groups, previous=None):
    """A group equal to previous needs a separator before every remaining item."""
    n = sum(groups.values())
    return all(count <= (n // 2 if group == previous else (n + 1) // 2)
               for group, count in groups.items())


def select(tracks, target, rng):
    if not math.isfinite(target) or target <= 0:
        raise RadioError('재생시간은 0보다 커야 합니다.')
    # Exponential race: weighted sampling without replacement.
    ranked = sorted(tracks, key=lambda t: (-math.log(max(rng.random(), 1e-15))
                                          * (1 + t['use_count']), t['recording_id']))
    picked, total = [], 0
    for track in ranked:
        picked.append(track)
        total += track['samples'] / 48000
        if total >= target:
            break
    if not picked:
        raise RadioError('사용 가능한 음원이 없습니다.')
    # Do not silently shorten a 5h broadcast when the available library is too small.
    if total < target:
        raise RadioError(f'중복 없이 사용 가능한 분량이 부족합니다: {total/3600:.2f}시간 (요청 {target/3600:.2f}시간). --hours를 줄이세요.')
    if not feasible(Counter(t['sibling_group'] for t in picked)):
        raise RadioError('선택된 곡의 sibling 비율 때문에 연속 배치를 피할 수 없습니다. seed 또는 시간을 변경하세요.')
    return picked


def sequence(tracks, rng):
    tracks = sorted(tracks, key=lambda t: t['recording_id'])
    if not tracks:
        return []
    if len(tracks) == 1:
        return tracks[:]
    # Scale each mood using this selected snapshot; save these values in manifest inputs.
    mean = [sum(t['mood'][k] for t in tracks) / len(tracks) for k in range(len(MOODS))]
    std = [max((sum((t['mood'][k]-mean[k])**2 for t in tracks) / len(tracks))**.5, 1e-8)
           for k in range(len(MOODS))]
    vectors = {t['recording_id']: [(v-mean[k])/std[k] for k,v in enumerate(t['mood'])] for t in tracks}
    def distance(a, b):
        return (sum((x-y)**2 for x,y in zip(a,b))/len(MOODS))**.5
    remaining = {t['recording_id']:t for t in tracks}
    groups = Counter(t['sibling_group'] for t in tracks)
    result, previous = [], None
    while remaining:
        candidates = []
        for rid, track in remaining.items():
            group = track['sibling_group']
            if group == previous:
                continue
            groups[group] -= 1
            possible = feasible(groups, group)
            groups[group] += 1
            if not possible:
                continue
            transition = distance(vectors[result[-1]['recording_id']], vectors[rid]) if result else 0
            candidates.append((transition, rid))
        if not candidates:
            raise RadioError('sibling adjacency를 피할 수 없습니다.')
        if not result:
            # No current mood yet: choose uniformly among feasible starting tracks.
            _, rid = rng.choice(candidates)
        else:
            nearby = sorted(candidates)[:8]
            nearest = nearby[0][0]
            # Equivalent to exp(-distance / 0.5); subtract nearest to avoid underflow.
            weights = [math.exp(-(d - nearest) / .5) for d, _ in nearby]
            _, rid = rng.choices(nearby, weights=weights, k=1)[0]
        track = remaining.pop(rid)
        result.append(track)
        previous = track['sibling_group']
        groups[previous] -= 1
    return result


def make_manifest(tracks, excluded, hours, seed, visuals: Path):
    images = sorted(p for p in (visuals/'unused').iterdir()
                    if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')) if (visuals/'unused').is_dir() else []
    if not images:
        raise RadioError('visual unused 폴더가 비어 있습니다. 이미지를 넣어 주세요.')
    rng = random.Random(seed)
    chosen = sequence(select(sorted(tracks, key=lambda t:t['recording_id']), hours*3600, rng), rng)
    image = rng.choice(images)
    offset = 0
    ordered = []
    for item in chosen:
        ordered.append({**item, 'start_sample': offset})
        offset += item['samples']
    return dict(schema_version=1, algorithm=ALGORITHM, session_id=str(uuid.uuid4()),
                created_at=datetime.now(timezone.utc).isoformat(), seed=seed,
                requested_hours=hours, duration_seconds=offset/48000,
                analyzer=ANALYZER, mood_dimensions=MOODS,
                weighting='1 / (1 + use_count)',
                visual={'filename': image.name, 'sha256': digest(image)},
                tracks=ordered, excluded=excluded,
                selection_pool=sorted(tracks, key=lambda t:t['recording_id']))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def read_manifest(path):
    m = json.loads(Path(path).read_text())
    # Older manifests already contain their final order; streaming need not resequence.
    if m.get('schema_version') != 1 or m.get('algorithm') not in (ALGORITHM, 'weighted-wave-v1'):
        raise RadioError('지원하지 않는 manifest 버전입니다.')
    uuid.UUID(m['session_id'])
    offset, ids, previous = 0, set(), None
    if not m['tracks']:
        raise RadioError('빈 session입니다.')
    for t in m['tracks']:
        if (t['recording_id'] in ids or t['sibling_group'] == previous
                or not isinstance(t['samples'], int) or t['samples'] <= 0
                or t['start_sample'] != offset):
            raise RadioError('manifest identity/순서/시간 검증 실패')
        offset += t['samples']
        ids.add(t['recording_id'])
        previous = t['sibling_group']
    if abs(offset/48000 - m['duration_seconds']) > .001:
        raise RadioError('manifest 총 재생시간 불일치')
    name = m['visual']['filename']
    if not name or Path(name).name != name:
        raise RadioError('잘못된 visual 이름입니다.')
    return m
