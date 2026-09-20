"""Build the console for release archives; installed wheels need no Node runtime."""
from pathlib import Path
import shutil
import subprocess

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        root = Path(self.root)
        web = root / 'src/eventmem/web'
        # A published sdist already contains the release assets.
        if (root / '.git').exists() or not (web / 'index.html').exists():
            npm = shutil.which('npm')
            if not npm:
                raise RuntimeError('Building from Git requires Node.js/npm for the console.')
            console = root / 'console'
            if not (console / 'node_modules').exists():
                subprocess.run([npm, 'ci'], cwd=console, check=True)
            subprocess.run([npm, 'run', 'build'], cwd=console, check=True)
        build_data.setdefault('artifacts', []).append('/src/eventmem/web/**')
