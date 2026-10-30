import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release

class FakeGitHub:
    def __init__(self, releases=(), prs=(), branches=()):
        self.releases, self.prs, self.branches = releases, prs, branches
    def pages(self, path):
        if '/releases' in path:
            return iter(self.releases)
        if '/pulls' in path:
            return iter(self.prs)
        return iter(self.branches)
    def commit(self, tag):
        return 'a' * 40

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = self.root / release.PKG
        (self.pkg / 'files').mkdir(parents=True)
        (self.root / 'README.md').write_text(f'{release.BEGIN}\nold\n{release.END}\n')
        self.add_version('7.3.0')
    def add_version(self, v, revision=''):
        (self.pkg / f'{release.NAME}-{v}{revision}.ebuild').write_text('packaging\n')
        release.patch(self.root, v).write_text('patch ' + v)
    def release(self, tag, **flags):
        return {'tag_name': tag, 'draft': flags.get('draft', False), 'prerelease': flags.get('prerelease', False)}
    def test_none_and_prerelease(self):
        self.assertEqual(release.discover(FakeGitHub([self.release('v8.0.0', prerelease=True)]), self.root)['outcome'], 'no-release')
        self.assertEqual(release.discover(FakeGitHub([]), self.root)['outcome'], 'no-release')
    def test_unsupported(self):
        with self.assertRaises(release.ReleaseError):
            release.discover(FakeGitHub([self.release('v8.0-rc1')]), self.root)
    def test_packaged_and_downgrade(self):
        for tag in ['v7.3.0', 'v7.2.0']:
            self.assertEqual(release.discover(FakeGitHub([self.release(tag)]), self.root)['outcome'], 'no-update')
    def test_first_rotation(self):
        self.assertEqual(release.rotate(self.root, 'v7.4.0'), ('7.3.0', ''))
        self.assertTrue((self.pkg / f'{release.NAME}-7.3.0.ebuild').exists())
        self.assertEqual(release.patch(self.root, '7.3.0').read_text(), release.patch(self.root, '7.4.0').read_text())
        self.assertIn('| 7.4.0 | 7.3.0 |', (self.root / 'README.md').read_text())
    def test_subsequent_rotation_and_revision(self):
        self.add_version('7.4.0', '-r2')
        self.assertEqual(release.rotate(self.root, 'v7.5.0'), ('7.4.0', '7.3.0'))
        self.assertFalse(release.patch(self.root, '7.3.0').exists())
        self.assertFalse((self.pkg / f'{release.NAME}-7.3.0.ebuild').exists())
        self.assertTrue((self.pkg / f'{release.NAME}-7.4.0-r2.ebuild').exists())
        self.assertEqual(release.patch(self.root, '7.4.0').read_text(), release.patch(self.root, '7.5.0').read_text())
        self.assertIn('| 7.5.0 | 7.4.0-r2 |', (self.root / 'README.md').read_text())
    def test_shared_patch_for_two_revisions_is_preserved(self):
        self.add_version('7.3.0', '-r2')
        self.assertEqual(release.rotate(self.root, 'v7.4.0'), ('7.3.0', '7.3.0'))
        self.assertFalse((self.pkg / f'{release.NAME}-7.3.0.ebuild').exists())
        self.assertTrue((self.pkg / f'{release.NAME}-7.3.0-r2.ebuild').exists())
        self.assertTrue(release.patch(self.root, '7.3.0').exists())
        self.assertIn('| 7.4.0 | 7.3.0-r2 |', (self.root / 'README.md').read_text())
    def test_open_pr_and_leftover_branch(self):
        rel = [self.release('v7.4.0')]
        pr = {'head': {'ref': 'automation/numberpad-v7.4.0'}}
        self.assertEqual(release.discover(FakeGitHub(rel, [pr]), self.root, 'owner/repo')['outcome'], 'open-pr')
        with self.assertRaisesRegex(release.ReleaseError, 'without an open PR'):
            release.discover(FakeGitHub(rel, [], [{'name': 'automation/numberpad-v7.4.0'}]), self.root, 'owner/repo')
    def test_dry_run_does_not_write(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(release, 'ROOT', self.root), patch.object(release, 'GitHub', return_value=FakeGitHub([self.release('v7.3.0')])), patch.object(sys, 'argv', ['release.py', 'watch', '--dry-run', '--result', str(self.root / 'result')]), patch.dict(os.environ, {'GITHUB_REPOSITORY': ''}):
            release.main()
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
    def test_handoff_rejects_extra_and_stale(self):
        hand = self.root / 'handoff'
        hand.mkdir()
        (hand / 'metadata.json').write_text(json.dumps({'tag':'v7.4.0', 'base':'b'*40, 'commit':'a'*40}))
        (hand / 'Manifest').write_text('')
        (hand / 'extra').write_text('bad')
        with self.assertRaisesRegex(release.ReleaseError, 'Unexpected handoff'):
            release.handoff_apply(self.root, hand)
        (hand / 'extra').unlink()
        with patch.object(release, 'git', return_value='c'*40), self.assertRaisesRegex(release.ReleaseError, 'stale'):
            release.handoff_apply(self.root, hand)
    def test_candidate_allowlist_and_manifest(self):
        digest = 'a' * 128
        fallback = f'DIST {release.NAME}-7.3.0.tar.gz 1 BLAKE2B {digest} SHA512 {digest}\n'
        candidate = f'DIST {release.NAME}-7.4.0.tar.gz 2 BLAKE2B {digest} SHA512 {digest}\n'
        (self.pkg / 'Manifest').write_text(fallback)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'overlay'
            shutil.copytree(self.root, target)
            release.rotate(target, 'v7.4.0')
            (target / release.PKG / 'Manifest').write_text(fallback + candidate)
            tracked = '\n'.join(str(p.relative_to(self.root)) for p in self.root.rglob('*') if p.is_file())
            with patch.object(release, 'git', return_value=tracked):
                release.verify_workspace(self.root, target, 'v7.4.0')
                (target / 'extra').write_text('unexpected')
                with self.assertRaisesRegex(release.ReleaseError, 'Unexpected candidate files'):
                    release.verify_workspace(self.root, target, 'v7.4.0')
                (target / 'extra').unlink()
                (target / release.PKG / 'Manifest').write_text(candidate)
                with self.assertRaisesRegex(release.ReleaseError, 'Manifest'):
                    release.verify_workspace(self.root, target, 'v7.4.0')
    def test_update_dry_run_does_not_write(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(release, 'ROOT', self.root), patch.object(release, 'GitHub', return_value=FakeGitHub([self.release('v7.4.0')])), patch.object(release, 'upstream_guard'), patch.object(sys, 'argv', ['release.py', 'watch', '--dry-run', '--result', str(self.root / 'result')]), patch.dict(os.environ, {'GITHUB_REPOSITORY': ''}):
            release.main()
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
    def test_dependency_and_install_guard(self):
        for name in ['requirements.txt', 'nix/default.nix', 'LICENSE.md', 'install.sh', 'pyproject.toml']:
            self.assertTrue(release.guarded(name))
        self.assertNotEqual(release.imports('import os'), release.imports('import os\nimport numpy'))

    def test_validation_failure_blocks_publish(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
        self.assertIn("needs.prepare.outputs.ready == 'true'", workflow)
        self.assertIn("if: steps.watch.outputs.update == 'true'", workflow)
        self.assertIn('pkgcheck scan --exit=error,warning', workflow)
        self.assertLess(workflow.index('Validate candidate in Gentoo'), workflow.index('id: ready'))

if __name__ == '__main__':
    unittest.main()
