# Isolated PostgreSQL tests for coding agents

Use a **disposable PostgreSQL cluster with its own data directory and Unix socket**.
This gives us real database testing without touching production. A separate
schema inside the production database is not equivalent isolation.

The alert, journal-review and Watch Levels retirement tests require
`ALERT_TEST_DATABASE_URL`; otherwise their database-backed cases are skipped.
They create/drop fixture schemas, insert/update/delete alerts and apply migrations.
Never point either test database URL at production or run production migrations
just to satisfy tests. Quotes, delivery and external services must stay mocked.

## Local prerequisites

On David's Mac:

- Python: `/Users/davidliao/git_repos/py312/venv/bin/python`
- PostgreSQL tools: `/Applications/Postgres.app/Contents/Versions/18/bin`
- Repository: `/Users/davidliao/git_repos/fifth_dragon_capital`

Verify these paths on another machine and adjust them. Run PostgreSQL as the
ordinary user, not root. Sandbox permission may be needed for PostgreSQL shared
memory and Unix sockets; request access for the disposable cluster only.

**Configuration trap:** `etrade_sync/config.py` loads the checkout's `.env` with
`override=True`. Exporting a test URL while running from the production checkout
is insufficient. Use a separate source copy with **no `.env`**, dummy broker
credentials, and both database URLs explicitly set to the disposable database.

## Reproducible run

Run this Python snippet from the repository root using the Python path above.
It snapshots HEAD plus tracked local edits and untracked Python tests, without
altering the working tree. Other untracked source files needed by a test must be
reviewed and copied explicitly; never copy `.env`, credentials or token files.
It creates a fresh UTF-8 cluster, listens only on its private Unix socket, runs
the full unittest suite, saves the log and stops the cluster even on test failure.

```python
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

python = '/Users/davidliao/git_repos/py312/venv/bin/python'
pg_bin = Path('/Applications/Postgres.app/Contents/Versions/18/bin')
repo = Path.cwd()
area = Path(tempfile.mkdtemp(prefix='fdc-db-tests-', dir='/private/tmp'))
checkout = area / 'checkout'
checkout.mkdir()
socket = area / 'socket'
socket.mkdir(mode=0o700)
data = area / 'pgdata'
port = '55442'

archive = subprocess.check_output(['git', 'archive', 'HEAD'], cwd=repo)
with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
    bundle.extractall(checkout, filter='data')
tracked = subprocess.check_output(
    ['git', 'diff', 'HEAD', '--name-only', '-z'], cwd=repo).split(b'\0')
new_tests = subprocess.check_output(
    ['git', 'ls-files', '--others', '--exclude-standard', '-z',
     '--', 'tests/*.py'], cwd=repo).split(b'\0')
for entry in set(tracked + new_tests):
    if not entry:
        continue
    name = os.fsdecode(entry)
    if '.env' in Path(name).parts or Path(name).name.startswith('.env'):
        raise RuntimeError('Refusing to copy environment files')
    source, target = repo / name, checkout / name
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    elif target.is_file():
        target.unlink()  # Preserve tracked local deletions in the test snapshot.
assert not (checkout / '.env').exists()

def pg(tool, *args):
    subprocess.run([str(pg_bin / tool), *args], check=True)

# No inherited connection options or service/credential configuration.
env = os.environ.copy()
for key in list(env):
    if key.startswith(('PG', 'ALERT_', 'ETRADE_', 'ANTHROPIC_', 'OPENAI_')) or key == 'DATABASE_URL':
        env.pop(key)
url = f'postgresql:///fdc_tests?host={socket}&port={port}'
env.update(DATABASE_URL=url, ALERT_TEST_DATABASE_URL=url,
           ETRADE_CONSUMER_KEY='dummy', ETRADE_CONSUMER_SECRET='dummy',
           PYTHONPATH=f'{checkout}:{checkout / "tests"}')

pg('initdb', '-D', str(data), '-A', 'trust', '--no-locale', '-E', 'UTF8')
try:
    pg('pg_ctl', '-D', str(data), '-l', str(area / 'server.log'),
       '-o', f'-p {port} -k {socket} -h ""', '-w', 'start')
    subprocess.run([str(pg_bin / 'createdb'), '-h', str(socket), '-p', port,
                    'fdc_tests'], env=env, check=True)
    log = area / 'tests.log'
    with log.open('w') as output:
        result = subprocess.run(
            [python, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
            cwd=checkout, env=env, stdout=output, stderr=subprocess.STDOUT)
    print(log.read_text())
    print(f'Test exit code: {result.returncode}; retained artifacts: {area}')
finally:
    pg('pg_ctl', '-D', str(data), '-m', 'fast', '-w', 'stop')
```

Trust authentication here is limited to the disposable cluster and private socket;
TCP listening is disabled by `-h ""`. Do not reuse these settings for production.
If interrupted before shutdown, stop only the printed test cluster:
`pg_ctl -D <printed-area>/pgdata -m fast -w stop` using the PostgreSQL binary above.
Retain logs until review; removing test artifacts is optional and must target only
the directory created by this run. Do not touch backups or recovery snapshots.

## Interpreting results

Check the final test summary and exit code. Report failures and skipped counts;
zero exit alone does not establish that database tests ran. Test output saying
“email delivered” refers to mocked delivery in these fixtures, not real SMTP.
Do not load launchd jobs or enable the alert poller for testing.

On October 18, 2026, the local review changes passed **228 tests with zero skips**,
including 78 database-backed tests. An outdated consolidation fixture was updated
to use an actual position-stop identity, and a database regression now verifies
that legacy watch pairs are rejected without mutation. The disposable cluster
was stopped afterward. These counts are a recorded run, not a permanent expected
suite size. Rerun against the code being reviewed.
