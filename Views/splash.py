"""Custom splash screen for the CCUS desktop app.

A dark, centered card showing:
  - The program logo (logo.png)
  - The app title and subtitle
  - An indeterminate loading bar
  - 'Developed by NGI' + ngi.png credit

This replaces the bare `QSplashScreen(pixmap)` from earlier builds so the
program brand (logo.png) is front and centre and the NGI institutional logo
appears as a small credit, not as a co-brand.
"""

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QFrame
)


class CCUSSplash(QWidget):
    def __init__(self, assets_dir="assets"):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(520, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # The card itself — gives the splash its rounded dark surface.
        card = QFrame(self)
        card.setObjectName("splashCard")
        card.setStyleSheet(
            """
            #splashCard {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 16px;
            }
            QLabel#splashTitle { color: #f8fafc; }
            QLabel#splashSub   { color: #94a3b8; }
            QLabel#splashCredit { color: #64748b; }
            QProgressBar {
                background-color: #1e293b;
                border: none;
                border-radius: 4px;
                height: 6px;
            }
            QProgressBar::chunk {
                background-color: #38bdf8;
                border-radius: 4px;
            }
            """
        )
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 32, 36, 28)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignCenter)

        # Program logo — primary brand.
        self.lbl_logo = QLabel()
        self.lbl_logo.setAlignment(Qt.AlignCenter)
        logo = QPixmap(os.path.join(assets_dir, "logo.png"))
        if not logo.isNull():
            self.lbl_logo.setPixmap(
                logo.scaledToHeight(200, Qt.SmoothTransformation)
            )
        layout.addWidget(self.lbl_logo)

        # Title
        self.lbl_title = QLabel("CCUS Suitability Analysis")
        self.lbl_title.setObjectName("splashTitle")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        tf = QFont("Segoe UI", 16)
        tf.setBold(True)
        self.lbl_title.setFont(tf)
        layout.addWidget(self.lbl_title)

        # Subtitle anchored to the actual project scope.
        self.lbl_sub = QLabel("Onshore CO₂ mineralization screening")
        self.lbl_sub.setObjectName("splashSub")
        self.lbl_sub.setAlignment(Qt.AlignCenter)
        sf = QFont("Segoe UI", 10)
        self.lbl_sub.setFont(sf)
        layout.addWidget(self.lbl_sub)

        layout.addSpacing(4)

        # Indeterminate progress bar — a real signal that the app is alive.
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)

        # Status line updated by update_status() during boot.
        self.lbl_status = QLabel("Starting…")
        self.lbl_status.setObjectName("splashSub")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setFont(QFont("Segoe UI", 9))
        layout.addWidget(self.lbl_status)

        layout.addStretch(1)

        # NGI credit — small, bottom of card.
        credit_row = QHBoxLayout()
        credit_row.setAlignment(Qt.AlignCenter)
        credit_row.setSpacing(6)
        lbl_dev = QLabel("Developed by")
        lbl_dev.setObjectName("splashCredit")
        lbl_dev.setFont(QFont("Segoe UI", 9))
        credit_row.addWidget(lbl_dev)
        ngi_lbl = QLabel()
        ngi_pix = QPixmap(os.path.join(assets_dir, "ngi.png"))
        if not ngi_pix.isNull():
            ngi_lbl.setPixmap(ngi_pix.scaledToHeight(28, Qt.SmoothTransformation))
        credit_row.addWidget(ngi_lbl)
        layout.addLayout(credit_row)

    def update_status(self, text):
        self.lbl_status.setText(text)
        # Pump the event loop so the new label paints immediately even though
        # the main thread is busy importing heavy libs.
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

    def finish(self, main_window):
        self.close()
        main_window.activateWindow()
        main_window.raise_()
