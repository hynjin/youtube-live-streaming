# Slow Wednesday Radio

Python 3.11+와 FFmpeg로 실행하는 로컬 YouTube Live Radio. 실행 중 AI/LLM 호출이 없다.
원본 음원을 복사하지 않고 순차 디코딩하여 하나의 RTMPS 송출 프로세스에 전달한다.

## 설치와 설정

```sh
brew install python ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -e .
source .venv/bin/activate
```

설치 없이 `python3 -m slow_wednesday_radio ...`로도 모든 명령을 실행할 수 있다.

```sh
export SW_SAVED_DIR="/your/music/library"
export RADIO_VISUALS_DIR="/your/radio-visuals"
export SUPABASE_URL="https://ldzdrmjonvgocfypuonf.supabase.co"
# 선택: 기본값은 ~/Library/Application Support/SlowWednesdayRadio
export RADIO_STATE_DIR="/your/private/radio-state"
mkdir -p "$RADIO_VISUALS_DIR/unused" "$RADIO_VISUALS_DIR/used"
```

직접 만든 PNG/JPEG/WebP를 `unused`에 넣는다. 테스트 카드는 `.local/test-visuals`에만 있다.
운영용 이미지는 준비하지 않았다. `used`에 같은 파일명이 있으면 새 이름을 사용한다.

### 자격 증명

이 프로그램은 개인 iMac 서버용이다. Supabase **secret key**(또는 legacy service role key)가 필요하다.
현재 DB에는 사용자용 RLS policy가 없으므로 publishable key로 운영하지 않는다.
키는 저장소나 manifest에 넣지 않는다.

foreground 명령은 `SUPABASE_SECRET_KEY`, `YOUTUBE_STREAM_KEY` 환경변수를 읽는다.
쉘 기록에 키를 남기지 않으려면 숨김 입력을 사용한다(zsh):

```sh
read -rs 'SUPABASE_SECRET_KEY?Supabase secret key: '; export SUPABASE_SECRET_KEY
read -rs 'YOUTUBE_STREAM_KEY?YouTube stream key: '; export YOUTUBE_STREAM_KEY
```

macOS `start`는 **Keychain**을 사용한다. Keychain Access에서 일반 암호 항목을 만든다.
두 항목 모두 계정은 `slow-wednesday-radio`, 서비스/항목 이름은 각각 다음과 같다.

- `slow-wednesday-radio.supabase`: Supabase secret key
- `slow-wednesday-radio.youtube`: YouTube stream key

foreground에서 먼저 접근을 확인해 Keychain 접근 요청을 처리한다. `start`의 plist에는 키를 쓰지 않는다.
launchd는 터미널의 secret 환경변수를 전달받지 않으므로 Keychain 설정이 필수다.
FFmpeg의 RTMPS URL은 실행 중 프로세스 인자에 존재한다. 프로그램은 명령·원본 stderr·키를 로그에 저장하지 않고 `FFREPORT`도 전달하지 않는다.

## 사용

```sh
# 실제 DB 읽기, 파일 SHA256/ffprobe 검증, 제외 이유 출력
slow-wednesday-radio inspect --save-snapshot "$RADIO_STATE_DIR/catalog.json"

# 생성만으로 사용 이력 증가나 visual 이동 없음
slow-wednesday-radio generate --hours 5 --seed 42

# 반환된 session JSON 경로 사용. key 없이 가능
slow-wednesday-radio stream /path/to/session.json --dry-run
slow-wednesday-radio preview /path/to/session.json --seconds 30 --output /tmp/radio-preview.flv

# foreground 실제 YouTube 송출
slow-wednesday-radio stream /path/to/session.json

# 운영 권장: launchd가 독립적으로 실행. key는 Keychain에서 읽음
slow-wednesday-radio start /path/to/session.json
slow-wednesday-radio status
slow-wednesday-radio stop /path/to/session.json

# DB 장애 중 누적된 이력 재전송
slow-wednesday-radio sync-history
```

`preview --fast`는 실시간 제한 없이 테스트 파일을 만든다. preview는 visual과 사용 이력을 변경하지 않는다.
`generate --snapshot /path/to/catalog.json`은 저장된 DB snapshot으로 오프라인 개발/테스트할 때 쓴다.
운영에서는 최신 DB를 조회하는 기본 모드를 권장한다. `--hours`가 가용 분량보다 길면 오류를 내며,
자동 반복하거나 몰래 짧은 방송을 만들지 않는다. 마지막 곡은 끝까지 사용하므로 목표보다 최대 한 곡 길게 생성된다.

### iMac 운영

- 코드와 session을 옮겨도 `sw-saved://` URI는 iMac의 `SW_SAVED_DIR`로 다시 resolve된다.
- 동일 내용의 원본 library와 선택한 visual을 준비한다. 송출 전 선택된 **모든 파일**을 다시 검증한다.
- iMac에 Python/FFmpeg, 위 경로 환경변수, Keychain 두 항목을 설정한다.
- `start`는 로그인된 사용자의 LaunchAgent로 실행한다. 터미널·SSH 연결 종료와 독립적이다.
- `caffeinate -i -w`가 실행 중 system idle sleep을 막는다. 디스플레이는 꺼져도 된다.
- 사용자 로그아웃·재부팅·수동 강제 sleep 뒤 자동 재개는 하지 않는다. 자동 재개로 곡을 중복 송출하지 않기 위한 첫 버전의 운영 방식이다.
- launchd `KeepAlive=false`: 네트워크나 FFmpeg 실패 후 자동 반복 방송하지 않는다. 로그와 `status`를 확인하고 새 session을 만든다.
- 같은 `RADIO_STATE_DIR`를 계속 사용한다. SQLite 파일/WAL은 진행 증거와 미전송 이력이다. 백업할 때 실행을 중지한 후 state 디렉터리 전체를 보존한다.
- `status`의 과거 `streaming` 기록은 SIGKILL/전원 손실 후 남을 수 있다. `updated_at`과 launchd 로그를 함께 확인한다.

