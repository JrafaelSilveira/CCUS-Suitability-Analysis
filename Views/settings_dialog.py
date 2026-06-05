from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QDoubleSpinBox, QGroupBox
)
from Views.translator import translator
from Views.theme_manager import theme_manager


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._main = parent  # MainWindow — used to read/write analysis params
        self.setWindowTitle(translator.tr("settings"))
        self.setMinimumWidth(360)
        self._setup_ui()
        self._apply_theme()

        translator.language_changed.connect(self._retranslate)
        theme_manager.theme_changed.connect(self._apply_theme)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Appearance / locale ----------------------------------------------
        appearance_box = QGroupBox("Appearance")
        appearance_form = QFormLayout(appearance_box)

        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["en", "no"])
        self.combo_lang.setCurrentText(translator.current_lang)
        self.combo_lang.currentTextChanged.connect(translator.set_language)

        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["dark", "light"])
        self.combo_theme.setCurrentText(theme_manager.current_theme)
        self.combo_theme.currentTextChanged.connect(theme_manager.set_theme)

        self.lbl_lang = QLabel(translator.tr("language"))
        self.lbl_theme = QLabel(translator.tr("theme"))
        appearance_form.addRow(self.lbl_lang, self.combo_lang)
        appearance_form.addRow(self.lbl_theme, self.combo_theme)
        layout.addWidget(appearance_box)

        # Analysis parameters (moved out of the main window's left panel) ---
        analysis_box = QGroupBox("Analysis parameters")
        analysis_form = QFormLayout(analysis_box)

        self.spin_poro = QDoubleSpinBox()
        self.spin_poro.setRange(0.1, 10.0)
        self.spin_poro.setSuffix(" %")
        self.spin_poro.setSingleStep(0.1)
        self.spin_poro.setToolTip(
            "Fracture porosity (φ) used in the storage-capacity formula "
            "V = A·h·φ·E·ρ_CO₂. Applied when the next 'Run Analysis' starts."
        )

        self.spin_exag = QDoubleSpinBox()
        self.spin_exag.setRange(1.0, 10.0)
        self.spin_exag.setSingleStep(0.5)
        self.spin_exag.setToolTip(
            "Vertical exaggeration for the 3D Map Viewer (does not affect "
            "any quantitative result)."
        )

        # Seed from the MainWindow so changes round-trip.
        if self._main is not None:
            self.spin_poro.setValue(
                getattr(self._main, "fracture_porosity_pct", 1.5)
            )
            self.spin_exag.setValue(
                getattr(self._main, "vertical_exag", 3.0)
            )

        analysis_form.addRow(QLabel("Fracture porosity (φ)"), self.spin_poro)
        analysis_form.addRow(QLabel("Vertical exaggeration (3D)"), self.spin_exag)
        layout.addWidget(analysis_box)

        # Buttons ----------------------------------------------------------
        btn_layout = QHBoxLayout()
        self.btn_close = QPushButton(translator.tr("save"))
        self.btn_close.clicked.connect(self._save_and_close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

    def _save_and_close(self):
        if self._main is not None:
            self._main.fracture_porosity_pct = float(self.spin_poro.value())
            self._main.vertical_exag = float(self.spin_exag.value())
        self.accept()

    def _retranslate(self):
        self.setWindowTitle(translator.tr("settings"))
        self.lbl_lang.setText(translator.tr("language"))
        self.lbl_theme.setText(translator.tr("theme"))
        self.btn_close.setText(translator.tr("save"))

    def _apply_theme(self):
        self.setStyleSheet(theme_manager.get_stylesheet())
