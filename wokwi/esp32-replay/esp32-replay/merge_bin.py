# PlatformIO post-build step: merge bootloader, partition table and app into firmware_merged.bin,
# the single image wokwi.toml loads (the app alone would boot with Wokwi's default partitions).
from pathlib import Path

Import("env")  # noqa: F821  (provided by PlatformIO/SCons)

APP_BIN = "$BUILD_DIR/${PROGNAME}.bin"
MERGED_BIN = "$BUILD_DIR/${PROGNAME}_merged.bin"
board = env.BoardConfig()  # noqa: F821


def esptool_command(env) -> list[str]:
    """esptool is a native executable in recent tool-esptool packages, a Python script before."""
    package = Path(env.PioPlatform().get_package_dir("tool-esptool") or "")
    for name in ("esptool.exe", "esptool"):
        if (package / name).is_file() and (package / name).suffix != ".py":
            return [f'"{package / name}"']
    script = package / "esptool.py"
    return ['"$PYTHONEXE"', f'"{script if script.is_file() else env.subst("$OBJCOPY")}"']


def merge_bin(source, target, env):
    images = env.Flatten(env.get("FLASH_EXTRA_IMAGES", [])) + ["$ESP32_APP_OFFSET", APP_BIN]
    command = esptool_command(env) + [
        "--chip",
        board.get("build.mcu", "esp32s3"),
        "merge_bin",
        "--fill-flash-size",
        board.get("upload.flash_size", "16MB"),
        "-o",
        f'"{MERGED_BIN}"',
    ]
    if env.Execute(" ".join(command + [f'"{image}"' for image in images])):
        env.Exit(1)  # Wokwi would otherwise keep loading a stale merged image


env.AddPostAction(APP_BIN, merge_bin)  # noqa: F821
