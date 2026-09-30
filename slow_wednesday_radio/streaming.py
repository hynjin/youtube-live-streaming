from __future__ import annotations

import os
import math
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

from .catalog import RadioError, digest, probe, resolve_uri, secret, Supabase
from .history import Journal, lock

RATE = 48000
BYTES_PER_FRAME = 4  # stereo signed 16-bit PCM


def child_env():
    return {k:v for k,v in os.environ.items()
            if k not in ('FFREPORT', 'YOUTUBE_STREAM_KEY', 'SUPABASE_SECRET_KEY')}


def preflight(manifest, music_root, visual_root):
    paths = []
    for t in manifest['tracks']:
        path = resolve_uri(t['uri'], music_root)
        if not path.is_file():
            raise RadioError(f'선택된 음원 파일이 없습니다: {path.name}')
        if digest(path) != t['sha256']:
            raise RadioError(f'선택된 음원이 변경되었습니다: {path.name}')
        duration = probe(path)
        if abs(duration - t['samples']/RATE) > .05:
            raise RadioError(f'음원 재생시간이 변경되었습니다: {path.name}')
        paths.append(path)
    visual = visual_root/'unused'/manifest['visual']['filename']
    if not visual.is_file():
        raise RadioError('session의 unused visual이 없습니다. 새 session을 생성하세요.')
    if digest(visual) != manifest['visual']['sha256']:
        raise RadioError('session visual이 변경되었습니다.')
    if (visual_root/'used'/visual.name).exists():
        raise RadioError('같은 이름의 used visual이 있습니다. 새 visual 이름을 사용하세요.')
    check = subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(visual),
                            '-frames:v','1','-f','null','-'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, env=child_env())
    if check.returncode:
        raise RadioError('visual 이미지를 디코딩할 수 없습니다.')
    return paths, visual


def consume_visual(manifest, root):
    source = root/'unused'/manifest['visual']['filename']
    dest = root/'used'/source.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if digest(dest) != manifest['visual']['sha256']:
            raise RadioError('used visual 이름 충돌')
        if source.exists() and digest(source) == manifest['visual']['sha256']:
            source.unlink()
        return
    if not source.is_file() or digest(source) != manifest['visual']['sha256']:
        raise RadioError('송출된 visual 원본을 확인할 수 없습니다.')
    # Atomic no-clobber move, recoverable if interrupted between link and unlink.
    os.link(source, dest)
    source.unlink()


def recover_visuals(journal, root):
    import json
    for (raw,) in journal.db.execute('SELECT manifest FROM sessions WHERE progress>2'):
        consume_visual(json.loads(raw), root)


def encoder_command(visual, seconds, destination, *, realtime=True):
    command = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-nostats',
               '-loop','1','-framerate','30','-i',str(visual)]
    if realtime:
        command += ['-re']
    command += ['-f','s16le','-ar',str(RATE),'-ac','2','-i','pipe:0',
        '-map','0:v:0','-map','1:a:0',
        '-vf','scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p',
        '-c:v','libx264','-preset','veryfast','-tune','stillimage','-threads','2',
        '-b:v','6000k','-minrate','6000k','-maxrate','6000k','-bufsize','12000k',
        '-g','60','-keyint_min','60','-sc_threshold','0',
        '-c:a','aac','-b:a','128k','-ar',str(RATE),'-ac','2',
        '-t',f'{seconds:.6f}','-shortest','-max_interleave_delta','1000000',
        '-flush_packets','1','-progress','pipe:1','-stats_period','0.5']
    if destination.startswith('rtmps://'):
        command += ['-rw_timeout','15000000']
    return command + ['-f','flv','-n',destination]


def decoder_command(path, samples):
    seconds = samples/RATE
    fade = min(.015, seconds/4)
    filters = (f'aresample={RATE},apad,atrim=end_sample={samples},'
               f'afade=t=in:st=0:d={fade},afade=t=out:st={seconds-fade}:d={fade}')
    return ['ffmpeg','-v','error','-nostdin','-i',str(path),'-map','0:a:0','-vn',
            '-af',filters,'-ar',str(RATE),'-ac','2','-c:a','pcm_s16le','-f','s16le','pipe:1']


