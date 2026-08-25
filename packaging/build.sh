#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Serpent Circle — Build Distributable Package
#
# Creates a self-contained tarball with everything needed for a fresh install.
# Output: dist/serpent-circle-<version>.tar.gz
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"

command -v tar >/dev/null 2>&1 || { echo "❌ tar is required" >&2; exit 1; }

# Get version from pyproject.toml (anchored to avoid matching target-version / python_version)
VERSION=$(grep -oP '^version = "\K[^"]+' "$PROJECT_ROOT/pyproject.toml" 2>/dev/null || echo "0.1.0")
TARBALL="serpent-circle-${VERSION}.tar.gz"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle — Building Distributable v${VERSION}"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Clean previous builds
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# Create a clean temporary build directory
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "── Step 1/4: Copying source files..."
cp -a "$PROJECT_ROOT" "$BUILD_DIR/Hype-Coin-Predictive-Engine-main"

# Clean build artifacts from the copy
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/.git"
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/.venv"
find "$BUILD_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/dist"
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/build"
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/.mypy_cache"
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/.pytest_cache"
rm -f "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/serpent.db"
rm -f "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/serpent.db-"*
rm -rf "$BUILD_DIR/Hype-Coin-Predictive-Engine-main/data/archive"
echo "  ✅ Source copied"

echo ""
echo "── Step 2/4: Creating install wrapper..."
cat > "$BUILD_DIR/install-serpent.sh" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle — Self-Extracting Installer"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Extract source to a temp directory
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "Extracting files..."
ARCHIVE_START=$(awk '/^__ARCHIVE_BELOW__$/{print NR + 1; exit 0;}' "$0")
tail -n +"$ARCHIVE_START" "$0" | tar xz -C "$TEMP_DIR"

echo "Running installer..."
exec bash "$TEMP_DIR/Hype-Coin-Predictive-Engine-main/packaging/install.sh" "$@"
__ARCHIVE_BELOW__
WRAPPER
chmod +x "$BUILD_DIR/install-serpent.sh"

echo ""
echo "── Step 3/4: Creating archives..."
cd "$BUILD_DIR"

# Self-extracting: use the full Hype-Coin-Predictive-Engine-main/ directory
tar czf "$DIST_DIR/$TARBALL" "Hype-Coin-Predictive-Engine-main/"

# Prepend the wrapper script to make it self-extracting
cat "$BUILD_DIR/install-serpent.sh" "$DIST_DIR/$TARBALL" > "$DIST_DIR/install-serpent-circle-${VERSION}.sh"
chmod +x "$DIST_DIR/install-serpent-circle-${VERSION}.sh"
rm "$DIST_DIR/$TARBALL"

# Plain tarball: same structure (with directory prefix)
tar czf "$DIST_DIR/$TARBALL" "Hype-Coin-Predictive-Engine-main/"

echo "  ✅ Archives created"

echo ""
echo "── Step 4/4: Done!"
echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  Build artifacts:"
echo ""
echo "    Self-extracting installer:"
echo "      dist/install-serpent-circle-${VERSION}.sh"
echo ""
echo "    Plain tarball:"
echo "      dist/${TARBALL}"
echo ""
echo "  To install on a fresh Ubuntu machine:"
echo "    sudo bash install-serpent-circle-${VERSION}.sh"
echo ""
echo "  Or from the tarball:"
echo "    tar xzf ${TARBALL}"
echo "    cd Hype-Coin-Predictive-Engine-main"
echo "    sudo bash packaging/install.sh"
echo "═══════════════════════════════════════════════════════════════════"
