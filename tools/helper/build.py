"""Rebuild scientist-helper.jar from src/ (needs a JDK and the Android SDK build-tools + a platform).

    python tools/helper/build.py

Writes tools/helper/scientist-helper.jar and refreshes the embedded copy in Scientist.py.
"""
import base64
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
           or Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")


def newest(pattern):
    found = sorted(glob.glob(str(SDK / pattern)))
    if not found:
        sys.exit(f"not found: {SDK / pattern}")
    return Path(found[-1])


def main():
    android_jar = newest("platforms/android-*/android.jar")
    d8 = newest("build-tools/*/d8.bat" if os.name == "nt" else "build-tools/*/d8")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        classes, dex = tmp / "classes", tmp / "dex"
        classes.mkdir()
        dex.mkdir()
        srcs = [str(p) for p in (HERE / "src").rglob("*.java")]
        subprocess.run(["javac", "--release", "8", "-nowarn", "-cp", str(android_jar), "-d", str(classes), *srcs],
                       check=True)
        class_files = [str(p) for p in classes.rglob("*.class")]
        subprocess.run([str(d8), "--release", "--min-api", "21", "--lib", str(android_jar),
                        "--output", str(dex), *class_files], check=True, shell=(os.name == "nt"))
        jar = HERE / "scientist-helper.jar"
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dex / "classes.dex", "classes.dex")
    data = base64.b64encode(jar.read_bytes()).decode()
    wrapped = "\n".join(data[i:i + 100] for i in range(0, len(data), 100))
    script = ROOT / "Scientist.py"
    text = script.read_text(encoding="utf-8")
    text, n = re.subn(r'HELPER_JAR_B64 = """[^"]*"""', f'HELPER_JAR_B64 = """\n{wrapped}\n"""', text)
    if n:
        script.write_text(text, encoding="utf-8")
    print(f"built {jar} ({jar.stat().st_size} bytes); embedded in Scientist.py: {bool(n)}")


if __name__ == "__main__":
    main()