## 선곡과 sequencing

1. `sw` venture에 연결된 recording을 DB identity로 조회한다. source file, SHA256, ffprobe, CLAP v5를 검증한다.
2. `curation_rejection`, `usable_for_curation=false`, rejected/unavailable 상태를 제외한다. 승인된 QC finding을 재검증하거나 다시 reject하지 않는다.
3. `playlist_usage` + `history_event=used`의 `use_count_increment`를 합산한다. superseded는 제외한다.
   Radio도 `playlist_usage`를 새로 추가하며 `usage_kind=radio_live`, `radio_session_id`, `venture_key=sw`로 구분한다.
   아직 DB에 전송하지 못한 로컬 이력도 ID로 중복 제거해 합산한다.
4. **가중치 `w=1/(1+use_count)`**. `-ln(U)/w` 오름차순으로 중복 없이 뽑고 목표 시간을 채운다.
5. sibling은 `generation_batch_id` 우선. 없으면 확장자와 마지막 `_<숫자>`를 제거한 이름을 그룹 키로 사용한다.
   recording ID, 제목, 파일명, 사용 횟수는 합치지 않는다. DB batch가 없는 파일명 fallback은 추정 관계다.
6. `clap-relative-mood-v5.metrics.within_track_centered_profile`의 10개 mood를 사용한다.
   선택된 집합에서 각 차원을 표준화한 RMS Euclidean 거리로 계산한다.
   첫 곡은 배치 가능한 곡 중 seed 기반 균등 선택한다.
   이후 sibling 금지와 남은 곡의 배치 가능성을 만족하는 후보 중 현재 곡과 가까운 최대 8곡을 추린다.
   각 후보에 `exp(-거리 / 0.5)` 가중치를 주고 seed 기반으로 하나를 선택한다.
   거리가 0.5 더 멀면 상대 가중치는 약 0.368배다. 같은 거리에서는 recording ID로 정렬해 재현성을 유지한다.
   별도의 mood trajectory나 cluster/repetition penalty는 없다. 가까운 후보들 사이에서 선택을 분산하지만,
   특정 cluster의 최대 체류 시간을 보장하지는 않는다. 후반에 가까운 곡이 소진되면 전환 거리가 커질 수 있다.
7. seed, 알고리즘 버전, 후보 snapshot, CLAP 값, 사용 횟수, 최종 순서를 manifest에 기록한다.
   같은 후보 snapshot과 seed로 동일한 선택·순서를 얻는다. 최신 DB로 같은 seed만 넣으면 이력 변화 때문에 달라질 수 있다.
   새 manifest의 알고리즘은 `weighted-neighbors-v2`이다. 기존 manifest도 저장된 순서 그대로 재생할 수 있다.

## 송출과 사용 이력의 기준

음원은 stereo 48kHz PCM으로 한 곡씩 메모리 파이프에 전달한다. sample 수로 경계를 고정하고
15ms de-click fade를 적용한다. 곡 간 crossfade나 사전 5시간 렌더링은 없다.
1080p30 H.264 6Mbps, AAC 128kbps, 2초 GOP로 RTMPS 송출한다.

사용의 기준은 **FFmpeg mux 출력 시간이 곡 시작을 2초 초과한 것**이다. 도달하지 않은 곡은 기록하지 않는다.
부분 송출도 1회 사용이며, 2초 이내로 끊긴 곡은 보수적으로 세지 않는다.
이는 YouTube ingest로 encoder가 출력한 증거이며, 시청자의 실제 재생이나 공개 방송 전환 확인은 아니다.
YouTube Studio에서 방송 생성/자동 시작 설정과 수신 상태를 직접 확인해야 한다.

첫 출력이 확인되면 visual을 `used`로 이동한다. generate/preview/연결 실패 시에는 이동하지 않는다.
출력 증거와 annotation payload를 SQLite에 먼저 commit하고 별도 작업으로 Supabase에 추가한다.
DB 연결이 실패해도 방송은 유지되고 outbox가 남는다. annotation ID는 session ID + recording ID의 UUIDv5라서
응답 유실 후 재시도해도 기존 기록을 덮어쓰거나 중복 증가시키지 않는다.
프로세스 강제 종료 직전의 마지막 progress 간격은 기록하지 못할 수 있다.

## 개발 검증

```sh
python3 -m unittest discover -s tests -v
# MacBook 실제 음원 및 .local/catalog.json이 있을 때
python3 tests/integration_local.py
```

검증 범위와 관찰한 DB 구조: [docs/verification.md](docs/verification.md).
구현 참고: 기존 playlist-video-generator의 ffprobe, PCM 연결 및 de-click 처리 방식.
직접 import하지 않아 iMac에 기존 프로젝트가 없어도 실행된다.

공식 문서: [Supabase REST](https://supabase.com/docs/guides/api),
[API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[FFmpeg](https://ffmpeg.org/ffmpeg.html),
[YouTube encoder 설정](https://support.google.com/youtube/answer/2853702).
