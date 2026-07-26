"""Security tools — Sejda-style focused pages."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QFormLayout, QLineEdit, QWidget

from app.engine import EngineError, PdfEngine

from ..widgets import DropZone, OutputPicker
from .base import BaseTool


class ProtectTool(BaseTool):
    title = "Protect with Password"
    subtitle = "Encrypt the PDF with AES-128 and an optional permission password."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.user_pw = QLineEdit()
        self.user_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.owner_pw = QLineEdit()
        self.owner_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.allow_print  = QCheckBox("Allow printing")
        self.allow_print.setChecked(True)
        self.allow_copy   = QCheckBox("Allow text / graphic copy")
        self.allow_copy.setChecked(True)
        self.allow_modify = QCheckBox("Allow modifications")
        self.allow_annot  = QCheckBox("Allow annotations")
        self.allow_annot.setChecked(True)
        self.allow_forms  = QCheckBox("Allow form fill")
        self.allow_forms.setChecked(True)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("User password", self.user_pw)
        form.addRow("Owner password (optional)", self.owner_pw)
        form.addRow("", self.allow_print)
        form.addRow("", self.allow_copy)
        form.addRow("", self.allow_modify)
        form.addRow("", self.allow_annot)
        form.addRow("", self.allow_forms)
        sec2 = self.add_section("Passwords & permissions")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file():
            raise EngineError("Pick a source PDF.")
        if not self.user_pw.text():
            raise EngineError("A user password is required.")
        if not self.out.path():
            raise EngineError("Pick an output file.")
        perms = {
            "print":    self.allow_print.isChecked(),
            "extract":  self.allow_copy.isChecked(),
            "modify":   self.allow_modify.isChecked(),
            "annotate": self.allow_annot.isChecked(),
            "forms":    self.allow_forms.isChecked(),
        }
        return PdfEngine.protect(
            self.src.first_file(), self.user_pw.text(),
            self.owner_pw.text() or None, perms, self.out.path(),
        )


class UnlockTool(BaseTool):
    title = "Unlock PDF"
    subtitle = "Remove the open password from a PDF (you must know it)."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a locked PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Password", self.pw)
        sec2 = self.add_section("Password")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.pw.text() or not self.out.path():
            raise EngineError("Fill in source, password, and output.")
        return PdfEngine.unlock(
            self.src.first_file(), self.pw.text(), self.out.path()
        )


class FlattenTool(BaseTool):
    title = "Flatten"
    subtitle = "Make fillable PDFs read-only by baking annotations into the page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.flatten(self.src.first_file(), self.out.path())
