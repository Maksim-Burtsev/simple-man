from pathlib import Path
import subprocess
import sys
import threading


process = subprocess.Popen(
    (sys.executable, "runtime.py"),
    cwd=Path(__file__).resolve().parent,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
assert process.stdin is not None
assert process.stdout is not None
assert process.stderr is not None
process.stdin.write(sys.stdin.buffer.read())
process.stdin.close()


def relay(source, target):
    while chunk := source.read(64 * 1024):
        target.write(chunk)
        target.flush()


threads = (
    threading.Thread(target=relay, args=(process.stdout, sys.stdout.buffer)),
    threading.Thread(target=relay, args=(process.stderr, sys.stderr.buffer)),
)
for thread in threads:
    thread.start()
returncode = process.wait()
for thread in threads:
    thread.join()
raise SystemExit(returncode)
