import sys
import os
import time
import traceback

# Stdout/stderr line-buffered + UTF-8: without this, prints can vanish on
# Windows PowerShell when the Qt event loop blocks the terminal.
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")
except Exception:
    pass

# When running as a PyInstaller-bundled .exe, the working directory is wherever
# the user double-clicked from — usually NOT the folder containing the .exe.
# All resource paths in this app are relative ("assets/logo.png", "data/…gdb"),
# so we anchor them by switching into the .exe's folder. In dev mode (running
# python main.py from the project root) nothing changes.
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
    print(f"[boot] frozen exe — cwd set to {os.getcwd()}", flush=True)

print("[boot] main.py starting", flush=True)

# GPU/OpenGL fallback. Enable only on machines where the QWebEngine widget
# renders as a blank window; on modern Windows with a working GPU driver,
# software rendering actually breaks large Folium/Leaflet maps.
# os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
# os.environ.setdefault("QT_OPENGL", "software")

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QFont
from PySide6.QtWidgets import QApplication

from Views.splash import CCUSSplash

print("[boot] importing Views.main_window", flush=True)
from Views.main_window import MainWindow
print("[boot] imports done", flush=True)


def main():
    print("[boot] entering main()", flush=True)
    # Software OpenGL fallback — re-enable only if the WebEngine widget
    # comes up blank with hardware GL on a specific machine.
    # try:
    #     QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL)
    # except Exception:
    #     pass

    app = QApplication(sys.argv)
    app.setOrganizationName("NGI")
    app.setApplicationName("CCUSApp")

    font = QFont("Segoe UI", 10)
    app.setFont(font)

    logo_path = os.path.join("assets", "logo.png")
    splash = CCUSSplash(assets_dir="assets")
    splash.show()
    splash.update_status("Loading geoscience libraries…")

    print("[boot] constructing MainWindow", flush=True)
    splash.update_status("Building user interface…")
    window = MainWindow()
    print("[boot] MainWindow constructed", flush=True)
    if os.path.exists(logo_path):
        window.setWindowIcon(QIcon(logo_path))

    splash.update_status("Ready.")
    # Brief pause so the user actually sees the splash on fast machines.
    time.sleep(0.3)
    splash.finish(window)

    window.show()
    print("[boot] window.show() called; entering event loop", flush=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        print("[boot] UNCAUGHT EXCEPTION in main():", flush=True)
        traceback.print_exc()
        input("Press Enter to close...")
        raise
