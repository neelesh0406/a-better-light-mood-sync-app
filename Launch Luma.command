#!/bin/zsh
cd "${0:A:h}"
if /usr/bin/curl --silent --fail http://127.0.0.1:8765/api/config >/dev/null; then
  open http://127.0.0.1:8765
else
  if [[ ! -x 'Luma Capture.app/Contents/MacOS/LumaCapture' ]]; then
    python3 build_capture.py || exit 1
  fi
  (sleep 1; open http://127.0.0.1:8765) &
  python3 app.py
fi
