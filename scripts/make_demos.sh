#!/bin/sh
# Graba y renderiza todas las demos. Pensado para ejecutarse dentro del contenedor `demo`:
#   docker run --rm -v "$PWD/docs/demos:/out" watcher-mcp:demo
set -e
OUT="${1:-docs/demos}"
ONLY="${2:-}"
mkdir -p "$OUT"
rec() {  # nombre, título, filas, comando...
  name="$1"; title="$2"; rows="$3"; shift 3
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then return; fi
  echo "== $name"
  python scripts/record_cast.py "$OUT/$name.cast" --title "$title" --cols 118 --rows "$rows" -- "$@"
  python scripts/render_cast.py "$OUT/$name.cast" "$OUT/$name.gif" "$OUT/$name.png"
}
rec compare "Comparativa: Bash + pip  vs  watcher-mcp" 48 python examples/compare.py
rec install "watcher-mcp · instalación real desde PyPI" 30 python examples/demo.py install
rec fail    "watcher-mcp · fallo con informe"           24 python examples/demo.py fail
rec stall   "watcher-mcp · atasco detectado (stalled)"  34 python examples/demo.py stall
rec timeout "watcher-mcp · timeout global (timed_out)"  34 python examples/demo.py timeout
ls -la "$OUT"
