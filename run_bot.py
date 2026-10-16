"""Windows/Linux supervisor with restart backoff, heartbeat and rotating logs."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from sqlite_store import acquire_process_lock


def restart_delay(failures):
    return min(60, 5 * 2 ** min(max(failures - 1, 0), 4))


def heartbeat_stale(started_at, modified_at, now, startup_grace=300, timeout=180):
    return now - started_at > startup_grace and now - max(started_at, modified_at) > timeout


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main():
    root = Path(__file__).resolve().parent
    (root / 'logs').mkdir(exist_ok=True)
    logger = logging.getLogger('bot-supervisor')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    output = RotatingFileHandler(root / 'logs/bot.log', maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    output.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(output)
    console = logging.StreamHandler()
    logger.addHandler(console)
    try:
        lock = acquire_process_lock(root / 'data/supervisor')
    except OSError:
        logger.error('Another supervisor is already running')
        for handler in (output, console):
            logger.removeHandler(handler)
            handler.close()
        return 1
    child = None
    failures = 0
    try:
        while True:
            started = time.time()
            environment = os.environ.copy()
            environment.update(PYTHONUNBUFFERED='1', PYTHONUTF8='1')
            child = subprocess.Popen([sys.executable, '-u', str(root / 'main.py')], cwd=root,
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace')
            def collect_output(stream):
                try:
                    for line in stream:
                        logger.info('%s', line.rstrip())
                finally:
                    stream.close()
            reader = threading.Thread(target=collect_output, args=(child.stdout,), daemon=True)
            reader.start()
            stalled = False
            while child.poll() is None:
                heartbeat = root / 'data/heartbeat'
                try:
                    modified = heartbeat.stat().st_mtime
                except FileNotFoundError:
                    modified = started
                if heartbeat_stale(started, modified, time.time()):
                    logger.error('Bot heartbeat stalled; restarting process')
                    stalled = True
                    stop_child(child)
                    break
                time.sleep(5)
            code = child.wait()
            reader.join(timeout=5)
            if code == 0 and not stalled:
                logger.info('Bot stopped normally; supervisor exiting')
                return 0
            failures = 1 if time.time() - started >= 300 else failures + 1
            delay = restart_delay(failures)
            logger.error('Bot exited (%s); restarting in %ss', code, delay)
            time.sleep(delay)
    except KeyboardInterrupt:
        logger.info('Supervisor stopped by user')
        return 0
    finally:
        if child:
            stop_child(child)
        lock.close()
        for handler in (output, console):
            logger.removeHandler(handler)
            handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
