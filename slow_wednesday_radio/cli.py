from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import random
import shutil
import subprocess
import sys

from .catalog import RadioError, Supabase, load_catalog, secret
from .history import Journal, lock
from .planning import make_manifest, read_manifest, save_json
from .streaming import encoder_command, preflight, recover_visuals, run


def root_env(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        raise RadioError(f'{name}를 설정하세요.')
    return Path(value).expanduser().resolve()


def launch(manifest, music, visuals, state):
    if sys.platform != 'darwin':
        raise RadioError('start는 macOS launchd에서 사용합니다.')
    secret('SUPABASE_SECRET_KEY', 'slow-wednesday-radio.supabase', keychain_only=True)
    secret('YOUTUBE_STREAM_KEY', 'slow-wednesday-radio.youtube', keychain_only=True)
    m = read_manifest(manifest)
    preflight(m, music, visuals)
    label = 'studio.slowwednesday.radio.' + m['session_id']
    state.mkdir(parents=True, exist_ok=True)
    # launchd loads credentials from Keychain at runtime, never from this plist.
    env = {'PATH': os.environ['PATH'], 'SW_SAVED_DIR': str(music),
           'RADIO_VISUALS_DIR':str(visuals), 'RADIO_STATE_DIR':str(state),
           'SUPABASE_URL':os.environ.get('SUPABASE_URL','')}
    repo = Path(__file__).resolve().parent.parent
    spec = {'Label':label, 'ProgramArguments':[sys.executable,'-m','slow_wednesday_radio','stream',str(Path(manifest).resolve())],
            'WorkingDirectory':str(repo), 'EnvironmentVariables':env,
            'RunAtLoad':True, 'KeepAlive':False, 'ProcessType':'Background',
            'StandardOutPath':str(state/(label+'.log')), 'StandardErrorPath':str(state/(label+'.log'))}
    plist = state/(label+'.plist')
    if plist.exists():
        raise RadioError('이 session의 launchd 설정이 이미 있습니다. status 또는 stop을 사용하세요.')
    plist.write_bytes(plistlib.dumps(spec))
    os.chmod(plist, 0o600)
    result = subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(plist)], capture_output=True)
    if result.returncode:
        raise RadioError('launchd 등록 실패. 로그인된 macOS 사용자 세션과 Keychain 설정을 확인하세요.')
    return {'launchd_label':label, 'log':str(state/(label+'.log')),
            'note':'등록 완료. 실제 연결 여부는 status와 로그를 확인하세요.'}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='slow-wednesday-radio')
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('generate', help='DB 조회 및 session 생성 (사용 이력 변경 없음)')
    generate.add_argument('--hours',type=float,default=5)
    generate.add_argument('--seed',type=int)
    generate.add_argument('--snapshot',type=Path,help='저장된 DB 데이터로 오프라인 생성')
    generate.add_argument('--output',type=Path)
    inspect = commands.add_parser('inspect', help='파일 매칭 및 제외 사유 확인')
    inspect.add_argument('--snapshot',type=Path)
    inspect.add_argument('--save-snapshot',type=Path)
    for name in ('stream','preview','start','stop'):
        cmd = commands.add_parser(name)
        cmd.add_argument('session',type=Path)
        if name == 'stream':
            cmd.add_argument('--dry-run',action='store_true')
        if name == 'preview':
            cmd.add_argument('--output',type=Path,required=True)
            cmd.add_argument('--seconds',type=float,default=30)
            cmd.add_argument('--fast',action='store_true',help='실시간 속도 제한 해제')
    commands.add_parser('sync-history')
    commands.add_parser('status')
    args = parser.parse_args()
    try:
        state = root_env('RADIO_STATE_DIR', str(Path.home()/'Library/Application Support/SlowWednesdayRadio'))
        if args.command in ('status','sync-history'):
            j = Journal(state)
            if args.command == 'sync-history':
                with lock(state/'sync.lock'):
                    result = {'synced':j.sync(Supabase())}
            else:
                result = j.status()
                if sys.platform == 'darwin':
                    for s in result['sessions']:
                        label='studio.slowwednesday.radio.'+s['id']
                        p = subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True)
                        s['launchd_loaded'] = p.returncode == 0
                result['note']='starting/streaming이면서 updated_at이 오래되었다면 프로세스 상태를 확인하세요.'
            j.db.close()
        elif args.command == 'stop':
            m = read_manifest(args.session)
            label='studio.slowwednesday.radio.'+m['session_id']
            p = subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],capture_output=True)
            if p.returncode:
                raise RadioError('해당 launchd job을 중지하지 못했습니다. foreground 송출은 Ctrl-C로 중지하세요.')
            result={'stopped':label}
        else:
            for executable in ('ffmpeg','ffprobe'):
                if not shutil.which(executable):
                    raise RadioError(f'{executable}이 필요합니다. brew install ffmpeg')
            music = root_env('SW_SAVED_DIR')
            visuals = root_env('RADIO_VISUALS_DIR',str(Path.cwd()/'radio-visuals'))
            if args.command in ('generate','inspect'):
                data = json.loads(args.snapshot.read_text()) if args.snapshot else Supabase().snapshot()
                if (state/'history.sqlite3').exists():
                    j = Journal(state)
                    known = {a['id'] for a in data['recording_annotations']}
                    for aid, payload in j.db.execute('SELECT id,payload FROM outbox'):
                        if aid not in known:
                            data['recording_annotations'].append(json.loads(payload))
                    j.db.close()
                if args.command == 'inspect' and args.save_snapshot:
                    save_json(args.save_snapshot,data)
                tracks, excluded = load_catalog(data,music)
                if args.command == 'inspect':
                    result={'available':len(tracks),'available_hours':sum(t['samples'] for t in tracks)/48000/3600,
                            'excluded':excluded}
                else:
                    if (state/'history.sqlite3').exists():
                        with lock(state/'broadcast.lock'):
                            j=Journal(state)
                            recover_visuals(j,visuals)
                            j.db.close()
                    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**63)
                    manifest=make_manifest(tracks,excluded,args.hours,seed,visuals)
                    path=args.output or state/'sessions'/(manifest['session_id']+'.json')
                    if path.exists():
                        raise RadioError('manifest 출력 파일이 이미 존재합니다.')
                    save_json(path,manifest)
                    result={'session':str(path.resolve()),'seed':seed,'tracks':len(manifest['tracks']),
                            'duration_hours':manifest['duration_seconds']/3600,'excluded':len(excluded)}
            elif args.command == 'start':
                result=launch(args.session,music,visuals,state)
            else:
                m=read_manifest(args.session)
                if args.command=='stream' and args.dry_run:
                    _,visual=preflight(m,music,visuals)
                    result={'encoder_command':encoder_command(visual,m['duration_seconds'],
                            'rtmps://a.rtmps.youtube.com:443/live2/<YOUTUBE_STREAM_KEY>'),
                            'audio_source':'Sequential FFmpeg decoders → bounded stereo PCM pipe',
                            'tracks':len(m['tracks'])}
                else:
                    result=run(m,music,visuals,state,
                               preview=args.output if args.command=='preview' else None,
                               limit=args.seconds if args.command=='preview' else None,
                               realtime=not args.fast if args.command=='preview' else True)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    except (RadioError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # subprocess exceptions can contain destination credentials: never stringify them.
        message = str(exc) if isinstance(exc,RadioError) else '파일, 설정 또는 외부 프로세스 오류. 입력과 실행 환경을 확인하세요.'
        print('오류: '+message,file=sys.stderr,flush=True)
        sys.exit(1)
