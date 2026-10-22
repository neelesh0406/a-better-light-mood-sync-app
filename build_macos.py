"""Build Luma.app with an AppKit/WebKit window and its local controller."""
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parent
app = root / 'Luma.app'
contents = app / 'Contents'
resources = contents / 'Resources'
controller = resources / 'Controller'
binary = contents / 'MacOS/Luma'
binary.parent.mkdir(parents=True, exist_ok=True)
controller.mkdir(parents=True, exist_ok=True)
subprocess.run(['/usr/bin/python3', str(root / 'build_capture.py')], check=True)
for name in ('app.py', 'wiz.py', 'live_sync.py'):
    shutil.copy2(root / name, controller / name)
shutil.copytree(root / 'web', controller / 'web', dirs_exist_ok=True)
shutil.copytree(root / 'Luma Capture.app', controller / 'Luma Capture.app', dirs_exist_ok=True)
state = root / '.luma.json'
if state.exists():
    # Preserves existing lights, moods and routines on the first native launch.
    json.loads(state.read_text())
    shutil.copy2(state, resources / 'InitialState.json')
metadata = {
    'CFBundleIdentifier': 'local.luma.desktop', 'CFBundleName': 'Luma',
    'CFBundleDisplayName': 'Luma', 'CFBundleExecutable': 'Luma',
    'CFBundlePackageType': 'APPL', 'CFBundleVersion': '2',
    'CFBundleShortVersionString': '1.1', 'CFBundleIconFile': 'Luma.icns',
    'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True,
    'NSPrincipalClass': 'NSApplication',
    'NSLocalNetworkUsageDescription': 'Luma discovers and controls your WiZ lights on your local Wi-Fi network.',
    'NSAudioCaptureUsageDescription': 'Luma analyzes system audio energy locally to synchronize your lights with music. Audio is never saved or uploaded.',
    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    'NSHumanReadableCopyright': 'Luma · Personal local light controller',
}
with (contents / 'Info.plist').open('wb') as f:
    plistlib.dump(metadata, f)
subprocess.run(['xcrun','clang','-fobjc-arc','-fblocks','-O2',
    '-fmodules-cache-path=/private/tmp/luma-clang-cache',
    '-framework','AppKit','-framework','WebKit','-framework','ImageIO',
    str(root / 'native/Desktop.m'),'-o',str(binary)],check=True)
with tempfile.TemporaryDirectory(suffix='.iconset') as icons:
    subprocess.run([str(binary),'--make-icons',icons],check=True)
    subprocess.run(['/usr/bin/iconutil','-c','icns',icons,'-o',str(resources / 'Luma.icns')],check=True)
subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(app)],check=True)
subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True)
print('Built ' + str(app))
