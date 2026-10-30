#!/usr/bin/env python3
"""Discover, prepare and validate the numberpad release handoff."""
import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PKG = Path('sys-apps/asus-numberpad-driver')
NAME = 'asus-numberpad-driver'
UPSTREAM = 'asus-linux-drivers/asus-numberpad-driver'
TAG = re.compile(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')
EBUILD = re.compile(r'asus-numberpad-driver-((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?:-r(\d+))?\.ebuild\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
BEGIN = '<!-- numberpad-versions:start -->'
END = '<!-- numberpad-versions:end -->'

class ReleaseError(Exception):
    pass

def version(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ReleaseError(f'Unsupported release tag: {tag!r}')
    return tuple(map(int, match.groups()))

def packages(root):
    result = []
    for path in (root / PKG).glob('*.ebuild'):
        match = EBUILD.fullmatch(path.name)
        if not match:
            raise ReleaseError(f'Unsupported ebuild name: {path.name}')
        result.append((tuple(map(int, match.group(1).split('.'))), int(match.group(2) or 0), match.group(1), path))
    result.sort()
    if not 1 <= len(result) <= 2:
        raise ReleaseError('Expected one or two packaged ebuilds')
    return result

def patch(root, pv):
    # ${P} excludes Gentoo's -rN revision.
    return root / PKG / 'files' / f'{NAME}-{pv}-shutdown.patch'

def rotate(root, tag):
    target = version(tag)
    entries = packages(root)
    if target <= entries[-1][0]:
        raise ReleaseError('Release is already packaged or would downgrade')
    current = entries[-1]
    source_patch = patch(root, current[2])
    if not source_patch.is_file():
        raise ReleaseError(f'Missing current patch: {source_patch}')
    new_pv = tag[1:]
    new_ebuild = root / PKG / f'{NAME}-{new_pv}.ebuild'
    new_patch = patch(root, new_pv)
    if new_ebuild.exists() or new_patch.exists():
        raise ReleaseError('Candidate file already exists')
    readme = root / 'README.md'
    text = readme.read_text()
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ReleaseError('README version markers missing or duplicated')
    fallback_display = current[3].name.removeprefix(NAME + '-').removesuffix('.ebuild')
    table = f'{BEGIN}\n| Package | Current | Fallback |\n| --- | --- | --- |\n| sys-apps/asus-numberpad-driver | {new_pv} | {fallback_display} |\n{END}'
    before, tail = text.split(BEGIN)
    _, after = tail.split(END)
    # All preconditions are checked before changing the disposable workspace.
    old = entries[0] if len(entries) == 2 else None
    if old and not patch(root, old[2]).is_file():
        raise ReleaseError('Missing old fallback patch')
    shutil.copyfile(current[3], new_ebuild)
    shutil.copyfile(source_patch, new_patch)
    readme.write_text(before + table + after)
    if old:
        old[3].unlink()
        # Two revisions of one PV share ${P}; keep a patch still in use.
        if old[2] != current[2]:
            patch(root, old[2]).unlink()
    return current[2], old[2] if old else ''

class GitHub:
    def __init__(self, token=None):
        self.token = token
    def get(self, path):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'numberpad-overlay', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        req = urllib.request.Request('https://api.github.com/' + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ReleaseError(f'GitHub API {path}: HTTP {exc.code}') from exc
    def pages(self, path):
        for page in range(1, 101):
            sep = '&' if '?' in path else '?'
            items = self.get(f'{path}{sep}per_page=100&page={page}')
            if not isinstance(items, list):
                raise ReleaseError('Unexpected GitHub API response')
            yield from items
            if len(items) < 100:
                return
        raise ReleaseError('GitHub pagination limit exceeded')
    def commit(self, tag):
        obj = self.get(f'repos/{UPSTREAM}/git/ref/tags/{urllib.parse.quote(tag, safe="")}')['object']
        for _ in range(8):
            if obj['type'] == 'commit':
                if not SHA.fullmatch(obj['sha']):
                    break
                return obj['sha']
            if obj['type'] != 'tag':
                break
            obj = self.get(f'repos/{UPSTREAM}/git/tags/{obj["sha"]}')['object']
        raise ReleaseError(f'Cannot resolve {tag} to a commit')

def guarded(path):
    name = path.rsplit('/', 1)[-1].lower()
    return (name.startswith(('requirements', 'install', 'uninstall', 'license', 'copying', 'setup.'))
            or name in {'pyproject.toml', 'pipfile', 'pipfile.lock', 'poetry.lock', 'flake.nix', 'flake.lock', 'makefile'}
            or path.startswith('nix/'))

def imports(source):
    tree = ast.parse(source)
    return sorted({ast.dump(node, include_attributes=False) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))})

def upstream_guard(client, old_sha, new_sha):
    def tree(sha):
        commit = client.get(f'repos/{UPSTREAM}/git/commits/{sha}')
        tree_sha = commit['tree']['sha']
        if not SHA.fullmatch(tree_sha):
            raise ReleaseError('Invalid upstream tree SHA')
        data = client.get(f'repos/{UPSTREAM}/git/trees/{tree_sha}?recursive=1')
        if data.get('truncated'):
            raise ReleaseError('Upstream tree truncated')
        return {x['path']: x['sha'] for x in data['tree'] if x['type'] == 'blob'}
    old, new = tree(old_sha), tree(new_sha)
    changed = sorted(p for p in old.keys() | new.keys() if guarded(p) and old.get(p) != new.get(p))
    if changed:
        raise ReleaseError('Upstream packaging-related files changed: ' + ', '.join(changed))
    def code(sha):
        data = client.get(f'repos/{UPSTREAM}/contents/numberpad.py?ref={sha}')
        return base64.b64decode(data['content']).decode('utf-8')
    if imports(code(old_sha)) != imports(code(new_sha)):
        raise ReleaseError('numberpad.py imports changed; review dependencies manually')

def discover(client, root, repository=None):
    releases = client.pages(f'repos/{UPSTREAM}/releases')
    release = next((r for r in releases if not r['draft'] and not r['prerelease']), None)
    if not release:
        return {'outcome': 'no-release'}
    tag = release['tag_name']
    target = version(tag)
    current = packages(root)[-1]
    commit = client.commit(tag)
    branch = f'automation/numberpad-{tag}'
    if repository:
        prs = list(client.pages(f'repos/{repository}/pulls?state=open'))
        if any(p['head']['ref'].startswith('automation/numberpad-') for p in prs):
            return {'outcome': 'open-pr', 'tag': tag, 'commit': commit}
        branches = list(client.pages(f'repos/{repository}/branches'))
        leftovers = [b['name'] for b in branches if b['name'].startswith('automation/numberpad-')]
        if leftovers:
            raise ReleaseError('Automation branch exists without an open PR: ' + ', '.join(leftovers))
    if target <= current[0]:
        return {'outcome': 'no-update', 'tag': tag, 'commit': commit}
    old_sha = client.commit('v' + current[2])
    upstream_guard(client, old_sha, commit)
    return {'outcome': 'update', 'tag': tag, 'commit': commit, 'branch': branch, 'base_version': current[2]}

def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'{name}={value}\n')

def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

def verify_manifest(root, manifest, tag):
    def lines(path):
        result = {}
        for line in path.read_text().splitlines():
            parts = line.split()
            if (len(parts) != 7 or parts[0] != 'DIST' or not parts[2].isdigit()
                    or parts[3] != 'BLAKE2B' or parts[5] != 'SHA512'
                    or not re.fullmatch(r'[0-9a-f]{128}', parts[4])
                    or not re.fullmatch(r'[0-9a-f]{128}', parts[6]) or parts[1] in result):
                raise ReleaseError('Invalid Manifest line')
            result[parts[1]] = line
        return result
    old = lines(root / PKG / 'Manifest')
    new = lines(manifest)
    current = packages(root)[-1][2]
    fallback_name = f'{NAME}-{current}.tar.gz'
    candidate_name = f'{NAME}-{tag[1:]}.tar.gz'
    if set(new) != {fallback_name, candidate_name} or new[fallback_name] != old.get(fallback_name):
        raise ReleaseError('Manifest does not contain exactly the unchanged fallback and candidate')


def verify_changes(root, tag):
    changed = set(git(root, 'diff', '--name-only').splitlines())
    changed.update(git(root, 'ls-files', '--others', '--exclude-standard').splitlines())
    original = git(root, 'ls-tree', '-r', '--name-only', 'HEAD', str(PKG)).splitlines()
    ebuilds = [p for p in original if EBUILD.fullmatch(Path(p).name)]
    if not 1 <= len(ebuilds) <= 2:
        raise ReleaseError('Invalid base package versions')
    entries = []
    for p in ebuilds:
        m = EBUILD.fullmatch(Path(p).name)
        entries.append((tuple(map(int, m.group(1).split('.'))), int(m.group(2) or 0), m.group(1), p))
    entries.sort()
    allowed = {'README.md', str(PKG / 'Manifest'), str(PKG / f'{NAME}-{tag[1:]}.ebuild'), str(PKG / 'files' / f'{NAME}-{tag[1:]}-shutdown.patch')}
    if len(entries) == 2:
        allowed.add(entries[0][3])
        if entries[0][2] != entries[1][2]:
            allowed.add(str(PKG / 'files' / f'{NAME}-{entries[0][2]}-shutdown.patch'))
    if changed != allowed:
        raise ReleaseError(f'Unexpected changed files: {sorted(changed ^ allowed)}')
    subprocess.run(['git', '-C', str(root), 'diff', '--check'], check=True)

def verify_workspace(base, candidate, tag):
    # Recreate the allowed transformation using trusted code, then compare
    # every tracked file and the generated candidate files byte-for-byte.
    tracked = git(base, 'ls-files').splitlines()
    with tempfile.TemporaryDirectory() as temp:
        expected = Path(temp)
        for name in tracked:
            src = base / name
            dst = expected / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        rotate(expected, tag)
        expected_files = {str(p.relative_to(expected)) for p in expected.rglob('*') if p.is_file()}
        actual_files = set()
        for p in candidate.rglob('*'):
            if p.is_symlink():
                raise ReleaseError(f'Symlink in candidate workspace: {p}')
            if p.is_file():
                actual_files.add(str(p.relative_to(candidate)))
        if actual_files != expected_files:
            raise ReleaseError(f'Unexpected candidate files: {sorted(actual_files ^ expected_files)}')
        verify_manifest(base, candidate / PKG / 'Manifest', tag)
        for name in expected_files - {str(PKG / 'Manifest')}:
            if (candidate / name).read_bytes() != (expected / name).read_bytes():
                raise ReleaseError(f'Unexpected candidate content: {name}')

def load_handoff(handoff):
    if handoff.is_symlink() or not handoff.is_dir():
        raise ReleaseError('Invalid handoff directory')
    if {p.name for p in handoff.iterdir()} != {'metadata.json', 'Manifest'}:
        raise ReleaseError('Unexpected handoff files')
    if any(not p.is_file() or p.is_symlink() or p.stat().st_size > 65536 for p in handoff.iterdir()):
        raise ReleaseError('Invalid handoff file type or size')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseError('Duplicate handoff metadata key')
            result[key] = value
        return result
    data = json.loads((handoff / 'metadata.json').read_text(), object_pairs_hook=unique)
    if (not isinstance(data, dict) or set(data) != {'tag', 'commit', 'base'}
            or not all(isinstance(data[k], str) for k in data)
            or not SHA.fullmatch(data['commit']) or not SHA.fullmatch(data['base'])):
        raise ReleaseError('Invalid handoff metadata')
    version(data['tag'])
    return data

def recheck(client, root, handoff, repository):
    data = load_handoff(handoff)
    if git(root, 'rev-parse', 'HEAD') != data['base']:
        raise ReleaseError('Publication base is stale')
    verify_manifest(root, handoff / 'Manifest', data['tag'])
    found = discover(client, root, repository)
    if found.get('outcome') != 'update' or found.get('tag') != data['tag'] or found.get('commit') != data['commit']:
        raise ReleaseError('Release, tag commit or automation PR/branch changed before publication')
    filename = f'{NAME}-{data["tag"][1:]}.tar.gz'
    entry = next(line.split() for line in (handoff / 'Manifest').read_text().splitlines() if line.split()[1] == filename)
    url = f'https://github.com/{UPSTREAM}/archive/refs/tags/{data["tag"]}.tar.gz'
    request = urllib.request.Request(url, headers={'User-Agent': 'numberpad-overlay'})
    blake, sha512, size = hashlib.blake2b(), hashlib.sha512(), 0
    with urllib.request.urlopen(request, timeout=60) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 100 * 1024 * 1024:
                raise ReleaseError('Upstream archive exceeds size limit')
            blake.update(chunk)
            sha512.update(chunk)
    if (str(size), blake.hexdigest(), sha512.hexdigest()) != (entry[2], entry[4], entry[6]):
        raise ReleaseError('Candidate Manifest does not match upstream archive')
    if client.commit(data['tag']) != data['commit']:
        raise ReleaseError('Upstream tag moved during archive verification')

def handoff_apply(root, handoff):
    data = load_handoff(handoff)
    if git(root, 'rev-parse', 'HEAD') != data['base'] or git(root, 'status', '--porcelain'):
        raise ReleaseError('Publication base is stale or checkout is dirty')
    verify_manifest(root, handoff / 'Manifest', data['tag'])
    rotate(root, data['tag'])
    (root / PKG / 'Manifest').write_bytes((handoff / 'Manifest').read_bytes())
    output('tag', data['tag'])
    output('branch', 'automation/numberpad-' + data['tag'])
    print('Applied validated release handoff for', data['tag'])

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    watch = sub.add_parser('watch')
    watch.add_argument('--dry-run', action='store_true')
    watch.add_argument('--result', type=Path)
    prep = sub.add_parser('prepare')
    prep.add_argument('--root', type=Path, required=True)
    prep.add_argument('--tag', required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--root', type=Path, required=True)
    apply.add_argument('--handoff', type=Path, required=True)
    check = sub.add_parser('recheck')
    check.add_argument('--handoff', type=Path, required=True)
    workspace = sub.add_parser('verify-workspace')
    workspace.add_argument('--candidate', type=Path, required=True)
    workspace.add_argument('--tag', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--tag', required=True)
    args = parser.parse_args()
    if args.command == 'watch':
        result = discover(GitHub(os.environ.get('GH_TOKEN')), ROOT, os.environ.get('GITHUB_REPOSITORY'))
        print(json.dumps(result, sort_keys=True))
        output('update', str(result['outcome'] == 'update' and not args.dry_run).lower())
        if result['outcome'] == 'update':
            print('Plan:', result['tag'], 'fallback:', result['base_version'], 'branch:', result['branch'])
            output('tag', result['tag'])
            output('commit', result['commit'])
        if args.result and not args.dry_run:
            args.result.write_text(json.dumps(result))
    elif args.command == 'prepare':
        rotate(args.root, args.tag)
    elif args.command == 'apply':
        handoff_apply(args.root, args.handoff)
    elif args.command == 'recheck':
        recheck(GitHub(os.environ.get('GH_TOKEN')), ROOT, args.handoff, os.environ['GITHUB_REPOSITORY'])
    elif args.command == 'verify-workspace':
        version(args.tag)
        verify_workspace(ROOT, args.candidate, args.tag)
    else:
        version(args.tag)
        verify_changes(ROOT, args.tag)

if __name__ == '__main__':
    try:
        main()
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.CalledProcessError, SyntaxError) as exc:
        print(f'Release error: {exc}', file=sys.stderr)
        sys.exit(1)