def run(manifest, music_root, visual_root, state_root, *, preview=None, limit=None, realtime=True):
    live = preview is None
    with lock(state_root/'broadcast.lock'):
        journal = Journal(state_root) if live else None
        if journal:
            recover_visuals(journal, visual_root)
        paths, visual = preflight(manifest, music_root, visual_root)
        if live:
            key = secret('YOUTUBE_STREAM_KEY', 'slow-wednesday-radio.youtube')
            if not key.strip() or any(c.isspace() for c in key) or '/' in key or '?' in key:
                raise RadioError('YouTube stream key 형식이 잘못되었습니다.')
            destination = 'rtmps://a.rtmps.youtube.com:443/live2/' + key
            client = Supabase()
            # Read access is checked before broadcast. Insert is tested only on real usage.
            client.request('recording_annotations', {'select':'id','limit':1})
            if client.request('recording_annotations', {'select':'id','limit':1,
                    'metadata->>radio_session_id':'eq.'+manifest['session_id']}):
                raise RadioError('이 session의 송출 이력이 이미 DB에 있습니다. 새 session을 생성하세요.')
        else:
            destination = str(Path(preview).resolve())
            if Path(destination).exists():
                raise RadioError('preview 출력 파일이 이미 존재합니다.')
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
        duration = min(manifest['duration_seconds'], limit) if limit is not None else manifest['duration_seconds']
        if not math.isfinite(duration) or duration <= 0 or (limit is not None and not math.isfinite(limit)):
            raise RadioError('preview 시간은 0보다 커야 합니다.')
        command = encoder_command(visual, duration, destination, realtime=realtime)
        if journal:
            journal.begin(manifest)
        stopped = threading.Event()
        events = queue.Queue()
        produced = [0]
        producer_failure = []
        progress = 0.0
        moved = False
        process = None
        caffeine = None
        decoder = [None]
        feeder = None
        sync_thread = None
        old_signals = {}
        status = 'failed'
        def stop(signum=None, frame=None):
            stopped.set()
            if process and process.poll() is None:
                process.terminate()
            if decoder[0] and decoder[0].poll() is None:
                decoder[0].terminate()
        def feed():
            try:
                for track, path in zip(manifest['tracks'], paths):
                    if stopped.is_set():
                        break
                    d = subprocess.Popen(decoder_command(path, track['samples']), stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=child_env())
                    decoder[0] = d
                    count = 0
                    try:
                        while not stopped.is_set():
                            chunk = d.stdout.read(65536)
                            if not chunk:
                                break
                            process.stdin.write(chunk)
                            process.stdin.flush()
                            count += len(chunk)
                            produced[0] += len(chunk)
                        rc = d.wait(timeout=10)
                        if not stopped.is_set() and (rc or count != track['samples'] * BYTES_PER_FRAME):
                            raise RadioError('음원 디코딩 중 오류가 발생했습니다.')
                    finally:
                        if d.poll() is None:
                            d.kill()
                            d.wait()
                        d.stdout.close()
                        decoder[0] = None
            except BrokenPipeError:
                pass  # Expected at a preview cutoff; exit code/progress decide success.
            except OSError:
                producer_failure.append('PCM 전달 실패')
                stop()
            except Exception:
                producer_failure.append('음원 디코딩 실패')
                stop()
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        def read_progress():
            for line in process.stdout:
                events.put(line.decode('ascii', 'replace').strip())
            events.put(None)
        def sync_worker():
            local = Journal(state_root)
            try:
                while not stopped.is_set():
                    try:
                        local.sync(client)
                    except RadioError:
                        pass  # Durable pending outbox; retry without interrupting audio.
                    stopped.wait(15)
            finally:
                local.db.close()
        try:
            if live and os.uname().sysname == 'Darwin':
                caffeine = subprocess.Popen(['caffeinate','-i','-w',str(os.getpid())],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=child_env())
            # Never log command, stderr, or exceptions containing a Popen command.
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_signals[sig] = signal.signal(sig, stop)
            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            threading.Thread(target=read_progress, daemon=True).start()
            if live:
                sync_thread = threading.Thread(target=sync_worker, daemon=True)
                sync_thread.start()
            last_advance = time.monotonic()
            while True:
                if time.monotonic()-last_advance > 60:
                    raise RadioError('FFmpeg 출력이 60초 동안 진행되지 않아 송출을 중단했습니다.')
                try:
                    line = events.get(timeout=1)
                except queue.Empty:
                    if time.monotonic()-last_advance > 60:
                        raise RadioError('FFmpeg 출력이 60초 동안 진행되지 않아 송출을 중단했습니다.')
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if line.startswith('out_time_us='):
                    try:
                        seconds = min(float(line.split('=',1)[1])/1e6,
                                      produced[0]/(RATE*BYTES_PER_FRAME), duration)
                    except ValueError:
                        continue
                    if seconds > progress:
                        progress = seconds
                        last_advance = time.monotonic()
                        if journal:
                            journal.progress(manifest, progress)
                            if progress > 2 and not moved:
                                consume_visual(manifest, visual_root)
                                moved = True
            rc = process.wait(timeout=15)
            if producer_failure:
                raise RadioError(producer_failure[0])
            if stopped.is_set():
                status = 'stopped'
            elif rc != 0 or progress < duration - 1:
                raise RadioError('FFmpeg 송출 실패. 연결·stream key·encoder 설정을 확인하세요. 비밀 보호를 위해 원본 stderr는 저장하지 않습니다.')
            else:
                status = 'completed'
            return {'status':status, 'output_seconds':round(progress,3)}
        finally:
            stop()
            if process:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if feeder:
                feeder.join(timeout=5)
            for sig, handler in old_signals.items():
                signal.signal(sig, handler)
            if caffeine:
                caffeine.terminate()
                caffeine.wait()
            if journal:
                journal.finish(manifest['session_id'], status)
                if sync_thread:
                    sync_thread.join(timeout=17)
                try:
                    journal.sync(client)
                except RadioError:
                    print('사용 이력은 로컬 outbox에 보존되었습니다. sync-history를 실행하세요.', flush=True)
                journal.db.close()
