"""Build the native macOS screen/system-audio sampler from local source."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
app = root / 'Luma Capture.app'
binary = app / 'Contents/MacOS/LumaCapture'
binary.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-fblocks', '-O2',
                '-fmodules-cache-path=/private/tmp/luma-clang-cache',
                '-framework', 'Foundation', '-framework', 'AppKit', '-framework', 'CoreMedia',
                '-framework', 'CoreVideo', '-framework', 'AudioToolbox',
                str(root / 'native/Capture.m'), '-o', str(binary)], check=True)
subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
print('Built Luma Capture.')
