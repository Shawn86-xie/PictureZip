#!/bin/bash
# 构建 macOS 双击启动的 PictureZip.app
#
#   ./make_app.sh              # 在项目目录下生成 PictureZip.app
#   ./make_app.sh --install    # 同时复制到 ~/Applications
#
# 生成后即可在 Finder 里双击启动，不会弹出终端窗口。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="PictureZip"
APP_DIR="$PROJECT_DIR/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

echo "==> 项目目录: $PROJECT_DIR"

# ---------------------------------------------------------------- Python 解释器
# 优先使用项目自带的 .venv
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "错误: 找不到 Python，请先安装 Python 3.7+" >&2
    exit 1
fi
echo "==> 使用解释器: $PYTHON"

# 依赖检查
if ! "$PYTHON" -c "import PIL" 2>/dev/null; then
    echo "错误: 缺少 Pillow，请先运行: $PYTHON -m pip install -r requirements.txt" >&2
    exit 1
fi

# ---------------------------------------------------------------- 清理旧包
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RES_DIR"

# ---------------------------------------------------------------- 启动器
# 用 shell 脚本作为可执行入口：它负责定位项目目录并启动 GUI。
# 项目根目录的定位顺序：
#   1) 环境变量 PICTUREZIP_HOME
#   2) 从 .app 所在位置逐级向上查找 compress_images.py（支持把 .app 拷走）
#   3) 构建时记录的绝对路径
cat > "$MACOS_DIR/$APP_NAME" <<LAUNCHER
#!/bin/bash
# PictureZip 启动器（由 make_app.sh 生成，请勿手动编辑）

APP_BUNDLE_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")/../.." && pwd)"

find_project() {
    local d="\$1"
    while [ "\$d" != "/" ] && [ -n "\$d" ]; do
        if [ -f "\$d/compress_images.py" ]; then
            echo "\$d"
            return 0
        fi
        d="\$(dirname "\$d")"
    done
    return 1
}

HOME_DIR=""
if [ -n "\${PICTUREZIP_HOME:-}" ] && [ -f "\${PICTUREZIP_HOME}/compress_images.py" ]; then
    HOME_DIR="\$PICTUREZIP_HOME"
elif HOME_DIR="\$(find_project "\$APP_BUNDLE_DIR")"; then
    :
elif [ -f "$PROJECT_DIR/compress_images.py" ]; then
    HOME_DIR="$PROJECT_DIR"
else
    osascript -e 'display alert "PictureZip 启动失败" message "找不到 compress_images.py，请重新运行 make_app.sh 生成应用程序。" as critical' >&2
    exit 1
fi

if [ -x "\$HOME_DIR/.venv/bin/python" ]; then
    PY="\$HOME_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="\$(command -v python3)"
else
    osascript -e 'display alert "PictureZip 启动失败" message "找不到 Python 解释器。" as critical' >&2
    exit 1
fi

cd "\$HOME_DIR" || exit 1
exec "\$PY" compress_images.py
LAUNCHER
chmod +x "$MACOS_DIR/$APP_NAME"

# ---------------------------------------------------------------- Info.plist
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>图片批量压缩</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>com.local.picturezip</string>
    <key>CFBundleVersion</key>
    <string>2.0.1</string>
    <key>CFBundleShortVersionString</key>
    <string>2.0.1</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
</dict>
</plist>
PLIST

# ---------------------------------------------------------------- 图标
# 用 Pillow 生成一张图标再转成 .icns；失败不影响使用
echo "==> 生成图标…"
ICON_TMP="$(mktemp -d)"
if "$PYTHON" - "$ICON_TMP" <<'PYICON' 2>/dev/null
import sys
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(sys.argv[1])
size = 1024
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角蓝色底板
d.rounded_rectangle([0, 0, size - 1, size - 1], radius=200, fill=(52, 120, 246, 255))

# 白色"图片"框 + 山峰 + 太阳（简化图标）
m = 150
d.rounded_rectangle([m, m, size - m, size - m], radius=60, fill=(255, 255, 255, 255))
d.ellipse([size * 0.62, size * 0.26, size * 0.74, size * 0.38], fill=(255, 190, 60, 255))
d.polygon(
    [(m + 20, size - m - 20), (size * 0.45, size * 0.50), (size * 0.62, size - m - 20)],
    fill=(52, 120, 246, 255),
)
d.polygon(
    [(size * 0.55, size - m - 20), (size * 0.72, size * 0.60), (size - m - 20, size - m - 20)],
    fill=(110, 190, 250, 255),
)

for name, px in [("16", 16), ("32", 32), ("64", 64), ("128", 128), ("256", 256), ("512", 512)]:
    img.resize((px, px), Image.LANCZOS).save(out / f"icon_{name}.png")

iconset = out / "AppIcon.iconset"
iconset.mkdir(exist_ok=True)
for name, px in [("16", 16), ("32", 32), ("64", 64), ("128", 128), ("256", 256), ("512", 512)]:
    src = out / f"icon_{name}.png"
    for scale, tag in [(1, ""), (2, "@2x")]:
        size_px = px * scale
        if size_px > 1024:
            continue
        img.resize((size_px, size_px), Image.LANCZOS).save(iconset / f"icon_{name}x{size_px}{tag}.png")
PYICON
then
    iconutil -c icns "$ICON_TMP/AppIcon.iconset" -o "$RES_DIR/AppIcon.icns" 2>/dev/null \
        && echo "    图标已生成" \
        || echo "    图标转换失败（不影响使用）"
else
    echo "    跳过图标（不影响使用）"
fi
rm -rf "$ICON_TMP"

# ---------------------------------------------------------------- 完成
echo "==> 完成: $APP_DIR"

if [ "${1:-}" = "--install" ]; then
    DEST="$HOME/Applications"
    mkdir -p "$DEST"
    TARGET="$DEST/$APP_NAME.app"
    if [ -e "$TARGET" ]; then
        # 先移到备份位置，安装成功后再删除备份，避免中途失败导致旧版本丢失
        BACKUP="$TARGET.backup.$$"
        mv "$TARGET" "$BACKUP"
        if cp -R "$APP_DIR" "$TARGET"; then
            rm -rf "$BACKUP"
        else
            mv "$BACKUP" "$TARGET"
            echo "错误: 安装失败，已还原原有版本" >&2
            exit 1
        fi
    else
        cp -R "$APP_DIR" "$TARGET"
    fi
    echo "==> 已安装到: $TARGET"
    echo "    现在可以把它拖到 Dock 栏，或在启动台中打开。"
    echo
    echo "    若首次打开被 macOS 拦下（提示无法验证开发者），"
    echo "    请在 Finder 中右键该应用 → 打开 → 再点「打开」，"
    echo "    或在「系统设置 → 隐私与安全性」中点击「仍要打开」。"
fi
