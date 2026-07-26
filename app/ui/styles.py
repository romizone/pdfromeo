"""Sejda-inspired stylesheet for PdfRomeo.

A clean, light, user-friendly look:
  * white canvas, soft grays for surfaces
  * single subtle accent (calm blue)
  * generous spacing and rounded corners
  * minimal borders, soft shadows via background tints
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG_BASE       = "#ffffff"   # main canvas
BG_PANEL      = "#f7f8fa"   # subtle surface
BG_RAISED     = "#ffffff"   # cards
BG_HOVER      = "#f0f4ff"   # light blue tint on hover
BG_SELECTED   = "#e6efff"   # blue tint when selected
BORDER        = "#e5e7eb"   # default border
BORDER_STRONG = "#d1d5db"   # emphasized border
BORDER_FOCUS  = "#3b82f6"   # blue focus ring
TEXT_PRIMARY  = "#1f2937"   # slate-800
TEXT_SECONDARY= "#4b5563"   # slate-600
TEXT_MUTED    = "#9ca3af"   # slate-400
ACCENT        = "#3b82f6"   # blue-500 (calm, Sejda-ish)
ACCENT_HOVER  = "#2563eb"   # blue-600
ACCENT_SOFT   = "#dbeafe"   # blue-100
DANGER        = "#ef4444"
SUCCESS       = "#10b981"


QSS = f"""
/* ============ Base ============ */
QWidget {{
    background-color: {BG_BASE};
    color: {TEXT_PRIMARY};
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
                 "Segoe UI", "Helvetica Neue", sans-serif;
    font-size: 14px;
}}

QMainWindow {{
    background-color: {BG_BASE};
}}

/* ============ Top bar ============ */
#TopBar {{
    background: {BG_BASE};
    border-bottom: 1px solid {BORDER};
}}
#TopBarLogo {{
    color: {TEXT_PRIMARY};
    font-size: 18px;
    font-weight: 700;
    padding: 0 16px;
}}
#TopBarLogoAccent {{
    color: {ACCENT};
}}
#TopBarToolName {{
    color: {TEXT_PRIMARY};
    font-size: 15px;
    font-weight: 600;
}}
#TopBarBack {{
    background: transparent;
    color: {ACCENT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}}
#TopBarBack:hover {{
    background: {BG_HOVER};
    border-color: {ACCENT};
}}
#TopBarOpen {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 600;
}}
#TopBarOpen:hover {{
    background: {ACCENT_HOVER};
}}

/* ============ Menus ============ */
QMenuBar {{
    background-color: {BG_BASE};
    color: {TEXT_PRIMARY};
    border-bottom: 1px solid {BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background: {BG_HOVER};
}}
QMenu {{
    background-color: {BG_BASE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 24px 8px 16px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: white;
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

/* ============ Toolbars (mostly hidden by default) ============ */
QToolBar {{
    background: {BG_BASE};
    border: none;
    spacing: 2px;
    padding: 4px;
}}
QToolButton {{
    background: transparent;
    color: {TEXT_PRIMARY};
    border: 1px solid transparent;
    padding: 6px 10px;
    border-radius: 6px;
}}
QToolButton:hover {{ background: {BG_HOVER}; }}
QToolButton:pressed, QToolButton:checked {{
    background: {ACCENT};
    color: white;
}}

/* ============ Status bar ============ */
QStatusBar {{
    background: {BG_PANEL};
    color: {TEXT_SECONDARY};
    border-top: 1px solid {BORDER};
    font-size: 12px;
}}

/* ============ Home: tool grid ============ */
#HomeRoot {{
    background: {BG_BASE};
}}
#HomeHero {{
    background: {BG_BASE};
}}
#HomeHeroTitle {{
    color: {TEXT_PRIMARY};
    font-size: 32px;
    font-weight: 700;
}}
#HomeHeroSubtitle {{
    color: {TEXT_SECONDARY};
    font-size: 16px;
    font-weight: 400;
}}
#HomeSearch {{
    background: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px 16px;
    font-size: 14px;
    selection-background-color: {ACCENT};
}}
#HomeSearch:focus {{
    border-color: {BORDER_FOCUS};
}}
#CategoryHeader {{
    color: {TEXT_PRIMARY};
    font-size: 18px;
    font-weight: 700;
    padding: 16px 4px 12px 4px;
}}
#ToolCard {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
#ToolCard:hover {{
    border-color: {ACCENT};
    background: {BG_RAISED};
}}
#ToolCardIcon {{
    color: {ACCENT};
    font-size: 28px;
    background: {ACCENT_SOFT};
    border-radius: 8px;
    padding: 8px 12px;
}}
#ToolCardTitle {{
    color: {TEXT_PRIMARY};
    font-size: 15px;
    font-weight: 600;
}}
#ToolCardDesc {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}

