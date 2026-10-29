import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / '.build/Diskie.app/Contents/Resources/disk-scanner'


class ScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCANNER.exists():
            raise RuntimeError('Build the app with ./scripts/build.sh first')
        cls.temp = tempfile.TemporaryDirectory(prefix='diskie-test-')
        cls.base = Path(cls.temp.name)
        cls.root = cls.base / 'root'
        cls.root.mkdir()
        # A one-worker run fills the 2,048-entry queue and exercises inline work.
        for i in range(2200):
            directory = cls.root / str(i)
            directory.mkdir()
            (directory / 'small').write_bytes(b'x' * 4096)
        large = cls.root / 'large'
        large.mkdir()
        block = os.urandom(1024 * 1024)
        with (large / 'weights.bin').open('wb') as stream:
            for _ in range(101):
                stream.write(block)
        os.link(large / 'weights.bin', cls.root / '2199/duplicate.bin')
        (cls.root / 'quote" backslash\\ café').write_bytes(b'example')
        with (cls.root / 'sparse').open('wb') as stream:
            stream.truncate(1024 ** 3)
        outside = cls.base / 'outside'
        outside.mkdir()
        (outside / 'excluded').write_bytes(b'excluded')
        (cls.root / 'symlink').symlink_to(outside, target_is_directory=True)
        os.mkfifo(cls.root / 'fifo')
        cls.expected_bytes = 0
        cls.expected_files = 0
        seen = set()
        for directory, _, names in os.walk(cls.root, followlinks=False):
            for name in names:
                st = (Path(directory) / name).lstat()
                if not stat.S_ISREG(st.st_mode):
                    continue
                cls.expected_files += 1
                identity = (st.st_dev, st.st_ino)
                if identity not in seen:
                    cls.expected_bytes += st.st_blocks * 512
                    seen.add(identity)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def scan(self, workers, posix=False, root=None):
        output = self.base / f'result-{workers}-{posix}.json'
        command = [str(SCANNER), str(output), str(root or self.root), str(workers)]
        if posix:
            command.append('posix')
        subprocess.run(command, check=True, timeout=30)
        result = json.loads(output.read_text())
        self.assertFalse(Path(str(output) + '.progress').exists())
        self.assertFalse(Path(str(output) + '.progress.new').exists())
        return result

    def check_accounting(self, result):
        self.assertEqual(result['tree']['size'], self.expected_bytes)
        self.assertEqual(result['files'], self.expected_files)
        self.assertEqual(result['directories'], 2202)
        self.assertEqual(result['errors']['unreadable'], 0)
        self.assertGreater(result['total'], 0)
        large_files = []

        def check(node):
            children = node.get('children', [])
            self.assertLessEqual(sum(c['size'] for c in children), node['size'])
            for child in children:
                self.assertGreaterEqual(child['size'], 100 * 1024 ** 2)
                if child.get('file'):
                    large_files.append(child)
                self.assertNotEqual(child['name'], 'symlink')
                check(child)

        check(result['tree'])
        self.assertEqual(len(large_files), 1)
        self.assertIn(large_files[0]['name'], ('weights.bin', 'duplicate.bin'))

    def test_single_worker_and_bounded_queue(self):
        result = self.scan(1)
        self.check_accounting(result)
        self.assertEqual(result['fallbackCalls'], 0)

    def test_parallel_workers(self):
        result = self.scan(6)
        self.check_accounting(result)
        self.assertEqual(result['workers'], 6)

    def test_posix_fallback(self):
        result = self.scan(6, posix=True)
        self.check_accounting(result)
        self.assertGreater(result['fallbackCalls'], 0)

    @unittest.skipIf(os.geteuid() == 0, 'Root bypasses directory permissions')
    def test_unreadable_directory(self):
        root = self.base / 'permission-root'
        root.mkdir()
        denied = root / 'denied'
        denied.mkdir()
        (denied / 'hidden').write_bytes(b'x')
        denied.chmod(0)
        try:
            result = self.scan(2, root=root)
            self.assertEqual(result['errors']['unreadable'], 1)
            self.assertEqual(result['files'], 0)
        finally:
            denied.chmod(0o700)

    def test_rejects_invalid_worker_count(self):
        for count in ('0', '17', '-1'):
            result = subprocess.run([str(SCANNER), str(self.base / 'invalid.json'), str(self.root), count], timeout=10)
            self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
