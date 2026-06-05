import sys
import os
import re
import json
import tempfile
import pandas as pd
import geopandas as gpd
import numpy as np
import folium

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QObject, QTimer
from PySide6.QtGui import QIcon, QPixmap, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QDoubleSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter, QGroupBox, QFormLayout,
    QTabWidget, QMessageBox, QToolBar, QComboBox, QScrollArea, QSlider,
    QProgressBar, QStackedWidget, QTextEdit, QSizePolicy, QFileDialog
)

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage
except Exception:  # pragma: no cover - PySide versions differ
    QWebEngineSettings = None
    QWebEnginePage = None
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import QColor


class DiagnosticWebPage(QWebEnginePage if QWebEnginePage is not None else object):
    """QWebEnginePage that pipes Leaflet/JS errors to the terminal.

    The 2D map relies on Folium HTML running inside QWebEngineView. When the
    widget stays blank the cause is almost always a silent JS exception, an
    HTTP failure fetching tiles, or the render process being killed. This
    page surfaces all three.
    """

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        # PySide6 hands us a JavaScriptConsoleMessageLevel enum; map by name,
        # not by int(), because the enum can't be cast directly to int in 6.11.
        name = getattr(level, "name", str(level))
        level_name = {
            "InfoMessageLevel": "INFO",
            "WarningMessageLevel": "WARN",
            "ErrorMessageLevel": "ERROR",
        }.get(name, name)
        print(f"[WebMap JS {level_name}] {source_id}:{line_number} {message}")
from pyvistaqt import QtInteractor

from Views.translator import translator
from Views.theme_manager import theme_manager
from Views.settings_dialog import SettingsDialog

import models.ccus_analysis as ca
from models.dem_3d_viewer import create_3d_viewer


# =============================================================================
# GEOLOGY HELPERS, MATCHING THE NOTEBOOK DEFAULT LOGIC
# =============================================================================
FAMILY_MAP = {
    "Ga": "Ga - Gabbro",
    "Gr": "Gr - Granitt/Gneis/Gronnstein",
    "Gl": "Gl - Glimmer",
    "Kv": "Kv - Kvarts",
    "Mo": "Mo - Monzonitt",
    "Ry": "Ry - Ryolitt",
    "Øy": "Oy - Oyegneis",
    "øy": "Oy - Oyegneis",
}


def get_family_from_ngu_name(rock_name):
    if pd.isna(rock_name):
        return "Unknown"
    rock_name = str(rock_name)
    prefix = rock_name[:2]
    return FAMILY_MAP.get(prefix, f"{prefix} - {rock_name}")


def prepare_bedrock_gdf(gdf):
    """Prepare the NGU bedrock polygons exactly like the notebook default."""
    gdf = gdf.copy()
    for col in gdf.select_dtypes(include=["datetime", "datetimetz"]).columns:
        gdf[col] = gdf[col].astype(str)

    if "hovedbergart_navn" in gdf.columns:
        gdf["family"] = gdf["hovedbergart_navn"].apply(get_family_from_ngu_name)
        gdf["subfamily"] = gdf["hovedbergart_navn"].astype(str)
    else:
        if "family" not in gdf.columns:
            gdf["family"] = "Unknown"
        if "subfamily" not in gdf.columns:
            gdf["subfamily"] = gdf["family"]

    if "area_km2" not in gdf.columns:
        gdf["area_km2"] = gdf.geometry.area / 1e6

    if "shape_id" not in gdf.columns:
        counter = {}
        shape_ids = []
        for idx in gdf.index:
            fam = str(gdf.loc[idx, "family"])[:2]
            sub = str(gdf.loc[idx, "subfamily"])[:3]
            key = f"{fam}_{sub}"
            counter[key] = counter.get(key, 0) + 1
            shape_ids.append(f"{key}_{counter[key]}")
        gdf["shape_id"] = shape_ids

    return gdf


def cmyk_to_hex(cmyk_value, default="#808080"):
    if pd.isna(cmyk_value):
        return default
    if isinstance(cmyk_value, (list, tuple, np.ndarray)) and len(cmyk_value) == 4:
        parts = [float(x) for x in cmyk_value]
    else:
        nums = re.findall(r"-?\d+(?:\.\d+)?", str(cmyk_value).strip())
        if len(nums) != 4:
            return default
        parts = [float(x) for x in nums]
    c, m, y, k = [max(0, min(100, v)) / 100.0 for v in parts]
    r = int(255 * (1 - c) * (1 - k))
    g = int(255 * (1 - m) * (1 - k))
    b = int(255 * (1 - y) * (1 - k))
    return f"#{r:02x}{g:02x}{b:02x}"


# =============================================================================
# LOGGING SYSTEM
# =============================================================================
class StreamLogger(QObject):
    new_text = Signal(str)

    def write(self, text):
        self.new_text.emit(text)

    def flush(self):
        pass


# =============================================================================
# BACKGROUND WORKER
# =============================================================================
class AnalysisWorker(QThread):
    finished = Signal(object, object, object, object)
    error = Signal(str)

    def __init__(self, gdb_path, fracture_porosity=0.015):
        super().__init__()
        self.gdb_path = gdb_path
        self.fracture_porosity = fracture_porosity

    def run(self):
        try:
            ca.FRACTURE_POROSITY = self.fracture_porosity
            print(f"Loading Geodatabase: {self.gdb_path}...")
            gdf = gpd.read_file(self.gdb_path, layer="BergartFlate_N250")
            gdf = prepare_bedrock_gdf(gdf)
            print(f"Loaded {len(gdf)} features from BergartFlate_N250.")
            print("Starting CCUS analysis pipeline...")
            gdf_res, faults, mpts, petro_df = ca.run_analysis(gdf, gdb_path=self.gdb_path)
            print("CCUS analysis completed successfully.")
            self.finished.emit(gdf_res, faults, mpts, petro_df)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