/* ============ Tool page (centered, focused) ============ */
#ToolPage {{
    background: {BG_BASE};
}}
#ToolPageHeader {{
    color: {TEXT_PRIMARY};
    font-size: 26px;
    font-weight: 700;
}}
#ToolPageSubtitle {{
    color: {TEXT_SECONDARY};
    font-size: 14px;
    font-weight: 400;
}}
#ToolSection {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
#ToolSectionTitle {{
    color: {TEXT_PRIMARY};
    font-size: 15px;
    font-weight: 600;
}}

/* ============ Drop zone ============ */
#DropZone {{
    background: {BG_PANEL};
    border: 2px dashed {BORDER_STRONG};
    border-radius: 12px;
}}
#DropZone[active="true"] {{
    background: {BG_HOVER};
    border-color: {ACCENT};
}}
#DropZoneIcon {{
    color: {ACCENT};
    font-size: 36px;
}}
#DropZoneTitle {{
    color: {TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
}}
#DropZoneHint {{
    color: {TEXT_SECONDARY};
    font-size: 13px;
}}
#DropZoneBrowse {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 9px 22px;
    font-size: 14px;
    font-weight: 600;
}}
#DropZoneBrowse:hover {{ background: {ACCENT_HOVER}; }}

/* ============ Buttons ============ */
QPushButton {{
    background: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 18px;
    min-height: 24px;
    font-size: 14px;
}}
QPushButton:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton:pressed {{
    background: {ACCENT};
    color: white;
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    background: {BG_PANEL};
    border-color: {BORDER};
}}
QPushButton#Primary {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
    font-weight: 600;
    padding: 10px 28px;
    font-size: 15px;
}}
QPushButton#Primary:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton#Primary:pressed {{
    background: {ACCENT};
}}
QPushButton#Danger {{
    background: transparent;
    color: {DANGER};
    border: 1px solid {DANGER};
}}
QPushButton#Danger:hover {{
    background: {DANGER};
    color: white;
}}

/* ============ Inputs ============ */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_BASE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 8px 12px;
    selection-background-color: {ACCENT};
    selection-color: white;
    font-size: 14px;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {BORDER_FOCUS};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {BG_BASE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: white;
    outline: 0;
    padding: 4px;
}}

/* ============ Labels ============ */
QLabel {{
    background: transparent;
    color: {TEXT_PRIMARY};
}}
QLabel#Muted {{ color: {TEXT_SECONDARY}; }}
QLabel#Hint  {{ color: {TEXT_MUTED};   font-size: 12px; }}

/* ============ Lists & trees ============ */
QListWidget, QTreeView, QTableView, QTreeWidget {{
    background: {BG_BASE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 8px;
    alternate-background-color: {BG_PANEL};
    outline: 0;
    padding: 4px;
}}
QListWidget::item, QTreeView::item, QTableView::item {{
    padding: 8px 6px;
    border-radius: 6px;
}}
QListWidget::item:selected, QTreeView::item:selected, QTableView::item:selected {{
    background: {BG_SELECTED};
    color: {ACCENT};
}}
QHeaderView::section {{
    background: {BG_PANEL};
    color: {TEXT_SECONDARY};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}

/* ============ Scrollbars (slim, unobtrusive) ============ */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 30px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_MUTED};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-width: 30px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT_MUTED};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; background: transparent;
}}

