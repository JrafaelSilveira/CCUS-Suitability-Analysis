from PySide6.QtCore import QObject, Signal, QSettings

class ThemeManager(QObject):
    theme_changed = Signal()

    DARK_THEME = {
        'bg_primary': '#09090b',       # Very dark background (Shadcn dark)
        'bg_secondary': '#18181b',     # Card background
        'bg_tertiary': '#27272a',      # Hover / muted
        'text_primary': '#fafafa',
        'text_secondary': '#a1a1aa',   # Muted foreground
        'accent': '#ffffff',           # Primary action (white on dark)
        'accent_text': '#09090b',      # Text on primary action
        'border': '#27272a',
    }

    LIGHT_THEME = {
        'bg_primary': '#ffffff',       # White background
        'bg_secondary': '#ffffff',     # Card background
        'bg_tertiary': '#f4f4f5',      # Hover / muted
        'text_primary': '#09090b',
        'text_secondary': '#71717a',   # Muted foreground
        'accent': '#18181b',           # Primary action (black on light)
        'accent_text': '#fafafa',      # Text on primary action
        'border': '#e4e4e7',
    }

    def __init__(self):
        super().__init__()
        self.settings = QSettings("NGI", "CCUSApp")
        self.current_theme = self.settings.value("theme", "dark")

    def get_theme(self) -> dict:
        return self.DARK_THEME if self.current_theme == "dark" else self.LIGHT_THEME

    def set_theme(self, theme_name: str):
        self.current_theme = theme_name
        self.settings.setValue("theme", theme_name)
        self.theme_changed.emit()

    def get_stylesheet(self) -> str:
        t = self.get_theme()
        return f"""
        QMainWindow, QWidget {{ background-color: {t['bg_primary']}; color: {t['text_primary']}; font-family: "Segoe UI", sans-serif; font-size: 13px; }}
        
        QLabel {{ color: {t['text_primary']}; background: transparent; }}
        QLabel[muted="true"] {{ color: {t['text_secondary']}; font-size: 12px; }}
        QLabel[title="true"] {{ font-size: 16px; font-weight: bold; }}
        
        QPushButton {{ background-color: {t['bg_primary']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 6px 16px; color: {t['text_primary']}; font-weight: 500; }}
        QPushButton:hover {{ background-color: {t['bg_tertiary']}; }}
        
        QPushButton[hero="true"] {{ background-color: {t['accent']}; border: none; color: {t['accent_text']}; font-weight: 500; }}
        QPushButton[hero="true"]:hover {{ background-color: {t['text_secondary']}; }}
        
        QPushButton[ghost="true"] {{ background-color: transparent; border: none; color: {t['text_secondary']}; }}
        QPushButton[ghost="true"]:hover {{ background-color: {t['bg_tertiary']}; color: {t['text_primary']}; }}
        
        QGroupBox {{ background-color: {t['bg_secondary']}; border: 1px solid {t['border']}; border-radius: 8px; margin-top: 14px; padding-top: 10px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 16px; padding: 0 4px; color: {t['text_primary']}; font-weight: 600; font-size: 14px; top: -8px; background-color: {t['bg_primary']}; }}
        
        QTableWidget {{ background-color: {t['bg_secondary']}; border: 1px solid {t['border']}; border-radius: 8px; gridline-color: {t['bg_tertiary']}; color: {t['text_primary']}; }}
        QTableWidget::item {{ padding: 8px; border-bottom: 1px solid {t['border']}; }}
        QHeaderView::section {{ background-color: {t['bg_primary']}; color: {t['text_secondary']}; padding: 8px 12px; border: none; border-bottom: 1px solid {t['border']}; font-weight: 600; text-transform: uppercase; font-size: 11px; }}
        
        QDoubleSpinBox, QComboBox {{ background-color: {t['bg_primary']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 6px 12px; color: {t['text_primary']}; }}
        
        QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 8px; background-color: {t['bg_secondary']}; }}
        QTabBar::tab {{ background-color: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 16px; color: {t['text_secondary']}; font-weight: 500; margin-right: 4px; }}
        QTabBar::tab:selected {{ color: {t['text_primary']}; border-bottom: 2px solid {t['text_primary']}; }}
        QTabBar::tab:hover:!selected {{ color: {t['text_primary']}; }}
        
        QDialog {{ background-color: {t['bg_primary']}; color: {t['text_primary']}; border: 1px solid {t['border']}; border-radius: 8px; }}
        
        QSlider::groove:horizontal {{ border: 1px solid {t['bg_tertiary']}; height: 6px; background: {t['bg_tertiary']}; border-radius: 3px; }}
        QSlider::handle:horizontal {{ background: {t['accent']}; border: 1px solid {t['border']}; width: 16px; margin: -5px 0; border-radius: 8px; }}
        QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 3px; }}
        
        QProgressBar {{ border: none; border-radius: 4px; background-color: {t['bg_tertiary']}; text-align: center; color: transparent; max-height: 8px; }}
        QProgressBar::chunk {{ background-color: {t['accent']}; border-radius: 4px; }}
        """

theme_manager = ThemeManager()
