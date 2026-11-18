#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cp tailcat_js.go "$scratch/"
(
  cd "$scratch"
  go mod init tabletop >/dev/null
  go get github.com/tailscale/tailcat@v0.7.0
  GOOS=js GOARCH=wasm CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o tailcat.wasm .
  go list -m -f '{{if .Dir}}{{.Dir}}{{end}}' all > modules
)
mkdir -p dist
goroot=$(go env GOROOT)
if [[ -f "$goroot/LICENSE" ]]; then
  cp "$goroot/LICENSE" "$scratch/go-license"
else
  curl -fsSL "https://raw.githubusercontent.com/golang/go/$(go env GOVERSION)/LICENSE" > "$scratch/go-license"
fi
python3 - "$scratch" "$goroot" <<'PY'
import base64
import gzip
from pathlib import Path
import sys

scratch, goroot = map(Path, sys.argv[1:])
source = Path("tabletop.html").read_text(encoding="utf-8")
runtime = (goroot / "lib/wasm/wasm_exec.js").read_text(encoding="utf-8")
binary = gzip.compress((scratch / "tailcat.wasm").read_bytes(), mtime=0)
notices = {(scratch / "go-license").read_text(encoding="utf-8", errors="replace")}
for directory in (scratch / "modules").read_text().splitlines():
    if directory:
        for license_file in sorted(Path(directory).glob("LICENSE*")):
            if license_file.is_file():
                notices.add(license_file.read_text(encoding="utf-8", errors="replace"))
output = source.replace("/* GO_RUNTIME */", runtime)
output = output.replace("<!-- THIRD_PARTY_NOTICES -->", "<!--\n" + "\n\n".join(sorted(notices)).replace("--", "- -") + "\n-->")
Path("dist/index.html").write_text(
    output.replace("__TAILCAT_WASM__", base64.b64encode(binary).decode()).replace("__TAILCAT_PARTS__", "0"),
    encoding="utf-8",
)

# Cloudflare temporary accounts limit each upload to 5 MiB.
preview = Path("dist/cloudflare-preview")
preview.mkdir(exist_ok=True)
for part in preview.glob("tailcat-*.bin"):
    part.unlink()
chunk_size = 4 * 1024 * 1024
parts = range(0, len(binary), chunk_size)
for index, start in enumerate(parts):
    (preview / f"tailcat-{index}.bin").write_bytes(binary[start:start + chunk_size])
(preview / "index.html").write_text(
    output.replace("__TAILCAT_WASM__", "").replace("__TAILCAT_PARTS__", str(len(parts))),
    encoding="utf-8",
)
PY
