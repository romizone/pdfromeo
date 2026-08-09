"""Dark, Acrobat-Pro-inspired stylesheet for PdfRomeo v2.0.

Why this module looks the way it does:
  * One monolithic module-level f-string ``QSS`` — all styling flows through
    a single ``app.setStyleSheet`` call; widgets opt in via ``setObjectName``
    and dynamic Qt properties (repolished manually by their owners), never
    per-widget ``setStyleSheet``.
  * Belt-and-suspenders theming: the QSS covers our widgets, while the
    ``QPalette`` block keeps native menus, tooltips, and file dialogs dark
    so Fusion-drawn chrome never flashes light.
  * All literal CSS braces are doubled ({{ }}) because ``QSS`` is an
    f-string — a single stray brace silently kills the ENTIRE stylesheet.
  * Re-theming means editing only the constants block below; every colour
    in the QSS is interpolated from it (no hardcoded light-theme leftovers).
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
# Brand swatches — kept for compatibility with modules importing them.
BRAND_BLUE_DEEP = "#2b54b8"   # 1 — royal blue
BRAND_BLUE      = "#4380d6"   # 2 — medium blue
BRAND_SKY       = "#86c5e6"   # 3 — light blue
BRAND_CHARCOAL  = "#4a4a4d"   # 4 — neutral dark

BG_BASE       = "#252528"     # chrome / panels
BG_PANEL      = "#2b2b2f"     # raised panels, cards
BG_RAISED     = "#323236"     # hover targets, inputs
BG_HOVER      = "#3a3a40"     # hover tint
BG_SELECTED   = "#1f3a5f"     # selection tint
CANVAS        = "#19191c"     # document canvas behind pages
PAPER         = "#ffffff"     # the page itself — chrome stops at its edge
BORDER        = "#3d3d42"     # default border
BORDER_STRONG = "#4a4a52"     # emphasized border
TEXT_PRIMARY  = "#e8e8ea"     # body text
TEXT_SECONDARY= "#b8b8bd"     # secondary text
TEXT_MUTED    = "#8a8a92"     # muted text / hints
ACCENT        = "#3b82f6"     # primary actions
ACCENT_HOVER  = "#2f6fe0"     # primary, hover/pressed
ACCENT_SOFT   = "#1e3a5f"     # accent, heavily tinted surface
BORDER_FOCUS  = ACCENT        # focus ring
DANGER        = "#ef4444"
SUCCESS       = "#34d399"


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

QToolTip {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    padding: 4px 8px;
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
    background-color: {BG_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
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
QMenu::item:disabled {{
    color: {TEXT_MUTED};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

/* ============ Toolbars ============ */
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
QToolButton:disabled {{
    color: {TEXT_MUTED};
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
    background: {BG_PANEL};
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
    background: transparent;
}}
#ToolCardDesc {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    background: transparent;
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
    background: {BG_PANEL};
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
    background: {BG_SELECTED};
    border-color: {ACCENT};
}}
#DropZoneIcon {{
    color: {ACCENT};
    font-size: 36px;
    background: transparent;
}}
#DropZoneTitle {{
    color: {TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
    background: transparent;
}}
#DropZoneHint {{
    color: {TEXT_SECONDARY};
    font-size: 13px;
    background: transparent;
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
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 8px 18px;
    min-height: 24px;
    font-size: 14px;
}}
QPushButton:hover {{
    border-color: {ACCENT};
    background: {BG_HOVER};
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
    background: {BG_RAISED};
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
/* The paragraph editor is the one input that must NOT look like an input.
   It sits on the page, over the words it replaces, so it takes the paper's
   colour rather than the theme's — the dark field, the 8px radius and above
   all the 12px padding of the rule above made it a dialog dropped onto the
   document, and shunted the text sideways the moment it opened. Padding is
   zero so the glyphs stay exactly where they were; the accent hairline is
   the only sign that typing is now live. Text colour is set in code, from
   the paragraph's own runs. */
QTextEdit#ParagraphEditor {{
    background: {PAPER};
    border: 1px solid {ACCENT};
    border-radius: 2px;
    padding: 0px;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {BG_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
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
    background: {BG_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 8px;
    alternate-background-color: {BG_BASE};
    outline: 0;
    padding: 4px;
}}
QListWidget::item, QTreeView::item, QTableView::item {{
    padding: 8px 6px;
    border-radius: 6px;
}}
QListWidget::item:selected, QTreeView::item:selected, QTableView::item:selected {{
    background: {BG_SELECTED};
    color: {TEXT_PRIMARY};
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
    background: {BG_PANEL};
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

/* ============ Tabs (generic) ============ */
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
    background: {BG_RAISED};
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
    background: {BG_PANEL};
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
    background: {BG_HOVER};
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
    background: {BG_RAISED};
    color: {TEXT_MUTED};
    border-color: {BORDER};
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
    background: {BG_BASE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    opacity: 0.55;
}}
#ToolCard[disabled="true"] #ToolCardTitle {{
    color: {TEXT_MUTED};
}}
#ToolCard[disabled="true"] #ToolCardIcon {{
    background: {BG_RAISED};
    color: {TEXT_MUTED};
}}
#ToolCard[disabled="true"]:hover {{
    border-color: {BORDER};
}}

/* ============ Page preview ============ */
#PreviewPane {{
    background: {CANVAS};
    border-left: 1px solid {BORDER};
}}

#PreviewScroll {{
    background: {CANVAS};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#PreviewScroll > QWidget > QWidget {{
    background: {CANVAS};
}}
#PreviewRoot {{
    background: {CANVAS};
}}
/* Bare selector on purpose: pages render on white sheets wherever they
   appear (PreviewRoot AND the workspace DocView). */
QLabel[pageCanvas="true"] {{
    background: white;
    border: 1px solid {BORDER_STRONG};
}}

/* ============ Recent files strip ============ */
#RecentHeader {{
    color: {TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
    padding: 4px 0;
}}
#RecentChip {{
    background: {BG_PANEL};
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
    background: rgba(59, 130, 246, 180);
    border: 4px dashed {TEXT_PRIMARY};
    border-radius: 16px;
}}
#DragOverlayText {{
    color: white;
    font-size: 28px;
    font-weight: 700;
    background: transparent;
}}

/* ============ Success banner (after processing) ============ */
#SuccessBanner {{
    background: {BG_PANEL};
    border: 1px solid {SUCCESS};
    border-radius: 10px;
    padding: 14px 18px;
}}
#SuccessBannerText {{
    color: {SUCCESS};
    font-size: 14px;
    font-weight: 500;
    background: transparent;
}}
#SuccessBannerPath {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    background: transparent;
}}

/* ============ Workspace: document tab bar ============ */
/* Tab chrome only — the modified dot is rendered in the tab TEXT by the
   workspace (per spec §10.2), so there is deliberately NO per-tab
   property selector here. */
#DocTabBar {{
    background: {BG_BASE};
    border: none;
}}
#DocTabBar QTabBar::tab {{
    background: {BG_BASE};
    color: {TEXT_SECONDARY};
    padding: 8px 18px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}}
#DocTabBar QTabBar::tab:selected {{
    background: {BG_PANEL};
    color: {TEXT_PRIMARY};
    border-color: {BORDER};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}
#DocTabBar QTabBar::tab:!selected:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}
#DocTabBar QTabBar::close-button {{
    subcontrol-position: right;
}}
/* The tabs carry their own ✕ button (see MainWindow._style_close_button)
   because the style's stock close icon is a red badge that reads as an
   error on a dark strip. */
#DocTabClose {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: 4px;
    padding: 0;
    font-size: 12px;
    min-width: 16px;
    max-width: 16px;
    min-height: 16px;
    max-height: 16px;
}}
#DocTabClose:hover {{
    background: {DANGER};
    color: #ffffff;
}}

/* ============ Workspace: toolbar & status ============ */
#WorkspaceToolbar {{
    background: {BG_PANEL};
    border-bottom: 1px solid {BORDER};
    spacing: 4px;
    padding: 4px 8px;
}}
#WorkspaceToolbar QToolButton {{
    background: transparent;
    color: {TEXT_PRIMARY};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 10px;
}}
#WorkspaceToolbar QToolButton:hover {{
    background: {BG_HOVER};
}}
#WorkspaceToolbar QToolButton:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border-color: {ACCENT};
}}
#WorkspaceStatus {{
    background: {BG_PANEL};
    color: {TEXT_SECONDARY};
    border-top: 1px solid {BORDER};
    font-size: 12px;
}}

/* ============ Workspace: left rail & panels ============ */
#LeftRail {{
    background: {BG_BASE};
    border-right: 1px solid {BORDER};
}}
#RailButton {{
    background: transparent;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 8px;
    font-size: 18px;
}}
#RailButton:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}
#RailButton:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border-color: {ACCENT};
}}
#PanelHost {{
    background: {BG_PANEL};
    border-right: 1px solid {BORDER};
}}
#PanelTitle {{
    color: {TEXT_PRIMARY};
    font-size: 13px;
    font-weight: 700;
    padding: 10px 12px 6px 12px;
    background: transparent;
}}
#ThumbList {{
    background: {BG_PANEL};
    border: none;
    padding: 6px;
}}
#ThumbList::item {{
    padding: 6px;
    border-radius: 8px;
    color: {TEXT_SECONDARY};
}}
#ThumbList::item:selected {{
    background: {BG_SELECTED};
    color: {TEXT_PRIMARY};
}}
#ThumbList::item:hover {{
    background: {BG_HOVER};
}}

/* ============ Workspace: comments ============ */
#CommentToolbar {{
    background: {BG_PANEL};
    border-bottom: 1px solid {BORDER};
    padding: 4px 8px;
}}
/* These are 34px-wide icon buttons; without their own padding they inherit
   QPushButton's 18px horizontal padding and clip the glyph away entirely. */
#CommentToolButton {{
    padding: 4px 0;
    min-width: 0;
    font-size: 16px;
    border-radius: 6px;
}}
#CommentToolButton:hover {{
    background: {BG_HOVER};
    border-color: {ACCENT};
}}
#CommentToolButton:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
    color: {TEXT_PRIMARY};
}}
#CommentColorButton {{
    padding: 4px 8px;
    min-width: 0;
}}
#CommentCard {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px;
}}
#CommentCard:hover {{
    border-color: {BORDER_STRONG};
}}
#CommentCard[selected="true"] {{
    background: {BG_SELECTED};
    border-color: {ACCENT};
}}

/* ============ Workspace: tools pane ============ */
#ToolsPane {{
    background: {BG_PANEL};
    border-left: 1px solid {BORDER};
}}
#ToolsPaneItem {{
    background: transparent;
    color: {TEXT_PRIMARY};
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 10px 12px;
    text-align: left;
    font-size: 13px;
}}
#ToolsPaneItem:hover {{
    background: {BG_HOVER};
    border-color: {BORDER};
}}
#ToolsPaneItem:pressed, #ToolsPaneItem:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border-color: {ACCENT};
}}

/* ============ Workspace: document view canvas ============ */
#DocViewScroll {{
    background: {CANVAS};
    border: none;
}}
#DocViewScroll > QWidget > QWidget {{
    background: {CANVAS};
}}

/* ============ Workspace: search results ============ */
#SearchResultList {{
    background: {BG_PANEL};
    border: none;
    padding: 4px;
}}
#SearchResultList::item {{
    padding: 8px;
    border-radius: 6px;
    color: {TEXT_SECONDARY};
}}
#SearchResultList::item:selected {{
    background: {BG_SELECTED};
    color: {TEXT_PRIMARY};
}}
#SearchResultList::item:hover {{
    background: {BG_HOVER};
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    """Apply the dark Acrobat-Pro-style theme (name finally honest)."""
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_BASE))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(DANGER))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(TEXT_MUTED),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(TEXT_MUTED),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
        QColor(TEXT_MUTED),
    )
    app.setPalette(palette)
