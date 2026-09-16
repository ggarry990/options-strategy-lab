"""Best-effort public progress; never reads or modifies portfolio files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone


class Progress:
    def __init__(self, path=None):
        self.path = Path(path or os.environ['SCAN_PROGRESS_PATH']) if path or os.environ.get('SCAN_PROGRESS_PATH') else None
        self.started = datetime.now(timezone.utc).isoformat()

    def __call__(self, stage, completed=0, total=0, ticker=None, status='running'):
        row = dict(run_id=os.environ.get('GITHUB_RUN_ID'), started_at=self.started,
            updated_at=datetime.now(timezone.utc).isoformat(), stage=stage,
            completed=completed, total=total, ticker=ticker, status=status)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(row), encoding='utf-8')
                os.replace(tmp, self.path)
            except OSError:
                print('Progress update unavailable; portfolio processing continues')


def publish(path):
    """An independent branch avoids exposing half-written portfolio state."""
    def git(*args, input=None):
        result = subprocess.run(['git', *args], input=input, text=True,
            capture_output=True, timeout=20, check=True, env=dict(os.environ,
                GIT_AUTHOR_NAME='github-actions[bot]', GIT_COMMITTER_NAME='github-actions[bot]',
                GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com'))
        return result.stdout.strip()
    content = Path(path).read_text(encoding='utf-8')
    row = json.loads(content)
    if str(row.get('run_id')) != os.environ.get('GITHUB_RUN_ID'):
        return
    parent = None
    if git('ls-remote', '--heads', 'origin', 'scan-progress'):
        git('fetch', '--depth=1', 'origin', 'scan-progress')
        parent = git('rev-parse', 'FETCH_HEAD')
    blob = git('hash-object', '-w', '--stdin', input=content)
    tree = git('mktree', input=f'100644 blob {blob}\tprogress.json\n')
    commit = git('commit-tree', tree, *(['-p', parent] if parent else []),
        input='Update scan progress\n')
    git('push', 'origin', f'{commit}:refs/heads/scan-progress')


def progress_view(progress, workflow, now):
    """Only matching run IDs may describe the current workflow's stage."""
    status = workflow.get('status')
    matching = str(workflow.get('id')) == str(progress.get('run_id')) and bool(workflow.get('id'))
    if status == 'completed':
        return f"Last automation: {workflow.get('conclusion', 'completed')}", None
    if status in ('queued', 'requested', 'waiting', 'pending'):
        return 'Automation queued — waiting for GitHub', None
    if status == 'in_progress':
        if matching and progress.get('updated_at'):
            age = (now-datetime.fromisoformat(progress['updated_at'])).total_seconds()
            suffix = ' — progress update delayed' if age > 180 else ''
            return f"Running: {progress.get('stage', 'starting')}{suffix}", progress
        return 'Automation running — preparing scan or saving results', None
    return 'Live automation status unavailable; saved results remain below', None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status')
    args = parser.parse_args()
    if args.status:
        Progress(args.publish)('Automation '+args.status, status=args.status)
    while True:
        try:
            publish(args.publish)
        except Exception:
            print('Progress publication unavailable; see workflow logs for run status', flush=True)
        if args.once:
            break
        time.sleep(60)
