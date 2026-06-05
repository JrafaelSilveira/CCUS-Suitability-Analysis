from PySide6.QtCore import QObject, Signal, QSettings

TRANSLATIONS = {
    "en": {
        "app_title": "CCUS Suitability Analysis",
        "settings": "Settings",
        "language": "Language:",
        "theme": "Theme:",
        "dark": "Dark",
        "light": "Light",
        "config": "Configuration",
        "fracture_poro": "Fracture Porosity (%)",
        "vert_exag": "Vertical Exag. (3D)",
        "run_analysis": "Run Analysis",
        "results": "Results",
        "tab_table": "Summary Table",
        "tab_3d": "3D Map Viewer",
        "tab_2d": "2D Interactive Maps",
        "tab_plots": "Analysis Plots",
        "col_family": "Rock Family",
        "col_role": "CCUS Role",
        "col_area": "Area (km²)",
        "col_capacity": "Capacity (Mt CO₂)",
        "status_ready": "Ready.",
        "status_running": "Running CCUS analysis... please wait.",
        "status_done": "Analysis completed.",
        "error": "Error",
        "save": "Save",
        "cancel": "Cancel",
    },
    "no": {
        "app_title": "CCUS Egnethetsanalyse",
        "settings": "Innstillinger",
        "language": "Språk:",
        "theme": "Tema:",
        "dark": "Mørk",
        "light": "Lys",
        "config": "Konfigurasjon",
        "fracture_poro": "Sprekkeporøsitet (%)",
        "vert_exag": "Vertikal Forvrengning (3D)",
        "run_analysis": "Kjør Analyse",
        "results": "Resultater",
        "tab_table": "Oppsummeringstabell",
        "tab_3d": "3D Kartvisning",
        "tab_2d": "2D Interaktive Kart",
        "tab_plots": "Analyseplott",
        "col_family": "Bergartsfamilie",
        "col_role": "CCUS Rolle",
        "col_area": "Areal (km²)",
        "col_capacity": "Kapasitet (Mt CO₂)",
        "status_ready": "Klar.",
        "status_running": "Kjører CCUS-analyse... vennligst vent.",
        "status_done": "Analyse fullført.",
        "error": "Feil",
        "save": "Lagre",
        "cancel": "Avbryt",
    }
}

class Translator(QObject):
    language_changed = Signal()

    def __init__(self):
        super().__init__()
        self.settings = QSettings("NGI", "CCUSApp")
        self.current_lang = self.settings.value("language", "en")

    def tr(self, key: str) -> str:
        return TRANSLATIONS.get(self.current_lang, TRANSLATIONS["en"]).get(key, key)

    def set_language(self, lang: str):
        if lang in TRANSLATIONS:
            self.current_lang = lang
            self.settings.setValue("language", lang)
            self.language_changed.emit()

translator = Translator()