/* ============ Group / Card ============ */
QGroupBox {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {TEXT_PRIMARY};
}}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

/* ============ ProgressBar ============ */
QProgressBar {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT_PRIMARY};
    height: 20px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 5px;
}}

/* ============ Tabs ============ */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: {BG_BASE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{
    background: {BG_BASE};
    color: {ACCENT};
    border-color: {BORDER};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}
QTabBar::tab:!selected:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}

/* ============ Checkbox / Radio ============ */
QCheckBox, QRadioButton {{
    spacing: 8px;
    background: transparent;
    color: {TEXT_PRIMARY};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    background: {BG_BASE};
    border: 1.5px solid {BORDER_STRONG};
    border-radius: 3px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* ============ File chip (after upload) ============ */
#FileChip {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 0;
}}
#FileChipIcon {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border: none;
    border-top-left-radius: 10px;
    border-bottom-left-radius: 10px;
    padding: 0 14px;
    font-size: 22px;
}}
#FileChipName {{
    color: {TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 500;
    background: transparent;
    border: none;
}}
#FileChipMeta {{
    color: {TEXT_MUTED};
    font-size: 12px;
    background: transparent;
    border: none;
}}
#FileChipRemove {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
    padding: 0 12px;
    font-size: 18px;
    font-weight: 600;
}}
#FileChipRemove:hover {{
    background: #fee2e2;
    color: {DANGER};
}}

/* ============ Run button states ============ */
QPushButton#Primary[processing="true"] {{
    background: {ACCENT};
    color: white;
}}
QPushButton#Primary[processing="true"]:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#Primary:disabled {{
    background: {BORDER};
    color: {TEXT_MUTED};
}}

/* ============ Progress (inline) ============ */
QProgressBar#InlineProgress {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    text-align: center;
    color: {TEXT_PRIMARY};
    height: 22px;
    font-size: 12px;
}}
QProgressBar#InlineProgress::chunk {{
    background: {ACCENT};
    border-radius: 7px;
}}

/* ============ Tool card dimmed (no doc open or sys dep missing) ============ */
#ToolCard[disabled="true"] {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 12px;
    opacity: 0.55;
}}
#ToolCard[disabled="true"] #ToolCardTitle {{
    color: {TEXT_MUTED};
}}
#ToolCard[disabled="true"] #ToolCardIcon {{
    background: {BORDER};
    color: {TEXT_MUTED};
}}
#ToolCard[disabled="true"]:hover {{
    border-color: {BORDER};
}}

/* ============ Recent files strip ============ */
#RecentHeader {{
    color: {TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
    padding: 4px 0;
}}
#RecentChip {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 18px;
    padding: 8px 14px;
    color: {TEXT_PRIMARY};
    font-size: 13px;
}}
#RecentChip:hover {{
    border-color: {ACCENT};
    background: {BG_HOVER};
}}

/* ============ Drag overlay ============ */
#DragOverlay {{
    background: rgba(59, 130, 246, 200);
    border: 4px dashed white;
    border-radius: 16px;
}}
#DragOverlayText {{
    color: white;
    font-size: 28px;
    font-weight: 700;
}}

/* ============ Success banner (after processing) ============ */
#SuccessBanner {{
    background: #ecfdf5;
    border: 1px solid #a7f3d0;
    border-radius: 10px;
    padding: 14px 18px;
}}
#SuccessBannerText {{
    color: #047857;
    font-size: 14px;
    font-weight: 500;
}}
#SuccessBannerPath {{
    color: #065f46;
    font-size: 12px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
}}
"""


def apply_dark_theme(app: QApplication) -> None:  # noqa: D401
    """Apply the Sejda-inspired light theme. Name kept for API compatibility."""
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_BASE))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_BASE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(DANGER))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))
    app.setPalette(palette)