# =============================================================================
# MAIN WINDOW
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.gdb_path = os.path.join("data", "BerggrunnN250.gdb")
        self.boreholes_gdb_path = os.path.join("data", "Grunnvannsborehull.gdb")
        self.current_results = None
        self.current_faults = None
        self.current_mpts = None
        self.current_petro_df = None
        self.current_scored_results = None
        self.current_top5 = None
        self.current_boreholes = None  # dict[layer_name -> GeoDataFrame], lazy.
        self.base_gdf = None
        self._last_runtime_html = None
        self._last_loaded_map = None

        # WLC state. Seal intentionally removed.
        self.w_reservoir = 45
        self.w_fault = 30
        self.w_structure = 15
        self.w_petro = 10

        # Moved out of the left Configuration panel — now edited in Settings.
        self.fracture_porosity_pct = 1.5
        self.vertical_exag = 3.0

        # Redirect Output
        self.logger = StreamLogger()
        self.logger.new_text.connect(self.append_log)
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        sys.stdout = self.logger
        sys.stderr = self.logger

        self._setup_ui()
        self._apply_translations()
        self._apply_theme()

        translator.language_changed.connect(self._apply_translations)
        theme_manager.theme_changed.connect(self._apply_theme)

    def closeEvent(self, event):
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr
        super().closeEvent(event)

    def append_log(self, text):
        if hasattr(self, "log_console"):
            cursor = self.log_console.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.log_console.setTextCursor(cursor)
            self.log_console.insertPlainText(text)
            self.log_console.verticalScrollBar().setValue(self.log_console.verticalScrollBar().maximum())

    def _setup_ui(self):
        self.setWindowTitle(translator.tr("app_title"))
        self.resize(1350, 900)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        self._setup_intro_page()
        self._setup_ccus_page()
        self._setup_about_page()

        self.stacked_widget.addWidget(self.intro_page)
        self.stacked_widget.addWidget(self.ccus_page)
        self.stacked_widget.addWidget(self.about_page)
        self.stacked_widget.setCurrentIndex(0)

    def _setup_intro_page(self):
        self.intro_page = QWidget()
        layout = QVBoxLayout(self.intro_page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(40, 40, 40, 40)

        # Program brand — logo.png is the app identity, large and centered.
        lbl_app = QLabel()
        lbl_app.setAlignment(Qt.AlignCenter)
        app_pix = QPixmap(os.path.join("assets", "logo.png"))
        if not app_pix.isNull():
            lbl_app.setPixmap(app_pix.scaledToHeight(220, Qt.SmoothTransformation))
        layout.addWidget(lbl_app)

        layout.addSpacing(24)
        lbl_title = QLabel("CCUS Suitability Analysis")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setProperty("title", True)
        f = lbl_title.font()
        f.setPointSize(26)
        f.setBold(True)
        lbl_title.setFont(f)
        layout.addWidget(lbl_title)

        lbl_sub = QLabel(
            "Onshore CO₂ mineralization screening — Kirkenær & Rakkestad case studies"
        )
        lbl_sub.setAlignment(Qt.AlignCenter)
        lbl_sub.setProperty("muted", True)
        sf = lbl_sub.font()
        sf.setPointSize(11)
        lbl_sub.setFont(sf)
        layout.addWidget(lbl_sub)

        # Show which geodatabase is currently loaded; updated by Load button.
        self.lbl_gdb_status = QLabel()
        self.lbl_gdb_status.setAlignment(Qt.AlignCenter)
        self.lbl_gdb_status.setProperty("muted", True)
        self._refresh_gdb_status_label()
        layout.addSpacing(8)
        layout.addWidget(self.lbl_gdb_status)

        layout.addSpacing(40)

        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignCenter)
        btn_layout.setSpacing(20)

        btn_ccus = QPushButton("CO₂ Storage\n(CCUS Analysis)")
        btn_ccus.setProperty("hero", True)
        btn_ccus.setFixedSize(220, 120)
        btn_ccus.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))

        btn_load = QPushButton("Load Geodatabase\n(.gdb folder)")
        btn_load.setFixedSize(220, 120)
        btn_load.clicked.connect(self.choose_geodatabase)

        btn_about = QPushButton("About\n(devs · math · features)")
        btn_about.setFixedSize(220, 120)
        btn_about.clicked.connect(self.open_about_page)

        btn_layout.addWidget(btn_ccus)
        btn_layout.addWidget(btn_load)
        btn_layout.addWidget(btn_about)
        layout.addLayout(btn_layout)

        layout.addStretch(1)

        # NGI credit at the bottom — small institutional logo, not the brand.
        credit_layout = QHBoxLayout()
        credit_layout.setAlignment(Qt.AlignCenter)
        credit_layout.setSpacing(8)
        lbl_credit_text = QLabel("Developed by")
        lbl_credit_text.setProperty("muted", True)
        cf = lbl_credit_text.font()
        cf.setPointSize(9)
        lbl_credit_text.setFont(cf)
        lbl_ngi = QLabel()
        ngi_pix = QPixmap(os.path.join("assets", "ngi.png"))
        if not ngi_pix.isNull():
            lbl_ngi.setPixmap(ngi_pix.scaledToHeight(36, Qt.SmoothTransformation))
        credit_layout.addWidget(lbl_credit_text)
        credit_layout.addWidget(lbl_ngi)
        layout.addLayout(credit_layout)

    def _setup_ccus_page(self):
        self.ccus_page = QWidget()
        ccus_main_layout = QVBoxLayout(self.ccus_page)
        ccus_main_layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        ccus_main_layout.addWidget(toolbar)

        btn_back = QPushButton("◀ Home")
        btn_back.setProperty("ghost", True)
        btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        toolbar.addWidget(btn_back)
        toolbar.addSeparator()

        # App brand: logo.png is the program identity (not NGI).
        lbl_tb_app = QLabel()
        app_pix_tb = QPixmap(os.path.join("assets", "logo.png"))
        if not app_pix_tb.isNull():
            lbl_tb_app.setPixmap(app_pix_tb.scaledToHeight(28, Qt.SmoothTransformation))
            lbl_tb_app.setContentsMargins(10, 0, 10, 0)
        toolbar.addWidget(lbl_tb_app)

        self.lbl_brand = QLabel(translator.tr("app_title"))
        self.lbl_brand.setProperty("title", True)
        self.lbl_brand.setContentsMargins(10, 0, 20, 0)
        toolbar.addWidget(self.lbl_brand)

        toolbar.addSeparator()

        # Load a different .gdb folder at any time.
        self.btn_load_gdb_tb = QPushButton("📂 Load GDB…")
        self.btn_load_gdb_tb.setProperty("ghost", True)
        self.btn_load_gdb_tb.setToolTip("Pick a different Esri File Geodatabase (.gdb folder)")
        self.btn_load_gdb_tb.clicked.connect(self.choose_geodatabase)
        toolbar.addWidget(self.btn_load_gdb_tb)

        self.lbl_gdb_name_tb = QLabel(self._format_gdb_name())
        self.lbl_gdb_name_tb.setProperty("muted", True)
        self.lbl_gdb_name_tb.setContentsMargins(8, 0, 8, 0)
        toolbar.addWidget(self.lbl_gdb_name_tb)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        # NGI credit on the right side of the toolbar.
        lbl_tb_ngi = QLabel()
        ngi_pix = QPixmap(os.path.join("assets", "ngi.png"))
        if not ngi_pix.isNull():
            lbl_tb_ngi.setPixmap(ngi_pix.scaledToHeight(22, Qt.SmoothTransformation))
            lbl_tb_ngi.setContentsMargins(10, 0, 10, 0)
            lbl_tb_ngi.setToolTip("Developed by NGI")
        toolbar.addWidget(lbl_tb_ngi)

        # Run Analysis is the most-used action — promote to the toolbar so the
        # left configuration panel can be removed.
        self.btn_run = QPushButton("▶ " + translator.tr("run_analysis"))
        self.btn_run.setProperty("hero", True)
        self.btn_run.clicked.connect(self.run_analysis)
        toolbar.addWidget(self.btn_run)

        # Compact status label next to Run Analysis (replaces the big lbl_status
        # that used to live in the left panel).
        self.lbl_status = QLabel(translator.tr("status_ready"))
        self.lbl_status.setProperty("muted", True)
        self.lbl_status.setContentsMargins(8, 0, 8, 0)
        toolbar.addWidget(self.lbl_status)

        self.btn_about_tb = QPushButton("ℹ About")
        self.btn_about_tb.setProperty("ghost", True)
        self.btn_about_tb.setToolTip("About this program — developers, math and features")
        self.btn_about_tb.clicked.connect(self.open_about_page)
        toolbar.addWidget(self.btn_about_tb)

        self.btn_settings = QPushButton("⚙️ " + translator.tr("settings"))
        self.btn_settings.setProperty("ghost", True)
        self.btn_settings.clicked.connect(self.open_settings)
        toolbar.addWidget(self.btn_settings)

        # Right panel takes the full width now — no left config panel.
        self.splitter = QSplitter(Qt.Horizontal)
        ccus_main_layout.addWidget(self.splitter, 1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 16, 16, 16)

        self.v_splitter = QSplitter(Qt.Vertical)
        right_layout.addWidget(self.v_splitter)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.on_tab_changed)

        # 1. Dashboard
        self.tab_dashboard = QWidget()
        dash_layout = QVBoxLayout(self.tab_dashboard)
        dash_split = QSplitter(Qt.Horizontal)

        self.card_wlc = QGroupBox("WLC Weights (No Seal Criterion)")
        wlc_layout = QVBoxLayout(self.card_wlc)

        self.sliders = {}
        self.slider_value_labels = {}
        for key, lbl, val in [
            ("reservoir", "Reservoir", self.w_reservoir),
            ("fault", "Fault / Injectivity", self.w_fault),
            ("structure", "Structure", self.w_structure),
            ("petrophysics", "Petrophysics", self.w_petro),
        ]:
            row = QWidget()
            row_lyt = QVBoxLayout(row)
            row_lyt.setContentsMargins(0, 0, 0, 0)

            header = QWidget()
            h_lyt = QHBoxLayout(header)
            h_lyt.setContentsMargins(0, 0, 0, 0)
            lbl_w = QLabel(lbl)
            val_w = QLabel(f"{val}%")
            val_w.setProperty("muted", True)
            h_lyt.addWidget(lbl_w)
            h_lyt.addStretch()
            h_lyt.addWidget(val_w)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(val)
            slider.valueChanged.connect(lambda v, k=key, lab=val_w: self.on_slider_change(k, v, lab))

            row_lyt.addWidget(header)
            row_lyt.addWidget(slider)
            wlc_layout.addWidget(row)
            self.sliders[key] = slider
            self.slider_value_labels[key] = val_w

        self.btn_apply_weights = QPushButton("Apply WLC Weights")
        self.btn_apply_weights.setProperty("hero", True)
        self.btn_apply_weights.clicked.connect(self.apply_wlc_weights)
        wlc_layout.addWidget(self.btn_apply_weights)

        self.lbl_weights_status = QLabel("Change weights, then click Apply. Seal is ignored.")
        self.lbl_weights_status.setProperty("muted", True)
        wlc_layout.addWidget(self.lbl_weights_status)

        dash_split.addWidget(self.card_wlc)

        self.card_top5 = QGroupBox("Top 5 Candidates")
        top5_layout = QVBoxLayout(self.card_top5)
        self.table_top5 = QTableWidget()
        self.table_top5.setColumnCount(4)
        self.table_top5.setHorizontalHeaderLabels(["Rank", "Family", "Score", "Capacity"])
        self.table_top5.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        top5_layout.addWidget(self.table_top5)
        dash_split.addWidget(self.card_top5)
        dash_split.setSizes([300, 700])
        dash_layout.addWidget(dash_split)

        self.card_cap = QGroupBox("Storage Capacity Breakdown")
        cap_layout = QVBoxLayout(self.card_cap)
        self.table_cap = QTableWidget()
        self.table_cap.setColumnCount(7)
        self.table_cap.setHorizontalHeaderLabels([
            "Shape ID", "Family", "Area (km²)", "h (m)", "Porosity", "Capacity (Mt)", "Mt/km²"
        ])
        self.table_cap.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        cap_layout.addWidget(self.table_cap)
        dash_layout.addWidget(self.card_cap)
        self.tabs.addTab(self.tab_dashboard, "CCUS Dashboard")

        # 2. 3D
        self.tab_3d = QWidget()
        tab_3d_layout = QVBoxLayout(self.tab_3d)
        self.plotter = QtInteractor(self.tab_3d)
        tab_3d_layout.addWidget(self.plotter.interactor)
        self.tabs.addTab(self.tab_3d, translator.tr("tab_3d"))

        # 3. 2D interactive Folium/Leaflet map inside the app
        self.tab_2d = QWidget()
        tab_2d_layout = QVBoxLayout(self.tab_2d)
        map_controls = QHBoxLayout()
        self.combo_2d = QComboBox()
        # Order matches user priorities — Capacity is the primary deliverable,
        # Bedrock Family is the no-analysis-needed fallback for browsing data.
        self.combo_2d.addItems([
            "CCUS Capacity Map",
            "CCUS WLC Map",
            "CCUS AHP Map",
            "Bedrock Family Map",
        ])
        self.combo_2d.currentTextChanged.connect(self.load_2d_map)
        self.btn_refresh_map = QPushButton("Refresh Map")
        self.btn_refresh_map.clicked.connect(lambda: self.load_2d_map(self.combo_2d.currentText()))
        self.btn_expand_map = QPushButton("⛶ Expand")
        self.btn_expand_map.setToolTip("Hide tabs and log console to enlarge the map. Click again to restore.")
        self.btn_expand_map.setCheckable(True)
        self.btn_expand_map.toggled.connect(self._toggle_map_expanded)
        map_controls.addWidget(self.combo_2d, 1)
        map_controls.addWidget(self.btn_refresh_map)
        map_controls.addWidget(self.btn_expand_map)
        tab_2d_layout.addLayout(map_controls)

        # Parent the QWebEngineView to the 2D tab from the start. Without an
        # explicit parent Qt briefly treats it as a top-level window — that's
        # the "QWidgetWindow must be a top level window" warning at boot, and
        # in some setups it leaves the widget floating outside its tab.
        self.web_view = QWebEngineView(self.tab_2d)
        # Without this, the widget can resolve to 0 px tall during the brief
        # window when Leaflet measures its container — leaving a blank map.
        self.web_view.setMinimumHeight(400)
        self.web_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if QWebEnginePage is not None:
            self._web_page = DiagnosticWebPage(self.web_view)
            self.web_view.setPage(self._web_page)
        # The dark app stylesheet bleeds through QWebEngineView and shows a
        # solid black rectangle while Folium loads (and forever if tiles fail).
        # Force the page background to white so the map area looks correct
        # even before the first tile arrives.
        try:
            self.web_view.page().setBackgroundColor(QColor("#ffffff"))
        except Exception as exc:
            print(f"[WebMap] could not set page background: {exc}")
        self._configure_web_view()
        self.web_view.loadStarted.connect(
            lambda: print(f"[WebMap] loadStarted url={self.web_view.url().toString()}")
        )
        self.web_view.loadProgress.connect(
            lambda p: print(f"[WebMap] loadProgress {p}%")
        )
        self.web_view.loadFinished.connect(
            lambda ok: print(f"[WebMap] loadFinished ok={ok} url={self.web_view.url().toString()}")
        )
        try:
            self.web_view.page().renderProcessTerminated.connect(
                lambda status, code: print(
                    f"[WebMap] !!! renderProcessTerminated status={status} exitCode={code} "
                    "(blank widget is expected after this; HTML is too heavy or Chromium crashed)"
                )
            )
        except Exception as exc:
            print(f"[WebMap] could not hook renderProcessTerminated: {exc}")
        tab_2d_layout.addWidget(self.web_view)
        self.tabs.addTab(self.tab_2d, translator.tr("tab_2d"))

        # 4. Plots
        self.tab_plots = QWidget()
        tab_plots_layout = QVBoxLayout(self.tab_plots)
        self.combo_plots = QComboBox()
        self.combo_plots.addItems(["DEM Bedrock Overview", "Elevation By Family"])
        self.combo_plots.currentTextChanged.connect(self.load_plot)
        tab_plots_layout.addWidget(self.combo_plots)
        self.scroll_plot = QScrollArea()
        self.scroll_plot.setWidgetResizable(True)
        self.lbl_plot = QLabel()
        self.lbl_plot.setAlignment(Qt.AlignCenter)
        self.scroll_plot.setWidget(self.lbl_plot)
        tab_plots_layout.addWidget(self.scroll_plot)
        self.tabs.addTab(self.tab_plots, translator.tr("tab_plots"))

        self.v_splitter.addWidget(self.tabs)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setProperty("console", True)
        self.log_console.setMinimumHeight(150)
        self.v_splitter.addWidget(self.log_console)
        self.v_splitter.setSizes([700, 200])

        # No left panel anymore — single child fills the splitter.
        self.splitter.addWidget(right_panel)

        self.load_2d_map(self.combo_2d.currentText())
        self.load_plot(self.combo_plots.currentText())

    def _configure_web_view(self):
        settings = self.web_view.settings()
        # PySide enum names differ between versions. Try both styles.
        attr_names = [
            "JavascriptEnabled",
            "LocalContentCanAccessRemoteUrls",
            "LocalContentCanAccessFileUrls",
            "ScrollAnimatorEnabled",
        ]
        for name in attr_names:
            try:
                attr = getattr(QWebEngineSettings.WebAttribute, name)
            except Exception:
                attr = getattr(QWebEngineSettings, name, None) if QWebEngineSettings else None
            if attr is not None:
                try:
                    settings.setAttribute(attr, True)
                except Exception:
                    pass

    def _show_map_message(self, title, body):
        html = f"""
        <html><body style="background:#111;color:#eee;font-family:Arial;padding:24px;">
        <h2>{title}</h2>
        <pre style="white-space:pre-wrap;background:#222;padding:16px;border-radius:8px;">{body}</pre>
        </body></html>
        """
        self.web_view.setHtml(html)

    def _render_folium_in_webview(self, fmap, label="map"):
        """Render a Folium/Leaflet map inside QWebEngineView.

        For large GeoJSON maps, QWebEnginePage.setHtml can hit an internal URL
        size limit. Writing a runtime temporary HTML file is more reliable. This
        is not a precomputed output map. It is generated on demand by the app.
        """
        html = fmap.get_root().render()
        # Defensive patches for embedded Leaflet inside QWebEngineView:
        #   - vh/vw + !important on html/body/.folium-map: bypasses any
        #     parent-height chain that resolves to 0 during first paint.
        #   - invalidateSize() loop after load: tells Leaflet to remeasure
        #     its container once the QWebEngineView has its final size.
        # Without these, the HTML loads (ok=True) but the map is blank.
        embed_patch = """
<style>
  html, body { height: 100vh !important; width: 100vw !important; margin: 0 !important; padding: 0 !important; background:#ffffff !important; }
  .folium-map { height: 100vh !important; width: 100vw !important; min-height: 400px !important; background:#ffffff !important; }
  .leaflet-container { background:#dddddd !important; }
</style>
<script>
  (function ensureLeafletSize() {
    function fixAll() {
      if (typeof L === "undefined" || !L.Map) return false;
      var fixed = 0;
      for (var k in window) {
        try {
          var v = window[k];
          if (v && v instanceof L.Map) {
            v.invalidateSize(true);
            // Hook tile-layer error events so we can see if CartoDB tiles
            // are being blocked (firewall, proxy, no internet).
            v.eachLayer(function (lyr) {
              if (lyr && lyr._url && !lyr._diagHooked) {
                lyr._diagHooked = true;
                lyr.on("tileerror", function (e) {
                  console.error("TILE_ERROR " + (e && e.tile && e.tile.src ? e.tile.src : "unknown"));
                });
                lyr.on("tileload", function () {
                  if (!lyr._diagFirstTile) {
                    lyr._diagFirstTile = true;
                    console.log("TILE_OK " + lyr._url);
                  }
                });
              }
            });
            fixed++;
          }
        } catch (e) {}
      }
      return fixed > 0;
    }
    var tries = 0;
    var iv = setInterval(function() {
      tries++;
      if (fixAll() || tries > 30) clearInterval(iv);
    }, 150);
    window.addEventListener("resize", fixAll);
    // Surface fatal load errors that would otherwise be silent.
    window.addEventListener("error", function (e) {
      console.error("JS_ERROR " + (e.message || "(no message)") + " @ " + (e.filename || "?") + ":" + (e.lineno || "?"));
    });
  })();
</script>
"""
        # Inject right before </body> so Leaflet is already constructed.
        if "</body>" in html:
            html = html.replace("</body>", embed_patch + "</body>", 1)
        else:
            html += embed_patch
        runtime_dir = os.path.join(tempfile.gettempdir(), "ccus_app_runtime_maps")
        os.makedirs(runtime_dir, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_") or "map"
        path = os.path.join(runtime_dir, f"{safe_label}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        self._last_runtime_html = path
        size_mb = os.path.getsize(path) / (1024 * 1024)
        local_url = QUrl.fromLocalFile(path)
        print(
            f"[WebMap] wrote {path} ({size_mb:.2f} MB); "
            f"loading {local_url.toString()}"
        )
        if size_mb > 8:
            print(
                f"[WebMap] WARNING: HTML is {size_mb:.1f} MB — QWebEngineView "
                "frequently blanks above ~5-8 MB. Consider grouping features per layer."
            )
        self.web_view.setUrl(local_url)

    def on_tab_changed(self, index):
        widget = self.tabs.widget(index) if hasattr(self, "tabs") else None
        name = self.tabs.tabText(index) if hasattr(self, "tabs") else "?"
        print(f"[Tabs] currentChanged index={index} tab='{name}'")
        if widget is getattr(self, "tab_2d", None):
            # Generating the Folium HTML (~6 MB) and writing it to disk blocks
            # the UI thread for several seconds. If we do it inline here, Qt
            # never paints the tab switch — the user clicks 2D and sees the
            # old (dashboard) content frozen. Defer to the next event loop
            # tick so the tab switch repaints first.
            current_map = self.combo_2d.currentText()
            if getattr(self, "_last_loaded_map", None) == current_map and \
                    getattr(self, "web_view", None) is not None and \
                    not self.web_view.url().isEmpty():
                print(f"[Tabs] '{current_map}' already loaded; skipping regenerate")
                return
            QTimer.singleShot(0, lambda m=current_map: self.load_2d_map(m))

    def on_slider_change(self, key, value, label_widget):
        if key == "petrophysics":
            self.w_petro = value
        else:
            setattr(self, f"w_{key}", value)

        total = self.w_reservoir + self.w_fault + self.w_structure + self.w_petro
        pct = (value / total) * 100 if total > 0 else 0
        label_widget.setText(f"{value} ({pct:.1f}%)")
        self.lbl_weights_status.setText("Weights changed. Click Apply WLC Weights to recompute.")

    def apply_wlc_weights(self):
        self.recalculate_wlc(refresh_map=True)

    def recalculate_wlc(self, refresh_map=False):
        if self.current_results is None:
            self.lbl_weights_status.setText("Run the analysis first, then apply weights.")
            return
        try:
            if "reservoir_score" not in self.current_results.columns:
                return

            gdf, top5 = ca.apply_wlc(
                self.current_results,
                w_reservoir=self.w_reservoir,
                w_fault=self.w_fault,
                w_seal=0,
                w_structure=self.w_structure,
                w_petrophysics=self.w_petro,
            )
            self.current_scored_results = gdf
            self.current_top5 = top5
            self._update_top5_table(top5)
            self.lbl_weights_status.setText("WLC weights applied. Seal criterion ignored.")
            if refresh_map and self.combo_2d.currentText() == "CCUS WLC Map":
                self.load_2d_map("CCUS WLC Map")
        except Exception as e:
            print("WLC Recalc error:", e)
            QMessageBox.warning(self, translator.tr("error"), f"WLC recalculation failed: {e}")

    def _update_top5_table(self, top5):
        self.table_top5.setRowCount(len(top5))
        for i, (_, row) in enumerate(top5.iterrows()):
            self.table_top5.setItem(i, 0, QTableWidgetItem(f"#{i+1}"))
            self.table_top5.setItem(i, 1, QTableWidgetItem(str(row.get("family", "")).split(" - ")[0]))
            score = row.get("ccus_pct", 0)
            score_widget = QWidget()
            score_layout = QHBoxLayout(score_widget)
            score_layout.setContentsMargins(4, 4, 4, 4)
            pb = QProgressBar()
            pb.setRange(0, 100)
            pb.setValue(int(score) if not pd.isna(score) else 0)
            lbl = QLabel(f"{score:.1f}%" if not pd.isna(score) else "N/A")
            lbl.setProperty("title", True)
            score_layout.addWidget(pb)
            score_layout.addWidget(lbl)
            self.table_top5.setCellWidget(i, 2, score_widget)
            cap = row.get("storage_mass_Mt", np.nan)
            self.table_top5.setItem(i, 3, QTableWidgetItem(f"{cap:.2f} Mt" if not pd.isna(cap) else "N/A"))

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    # ------------------------------------------------------------------
    # About page
    # ------------------------------------------------------------------
    def _setup_about_page(self):
        self.about_page = QWidget()
        outer = QVBoxLayout(self.about_page)
        outer.setContentsMargins(0, 0, 0, 0)

        bar = QToolBar()
        bar.setMovable(False)
        outer.addWidget(bar)
        btn_back = QPushButton("◀ Home")
        btn_back.setProperty("ghost", True)
        btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        bar.addWidget(btn_back)
        sp = QWidget()
        sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(sp)
        bar_ngi = QLabel()
        ngi_pix = QPixmap(os.path.join("assets", "ngi.png"))
        if not ngi_pix.isNull():
            bar_ngi.setPixmap(ngi_pix.scaledToHeight(22, Qt.SmoothTransformation))
            bar_ngi.setContentsMargins(10, 0, 10, 0)
        bar.addWidget(bar_ngi)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(48, 32, 48, 32)
        layout.setSpacing(16)

        # Header
        h_row = QHBoxLayout()
        h_row.setSpacing(20)
        lbl_logo = QLabel()
        logo_pix = QPixmap(os.path.join("assets", "logo.png"))
        if not logo_pix.isNull():
            lbl_logo.setPixmap(logo_pix.scaledToHeight(100, Qt.SmoothTransformation))
        h_row.addWidget(lbl_logo)
        title_box = QVBoxLayout()
        title = QLabel("About CCUS Suitability Analysis")
        tf = title.font(); tf.setPointSize(22); tf.setBold(True); title.setFont(tf)
        sub = QLabel(
            "Desktop tool for onshore CO₂ mineralization screening — "
            "case studies Kirkenær & Rakkestad."
        )
        sub.setProperty("muted", True)
        sub.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(sub)
        h_row.addLayout(title_box, 1)
        layout.addLayout(h_row)

        layout.addWidget(self._about_heading("Developers"))
        layout.addWidget(self._about_text(
            "<ul style='margin:0;padding-left:18px;'>"
            "<li><b>Bahman Bohloli</b> — Project leader, NGI. "
            "Reviewer and scientific oversight.</li>"
            "<li><b>Guro Margit Bjørge</b> — Co-author of the project report, "
            "geological evaluation of Kirkenær and Rakkestad.</li>"
            "<li><b>Thea Nilsen Grell</b> — Co-author of the project report, "
            "geological evaluation of Kirkenær and Rakkestad.</li>"            
            "<li><b>João Rafael da Silveira</b> — Program author, frontend &amp; backend "
            "</ul>"
            "<p style='margin:8px 0 0 0;'>Client: <b>Carbon Centric</b> "
            "(contact: Fredrik Häger). Report nr. 20260408-01-R.</p>"
        ))

        layout.addWidget(self._about_heading("The math behind the analysis"))
        layout.addWidget(self._about_text(
            "<p><b>Storage capacity per polygon</b> — adapted from the IEA/IPCC "
            "volumetric method:</p>"
            "<p style='font-family:Consolas,monospace;background:#1e293b;color:#f8fafc;"
            "padding:10px 14px;border-radius:6px;'>"
            "V<sub>CO₂</sub>&nbsp;=&nbsp;A · h · φ · E · ρ<sub>CO₂</sub>"
            "</p>"
            "<ul style='margin:0;padding-left:18px;'>"
            "<li><b>A</b>&nbsp;— polygon area (m²), computed in a projected "
            "metric CRS from NGU BergartFlate_N250.</li>"
            "<li><b>h</b>&nbsp;— fracture-zone thickness, default 200 m "
            "(conservative estimate of accessible depth).</li>"
            "<li><b>φ</b>&nbsp;— effective porosity, default 1.5 %; replaced "
            "polygon-by-polygon when measured petrophysics are available.</li>"
            "<li><b>E</b>&nbsp;— storage efficiency, default 2 % (only a "
            "fraction of pore volume is reachable).</li>"
            "<li><b>ρ<sub>CO₂</sub></b>&nbsp;≈ 700 kg/m³ at supercritical "
            "conditions (~1 km depth, ~40 °C).</li>"
            "</ul>"
            "<p>Result is converted to megatonnes: M = V · ρ<sub>CO₂</sub> / 10⁹.</p>"

            "<p style='margin-top:12px;'><b>Weighted Linear Combination (WLC)</b> — "
            "user-tunable suitability score:</p>"
            "<p style='font-family:Consolas,monospace;background:#1e293b;color:#f8fafc;"
            "padding:10px 14px;border-radius:6px;'>"
            "S<sub>WLC</sub>&nbsp;=&nbsp;w<sub>R</sub>·R&nbsp;+&nbsp;"
            "w<sub>F</sub>·F&nbsp;+&nbsp;w<sub>S</sub>·S&nbsp;+&nbsp;"
            "w<sub>P</sub>·P"
            "</p>"
            "<p>where R = reservoir, F = fault/injectivity, S = structure, "
            "P = petrophysics; each normalized to [0, 1]. Weights default to "
            "45 / 30 / 15 / 10 % and are editable in the CCUS Dashboard. "
            "<b>Seal/cap rock is intentionally omitted</b> — in-situ "
            "mineralization locks the CO₂ chemically.</p>"

            "<p style='margin-top:12px;'><b>Analytic Hierarchy Process (AHP)</b> — "
            "alternative weight derivation:</p>"
            "<p>Pairwise importance comparisons between the four criteria "
            "form a reciprocal matrix; the principal eigenvector of that "
            "matrix gives consistent weights, which are then plugged back "
            "into the WLC formula above.</p>"

            "<p style='margin-top:12px;'><b>Boreholes</b> — display-only "
            "context. Yield (L/h) reflects permeability k, not porosity φ, "
            "and most NGU wells are shallower than the supercritical-CO₂ "
            "window (≥ 800 m), so they do not enter V<sub>CO₂</sub>.</p>"
        ))

        layout.addWidget(self._about_heading("What the program does"))
        layout.addWidget(self._about_text(
            "<ul style='margin:0;padding-left:18px;'>"
            "<li>Reads any Esri File Geodatabase (.gdb folder), defaults to "
            "NGU's BerggrunnN250.gdb at 1:250 000.</li>"
            "<li>Classifies polygons into rock families/sub-families and "
            "computes per-polygon area in km².</li>"
            "<li>Runs the capacity formula above and ranks polygons by "
            "storage mass in megatonnes.</li>"
            "<li>Lets you re-weight the WLC criteria in real time and "
            "re-applies the ranking with a single click.</li>"
            "<li>Renders four views over the same dataset: 3D terrain "
            "(PyVista), 2D interactive maps (Folium/Leaflet), plots "
            "(matplotlib), and a tabular Dashboard with the Top 5 and the "
            "full capacity breakdown.</li>"
            "<li>Loads NGU groundwater boreholes as a context layer on the "
            "capacity map, with per-polygon popups showing borehole count, "
            "mean yield, and depth.</li>"
            "<li>Generates Folium HTML on disk in the user's TEMP folder so "
            "the same map can be opened in a normal browser if needed.</li>"
            "<li>Runs entirely offline — the only external requests are "
            "CartoDB basemap tiles.</li>"
            "</ul>"
        ))

        layout.addWidget(self._about_heading("Stack"))
        layout.addWidget(self._about_text(
            "<p>Python 3.14 · PySide6 (Qt 6) · GeoPandas · Shapely · "
            "pyogrio · Folium / Leaflet · PyVista / VTK · "
            "matplotlib · rasterio. Native desktop, not a web app.</p>"
        ))
        layout.addStretch(1)

    def _about_heading(self, text):
        lbl = QLabel(text)
        f = lbl.font(); f.setPointSize(14); f.setBold(True); lbl.setFont(f)
        lbl.setContentsMargins(0, 12, 0, 4)
        return lbl

    def _about_text(self, html):
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setOpenExternalLinks(True)
        return lbl

    def open_about_page(self):
        self.stacked_widget.setCurrentIndex(2)

    # ------------------------------------------------------------------
    # 2D map fullscreen toggle
    # ------------------------------------------------------------------
    def _toggle_map_expanded(self, checked):
        """Hide tab bar + log console so the map widget gets the whole pane."""
        if not hasattr(self, "tabs"):
            return
        self.tabs.tabBar().setVisible(not checked)
        if hasattr(self, "log_console"):
            self.log_console.setVisible(not checked)
        # Trigger Leaflet to remeasure its container — without this, the map
        # tiles snap to the new size only after the next user pan/zoom.
        try:
            QTimer.singleShot(
                50,
                lambda: self.web_view.page().runJavaScript(
                    "for (var k in window){try{var v=window[k];"
                    "if(v && L && v instanceof L.Map) v.invalidateSize(true);"
                    "}catch(e){}}"
                ),
            )
        except Exception as exc:
            print(f"[Expand] could not invalidate map size: {exc}")
        self.btn_expand_map.setText("⛶ Restore" if checked else "⛶ Expand")

    # ------------------------------------------------------------------
    # Geodatabase loader
    # ------------------------------------------------------------------
    def _format_gdb_name(self):
        if not getattr(self, "gdb_path", None):
            return "No dataset loaded"
        return os.path.basename(self.gdb_path.rstrip(r"\/")) or self.gdb_path

    def _refresh_gdb_status_label(self):
        if not hasattr(self, "lbl_gdb_status"):
            return
        if os.path.exists(self.gdb_path):
            self.lbl_gdb_status.setText(f"Current dataset: {self._format_gdb_name()}")
        else:
            self.lbl_gdb_status.setText(
                f"⚠ Default dataset not found at {self.gdb_path}. "
                "Use 'Load Geodatabase' to select a .gdb folder."
            )

    def _is_valid_gdb(self, path):
        """An Esri File Geodatabase is a folder containing a gdb table file."""
        if not path or not os.path.isdir(path):
            return False
        try:
            entries = os.listdir(path)
        except OSError:
            return False
        # Esri FileGDBs always contain files named a*.gdbtable / gdbindexes / etc.
        return any(e.lower().endswith(".gdbtable") for e in entries)

    def choose_geodatabase(self):
        start_dir = self.gdb_path if os.path.exists(self.gdb_path) else os.getcwd()
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Esri File Geodatabase (.gdb folder)",
            start_dir,
            QFileDialog.ShowDirsOnly,
        )
        if not folder:
            return
        if not self._is_valid_gdb(folder):
            QMessageBox.warning(
                self,
                "Invalid geodatabase",
                f"'{folder}' does not look like an Esri File Geodatabase. "
                "Pick the .gdb folder itself (it should contain *.gdbtable files).",
            )
            return

        self.gdb_path = folder
        # Drop cached analysis results — they belong to the previous dataset.
        self.current_results = None
        self.current_scored_results = None
        self.current_top5 = None
        self.current_faults = None
        self.current_mpts = None
        self.current_petro_df = None
        self.current_boreholes = None
        self.base_gdf = None
        self._last_loaded_map = None

        if hasattr(self, "lbl_gdb_name_tb"):
            self.lbl_gdb_name_tb.setText(self._format_gdb_name())
        self._refresh_gdb_status_label()
        if hasattr(self, "lbl_status"):
            self.lbl_status.setText(
                f"Loaded {self._format_gdb_name()}. Click Run Analysis."
            )
        # Refresh the bedrock map so the user immediately sees their data.
        if hasattr(self, "combo_2d"):
            try:
                self.load_2d_map(self.combo_2d.currentText())
            except Exception as e:
                print(f"[GDB] could not preview new dataset: {e}")
        print(f"[GDB] switched to {folder}")

    def _apply_translations(self):
        self.setWindowTitle(translator.tr("app_title"))
        self.lbl_brand.setText(translator.tr("app_title"))
        self.btn_settings.setText("⚙️ " + translator.tr("settings"))
        self.btn_run.setText("▶ " + translator.tr("run_analysis"))
        self.tabs.setTabText(1, translator.tr("tab_3d"))
        self.tabs.setTabText(2, translator.tr("tab_2d"))
        self.tabs.setTabText(3, translator.tr("tab_plots"))

    def _apply_theme(self):
        self.setStyleSheet(theme_manager.get_stylesheet())
        if theme_manager.current_theme == "dark":
            self.plotter.set_background("#09090b")
        else:
            self.plotter.set_background("#ffffff")

    def run_analysis(self):
        if not os.path.exists(self.gdb_path):
            QMessageBox.critical(self, translator.tr("error"), f"Geodatabase not found: {self.gdb_path}")
            return
        self.btn_run.setEnabled(False)
        self.lbl_status.setText(translator.tr("status_running"))
        poro_val = self.fracture_porosity_pct / 100.0
        self.worker = AnalysisWorker(self.gdb_path, poro_val)
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.error.connect(self.on_analysis_error)
        self.worker.start()

    def on_analysis_finished(self, gdf_res, faults, mpts, petro_df):
        self.current_results = gdf_res
        self.current_faults = faults
        self.current_mpts = mpts
        self.current_petro_df = petro_df
        self.base_gdf = gdf_res

        self.recalculate_wlc(refresh_map=False)
        self.update_capacity_table()
        self.update_3d(gdf_res)
        self.load_2d_map(self.combo_2d.currentText())

        self.btn_run.setEnabled(True)
        self.lbl_status.setText(translator.tr("status_done"))

    def on_analysis_error(self, err_msg):
        QMessageBox.critical(self, translator.tr("error"), err_msg)
        self.btn_run.setEnabled(True)
        self.lbl_status.setText(translator.tr("status_ready"))

    def update_capacity_table(self):
        source = self.current_scored_results if self.current_scored_results is not None else self.current_results
        if source is None:
            return

        ranked = source.copy()
        if "storage_density_Mt_km2" not in ranked.columns and "area_km2" in ranked.columns:
            safe_area = pd.to_numeric(ranked["area_km2"], errors="coerce").replace(0, np.nan)
            ranked["storage_density_Mt_km2"] = ranked["storage_mass_Mt"] / safe_area
        ranked = ranked.sort_values("storage_mass_Mt", ascending=False).head(20)
        max_cap = ranked["storage_mass_Mt"].max() if not ranked.empty else 1
        if pd.isna(max_cap) or max_cap == 0:
            max_cap = 1

        self.table_cap.setRowCount(len(ranked))
        for i, (_, row) in enumerate(ranked.iterrows()):
            self.table_cap.setItem(i, 0, QTableWidgetItem(str(row.get("shape_id", ""))))
            self.table_cap.setItem(i, 1, QTableWidgetItem(str(row.get("family", "")).split(" - ")[0]))
            self.table_cap.setItem(i, 2, QTableWidgetItem(f"{row.get('area_km2', 0):.2f}"))
            self.table_cap.setItem(i, 3, QTableWidgetItem(str(ca.FRACTURE_THICKNESS_M)))
            poro = row.get("effective_porosity", row.get("porosity", ca.FRACTURE_POROSITY))
            if pd.isna(poro):
                poro = ca.FRACTURE_POROSITY
            self.table_cap.setItem(i, 4, QTableWidgetItem(f"{poro:.3f}"))

            cap = row.get("storage_mass_Mt", 0)
            if pd.isna(cap):
                cap = 0
            cap_widget = QWidget()
            cap_layout = QHBoxLayout(cap_widget)
            cap_layout.setContentsMargins(4, 4, 4, 4)
            pb = QProgressBar()
            pb.setRange(0, int(max_cap * 100))
            pb.setValue(int(cap * 100))
            lbl = QLabel(f"{cap:.2f}")
            lbl.setProperty("title", True)
            cap_layout.addWidget(pb)
            cap_layout.addWidget(lbl)
            self.table_cap.setCellWidget(i, 5, cap_widget)

            dens = row.get("storage_density_Mt_km2", np.nan)
            self.table_cap.setItem(i, 6, QTableWidgetItem(f"{dens:.4f}" if not pd.isna(dens) else "N/A"))

    def update_3d(self, gdf_res):
        try:
            self.plotter.clear()
            exag = self.vertical_exag
            create_3d_viewer(gdf_res, gdb_path=self.gdb_path, vert_exag=exag, plotter=self.plotter)
        except Exception as e:
            QMessageBox.warning(self, translator.tr("error"), f"Failed to update 3D view: {str(e)}")

    def _load_base_gdf(self):
        if self.base_gdf is not None:
            return self.base_gdf
        if not os.path.exists(self.gdb_path):
            raise FileNotFoundError(f"Geodatabase not found: {self.gdb_path}")
        print(f"Loading base bedrock polygons for interactive map: {self.gdb_path}")
        gdf = gpd.read_file(self.gdb_path, layer="BergartFlate_N250")
        self.base_gdf = prepare_bedrock_gdf(gdf)
        return self.base_gdf

    def _ensure_boreholes_loaded(self):
        """Read NGU Grunnvannsborehull layers once, filtered to the study area.

        Boreholes are display-only context (count + mean yield + depth in the
        polygon popups, plus toggleable point layers). They are NOT part of the
        V = A·h·φ·E·ρ_CO2 capacity formula — yield measures permeability k, not
        porosity φ, and most wells are too shallow (<=300 m) to sample the
        supercritical-CO₂ depth window (>=800 m).
        """
        if self.current_boreholes is not None:
            return self.current_boreholes
        if not hasattr(ca, "load_groundwater_boreholes"):
            print("[Boreholes] ccus_analysis.load_groundwater_boreholes not available")
            self.current_boreholes = {}
            return self.current_boreholes
        if not os.path.exists(self.boreholes_gdb_path):
            print(f"[Boreholes] Not found: {self.boreholes_gdb_path}")
            self.current_boreholes = {}
            return self.current_boreholes
        try:
            base_gdf = self._load_base_gdf()
            study_bounds = tuple(base_gdf.to_crs(epsg=4326).total_bounds)
            print(f"[Boreholes] Loading from {self.boreholes_gdb_path} "
                  f"(study bounds={study_bounds})…")
            self.current_boreholes = ca.load_groundwater_boreholes(
                self.boreholes_gdb_path, study_bounds=study_bounds,
            )
            counts = ", ".join(
                f"{k}={len(v)}" for k, v in (self.current_boreholes or {}).items()
            ) or "(none)"
            print(f"[Boreholes] loaded: {counts}")
        except Exception as exc:
            print(f"[Boreholes] FAILED to load: {exc}")
            self.current_boreholes = {}
        return self.current_boreholes

    def create_bedrock_family_map(self):
        """Notebook-style interactive bedrock map with real basemap, layers and popups."""
        gdf = self._load_base_gdf()
        gdf_wgs = gdf.to_crs(epsg=4326)

        bounds = gdf_wgs.total_bounds
        center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
        m = folium.Map(location=center, zoom_start=10, tiles="CartoDB positron")

        # Family/subfamily layers, matching the notebook default map.
        families = sorted(gdf["family"].dropna().unique())
        for fam in families:
            fam_gdf = gdf_wgs[gdf_wgs["family"] == fam]
            subfams = sorted(fam_gdf["subfamily"].dropna().unique())
            for sub in subfams:
                sub_gdf = fam_gdf[fam_gdf["subfamily"] == sub]
                sub_area = gdf.loc[sub_gdf.index, "area_km2"].sum()
                layer_name = f"{fam} | {sub} ({sub_area:.2f} km²)"
                fg = folium.FeatureGroup(name=layer_name, show=True)
                for idx, row in sub_gdf.iterrows():
                    color = cmyk_to_hex(row.get("cmykFargekode", None))
                    akm = gdf.loc[idx, "area_km2"]
                    sid = gdf.loc[idx, "shape_id"]
                    popup_html = (
                        f"<b>{sid}</b><br>"
                        f"<b>Family:</b> {fam}<br>"
                        f"<b>Subfamily:</b> {sub}<br>"
                        f"<b>Area:</b> {akm:.4f} km²<br>"
                        f"{row.get('tegnforklaring', '')}"
                    )
                    folium.GeoJson(
                        row.geometry.__geo_interface__,
                        style_function=lambda x, c=color: {
                            "fillColor": c, "color": "#333", "weight": 1, "fillOpacity": 0.7,
                        },
                        popup=folium.Popup(popup_html, max_width=300),
                    ).add_to(fg)
                fg.add_to(m)

        # Borders layer from the same NGU GDB, as in the notebook.
        try:
            borders = gpd.read_file(self.gdb_path, layer="BergartGrense_N250")
            for col in borders.select_dtypes(include=["datetime", "datetimetz"]).columns:
                borders[col] = borders[col].astype(str)
            borders_wgs = borders.to_crs(epsg=4326)
            fg_b = folium.FeatureGroup(name="-- Borders", show=True)
            for _, row in borders_wgs.iterrows():
                folium.GeoJson(
                    row.geometry.__geo_interface__,
                    style_function=lambda x: {"color": "black", "weight": 1.2, "opacity": 0.5},
                ).add_to(fg_b)
            fg_b.add_to(m)
        except Exception as e:
            print(f"Could not load BergartGrense_N250 borders: {e}")

        folium.LayerControl(collapsed=False).add_to(m)
        return m

    def create_area_calculator_map(self):
        """Notebook-style area calculator map.

        This restores the behavior from the default notebook: each shape can be
        toggled on/off from a left panel, the selected area is recalculated, and
        clicking a polygon opens a popup with shape information.
        """
        gdf = self._load_base_gdf()
        gdf_wgs = gdf.to_crs(epsg=4326)
        bounds = gdf_wgs.total_bounds
        center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
        m = folium.Map(location=center, zoom_start=10, tiles="CartoDB positron")

        js_data = {}
        for idx, row in gdf.iterrows():
            fam = str(row.get("family", "Unknown"))
            sub = str(row.get("subfamily", fam))
            sid = str(row.get("shape_id", idx))
            akm = round(float(row.get("area_km2", 0.0)), 4)
            js_data.setdefault(fam, {}).setdefault(sub, {})[sid] = akm

        total_study_area = round(float(gdf["area_km2"].sum()), 4)
        layer_js_map = {}
        map_js_name = m.get_name()

        for idx, row in gdf_wgs.iterrows():
            sid = str(gdf.loc[idx, "shape_id"])
            fam = str(gdf.loc[idx, "family"])
            sub = str(gdf.loc[idx, "subfamily"])
            akm = float(gdf.loc[idx, "area_km2"])
            color = cmyk_to_hex(row.get("cmykFargekode", None))

            fg = folium.FeatureGroup(name=sid, show=True)
            folium.GeoJson(
                row.geometry.__geo_interface__,
                style_function=lambda x, c=color: {
                    "fillColor": c, "color": "#333", "weight": 1.5, "fillOpacity": 0.7,
                },
                popup=folium.Popup(
                    f"<b>{sid}</b><br>Family: {fam}<br>Subfamily: {sub}<br>Area: {akm:.4f} km²",
                    max_width=300,
                ),
            ).add_to(fg)

            centroid = row.geometry.centroid
            folium.Marker(
                location=[centroid.y, centroid.x],
                icon=folium.DivIcon(
                    html=(
                        '<div style="font-size:9px;font-weight:bold;background:rgba(0,0,0,0.7);'
                        'color:white;padding:1px 4px;border-radius:3px;white-space:nowrap;">'
                        f'{sid}</div>'
                    ),
                    icon_size=(100, 20), icon_anchor=(50, 10),
                ),
            ).add_to(fg)
            fg.add_to(m)
            layer_js_map[sid] = fg.get_name()

        layer_map_json = json.dumps(layer_js_map, ensure_ascii=False)
        js_data_json = json.dumps(js_data, ensure_ascii=False)

        calc_html = """
<div id="calc-panel" style="
    position:fixed; top:10px; left:10px; z-index:9999;
    background:white; color:#111; padding:12px; border-radius:8px;
    box-shadow:0 2px 12px rgba(0,0,0,0.35); font-family:Arial; font-size:12px;
    width:360px; max-height:90vh; overflow-y:auto;">
    <div style="font-size:16px;font-weight:bold;margin-bottom:8px;">Area Calculator + Layer Control</div>
    <div style="background:#f5f5f5;padding:8px;border-radius:4px;margin-bottom:10px;">
        <b>Total Study Area: """ + str(total_study_area) + """ km²</b>
    </div>
    <button onclick="selAll()" style="margin:2px;padding:4px 10px;cursor:pointer;font-size:11px;background:#4CAF50;color:white;border:none;border-radius:3px;">Select All</button>
    <button onclick="clrAll()" style="margin:2px;padding:4px 10px;cursor:pointer;font-size:11px;background:#f44336;color:white;border:none;border-radius:3px;">Clear All</button>
    <hr style="margin:8px 0;">
    <div id="tree"></div>
    <hr style="margin:8px 0;">
    <div id="calc-result" style="font-size:13px;font-weight:bold;padding:10px;background:#fffde7;border-radius:4px;"></div>
</div>
<script>
var DATA = """ + js_data_json + """;
var TOTAL = """ + str(total_study_area) + """;
var LAYER_MAP_NAMES = """ + layer_map_json + """;
var LAYERS = {};
for (var sid in LAYER_MAP_NAMES) { LAYERS[sid] = window[LAYER_MAP_NAMES[sid]]; }
var MAP = window[""" + map_js_name + """"];

var tree = document.getElementById('tree');
Object.keys(DATA).sort().forEach(function(fam) {
    var famDiv = document.createElement('div');
    famDiv.style.margin = '6px 0 2px 0';
    var famArea = 0;
    var subs = DATA[fam];
    Object.keys(subs).forEach(function(sub) {
        Object.values(subs[sub]).forEach(function(a) { famArea += a; });
    });
    famDiv.innerHTML = '<label style="font-weight:bold;font-size:13px;cursor:pointer;"><input type="checkbox" checked class="fam-cb" data-fam="' + fam + '"> ' + fam + ' <span style="color:#666;font-weight:normal;">(' + famArea.toFixed(2) + ' km²)</span></label>';
    tree.appendChild(famDiv);
    Object.keys(subs).sort().forEach(function(sub) {
        var subDiv = document.createElement('div');
        subDiv.style.margin = '2px 0 1px 18px';
        var subArea = Object.values(subs[sub]).reduce(function(a,b){return a+b}, 0);
        subDiv.innerHTML = '<label style="font-size:12px;cursor:pointer;"><input type="checkbox" checked class="sub-cb" data-fam="' + fam + '" data-sub="' + sub + '"> ' + sub + ' <span style="color:#888;">(' + subArea.toFixed(2) + ' km²)</span></label>';
        tree.appendChild(subDiv);
        var shapes = subs[sub];
        Object.keys(shapes).sort().forEach(function(sid) {
            var shpDiv = document.createElement('div');
            shpDiv.style.margin = '1px 0 0 36px';
            shpDiv.style.fontSize = '11px';
            shpDiv.innerHTML = '<label style="cursor:pointer;"><input type="checkbox" checked class="shp-cb" data-fam="' + fam + '" data-sub="' + sub + '" data-sid="' + sid + '"> ' + sid + ': ' + shapes[sid].toFixed(4) + ' km²</label>';
            tree.appendChild(shpDiv);
        });
    });
});

function toggleLayer(sid, show) {
    var layer = LAYERS[sid];
    if (!layer || !MAP) return;
    if (show) {
        if (!MAP.hasLayer(layer)) MAP.addLayer(layer);
    } else {
        if (MAP.hasLayer(layer)) MAP.removeLayer(layer);
    }
}

document.addEventListener('change', function(e) {
    if (e.target.classList.contains('fam-cb')) {
        var fam = e.target.dataset.fam; var ch = e.target.checked;
        document.querySelectorAll('.sub-cb[data-fam="'+fam+'"]').forEach(function(cb){ cb.checked=ch; });
        document.querySelectorAll('.shp-cb[data-fam="'+fam+'"]').forEach(function(cb){ cb.checked=ch; toggleLayer(cb.dataset.sid, ch); });
        update();
    }
    if (e.target.classList.contains('sub-cb')) {
        var fam = e.target.dataset.fam; var sub = e.target.dataset.sub; var ch = e.target.checked;
        document.querySelectorAll('.shp-cb[data-fam="'+fam+'"][data-sub="'+sub+'"]').forEach(function(cb){ cb.checked=ch; toggleLayer(cb.dataset.sid, ch); });
        update();
    }
    if (e.target.classList.contains('shp-cb')) {
        toggleLayer(e.target.dataset.sid, e.target.checked);
        update();
    }
});

function selAll() {
    document.querySelectorAll('.fam-cb,.sub-cb,.shp-cb').forEach(function(cb){ cb.checked=true; if(cb.dataset.sid) toggleLayer(cb.dataset.sid, true); });
    update();
}
function clrAll() {
    document.querySelectorAll('.fam-cb,.sub-cb,.shp-cb').forEach(function(cb){ cb.checked=false; if(cb.dataset.sid) toggleLayer(cb.dataset.sid, false); });
    update();
}
function update() {
    var total=0, count=0, allCount=0, famTotals={};
    document.querySelectorAll('.shp-cb').forEach(function(cb) {
        allCount++;
        var fam=cb.dataset.fam;
        if(!famTotals[fam]) famTotals[fam]=0;
        if(cb.checked) {
            var a=DATA[fam][cb.dataset.sub][cb.dataset.sid];
            total+=a; famTotals[fam]+=a; count++;
        }
    });
    var html='<b>Selected:</b> '+count+'/'+allCount+' shapes<br>';
    html+='<b>Selected Area:</b> <span style="color:#8B4513;font-size:16px;">'+total.toFixed(4)+' km²</span><br>';
    html+='<b>% of Study Area:</b> '+(total/TOTAL*100).toFixed(1)+'%';
    html+='<hr style="margin:6px 0;">';
    html+='<b>By Family:</b><br>';
    Object.keys(famTotals).sort().forEach(function(fam) {
        if(famTotals[fam]>0) html+='<span style="font-size:11px;">'+fam+': '+famTotals[fam].toFixed(4)+' km² ('+(famTotals[fam]/TOTAL*100).toFixed(1)+'%)</span><br>';
    });
    document.getElementById('calc-result').innerHTML=html;
}
update();
</script>
"""
        m.get_root().html.add_child(folium.Element(calc_html))
        return m

    def load_2d_map(self, map_name):
        try:
            print(f"2D interactive map selected: {map_name}")
            if map_name == "Bedrock Family Map":
                fmap = self.create_bedrock_family_map()
            elif map_name == "CCUS WLC Map":
                if self.current_results is None:
                    self._show_map_message("Run analysis first", "The WLC map needs the CCUS analysis result. Click Run Analysis first.")
                    return
                if self.current_scored_results is None or self.current_top5 is None:
                    self.recalculate_wlc(refresh_map=False)
                fmap = ca.create_map(
                    self.current_scored_results,
                    self.current_faults,
                    self.current_mpts,
                    self.current_top5,
                    self.current_petro_df,
                )
            elif map_name == "CCUS AHP Map":
                if self.current_results is None:
                    self._show_map_message("Run analysis first", "The AHP map needs the CCUS analysis result. Click Run Analysis first.")
                    return
                gdf_ahp, top5_ahp = ca.apply_ahp(self.current_results)
                fmap = ca.create_map(gdf_ahp, self.current_faults, self.current_mpts, top5_ahp, self.current_petro_df)
            elif map_name == "CCUS Capacity Map":
                if self.current_results is None:
                    self._show_map_message("Run analysis first", "The capacity map needs the CCUS analysis result. Click Run Analysis first.")
                    return
                source = self.current_scored_results if self.current_scored_results is not None else self.current_results
                try:
                    gdf_cap, top5_cap = ca.apply_capacity_ranking(source, ranking_mode="fair")
                except TypeError:
                    gdf_cap, top5_cap = ca.apply_capacity_ranking(source)
                # Boreholes are display-only context layers; load lazily so the
                # rest of the analysis stays fast on first run.
                self._ensure_boreholes_loaded()
                if hasattr(ca, "create_capacity_heatmap"):
                    fmap = ca.create_capacity_heatmap(
                        gdf_cap, top5_cap, self.current_petro_df,
                        boreholes=self.current_boreholes,
                    )
                else:
                    fmap = ca.create_map(gdf_cap, self.current_faults, self.current_mpts, top5_cap, self.current_petro_df)
            else:
                self._show_map_message("Unknown map", map_name)
                return

            self._render_folium_in_webview(fmap, label=map_name)
            self._last_loaded_map = map_name
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._show_map_message("2D map error", str(e))

    def load_plot(self, plot_name):
        plot_files = {
            "DEM Bedrock Overview": "dem_bedrock_overview.png",
            "Elevation By Family": "elevation_by_family.png",
        }
        filename = plot_files.get(plot_name)
        if filename:
            path = os.path.abspath(os.path.join("output", "figures", filename))
            if os.path.exists(path):
                pixmap = QPixmap(path)
                self.lbl_plot.setPixmap(pixmap.scaled(
                    max(800, self.scroll_plot.width() - 20),
                    max(600, self.scroll_plot.height() - 20),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                ))
            else:
                self.lbl_plot.setText(f"Plot not found: {path}")
