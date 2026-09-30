# 검증 기록

2026-09-29 MacBook에서 Supabase `ldzdrmjonvgocfypuonf`를 읽어 구현했다.
개발 snapshot, session, 출력 영상은 Git에서 제외한 `.local/`에 있다.

## 실제 DB 확인

- `music_recordings.asset_id` → `audio_assets.recording_id` → `asset_files.asset_id` → `files.id` → `file_locations.file_id`.
- recording/source audio/file/CLAP v5 분석 각각 155개. 모든 파일 URI가 `source_relative`, `sw-saved://...`.
- `asset_venture_links`에서 `ventures.stable_key=sw`로 범위를 제한한다.
- annotation: `playlist_usage` 75, `playlist_usage_superseded` 3, `qc_acceptance` 12, `curation_rejection` 1.
- 활성 playlist usage는 `history_event=used`, `use_count_increment=1`. superseded 3건은 계산하지 않는다.
- `Back On The Shelf.m4a`는 명시적 rejection이므로 제외한다.
- `generation_batch_id`는 전부 NULL. 현재는 파일명 fallback을 사용한다.
- CLAP schema_version 5, `within_track_centered_profile`에 10차원 mood가 있다.
- 분석 metadata에 과거 `automatic_playlist_decision_allowed=false` 값이 있지만, 이번 사용자의 명시적인 자동 sequencing 요청에 따라 기존 값은 변경하지 않고 읽기만 한다.
- 기존 annotation 테이블은 metadata 객체, recording FK, UUID PK로 Radio 이력을 표현할 수 있다.
- DB schema, RLS, 과거 annotation을 변경하지 않았다. 실제 broadcast를 하지 않아 신규 usage도 쓰지 않았다.

## 실제 음원 검증

- DB file identity 155개 중 MacBook에서 137개 발견, 전부 DB SHA256 일치.
- DB 파일 18개 누락. 별도 추가 다운로드 없이 정상 제외했다.
- DB에 없는 로컬 파일 2개도 제외했다.
- rejection까지 제외한 136곡, 6.0264시간이 선곡 가능했다.
- seed `20260929`: 113곡, 18196.4초(5시간 3분 16.4초).
- 중복 recording 0, sibling adjacency 0. 같은 snapshot/seed로 전체 순서가 동일하게 재현됐다.
- 모든 후보 파일에서 ffprobe duration을 읽고, 선택된 113개는 출력 전에 다시 SHA256/재생시간 검증했다.
- 실제 음원 첫 곡 `Off Ramp Morning`(158.48초) → `Side Street Home_1` 전환을 포함한 200초 FLV 생성.
- ffprobe 결과: H.264 1920×1080, AAC 48kHz stereo, container duration 200.067초.
- preview는 production annotation과 visual을 변경하지 않았다.

## 자동 테스트

`python3 -m unittest discover -s tests -v`:

- 사용 이력 합산, Radio 포함, superseded 제외, sibling identity 분리.
- URI decoding, 경로 이탈 및 symlink 이탈 차단.
- REST pagination이 서버의 작은 page 제한에서도 끝까지 진행.
- 2,000 seed에서 낮은 use count의 선택 확률이 더 높음.
- 100 seed에서 동일 seed 재현, 중복 없음, sibling adjacency 없음.
- sibling 비율이 한계에 가까운 경우의 배치 가능성 확인.
- 생성만으로 usage 없음, 부분 송출 경계, DB commit 후 응답 유실 재시도의 idempotency.
- visual 이동 재시도와 동시 송출 lock.

`tests/integration_local.py`:

- 실제 음원 + 실제 FFmpeg로 6초 실시간 로컬 출력.
- fake DB transport를 통해 실제 출력 progress 후 첫 recording만 1회 기록하는 것 확인.
- visual이 출력 확인 후에만 이동함을 확인.
- FFmpeg 출력 실패 시 visual/usage 미변경 확인.
- production Supabase와 YouTube에는 연결하지 않음.

macOS launchd 실기기 테스트:

- 임시 LaunchAgent가 도구의 셸 명령 종료 후 별도로 5초 preview를 실행했다.
- 완료 로그와 `last exit code = 0`, 실행 횟수 1을 확인했다.
- 검증 후 임시 job을 해제했다. 운영 방송/자동화는 등록하지 않았다.

## 아직 검증하지 못한 범위

- 실제 YouTube RTMPS 인증·수신, Studio 공개 방송 전환 및 시청자 재생.
- 운영용 Supabase secret key의 REST 읽기/쓰기 권한. 현재 데이터는 연결된 Supabase SQL 도구로 조회했다.
- 운영 iMac의 Keychain 접근과 5시간 실시간 CPU/네트워크 안정성.
- system sleep 방지의 장시간 실기기 검증. 구현은 macOS caffeinate를 사용한다.

SQLite는 로컬 실행 이력이며 Supabase schema 변경이 아니다. 실제 사용 기록은 기존 테이블에 append만 한다.
