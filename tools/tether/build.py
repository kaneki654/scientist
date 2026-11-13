"""Build the SCIENTIST Tether client APK (needs a JDK + Android SDK build-tools and a platform).

    python tools/tether/build.py

Output: tools/tether/scientist-tether.apk, and the copy embedded in Scientist.py (TETHER_APK_B64).
The signing key (scientist-tether.jks) is created on first build — keep it: phones only accept updates
signed with the same key.
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
KEYSTORE = HERE / "scientist-tether.jks"
STOREPASS = "scientist-tether"
EXE = ".exe" if os.name == "nt" else ""
BAT = ".bat" if os.name == "nt" else ""


def newest(pattern):
    found = sorted(glob.glob(str(SDK / pattern)))
    if not found:
        sys.exit(f"not found: {SDK / pattern}")
    return Path(found[-1])


def find_jdk_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for base in (os.environ.get("JAVA_HOME"), r"C:\Program Files\Java"):
        if base:
            for p in sorted(Path(base).rglob(f"{name}{EXE}"), reverse=True):
                return str(p)
    sys.exit(f"{name} not found (install a JDK)")


def run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True, shell=(os.name == "nt" and str(cmd[0]).endswith(".bat")))


def main():
    android_jar = newest("platforms/android-*/android.jar")
    bt = newest("build-tools/*/aapt2" + EXE).parent
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        classes, dex = tmp / "classes", tmp / "dex"
        classes.mkdir()
        dex.mkdir()
        run(find_jdk_tool("javac"), "--release", "8", "-nowarn", "-Xlint:-options", "-cp", android_jar,
            "-d", classes, *(HERE / "src").rglob("*.java"))
        run(bt / f"d8{BAT}", "--release", "--min-api", "21", "--lib", android_jar, "--output", dex,
            *classes.rglob("*.class"))
        unsigned = tmp / "unsigned.apk"
        run(bt / f"aapt2{EXE}", "link", "-o", unsigned, "-I", android_jar, "--manifest", HERE / "AndroidManifest.xml",
            "--min-sdk-version", "21", "--target-sdk-version", "29")
        with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
            z.write(dex / "classes.dex", "classes.dex")
        aligned = tmp / "aligned.apk"
        run(bt / f"zipalign{EXE}", "-p", "-f", "4", unsigned, aligned)
        if not KEYSTORE.exists():
            run(find_jdk_tool("keytool"), "-genkeypair", "-keystore", KEYSTORE, "-alias", "tether",
                "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-storepass", STOREPASS,
                "-keypass", STOREPASS, "-dname", "CN=SCIENTIST Tether")
        apk = HERE / "scientist-tether.apk"
        run(bt / f"apksigner{BAT}", "sign", "--ks", KEYSTORE, "--ks-pass", f"pass:{STOREPASS}",
            "--key-pass", f"pass:{STOREPASS}", "--out", apk, aligned)
    (HERE / "scientist-tether.apk.idsig").unlink(missing_ok=True)
    data = base64.b64encode(apk.read_bytes()).decode()
    wrapped = "\n".join(data[i:i + 100] for i in range(0, len(data), 100))
    script = ROOT / "Scientist.py"
    text = script.read_text(encoding="utf-8")
    text, n = re.subn(r'TETHER_APK_B64 = """[^"]*"""', f'TETHER_APK_B64 = """\n{wrapped}\n"""', text)
    if n:
        script.write_text(text, encoding="utf-8")
    print(f"built {apk} ({apk.stat().st_size} bytes); embedded in Scientist.py: {bool(n)}")


if __name__ == "__main__":
    main()
